"""Choose the core camera thread's pose backend.

realtime_gemini_10.py runs it in features/vision_process.py's child process, never in
Ember's own (pose there held the GIL and starved the mic and Gemini I/O):

    pose = Remote(partial(ChildPose, task_path, allow_npu=True))
    landmarks = pose.detect(frame_bgr)                 # None or 33 Points
    pose.device  -> "NPU" / "CPU" (read after each frame: the NPU can fail over)
    pose.close()

The backend itself (in that child, and in benchmark.py and validate.py):

    pose = create_pose_backend(model_path=task_path)   # or mediapipe_options=options
    landmarks = pose.detect(frame_bgr, timestamp_ms)   # None or 33 landmarks
    pose.name    -> "NPU (RK3588)" / "CPU (MediaPipe)" / "CPU (ONNX Runtime)"
    pose.device  -> "NPU" / "CPU"
    pose.close()

The NPU always runs the converted, checked pose_landmarker_full models; task_path (the
runtime's camera.pose_model .task, lite by default) is what MediaPipe runs on the CPU.

BAYMAX_POSE_BACKEND=auto (default) uses the RK3588 NPU only when all of these hold:
Linux aarch64; 'rockchip,rk3588' in /proc/device-tree/compatible; rknn-toolkit-lite2
imports; the converted .rknn files exist with the SHA-256s recorded at conversion; the
converter's on-board accuracy check passed with the rknn-toolkit-lite2 version this
Python has; init_runtime and a warm-up inference succeed; librknnrt/the driver report
the same version (get_sdk_version) as when the check ran. Otherwise it is MediaPipe on
the CPU, exactly as before (same options, same calls). Any error while probing or
opening the NPU also means MediaPipe. =cpu (or =mediapipe) forces MediaPipe; =npu skips
the board/accuracy-check gates (still needs rknnlite, matching files and a working
runtime) and =onnx runs the port on ONNX Runtime (development). Any other value is a
mistyped override: MediaPipe on the CPU with a warning, never the NPU. The port uses
the confidences in mediapipe_options, like MediaPipe. Every choice is printed once with
the reason the NPU was skipped. If the NPU fails during use, the backend switches to
MediaPipe for the rest of the run and prints the traceback once. In the pose child that
frame imports and loads MediaPipe, so it first tells the vision process
(features.vision_process.loading_model), whose watchdog then gives the frame the start's
time limit instead of a frame's.
"""
import os
from pathlib import Path
import platform
import sys
import time
import traceback

from features.pose import models
from features.vision_process import loading_model

ENV_BACKEND = "BAYMAX_POSE_BACKEND"
ENV_CORE = "BAYMAX_POSE_NPU_CORE"
CHOICES = ("auto", "cpu", "npu", "onnx")
ALIASES = {"mediapipe": "cpu"}
NPU_NAME = "NPU (RK3588)"
CPU_NAME = "CPU (MediaPipe)"
ONNX_NAME = "CPU (ONNX Runtime)"


def _log(message):
    print(f"[POSE] {message}", flush=True)


def default_mediapipe_options(model_path):
    """The camera's PoseLandmarker settings: ChildPose (realtime_gemini_10.py's pose
    process) uses them for MediaPipe on the CPU, and benchmark.py/validate.py too. VIDEO
    mode is required (MediaPipeCpuBackend calls detect_for_video). The NPU/ONNX port's
    PosePipeline defaults are the same 0.5 confidences."""
    from mediapipe.tasks import python as mp_tasks
    from mediapipe.tasks.python import vision
    return vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5)


class _Backend:
    name = ""
    device = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MediaPipeCpuBackend(_Backend):
    """MediaPipe PoseLandmarker on the CPU: the calls the camera thread always made."""
    name = CPU_NAME
    device = "CPU"

    def __init__(self, options):
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        self._mp = mp
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def detect(self, frame_bgr, timestamp_ms):
        import cv2
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        detection = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        return detection.pose_landmarks[0] if detection.pose_landmarks else None

    def close(self):
        self._landmarker.close()


