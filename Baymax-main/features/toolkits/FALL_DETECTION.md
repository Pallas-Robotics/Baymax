# Experimental fall-detection toolkit

Enable **experimental fall detection (CPU, or NPU on a set-up Orange Pi)** in devUI, select a camera, Save, then restart Ember. It is off by default. `BAYMAX_FALL_DETECTION_ENABLED=1` is the equivalent private .env setting; `0` disables it. A disabled toolkit never loads a pose model or starts its worker. This is an optional shared feature, separate from `BAYMAX_TOOLKIT_ENABLED`, which controls location/weather.

## Inputs, outputs and processing

The toolkit never opens a second camera. It works in one of two modes, chosen by the runtime:

- **Shared core pose** (`realtime_gemini_10.py` with `camera.pose_tracking` on, the default): the core camera thread already runs pose on every frame, so the toolkit loads no model, downloads nothing and receives no frames. For each frame the core passes its landmarks, its pose inference time, where the pose ran and when the frame was captured (`share(landmarks, inference_ms, device, captured_at)`). The pose runs in the core's own pose process (`features/vision_process.py`) on the core's backend: MediaPipe on the CPU, or the Orange Pi 5's RK3588 NPU once features/pose has verified it there (see below). On the CPU the model is the core's `camera.pose_model` .task, `Baymax-main/pose_landmarker_lite.task` by default (float16 `latest`, downloaded by `realtime_gemini_10.py` on the first camera start, with no SHA-256 check); the NPU runs the checked full model (for lite or full; heavy stays on the CPU), and features/pose warns when a full .task's weights differ from the pinned ones. The Monitor shows **Running on CPU (core pose)** or **Running on NPU (core pose)**, following the core if its pose process is restarted on the CPU. The inference time is the core's, including the round trip to its pose process.
- **Own CPU model** (`realtime_gemini_8.py`, or v10 with `camera.pose_tracking = false`): the camera thread shares copies of its frames with one CPU inference worker. Google's MediaPipe full PoseLandmarker float16 model (version 1) runs with an explicit CPU delegate. The first enabled run downloads a ~9.4 MB model from storage.googleapis.com into ignored `.models/`, verifies its recorded SHA-256, and caches it, before the pose process starts (a start that takes over 60 s is stopped as hung, so a slow download must not count). A corrupt model makes detection unavailable; remove `.models/pose_landmarker_full.task` and retry. A pose process that stops answering (no reply to a frame in 5 s) is stopped and detection reports unavailable until Ember restarts. No NPU/GPU driver is required. The Monitor shows **Running on CPU**.

In both modes a single pending item replaces older queued ones; items over 0.5 seconds old are discarded. Camera capture and Gemini audio/video processing do not wait for the detector. CPU contention can still affect conversation performance, so measure it on the target board.

Pose landmarks feed the repository's existing FallDetector, copied from JoeKojima/Baymax commit `6d2386101d676df476eb97554963d60f1b5950b2` with one local change, the same as opi4-demo's (ff168ca), recorded with both hashes in `fall_detection/UPSTREAM.json`: `update(landmarks, timestamp=None)` takes the frame's capture time. Both modes pass it (shared: the core's capture time; own model: when `submit()` got the frame), so velocities come from when frames were captured, not from when their pose finished or the worker got to them; without it the detector reads the clock as before. Core threshold values (trunk angle 45 degrees in v8, 60 degrees in v10, 25-degree/s angular velocity, 0.12 normalized hip descent/s, two confirming frames, a history of eight frames in v8 at 15 fps and six in v10 at 10 fps, about 0.5 s either way, and three-second cooldown) are passed into it. Its existing upright-history, one-second warmup and two-second stay-down checks remain unchanged.

The monitor displays disabled/starting/loading/running/waiting/unavailable/stopped state, the pose backend (`CPU`, `CPU (core pose)` or `NPU (core pose)`), measured inference time/FPS and compact possible-fall events. Backend failure leaves conversation available and reports detection unavailable. The configured camera must be available; no camera means no detection.

