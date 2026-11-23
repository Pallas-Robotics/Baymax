"""Opt-in CPU pose/fall toolkit. Reuses camera frames; never opens a camera."""
import asyncio
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task"
MODEL_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"
# Written by the local dev UI, read by the running Ember process (gitignored .runtime-*.json).
# Bound to the runtime's PID, so a restarted Ember always starts with demo mode off.
DEMO_PATH = ROOT / ".runtime-fall-demo.json"
# What each kind of possible fall triggers in this build; shown on the demo screen.
WOULD_FIRE_REPLAY = (
    "Ember is asked, through the live Gemini session, to check in calmly and ask whether the person is okay "
    "(unless 'Ember speaks the check-in' is off).",
    "A local monitor event; the eyes show surprise.",
    "Nothing else: a synthetic replay never reaches the runtime's own fall path, so no dashboard/cloud "
    "/api/fall report, caregiver email, SMS or call.")
WOULD_FIRE_CAMERA = (
    "Ember's built-in detection asks Gemini to check on the person ([FALL ALERT]); demo mode never silences it.",
    "A local monitor event; the eyes show surprise.",
    "A /api/fall report to the dashboard (Ember cloud when the robot is linked, else the local webapp), "
    "exactly as without demo mode. No caregiver email, SMS or call is sent by Ember itself.")


# Other local features that reuse the camera frames / live session (e.g. proactive greetings).
frame_observers = []   # observer(frame), called on the camera thread; must be cheap.
# async task(session, shutdown_event), run for each live session. shutdown_event is a
# threading.Event: poll is_set() and await asyncio.sleep(); never call its wait().
session_tasks = []


def enabled():
    return os.getenv("BAYMAX_FALL_DETECTION_ENABLED", "0") == "1"