class PipelineBackend(_Backend):
    """features.pose.pipeline on an engine (RKNN on the NPU, or ONNX Runtime).
    confidences: PosePipeline's min_*_confidence keywords (default 0.5 each)."""

    def __init__(self, engine, name, device, **confidences):
        from features.pose.pipeline import PosePipeline
        self.engine = engine
        self.name = name
        self.device = device
        self.pipeline = PosePipeline(engine, **confidences)

    def detect(self, frame_bgr, timestamp_ms):
        return self.pipeline.detect(frame_bgr, timestamp_ms)

    def close(self):
        self.engine.close()


class FallbackBackend(_Backend):
    """The NPU backend, replaced by MediaPipe on the CPU if it ever raises."""

    def __init__(self, primary, cpu_factory, log=_log):
        self.active = primary
        self.cpu_factory = cpu_factory
        self.log = log

    @property
    def name(self):
        return self.active.name

    @property
    def device(self):
        return self.active.device

    def detect(self, frame_bgr, timestamp_ms):
        try:
            return self.active.detect(frame_bgr, timestamp_ms)
        except Exception as error:
            if self.cpu_factory is None:
                raise
            failed = self.active
            self.log(f"{failed.name} failed during inference ({type(error).__name__}: {error}); "
                     f"switching to {CPU_NAME} for the rest of this run.")
            self.log("Traceback of that failure:\n" + "".join(traceback.format_exception(error)).rstrip())
            self.cpu_factory, factory = None, self.cpu_factory
            try:
                failed.close()
            except Exception:
                pass
            # In the pose child this frame now imports and loads MediaPipe (1.2-1.3 s on the
            # 5 Max when cached, longer from a cold SD card): the start's limit, not 5 s.
            loading_model()
            self.active = factory()
            return self.active.detect(frame_bgr, timestamp_ms)

    def close(self):
        self.active.close()


# ── Probes (replaced in tests) ───────────────────────────────────────────────
def on_rk3588():
    import pc_hardware
    return pc_hardware.is_rk3588()


def import_rknnlite():
    from features.pose.engines import rknnlite_class   # reached only after the platform checks
    return rknnlite_class()


def installed_rknnlite_version():
    """rknn-toolkit-lite2's version in this Python (None if unknown)."""
    try:
        from importlib.metadata import version
        return version("rknn-toolkit-lite2")
    except Exception:
        return None


def driver_hint():
    """Kernel/driver facts worth printing when the NPU will not start."""
    import pc_hardware
    drivers = pc_hardware.npu_drivers()
    if any(name.lower() == "rknpu" for name in drivers):
        driver = "rknpu driver loaded"
    elif drivers:
        driver = f"only the mainline '{drivers[0]}' NPU driver (RKNN needs Rockchip's vendor rknpu driver)"
    else:
        driver = "no rknpu driver (RKNN needs Rockchip's vendor kernel, e.g. 6.1 with rknpu 0.9.8)"
    return f"kernel {platform.release()}; {driver}"