A confirmed algorithm event creates an uncertain **possible-fall check-in** in the active Gemini conversation: ask whether the person is okay. Stale events over 15 seconds old are discarded. If a book, station or recording is playing, the check-in pauses it first and says so, because playback withholds the microphone from Gemini. A check-in that cannot reach Gemini is recorded locally, does not crash the conversation and is asked again (still saying that playback was paused) every 2 seconds, including on the next connection if that comes within 15 seconds of the failed try; one that reached Gemini but whose reply a disconnect cut off (or that had no reply yet after up to 30 seconds) is sent again on the new connection, unless the person has spoken since. No caregiver email, SMS, emergency call or production webapp alert is enabled. There is no independent alarm if Gemini is disconnected; the local event remains visible in the monitor.

## Dependencies and ARM compatibility

- Windows/macOS desktop pins remain in requirements-pc.txt (MediaPipe 0.10.35).
- Linux ARM64 uses requirements-orange-pi.txt automatically. MediaPipe 0.10.18 has an official Python 3.12 ARM64 wheel, but requires NumPy <2. That profile pins NumPy 1.26.4, OpenCV 4.10, spaCy 3.7.5 and compatible numerical dependencies separately. Core prompts/models/audio rates are unchanged; numerical/NLP library versions differ and need on-board verification.
- Praat-Parselmouth 0.4.7 has no ARM wheel and must build from source. The Debian prerequisite clickable installs build tools; it built on an Orange Pi 5 Max (Armbian Debian 13) in the full profile install.

`BAYMAX_FALL_FPS` may set a target from 2 through 30 (default: the core's camera rate, 10 in v10 and 15 in v8; in shared mode the core's frames set the pace). Actual FPS depends on inference time; a target is not a measured throughput guarantee. Tracking gaps above the upstream detector's 0.5-second reset threshold and low frame rates can materially affect detection. Profile CPU, memory, thermals, audio latency and missed frames before selecting deployment settings.

## Data flows

Pose inference, landmarks and angle/motion calculations remain local. The monitor receives state, timing and a small angle/result summary, not camera frames or joint coordinates. A possible-fall text check-in is sent to Gemini over the existing session. The existing runtime already sends camera/audio data to Gemini and records audio locally; enabling this feature does not remove that behavior. The model download sends no key or private camera data.

## Validation protocol

Run `python check_release.py` to include synthetic detector logic (including capture times), worker failure/backlog behavior, shared core pose (no model loaded, status and device label, capture times), model checksum and download before the pose process starts (own-model mode only), settings, passive monitor and Gemini check-in tests. The real CPU backend was smoke-tested on Windows with a synthetic blank image; that is not evidence of fall accuracy or ARM performance. Shared mode's Monitor status and inference time are still to be checked live on Windows, macOS, the LattePanda and the Orange Pi.

On the actual Orange Pi 5 Max and Orange Pi 4 Pro, record the OS/kernel/architecture, installation/source-build result, camera position and actual inference FPS. Use consented prerecorded test clips or controlled safe scenarios to check ordinary sitting/lying/bending, occlusion, camera loss, possible falls, warmup/stay-down/cooldown, failure recovery and simultaneous Gemini conversation. Compare false positives, misses and check-in delays. Do not ask a person to perform an unsafe fall. This development feature is not validated as an emergency-monitoring system. Lead approval requires real-device evidence.

## NPU backend (core pose)

In shared mode the core's pose can run on the Orange Pi 5 Max's RK3588 NPU: see
[features/pose](../pose/README.md). It is used only after the models are converted and
pass the converter's accuracy check on the board, and falls back to MediaPipe on the CPU
otherwise. That check verifies the conversion; on the tested board the NPU's fall
decisions were not identical to MediaPipe's: they differed on 2 of MediaPipe's 13 on
the test clips, a confirmed fall the NPU dropped and a "no fall" lie-down MediaPipe
flagged and the NPU did not ([VALIDATION.md](../pose/VALIDATION.md), "On the Orange
Pi"). The toolkit's own CPU model (own-model mode) never uses the NPU.

References: [Google pose task](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker/python), [ARM MediaPipe wheel](https://pypi.org/project/mediapipe/0.10.18/#files), [Rockchip RKNN stack](https://github.com/airockchip/rknn-toolkit2).
