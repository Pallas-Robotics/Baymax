# Orange Pi 5 Max and Orange Pi 4 Pro: Debian 13 (experimental)

One native Linux ARM64 profile for both boards: the same code and the same
`requirements-orange-pi.txt`. What differs is detected on the board (the NPU,
fast-core pinning) or comes from how the board is set up:

| | Orange Pi 4 Pro | Orange Pi 5 Max |
|---|---|---|
| CPU | Allwinner A733: 2x Cortex-A76 + 6x Cortex-A55 | RK3588: 4x Cortex-A76 (cpu4-7) + 4x Cortex-A55 (cpu0-3) |
| Install | the SD image ([OPI4-Flash](../devops/opi4-flash/README.md)), or the steps below on Debian 13 | the steps below (Armbian Debian 13, XFCE/Xorg) |
| Pose | MediaPipe on the CPU | MediaPipe on the CPU, or the NPU after the optional steps below |
| `BAYMAX_CPU_PINNING=auto` | not pinned | pinned to the A76s |
| Wi-Fi setup hotspot | optional, installed by hand ([deploy/host](deploy/host/README.md)): hostapd | optional, installed by hand (the same page): NetworkManager |

On the Orange Pi 4 Pro the SD image installs everything below itself (packages,
Python environment, services, updates that keep memory in
`~/ember-deployment/data` on a card built by this repository's builder); the board-specific sections here (pose, pinning, memory)
apply to it too. Nothing in this guide has been run on an Orange Pi 4 Pro yet.

On an Orange Pi 5 Max with Armbian Debian 13 (vendor kernel 6.1), `requirements-orange-pi.txt` installed (Praat-Parselmouth and miniaudio build from source, about 30 minutes; memory's packages and rknn-toolkit-lite2 joined the file later; at commit e938983 `pip install -r requirements-orange-pi.txt` added them in 55 s with `pip check` clean) and the offline suite passed (`check_release.py` at commit 61c410e: core 352 tests, 15 of them skipped, 0 failures; toolkit 158, 0 skipped). By the counts the 15 skipped are the 14 git-based updater tests (git was not installed on the board) and the off-RK3588 default test, so the updater's tests have not run on the board yet (**0 - Debian Prerequisites** now installs git: rerun; confirm the skips with a verbose run). Those figures predate this repository's merge (see Pose below). Still to be checked on the board: Ember running live (it has not been started there yet, on the CPU or the NPU: audio, camera, conversation, the live view, the fall toolkit's status; the camera loop's FPS and frame age with the 1-frame camera buffer below; with the NPU: the backend inside Ember end to end), the updater's tests, thermals over a long run, and fall accuracy at home.

1. Get the code as a git checkout of the `release` branch, in a folder you keep: `sudo apt install -y git`, then `git clone --branch release --single-branch https://github.com/Pallas-Robotics/Baymax.git ~/Baymax`. Automatic updates (below) need this checkout. An extracted ZIP runs the same way but never updates itself.
2. Open devops/clickables/Linux. On a minimal Debian 13 system, run **0 - Debian Prerequisites** first. It lists system libraries/build tools, asks permission, and uses sudo/pkexec for apt installation. It does not install NPU drivers, replace the kernel or configure an audio server. Ensure XFCE desktop audio and a browser already work.
3. Open **1 - Setup**. It installs private Python 3.12 and automatically selects requirements-orange-pi.txt for Linux aarch64/arm64. Expect a potentially long native build for Praat-Parselmouth. A resolver pass does not prove that compilation will succeed.
4. Enter your own Gemini key (its project needs Live quota for the models Ember uses: see [Gemini model](MVP.md#gemini-model)), select/test mic, speaker and camera, and optionally enable **experimental fall detection (CPU, or NPU on a set-up Orange Pi)**. Save. It is off by default and requires a camera. With camera pose tracking on (the default) it reuses Ember's own pose, which can run on the Orange Pi 5 Max's NPU (below). It runs its own detector on that pose for the Monitor; with Ember's built-in fall detection on (`fall_detection.enabled`, the default) it leaves the check-in to it, so one fall gives one check-in.
5. Open **2 - Monitor**, then **3 - Start Ember**. On an Orange Pi (Linux ARM64) it starts `run_mvp.py`, as MVP.md, the autostart service and the 4 Pro image do: the LED eyes (set `BAYMAX_EYES_PORT`; [features/eyes](features/eyes/README.md)) and the MVP defaults (400 ms turn-end silence; interruptions, the wait cue and voice analysis off; 10 s to save memories at stop; values saved in `.env` win). Ember downloads its pose model (`pose_landmarker_lite.task` by default; `camera.pose_model` in `/etc/ember/device.toml` picks lite, full or heavy) on the first camera start. With pose tracking on (the default) the fall toolkit loads no model of its own and the Monitor shows **Running on CPU (core pose)** (or **Running on NPU (core pose)** after the NPU steps below), with actual FPS and inference milliseconds. Only with `camera.pose_tracking = false` does the toolkit's first run download and verify its own model and show **Running on CPU**. Disabled/starting/loading/unavailable/waiting is not a working detector.
6. Use **4 - Stop Ember**. Test device release and restart; keep your evidence for review.

**Starting at login and updates (optional).** The Orange Pi 5 Max updates itself only
through the MVP autostart service, as in [MVP.md](MVP.md#automatic-startup):

1. Stop Ember (**4 - Stop Ember**). From the checkout, as the desktop user (no sudo), run
   `python3 devops/mvp/install_autostart.py`, then `bash devops/mvp/start-session.sh`.
   `ember-mvp.service` then starts `run_mvp.py` at each desktop login (automatic login
   is configured separately); do not also click **3 - Start Ember**. Stop it with
   `systemctl --user stop ember-mvp.service` (for example before the NPU check below).
2. Add `BAYMAX_AUTO_UPDATE=1` to `Baymax-main/.env` (and `BAYMAX_UPDATE_BRANCH=...` to
   follow another branch than `release`). Before each service start,
   `devops/mvp/update.py` fetches the branch, reinstalls `requirements-orange-pi.txt`
   when it changed, runs `check_release.py` (which ignores this `.env`, so its
   settings can't fail the checks), and rolls back a version that fails or never
   reaches listening ([devops/mvp](../devops/mvp/README.md)).

On an extracted ZIP every service start logs `[UPDATE] Skipped: git rev-parse HEAD
failed: ... not a git repository`. Without the service (clickables only) nothing
updates: run `git pull --ff-only` in the checkout and then **1 - Setup** (it reinstalls
packages only when the requirements changed).

If XFCE opens `.sh` files in an editor, use its file permissions/execution options to run them in a terminal; executing scripts depends on Thunar settings. The current launcher is not a packaged XFCE desktop application. Localized Desktop-folder layouts, USB/CSI camera drivers, user device permissions and sound-server routing can require OS-specific setup.

The prerequisite script is only for Debian 13. It installs curl/certificates, git (for `devops/mvp/update.py`), PortAudio, GL/EGL/GLib and sound-file libraries, ALSA plugins, compiler/CMake/pkg-config tools, xdg-utils and XFCE Terminal. It also offers Firefox ESR in the displayed package list when no common browser command is found. This system-package step is separate from the private Python environment and requires administrator authorization. NPU support is not installed by it (see Optional: pose on the NPU). A 64-bit OS and native ARM64 Python are required; 32-bit armhf is not supported by this profile.

## Pose: its own process, 10 fps

The camera thread captures at 640x480 and runs pose at 10 fps (`FALL_DETECTION_FPS`;
fall detection keeps about 0.5 s of history, `FALL_HISTORY_FRAMES = 6`). Pose runs in
a child process (`features/vision_process.py`, `python -m features.vision_process`):
in Ember's own process MediaPipe held Python's GIL, so on the Orange Pi 4 Pro replies
came up to a minute late and the microphone overflowed about every 7 s. Only frames
go in and landmarks come back. In that child, `features/pose/backend.py` picks the
backend and logs it to Ember's log:

- `camera.pose_model = "lite"` (the default) or `"full"`: on an Orange Pi 5 Max that
  passed the NPU steps below, the NPU runs the **checked full model**
  (`[POSE] Pose backend: NPU (RK3588) ...`). Everywhere else, including the Orange Pi
  4 Pro, MediaPipe runs the chosen `.task` on the CPU
  (`[POSE] Pose backend: CPU (MediaPipe). NPU skipped: <reason>`).
- `"heavy"`: always MediaPipe on the CPU (the NPU was only checked against full).
- If the pose process cannot start (for example librknnrt crashes it while it sets up
  the NPU: `[CAM] Pose process could not start (...)`, then `Starting the pose process
  again, on the CPU.`) or dies later (`[CAM] Pose process failed (...); restarting it
  on the CPU.`), Ember starts it again once, on the CPU; a second failure leaves video
  only until Ember restarts (`[CAM] Pose unavailable; video only ...`). Video to Gemini
  continues throughout. A pose process that hangs without exiting is stopped once a
  frame has had no reply for 5 s (60 s while it starts) and restarted the same way
  (`[CAM] Pose process failed (vision process hung: no reply in 5 s, so it was
  stopped); ...`). An NPU error during a frame does not use that restart: the pose
  process switches to MediaPipe itself (`[POSE] NPU (RK3588) failed during inference
  (...); switching to CPU (MediaPipe) for the rest of this run.`), and the frame in
  which it loads MediaPipe is allowed the start's 60 s instead of 5 s.

Eye-contact greetings (`BAYMAX_PRESENCE_GREETING=1`, off by default) run face
landmarks in a second child process the same way. The annotated live view is drawn
only while someone watches it.

**Measuring CPU:** Ember's work is split over its own process and these children, so
add them up. For example, over one minute:

```bash
EMBER=$(pgrep -o -f 'run_mvp.py|run_pc.py')                     # Ember's own process
PIDS=$(printf '%s\n' "$EMBER" $(pgrep -P "$EMBER") | paste -sd,)  # plus its pose/face children
top -b -d 60 -n 2 -p "$PIDS" | tail -n 8                           # the second sample: last minute's %CPU
```

The Orange Pi 5 Max figures below and in features/pose were all measured **before this
repository's merge**: pose in Ember's own process, the full model, 15 fps. For the
whole system (pose, audio cleanup, the Gemini link and the eyes), the orangepi5-npu
work reported about 130.7% of one core for mvp-v10, 91.8% with pose on the CPU and
42.7% with pose on the NPU (with the fall toolkit also on: 217% before that work, 42.6%
after, with the NPU). They
are not the current configuration (lite model, 10 fps, pose in a child process) and
need to be measured again, summed over the process tree as above.

## Fast cores

The RK3588 has four fast Cortex-A76 cores (cpu4-7) and four slow Cortex-A55s (cpu0-3).
Ember keeps itself on the fast ones (it prints `[CPU] Running on the fast cores
4,5,6,7, GIL switch interval 1 ms`): MediaPipe pose takes about 43 ms a frame on an A76
and over 200 ms on an A55. `run_mvp.py` (the MVP service and, on an Orange Pi, **3 -
Start Ember**) and `run_pc.py` (which it starts) do this first thing, before the eyes,
the network watch and numpy's thread pool start, and also move any thread that already
runs, so every thread of Ember's own process stays on the A76s. None of them is moved to
an A55 by itself: they all share Python's GIL, and whatever one of them did there while
holding it would keep the audio threads waiting 3.6-5x as long.

With Ember on the fast cores, and only then:

- **GIL switch interval 1 ms** instead of Python's 5 ms. The mic callback lets go of the
  GIL inside each WebRTC frame (2-3 times a block) and has to win it back each time; in
  the 5 Max's full-system test it took about 2.5 ms at the median but 9.3-10.2 ms at p99,
  just as long with pose on the NPU as on the CPU. `BAYMAX_GIL_SWITCH_MS` in `.env`
  changes it (0.1-100 ms; 5 is Python's own, 0 leaves Python's setting alone).
- **No idle thread pools.** OpenCV's pool is off in Ember and in the vision processes
  (its three workers spun at 1.2-1.6% of a core each around the camera's 0.3 ms colour
  conversion), and `OPENBLAS_NUM_THREADS=1`, `OPENCV_FOR_THREADS_NUM=1` and
  `TOKENIZERS_PARALLELISM=false` apply to Ember and everything it starts, unless `.env`
  or the shell sets them. ONNX Runtime keeps its two threads for the memory lookup
  (below). With `BAYMAX_MEMORY_NOTES=1` a lookup runs between turns, once Ember's reply
  has played, and nothing waits for it; two threads finish it sooner (19 ms instead of
  39 ms on one fast core) in case the person's next reply starts while it is running.
- **Speaker stream: 512-frame callbacks and low latency** instead of sounddevice's 1024
  and `high`. With the 5 Max's USB speaker on PipeWire's default device the speaker's
  output latency went from about 96 ms to about 42 ms (PortAudio reported 42.7 ms, now
  10.7 ms), with no output underflows in three 3.5-minute runs (one with MediaPipe pose on
  the CPU) and the mic and speaker callbacks' timing unchanged. `BAYMAX_OUTPUT_BLOCKSIZE`
  (64-4096) and `BAYMAX_OUTPUT_LATENCY` (`low`, `high` or up to 0.5 s) in `.env` change it,
  e.g. 1024 and `high` if another speaker crackles.
- **The pose process** (NPU or MediaPipe, and the fall toolkit's own with
  `camera.pose_tracking = false`) stays on the fast cores at nice 5, so Ember's audio
  wins any core it shares with pose. On an A55 the NPU path's own CPU work (about 11.5 ms
  a frame on an A76) would take 3.6-5x as long, estimated to push its slower frames past
  the 100 ms a frame has at 10 fps.
- **The face process** of eye-contact greetings (`BAYMAX_PRESENCE_GREETING=1`, off by
  default; 4 fps, and a look has to last 0.8 s anyway) runs on the slow cores at nice 10.
- Each vision process logs where it runs, e.g. `[CPU] Face process on the slow cores
  0,1,2,3 at nice 10.` `BAYMAX_VISION_CPUS=fast` or `slow` puts both on those cores, to
  measure. Nice values only ever go up (a higher priority needs root) and nothing uses
  real-time scheduling. The audio cleanup and memory stay inside Ember's process: their
  heavy parts already let go of the GIL, and another process would add a hop on the
  microphone's path.

In the full-system tests Ember and its pose process used 10-19% of the A76s on average
and no audio block was dropped, so this layout is about steadier audio timing, not
about CPU. Its effect on the mic callback is expected, not yet measured on the board.
To see where every thread runs (`psr`: the core it last ran on; `ni`: its nice value),
with `PIDS` from **Measuring CPU** above: `ps -L -o pid,tid,psr,ni,comm -p "$PIDS"`.

`BAYMAX_CPU_PINNING` in `.env`:

- `auto` (default): pin only on an RK3588 (Orange Pi 5 / 5 Max), where it was measured.
- `on`: pin on any Linux ARM64 board with fast and slow cores. On the Orange Pi 4 Pro
  that puts Ember and the pose process on its two A76s and the face process on its six
  A55s; this has not been measured, so `auto` leaves the 4 Pro unpinned. Use `on` there
  only to compare (`cat /sys/devices/system/cpu/cpu*/cpu_capacity` shows which cores
  are fast).
- `off`: never pin; Linux uses every core, and none of the settings above apply.

Windows, macOS and x86 Linux PCs (including Intel/AMD hybrid CPUs) are never pinned,
and none of the settings above apply there either. Started under `taskset`, Ember and
its children stay within the cores it was given (with `taskset -c 4-7` the face process
stays on cpu4-7, at nice 10). `features/pose/benchmark.py` pins itself like Ember, so it
also runs with the single-threaded pools and the 1 ms interval: compare its figures
only with runs made the same way.
On both Orange Pis the camera also keeps only its newest frame (a V4L2 buffer of
one); that is not measured with a camera yet: check that the FPS stays near 10 (the
Monitor shows it with Setup's fall-detection checkbox on; otherwise read the FPS
overlay in the live view).

## Memory

Ember remembers past conversations here as on the robot image. While Ember runs,
after every 20 user turns or 3 minutes with nobody talking, and again when it stops,
Gemini summarises the conversation since the last save (the same step on every
platform) and the summary lines are stored on the board in `chroma_store/` (in
`BAYMAX_DATA_DIR`, by default next to the code; on the Orange Pi 4 Pro SD image
`~/ember-deployment/data`, which updates keep). A crash or power cut loses only the
conversation since the last save. The 50 newest memories are part of Gemini's
instructions at every connection, and Gemini uses them (checked live: a remembered fact
there was used 5 of 5 times). Looking memories up during the conversation and handing
them to Gemini between turns is opt-in (`BAYMAX_MEMORY_NOTES=1`): Gemini 2.5 ignored
such notes in 10 of 10 live runs, so by default Ember does no per-turn lookups.
`requirements-orange-pi.txt` installs ChromaDB, ONNX Runtime and `tokenizers` for this,
without torch: `semantic_embedder.py` runs the same MiniLM model with the same results
as the robot image.

On its first start Ember downloads the model into `all-MiniLM-L6-v2-onnx/` in the
background: about 90 MB from huggingface.co, pinned to one revision and checked by
SHA-256. Ember starts and talks meanwhile, and memory turns on when the download
finishes (a first start has nothing to recall yet). The log shows `[MEMORY]
Downloading the memory model ...`, then `[MEMORY] Embedder ready — N memories in
store`. To fetch it beforehand, run `.venv/bin/python semantic_embedder.py --download`
from `Baymax-main`. If the download fails (no network), memory is off for that session
and the download is tried again on the next start.

Measured on the Orange Pi 5 Max (commit e938983, before this repository's merge: pose
in Ember's own process, full model, 15 fps; 500 memories, a 30-word query of about 36
tokens):
- **A lookup:** 34 ms of CPU, ready in 19 ms with its two threads on two fast cores
  (39 ms on one fast core, about 140 ms on a slow A55). It runs once per user turn,
  in the background: then while the person was still talking, now once Ember's reply
  has played.
- **In the full-system test** (pose, audio cleanup, the Gemini link and the eyes all
  running, one scripted user turn every 12 s): Ember used 42.8% of one core with pose
  on the NPU and 92.0% with pose on the CPU, against 42.7% and 91.8% with memory off.
  18 lookups per run, none timed out (the reply then waited up to 150 ms for one; now
  nothing waits).
- **RAM:** about 170-230 MB more. Saving 30 summary lines takes about 0.5 s of CPU;
  loading the model at start about 1.2 s.
- `pip install -r requirements-orange-pi.txt` added the packages in 55 s and the
  first download took 7 s.

ONNX Runtime prints `GPU device discovery failed ... /sys/class/drm/card1/device/vendor`
when it loads: harmless (it runs on the CPU).

An Orange Pi installed before this change needs the new packages once:
`.venv/bin/python -m pip install -r requirements-orange-pi.txt` (or rerun **1 - Setup**).

`retrieval_enabled = false` under `[memory]` in `/etc/ember/device.toml` turns off the
lookups, and then nothing downloads the model: run `--download` (above) once, or
session summaries can't be saved.

## Radio, check-ins and other conversation features

The same on both boards (and on every platform); `requirements-orange-pi.txt` already
has what they need. None of them has been tried on either board yet. Details, switches
and data flows: the [toolkit guide](features/toolkits/README.md) and
[conversational ability](features/conversational_ability/README.md).

- **Voice volume:** "Ember, speak louder", "softer" or "set your volume to 8" (1-10;
  7 leaves the audio unchanged). Only Ember's voice changes; audiobooks, radio and
  recordings keep `BAYMAX_BOOK_VOLUME`. The level is kept in `voice_volume.json` in
  `BAYMAX_DATA_DIR` (next to the code by default; on the Orange Pi 4 Pro SD image
  `~/ember-deployment/data`, which updates keep).
- **Radio and recordings** (`listen`): live stations from Radio Browser and openly
  licensed Internet Archive recordings, decoded by miniaudio and played like an
  audiobook. While one plays only the Vosk wake words ("Hey Ember", "stop") reach
  Ember, so a fall check-in or a due reminder pauses it first.
  `BAYMAX_LISTEN_ENABLED=0` turns it off.
- **Daily check-ins:** a morning, afternoon and evening question, at most once each
  per day, not in the first 20 minutes after a start, and only when it is quiet and
  the person spoke to Ember in the last 3 hours. The windows use the board's local
  clock, so check its time zone with `timedatectl` and set the home's with `sudo
  timedatectl set-timezone America/New_York` (then restart Ember). An OPI4-Flash card
  is set to `America/Los_Angeles` unless its image settings name another `timezone`
  ([OPI4-Flash](../devops/opi4-flash/README.md)). "Done today" is kept in
  `toolkit_checkins.json` in `BAYMAX_DATA_DIR`. `BAYMAX_CHECKINS=0` turns it off.
- **Look closely:** asked to read or identify something, Ember sends Gemini one
  sharper still: the camera thread's full 640x480 frame instead of the usual small
  video frame. `BAYMAX_LOOK_CLOSELY=0` turns it off.
- **Brain games:** word games played only when someone asks, from local word lists;
  on with the Discovery tools.
- **Reconnecting:** Ember reconnects after any disconnect from Gemini, and after a
  network that goes silent (a microphone send stuck for 5 s), and does not play the
  rest of a reply the disconnect cut off. A fall check-in or another message of
  Ember's own that the disconnect cut off is sent again on the new connection.

## Optional: pose on the NPU

Orange Pi 5 / 5 Max (RK3588) only; the Orange Pi 4 Pro has no NPU that Ember uses.
Ember runs MediaPipe Pose on the CPU unless the RK3588's NPU is ready; then the camera
thread's pose (fall detection, face presence, the live view and the fall toolkit) runs
on the NPU, in the pose child process, with the checked full model
([features/pose](features/pose/README.md)) whenever `camera.pose_model` is lite (the
default) or full; heavy stays on the CPU. Nothing changes until all of the steps below
are done, and any failure falls back to the CPU.
Steps 1-5 were run on an Orange Pi 5 Max with Armbian Debian 13 (vendor kernel
6.1.172, RKNPU 0.9.8) on 2026-10-28, with Ember stopped, including step 5's paced
`--fps 15` and `--empty` runs; see features/pose/VALIDATION.md for the results. Step 6
with Ember running is still pending on the board (the backend choice was confirmed
there by calling `create_pose_backend`, in-process, before pose moved to its own process).
The rknn converter prints a few `E RKNN: ... Unkown op target: 0` lines while building
and the runtime a `query RKNN_QUERY_INPUT_DYNAMIC_RANGE error` warning when it loads a
model: both are harmless (the check and the models are fine).

1. **Check the kernel, the NPU driver and access to the NPU.** RKNN needs Rockchip's vendor kernel (6.1 series)
   with the `rknpu` driver (0.9.8 is current); a mainline kernel's `rocket` driver does
   not work with RKNN.

   ```bash
   uname -r                                          # vendor kernels look like 6.1.x-...rk35xx / -rockchip-rk3588
   ls /sys/bus/platform/drivers | grep -i -e rknpu -e rocket   # need: RKNPU
   sudo cat /sys/kernel/debug/rknpu/version          # e.g. RKNPU driver: v0.9.8
   tr '\0' '\n' < /proc/device-tree/compatible       # need: rockchip,rk3588
   ls -l /dev/dri/by-path/ | grep -i npu             # the NPU's device nodes, e.g. card1 and renderD129
   ls -l /dev/dri/                                   # their group (video / render) and mode
   id -nG                                            # the groups of the user who runs Ember
   ```

   No `RKNPU` means this image cannot use the NPU: keep the CPU (nothing to do) or move
   to an image with the vendor kernel. Changing the kernel is outside Ember's setup.
   The user who runs Ember (and the converter) needs read/write access to the NPU's
   nodes. On the tested image they are `card1` (group `video`) and `renderD129` (group
   `render`), mode 0660, and the default Armbian user is in both groups. If yours is
   not: `sudo usermod -aG render,video $USER`, then log out and in again.
2. **Install the NPU runtime library (administrator step).** Use the same version as
   step 3 (2.3.2 here; 2.5.0 SHA-256 `4b6cc575bedf8952b13db4b1190fa3b3d2ac213f00be774ff6ccd19d651970c0`):

   ```bash
   curl -fLO https://github.com/airockchip/rknn-toolkit2/raw/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so
   echo "d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8  librknnrt.so" | sha256sum -c
   sudo install -m 0644 librknnrt.so /usr/lib/librknnrt.so && sudo ldconfig
   ```
3. **rknn-toolkit-lite2 (the NPU's Python side) comes with `requirements-orange-pi.txt`**
   (2.3.2, the converter's default; Python 3.12 aarch64 wheel), so **1 - Setup** and
   updates install it. On a Pi set up before it was added, rerun **1 - Setup** or:

   ```bash
   cd Baymax-main
   .venv/bin/python -m pip install -r requirements-orange-pi.txt
   ```

   Ember only uses the NPU on its own when the accuracy check (step 4) ran with the same
   rknn-toolkit-lite2 version. The converter's `--rknn-version 2.5.0` is for
   experiments: Setup and updates put 2.3.2 back, and Ember then logs the version
   mismatch and uses the CPU until the check is re-run with 2.3.2.
4. **Convert the models on the board.** First stop Ember with **4 - Stop Ember**: the
   check uses NPU core 0 like the running robot. About 3 GB of downloads/disk for the
   converter's own environments under `.models/pose_npu/build/`; takes a while:

   ```bash
   features/pose/setup_converter.sh               # add --rknn-version 2.5.0 to try the newer toolkit
   .venv/bin/python features/pose/convert_rknn.py
   ```

   It ends with `Accuracy check PASSED` (or `FAILED (<what missed its limit>)`: Ember
   then stays on the CPU) and writes `.models/pose_npu/manifest.json`. The check
   compares the NPU with ONNX Runtime (landmark positions and which landmarks are
   visible) on still frames and on a 72-frame tracked sequence (including a fall); it
   checks that the conversion is faithful, not how the NPU does on real footage. To
   repeat only the check: `.models/pose_npu/build/venv-rknn/bin/python
   features/pose/convert_rknn.py check`. Don't add `--video` to it: a real clip becomes
   part of the gate, whose limits are set for the synthetic sequence, and on the tested
   board every real clip tried missed them, which keeps Ember on the CPU (a plain
   `check` restores the NPU). To see how the NPU does on your own footage, compare it
   with MediaPipe instead (Ember stopped; the clip stays on the board):
   `.venv/bin/python -m features.pose.validate --engine npu --task pose_landmarker_full.task --onnx-dir .models/pose_npu --fps 10 my_clip.mp4`.
   A check that fails with `error during the check: RuntimeError: init_runtime(...)
   returned -1` usually means the NPU device could not be opened: see the access check
   in step 1 (librknnrt may print `failed to open rknpu module, need to insmod rknpu
   dirver!` even when the driver is loaded), or a driver problem.
5. **Measure** (Ember still stopped, so it does not compete for the CPU and NPU):
   `.venv/bin/python features/pose/benchmark.py` prints milliseconds and process CPU
   per frame for MediaPipe on the CPU and for the NPU, and how close their landmarks
   are. By default frames run back to back (throughput: about 11 ms on the NPU here);
   `--fps 10` paces them like Ember's camera thread, where an NPU frame takes longer
   (measured here at `--fps 15`, Ember's rate before it moved to 10 fps:
   median 16.2 ms with a person in view, 28.5 ms with nobody in view, `--empty`, against
   about 40-44 ms for MediaPipe) and process CPU is 12-20% of a core instead of 70-77%.
   Process CPU counts every thread while each frame's pose runs, not between frames.
6. **Confirm it is active.** Start Ember. The log (the pose process writes to Ember's) shows
   `[POSE] Pose backend: NPU (RK3588) on NPU core 0 (...)`; otherwise
   `[POSE] Pose backend: CPU (MediaPipe). NPU skipped: <reason>`. If the `librknnrt` or
   rknpu driver version changed since the check (e.g. a kernel update that brought a new
   rknpu driver) the reason says so: re-run the check. A kernel update that keeps both
   versions keeps the NPU without a new check; re-running the check after any kernel
   update does no harm. With `camera.pose_model = "full"`, a `[POSE] Warning:` line
   after the NPU line means the downloaded MediaPipe model changed (see the pose
   README). With the fall toolkit enabled the Monitor shows **Running on NPU (core
   pose)** and the inference time: the paced figure (measured at 15 fps before the
   merge: about 16-29 ms, higher than the benchmark's back-to-back number), now plus
   the round trip to the pose process. If the reason is
   `NPU init failed (... init_runtime ...)` and the check passed as another user, the
   user running Ember lacks access to the NPU's nodes: see step 1.
7. **Force the CPU** at any time with `BAYMAX_POSE_BACKEND=cpu` (or `mediapipe`) in
   `Baymax-main/.env` (or delete `.models/pose_npu`; or set `camera.pose_model = "heavy"`).
   The pose process inherits Ember's environment. Any other unrecognised value also
   means the CPU, with a warning. `BAYMAX_POSE_NPU_CORE=1` moves pose to another NPU core.

See [fall toolkit](features/toolkits/FALL_DETECTION.md) for model/algorithm provenance, synthetic checks, known limits and the real-device test protocol. Full conversation/fall integration must be measured on the board before approval or deployment.