def npu_unavailable_reason(env=None, *, system=None, machine=None, rk3588=on_rk3588,
                           rknnlite=import_rknnlite, lite_version=installed_rknnlite_version,
                           forced=False):
    """None when the NPU may be tried, else a one-line reason. Imports rknnlite only on
    Linux aarch64 (and only on an RK3588 unless forced)."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if not system.startswith("linux") or machine not in ("aarch64", "arm64"):
        return f"not Linux aarch64 ({system} {machine})"
    if not forced and not rk3588():
        return "not an RK3588 board ('rockchip,rk3588' not in /proc/device-tree/compatible)"
    try:
        rknnlite()
    except Exception as error:
        return f"rknn-toolkit-lite2 is not usable in this Python ({type(error).__name__}: {error})"
    directory = models.model_dir(env)
    try:
        manifest = models.load_manifest(directory)
        models.verified_files(directory, manifest, "rknn")
    except models.ModelError as error:
        return str(error)
    if forced:
        return None
    check = models.check_section(manifest)
    if check.get("passed") is not True:
        return "the converter's on-board accuracy check has not passed (see manifest.json; re-run convert_rknn.py)"
    checked, current = check.get("rknn_toolkit_lite2"), lite_version()
    if isinstance(checked, str) and current and checked != current:
        return (f"this Python has rknn-toolkit-lite2 {current} but the accuracy check ran with {checked} "
                f"(install {checked}, or re-run convert_rknn.py check)")
    return None


def _flat(text):
    return " ".join(str(text).split())


# PoseLandmarkerOptions field -> PosePipeline keyword
_CONFIDENCES = {"min_pose_detection_confidence": "min_detection_confidence",
                "min_pose_presence_confidence": "min_presence_confidence",
                "min_tracking_confidence": "min_tracking_confidence"}


def pipeline_confidences(mediapipe_options):
    """The confidences the runtime gives MediaPipe, as PosePipeline keywords, so the port
    and its MediaPipe fallback decide alike. Missing or invalid ones (not in 0..1) keep 0.5."""
    confidences = {}
    for option, keyword in _CONFIDENCES.items():
        try:
            value = getattr(mediapipe_options, option, None)
        except Exception:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0:
            confidences[keyword] = float(value)
    return confidences


def _open_npu(env, engine_factory, confidences):
    """(backend, core, toolkit version, runtime change or None) or an exception, with
    nothing left open. The runtime change is set when librknnrt/the driver report another
    version (get_sdk_version, whitespace ignored) than when the accuracy check ran."""
    directory = models.model_dir(env)
    manifest = models.load_manifest(directory)
    paths = models.verified_files(directory, manifest, "rknn")
    core = env.get(ENV_CORE, "0").strip() or "0"
    tools = manifest.get("tools")
    toolkit = tools.get("rknn-toolkit2", "?") if isinstance(tools, dict) else "?"
    checked_sdk = models.check_section(manifest).get("sdk")
    engine = engine_factory(paths["pose_detector"], paths["pose_landmarks_detector"], core)
    try:
        _warm_up(engine)
        backend = PipelineBackend(engine, NPU_NAME, "NPU", **confidences)
        sdk = getattr(engine, "sdk_version", lambda: None)()
    except Exception:
        engine.close()
        raise
    now, then = (_flat(v) if isinstance(v, str) else "" for v in (sdk, checked_sdk))
    changed = None
    if now and then and now != then:                  # unknown on either side: not a change
        changed = (f"the NPU runtime/driver ({now}) differs from the one the accuracy check "
                   f"ran on ({then}); re-run convert_rknn.py check")
    return backend, core, toolkit, changed


def _open_onnx(env, confidences):
    from features.pose.engines import OnnxEngine
    directory = models.model_dir(env)
    paths = models.verified_files(directory, models.load_manifest(directory), "onnx")
    engine = OnnxEngine(paths["pose_detector"], paths["pose_landmarks_detector"])
    return PipelineBackend(engine, ONNX_NAME, "CPU", **confidences)


def _warm_up(engine):
    import numpy as np
    boxes, scores = engine.detector(np.zeros((224, 224, 3), np.uint8))
    raw, flag, heatmap = engine.landmarks(np.zeros((256, 256, 3), np.uint8))
    if not (np.isfinite(boxes).all() and np.isfinite(raw).all() and np.isfinite(flag).all()):
        raise RuntimeError("warm-up inference returned non-finite values")


def weights_warning(mediapipe_options=None, model_path=None):
    """A warning when the runtime's full .task (MediaPipe, the fallback) holds other weights
    than the ones the converted models were made from (e.g. Google changed 'latest').
    None for a lite or heavy .task, which is a different model by choice."""
    try:
        path = model_path or getattr(getattr(mediapipe_options, "base_options", None), "model_asset_path", None)
        # Only a full .task can match: lite or heavy is a different network on purpose
        # (the NPU always runs the checked full models; the lite .task is the CPU fallback).
        if not path or not Path(path).name.startswith("pose_landmarker_full"):
            return None
        if models.task_matches_pinned(path) is not False:
            return None
    except Exception:
        return None
    return (f"{Path(path).name} has different weights from the converted models (models.TFLITE_SHA256), "
            f"so the port and MediaPipe (the fallback) may disagree")


def _rknn_engine(detector, landmarks, core):
    from features.pose.engines import RknnEngine
    return RknnEngine(detector, landmarks, core)


def create_pose_backend(mediapipe_options=None, model_path=None, env=None, log=_log,
                        cpu_factory=None, probe=None, engine_factory=_rknn_engine):
    """The pose backend for this machine (see the module docstring).

    mediapipe_options: the runtime's PoseLandmarkerOptions for the CPU path
    (default: default_mediapipe_options(model_path)). probe/engine_factory/cpu_factory
    are for tests."""
    env = os.environ if env is None else env
    if cpu_factory is None:
        def cpu_factory():
            options = mediapipe_options if mediapipe_options is not None else default_mediapipe_options(model_path)
            return MediaPipeCpuBackend(options)
    probe = probe or npu_unavailable_reason
    confidences = pipeline_confidences(mediapipe_options)

    value = env.get(ENV_BACKEND, "auto").strip().lower() or "auto"
    choice = ALIASES.get(value, value)
    if choice not in CHOICES:
        # Usually someone turning the NPU off with the wrong word: the safe side is the CPU.
        log(f"Warning: unknown {ENV_BACKEND}={value!r} (use auto, cpu or mediapipe, npu, onnx); "
            f"using {CPU_NAME}, not the NPU.")
        choice = "cpu"

    if choice == "cpu":
        log(f"Pose backend: {CPU_NAME} ({ENV_BACKEND}={value}).")
        return cpu_factory()

    if choice == "onnx":
        try:
            backend = _open_onnx(env, confidences)
        except Exception as error:
            log(f"Pose backend: {CPU_NAME}. ONNX Runtime skipped: {type(error).__name__}: {error}")
            return cpu_factory()
        log(f"Pose backend: {ONNX_NAME} ({ENV_BACKEND}=onnx).")
        _warn(log, [weights_warning(mediapipe_options, model_path)])
        return FallbackBackend(backend, cpu_factory, log)

    forced = choice == "npu"
    try:
        reason = probe(env, forced=forced)
    except Exception as error:
        reason = f"NPU probe failed ({type(error).__name__}: {error})"
    if reason is None:
        try:
            backend, core, toolkit, changed = _open_npu(env, engine_factory, confidences)
        except Exception as error:
            reason = f"NPU init failed ({type(error).__name__}: {error}); {_driver_hint()}"
        else:
            if changed and not forced:             # unchecked runtime: only ever falls back to the CPU
                _close_quietly(backend)
                reason = f"{changed} (or {ENV_BACKEND}=npu)"
            else:
                log(f"Pose backend: {NPU_NAME} on NPU core {core} (models from rknn-toolkit2 {toolkit}"
                    f"{'; forced' if forced else ''}). {ENV_BACKEND}=cpu returns to MediaPipe.")
                _warn(log, [changed, weights_warning(mediapipe_options, model_path)])
                return FallbackBackend(backend, cpu_factory, log)
    log(f"Pose backend: {CPU_NAME}. NPU skipped: {reason}.")
    return cpu_factory()


class ChildPose:
    """The camera thread's pose backend, built inside features/vision_process.py's child:
    Remote(partial(ChildPose, task_path, allow_npu=...)). One-argument detect(frame) like
    fall_detection.CpuPose; the strictly increasing ms timestamps are made here.

    allow_npu=False (camera.pose_model heavy, or a restart after the child died) forces
    MediaPipe on the CPU without probing the NPU."""

    def __init__(self, model_path, allow_npu=True, cpu_factory=None):
        env = dict(os.environ)
        if not allow_npu:
            env[ENV_BACKEND] = "cpu"
        self.backend = create_pose_backend(model_path=model_path, env=env, cpu_factory=cpu_factory)
        self.timestamp = -1

    name = property(lambda self: self.backend.name)
    device = property(lambda self: self.backend.device)

    def detect(self, frame_bgr):
        self.timestamp = max(self.timestamp + 1, int(time.monotonic() * 1000))
        return self.backend.detect(frame_bgr, self.timestamp)

    def close(self):
        self.backend.close()


def _close_quietly(backend):
    try:
        backend.close()
    except Exception:
        pass


def _driver_hint():
    try:
        return driver_hint()
    except Exception as error:
        return f"kernel {platform.release()} (driver check failed: {type(error).__name__})"


def _warn(log, warnings):
    for warning in warnings:
        if warning:
            log(f"Warning: {warning}.")