def ensure_model(path, fetch=None, expected_hash=MODEL_SHA256):
    path = Path(path)
    if path.is_file():
        data = path.read_bytes()
    else:
        if fetch is None:
            def fetch():
                with urllib.request.urlopen(MODEL_URL, timeout=30) as response:
                    return response.read(12 * 1024 * 1024 + 1)
        data = fetch()
        if len(data) > 12 * 1024 * 1024:
            raise RuntimeError("Pose model download exceeded its expected size.")
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise RuntimeError("Pose model checksum failed. Remove the cached .models file and retry; detection is unavailable.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".download")
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


class CpuPose:
    def __init__(self, model_path=None):
        import mediapipe as mp
        from mediapipe.tasks import python as tasks
        from mediapipe.tasks.python import vision
        self.mp = mp
        model = model_path or ensure_model(ROOT / ".models/pose_landmarker_full.task")
        options = vision.PoseLandmarkerOptions(
            base_options=tasks.BaseOptions(model_asset_path=str(model), delegate=tasks.BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_poses=1,
            min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5, min_tracking_confidence=0.5)
        self.landmarker = vision.PoseLandmarker.create_from_options(options)
        self.timestamp = -1

    def detect(self, frame):
        import cv2
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        self.timestamp = max(self.timestamp + 1, int(time.monotonic() * 1000))
        result = self.landmarker.detect_for_video(image, self.timestamp)
        return result.pose_landmarks[0] if result.pose_landmarks else None

    def close(self):
        self.landmarker.close()


def remote_pose(model_path=None):
    """CpuPose in its own process, so inference cannot stall the mic or Gemini I/O. The
    model is fetched here first: the child gets a limited time to start (vision_process)."""
    from functools import partial
    from features.vision_process import Remote
    model_path = model_path or ensure_model(ROOT / ".models/pose_landmarker_full.task")
    return Remote(partial(CpuPose, model_path))


def _detector_factory(**settings):
    from fall_detection.detector import FallDetector
    return FallDetector(**settings)


def _record(message):
    import runtime_monitor
    runtime_monitor.record("fall_detection", message)


class FallWorker:
    def __init__(self, backend_factory=remote_pose, detector_factory=_detector_factory):
        self.backend_factory = backend_factory
        self.detector_factory = detector_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.pending = None
        self.alerts = deque(maxlen=8)
        self.thread = None
        self.is_enabled = False
        self.state = "Disabled"
        self.inference_ms = None
        self.last_result = None
        self.samples = deque(maxlen=30)
        self.dropped = 0
        self.shared = False                 # landmarks come from the core's pose model
        self.builtin = False                # the core's own fall detection sends the check-in
        self.pose_device = "CPU"            # where the core's pose model runs: "CPU" or "NPU"
        self.running_state = "Running on CPU"
        # Demo mode: alerts listed on the dev-UI screen, optional spoken check-in for synthetic
        # replays, and a live pose summary (joint points only while demo mode is on).
        self.demo = {"enabled": False, "speak": True}
        self.demo_alerts = deque(maxlen=5)
        self.live = None
        self.replay_thread = None
        self.replay_speed = 1.0   # tests run replays faster; decisions use virtual frame times
        self._replay_id = None
        self._core_labels = None   # posture/countdown for the runtime's own detector (demo screen)

    def start(self, fps=15, shared_pose=False, builtin=False, **settings):
        """shared_pose=True: the core already runs pose on every frame and passes its
        landmarks to share(), so this worker loads no second model. builtin=True: the
        core's own fall detection is on and sends the possible-fall check-in, so this
        worker still detects and reports (Monitor) but queues no second check-in. Only
        with shared_pose: on its own model the core has no landmarks to check, so this
        worker still checks in."""
        if self.thread and self.thread.is_alive():
            return
        self.shared = bool(shared_pose)
        self.builtin = bool(builtin) and self.shared    # the core sees no pose otherwise
        self.running_state = self._running_label()
        self.is_enabled = enabled()
        if not self.is_enabled:
            self.state = "Disabled"
            return
        try:
            fps = float(os.getenv("BAYMAX_FALL_FPS", str(fps)))
        except ValueError:
            fps = 0
        if not 2 <= fps <= 30:
            self.state = "Unavailable: target FPS must be between 2 and 30"
            _record(self.state)
            return
        self.interval = 1.0 / fps
        self.settings = settings
        self.stop_event.clear()
        self.state = "Starting" if self.shared else "Loading CPU pose model"
        self.thread = threading.Thread(target=self._run, name="ember-fall-cpu", daemon=True)
        self.thread.start()

    def submit(self, frame):
        import runtime_monitor
        runtime_monitor.sensor_alive('camera')  # Every good frame passes here, fall detection on or off.
        for observer in list(frame_observers):
            try:
                observer(frame)
            except Exception:
                frame_observers.remove(observer)
                print("[CAM] Frame observer disabled after an error.", flush=True)
        if self.shared or not self.is_enabled or self.stop_event.is_set() or not self.thread or not self.thread.is_alive():
            return
        # Keep one frame, not a growing inference queue. Copy outside camera locks.
        self._put((time.monotonic(), frame.copy()))

    def _running_label(self):
        return f"Running on {self.pose_device} (core pose)" if self.shared else "Running on CPU"

    def share(self, landmarks, inference_ms=None, device=None, captured_at=None):
        """The core's pose result for the latest frame, how long it took, where it ran
        ("CPU" or "NPU", from features/pose/backend.py) and when the frame was captured
        (time.monotonic(); fall velocities use it) in shared_pose mode."""
        if device and self.shared and device != self.pose_device:
            with self.lock:
                previous = self.running_state
                self.pose_device = device
                self.running_state = self._running_label()
                if self.state == previous:
                    self.state = self.running_state
        if not self.shared or not self.is_enabled or self.stop_event.is_set() or not self.thread or not self.thread.is_alive():
            return
        self._put((time.monotonic(), landmarks, inference_ms, captured_at))

    def _put(self, item):
        with self.lock:
            if self.pending is not None:
                self.dropped += 1
            self.pending = item
        self.wake.set()

    def _run(self):
        backend = None
        try:
            backend = None if self.shared else self.backend_factory()
            detector = self.detector_factory(**self.settings)
            from features.toolkits.fall_logic import Labeler
            labels = Labeler()
            with self.lock:                 # share(device=...) may relabel running_state meanwhile
                self.state = self.running_state
            _record("Fall detection running on the core's pose model; development mode" if self.shared
                    else "Fall detection running on CPU; development mode")
            next_frame = 0.0
            while not self.stop_event.is_set():
                self.wake.wait(timeout=0.2)
                self.wake.clear()
                if self.stop_event.wait(max(0, next_frame - time.monotonic())):
                    break
                with self.lock:
                    item, self.pending = self.pending, None
                if item is None or time.monotonic() - item[0] > 0.5:
                    continue
                started = time.monotonic()
                landmarks = item[1] if self.shared else backend.detect(item[1])
                if self.stop_event.is_set():
                    break
                # Velocities use when the frame was captured (the core's time; or when
                # submit() got it), not when its pose was done.
                captured = item[3] if self.shared else item[0]
                result = detector.update(landmarks, timestamp=captured)
                finished = time.monotonic()
                with self.lock:
                    self.inference_ms = (item[2] if self.shared else round((finished - started) * 1000, 1))
                    self.samples.append(finished)
                    self.last_result = {k: result.get(k) for k in ("fall_pending", "fall_active", "trunk_angle")}
                    if result.get("fall_detected") and not self.builtin:   # the core's own already checks in
                        self.alerts.append({"time": time.time(), "recorded": finished})
                if not self.builtin:   # with the built-in detector on, observe_core() shows its results
                    self._show_camera(labels, detector, landmarks, result, captured, "camera (fall toolkit)")
                if result.get("fall_detected"):
                    _record("Possible fall detected locally; check-ins come from Ember's built-in fall detection."
                            if self.builtin else
                            "Possible fall detected locally; awaiting a check-in. No external alert sent.")
                next_frame = started + self.interval
        except Exception as error:
            self.state = "Unavailable: " + type(error).__name__
            _record("Fall detection unavailable (" + type(error).__name__ + "). Conversation can continue; check the fall toolkit setup.")
            print("[FALL] Detection unavailable: " + str(error), flush=True)
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            if not self.state.startswith("Unavailable"):
                self.state = "Stopped"

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            self.pending = None
            self.alerts.clear()

    # ── Demo mode ─────────────────────────────────────────────────────────
    def _replaying(self):
        return self.replay_thread is not None and self.replay_thread.is_alive()

    def _publish(self, landmarks, result, labels, source, stay_down_s):
        """Keep a small live summary for the demo screen; joint points only while demo mode is on."""
        from features.toolkits.fall_logic import points
        with self.lock:
            if not self.demo["enabled"]:
                self.live = None
                return
            angle, countdown = result.get("trunk_angle"), labels.get("countdown_s")
            self.live = {"source": source, "updated": time.time(), "points": points(landmarks),
                         "posture": labels.get("posture"), "trunk_angle": None if angle is None else round(angle, 1),
                         "fall_pending": bool(result.get("fall_pending")), "fall_active": bool(result.get("fall_active")),
                         "countdown_s": None if countdown is None else round(countdown, 2), "stay_down_s": stay_down_s}

    def _demo_alert(self, source, spoken, would_fire):
        with self.lock:
            if self.demo["enabled"]:
                self.demo_alerts.append({"time": time.time(), "source": source, "spoken": spoken,
                                         "would_fire": list(would_fire)})

    def _show_camera(self, labeler, detector, landmarks, result, captured_at, source):
        """Camera results on the demo screen (display only; the alert paths are unchanged)."""
        if not self.demo["enabled"]:
            return
        labels = labeler.label(detector, landmarks, result, captured_at)
        if not self._replaying():
            self._publish(landmarks, result, labels, source, detector.stay_down_s)
        if result.get("fall_detected"):
            self._demo_alert(source, True, WOULD_FIRE_CAMERA)

    def observe_core(self, landmarks, result, detector, captured_at):
        """realtime_gemini_10's own detector result for one camera frame. Only while demo mode
        is on: shown on the demo screen. Never changes the decision or its check-in/report."""
        try:
            if not self.demo["enabled"]:
                self._core_labels = None
                return
            if self._core_labels is None:
                from features.toolkits.fall_logic import Labeler
                self._core_labels = Labeler()
            self._show_camera(self._core_labels, detector, landmarks, result, captured_at,
                              "camera (Ember's built-in detection)")
        except Exception:
            pass   # the demo screen must never disturb the camera thread

    def _replay_detected(self, name):
        """A confirmed possible fall from a synthetic replay: the toolkit's check-in path only."""
        with self.lock:
            speak = self.demo["speak"] or not self.demo["enabled"]
            # Queued even when the built-in detector owns camera check-ins: it never sees replays.
            self.alerts.append({"time": time.time(), "recorded": time.monotonic(),
                                "source": "synthetic replay", "quiet": not speak})
        self._demo_alert("synthetic replay: " + name, speak, WOULD_FIRE_REPLAY)
        _record("Demo: possible fall detected from a synthetic pose replay (not a camera). No external alert sent.")

    def set_demo(self, enabled, speak=True):
        with self.lock:
            new = {"enabled": bool(enabled), "speak": bool(speak)}
            changed, self.demo = self.demo != new, new
            if not enabled:
                self.live = None
                self.demo_alerts.clear()
        if changed:
            _record("Fall demo mode " + ("on: alerts shown on screen" + ("; Ember speaks replay check-ins" if speak else
                                         "; Ember stays quiet for replays") if enabled else "off"))

    def replay(self, name="fast_fall"):
        """Push a made-up pose sequence, in real time, through v10's decision (fall_logic.FallDecision:
        realtime_gemini_10's detector and thresholds) and the toolkit's check-in path."""
        from features.toolkits import fall_poses
        from features.toolkits.fall_logic import FallDecision, V10_FPS
        if name not in fall_poses.DEMO_SCENARIOS:
            raise ValueError("Unknown synthetic scenario.")
        if self._replaying():
            return False
        scenario = fall_poses.scenario(name, V10_FPS)
        decision = FallDecision()

        def run():
            _record("Demo: replaying synthetic poses '" + name + "' (" + str(round(scenario.duration, 1)) + " s)")
            base = time.monotonic()
            for moment, landmarks in scenario.frames:
                delay = base + moment / self.replay_speed - time.monotonic()
                if delay > 0 and self.stop_event.wait(delay):
                    return
                result = decision.update(landmarks, base + moment)  # virtual frame time
                self._publish(landmarks, result, result, "synthetic replay: " + name, decision.stay_down_s)
                if result.get("fall_detected"):
                    self._replay_detected(name)

        self.replay_thread = threading.Thread(target=run, name="ember-fall-replay", daemon=True)
        self.replay_thread.start()
        return True

    def poll_demo_control(self, path=None):
        """Apply the dev UI's demo settings/replay request when written for this process."""
        try:
            request = json.loads(Path(path or DEMO_PATH).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(request, dict) or request.get("pid") != os.getpid():
            return
        enabled, speak = request.get("enabled"), request.get("speak", True)
        if isinstance(enabled, bool) and isinstance(speak, bool):
            self.set_demo(enabled, speak)
        replay = request.get("replay")
        requested_at = replay.get("requested_at", 0) if isinstance(replay, dict) else 0
        if (enabled is True and isinstance(replay, dict) and replay.get("id") != self._replay_id
                and isinstance(requested_at, (int, float)) and 0 <= time.time() - requested_at < 15):
            self._replay_id = replay.get("id")
            try:
                self.replay(replay.get("scenario"))
            except ValueError:
                pass

    def pop_alert(self):
        with self.lock:
            while self.alerts:
                alert = self.alerts.popleft()
                if time.monotonic() - alert["recorded"] <= 15:
                    return alert
        return None

    def status(self):
        with self.lock:
            fps = ((len(self.samples) - 1) / (self.samples[-1] - self.samples[0])
                   if len(self.samples) > 1 and self.samples[-1] > self.samples[0] else None)
            state = self.state
            if state == self.running_state and (not self.samples or time.monotonic() - self.samples[-1] > 2):
                state = "Waiting for fresh camera frames"
                fps = None
            if state == "Disabled" and enabled():
                state = "Waiting for camera"
            if self.stop_event.is_set() and self.is_enabled and self.thread and self.thread.is_alive():
                state = "Stopping"
            live = self.live if self.live and time.time() - self.live["updated"] <= 2 else None
            return {"state": state, "backend": f"{self.pose_device} (core pose)" if self.shared else "CPU",
                    "inference_ms": self.inference_ms,
                    "fps": round(fps, 1) if fps is not None else None, "dropped": self.dropped,
                    "last_result": dict(self.last_result) if self.last_result else None,
                    "demo": {**self.demo, "replaying": self._replaying(), "live": dict(live) if live else None,
                             "alerts": [dict(alert) for alert in self.demo_alerts]}}

    async def alert_monitor(self, session, shutdown_event):
        tasks = [asyncio.create_task(task(session, shutdown_event)) for task in list(session_tasks)]
        try:
            await self._alerts(session, shutdown_event)
        finally:
            for task in tasks:
                task.cancel()

    async def _alerts(self, session, shutdown_event):
        while not shutdown_event.is_set():
            alert = self.pop_alert()
            if alert:
                import runtime_monitor
                runtime_monitor.notify('fall_alert')
                if alert.get("quiet"):
                    _record("Demo mode: synthetic possible fall shown on screen; Ember was not asked to speak.")
                    await asyncio.sleep(0.1)
                    continue
                from features.conversational_ability.engage import engagement
                engagement.engage("possible fall")  # The person must be able to answer the check-in.
                from features.toolkits import audiobooks
                # Playback withholds the mic too. A retry finds it already paused (nothing new
                # to say), so it keeps the first try's note.
                held = audiobooks.hold_for_answer("possible fall") or alert.get("held", "")
                try:
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text": "[Local experimental fall detector] A possible fall was detected. "
                               "Check in calmly and ask whether I am okay. This is uncertain; do not claim an emergency "
                               "or that anyone has been contacted." + held}]}, turn_complete=True)
                except BaseException as error:
                    # Not delivered (a reconnect only resends what was): ask again on the next try.
                    with self.lock:
                        self.alerts.appendleft({**alert, "recorded": time.monotonic(), "held": held})
                    if not isinstance(error, Exception):
                        raise
                    _record("Possible fall check-in could not reach Gemini yet; it will be asked again.")
                    await asyncio.sleep(2)
            await asyncio.sleep(0.1)


worker = FallWorker()
