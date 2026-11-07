"""features/pose: backend choice, MediaPipe-graph math, smoothing and wiring. Offline:
no models, no NPU, no camera. Parity with MediaPipe runs only when converted models,
onnxruntime, mediapipe and the sample image are present, and is skipped otherwise."""
import ast
import functools
import inspect
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from features.pose import backend, engines, models, pipeline as P
from features.pose.engines import split_detector, split_landmarks, heatmap_hwc
import pc_hardware

ROOT = Path(__file__).resolve().parents[1]
TAU = 2 * math.pi


def write_models(directory, passed=True, corrupt=None, drop=None, **sections):
    """A model directory with fake .rknn files and a manifest that records their hashes;
    keyword arguments replace manifest sections (e.g. check="passed")."""
    directory = Path(directory)
    manifest = {"format": models.MANIFEST_FORMAT, "rknn": {}, "onnx": {}, "tools": {"rknn-toolkit2": "2.3.2"}}
    for kind in ("rknn", "onnx"):
        for name in models.NAMES:
            path = directory / f"{name}.{kind}"
            path.write_bytes(f"{kind}-{name}".encode())
            manifest[kind][name] = {"file": path.name, "sha256": models.sha256(path)}
    if passed is not None:
        manifest["check"] = {"passed": passed, "rknn_toolkit_lite2": "2.3.2", "sdk": "API: 2.3.2 DRV: 0.9.8"}
    manifest.update(sections)
    if corrupt:
        (directory / corrupt).write_bytes(b"changed after conversion")
    if drop:
        (directory / drop).unlink()
    (directory / models.MANIFEST).write_text(json.dumps(manifest))
    return manifest


class NpuSelectionTests(unittest.TestCase):
    """Every gate between auto and the NPU, each with its own reason."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"BAYMAX_POSE_MODEL_DIR": self.temp.name}
        self.rknnlite = Mock()

    def reason(self, forced=False, **overrides):
        probes = dict(system="linux", machine="aarch64", rk3588=lambda: True, rknnlite=self.rknnlite,
                      lite_version=lambda: "2.3.2")
        probes.update(overrides)
        return backend.npu_unavailable_reason(self.env, forced=forced, **probes)

    def test_all_gates_pass(self):
        write_models(self.temp.name)
        self.assertIsNone(self.reason())

    def test_other_systems_never_look_for_rknnlite(self):
        for system, machine in (("darwin", "arm64"), ("win32", "AMD64"), ("linux", "x86_64")):
            self.assertIn("not Linux aarch64", self.reason(system=system, machine=machine))
        self.rknnlite.assert_not_called()

    def test_other_arm_boards_are_skipped(self):
        write_models(self.temp.name)
        self.assertIn("not an RK3588 board", self.reason(rk3588=lambda: False))
        self.rknnlite.assert_not_called()

    def test_missing_rknnlite(self):
        def missing():
            raise ImportError("No module named 'rknnlite'")
        self.assertIn("rknn-toolkit-lite2 is not usable", self.reason(rknnlite=missing))

    def test_models_not_converted(self):
        self.assertIn("no converted models", self.reason())

    def test_missing_rknn_file(self):
        write_models(self.temp.name, drop="pose_landmarks_detector.rknn")
        self.assertIn("pose_landmarks_detector.rknn is missing", self.reason())

    def test_checksum_mismatch(self):
        write_models(self.temp.name, corrupt="pose_detector.rknn")
        self.assertIn("does not match the SHA-256", self.reason())

    def test_unknown_manifest_format(self):
        (Path(self.temp.name) / models.MANIFEST).write_text(json.dumps({"format": 99}))
        self.assertIn("unknown format", self.reason())

    def test_accuracy_check_must_have_passed(self):
        write_models(self.temp.name, passed=False)
        self.assertIn("accuracy check", self.reason())
        write_models(self.temp.name, passed=None)
        self.assertIn("accuracy check", self.reason())

    def test_forcing_skips_board_and_accuracy_gates_only(self):
        write_models(self.temp.name, passed=False)
        self.assertIsNone(self.reason(forced=True, rk3588=lambda: False))
        self.assertIn("not Linux aarch64", self.reason(forced=True, system="darwin"))
        write_models(self.temp.name, corrupt="pose_detector.rknn")
        self.assertIn("SHA-256", self.reason(forced=True))

    def test_unreadable_model_file_is_a_reason_not_an_error(self):
        write_models(self.temp.name)
        with patch.object(models, "sha256", side_effect=PermissionError(13, "Permission denied")):
            reason = self.reason()
        self.assertIn("cannot read", reason)
        self.assertIn("PermissionError", reason)

    def test_malformed_manifest_sections_are_reasons(self):
        cases = [(dict(check="passed"), "accuracy check"),
                 (dict(check=["passed"]), "accuracy check"),
                 (dict(rknn="pose_detector.rknn"), "no rknn section"),
                 (dict(rknn={"pose_detector": "pose_detector.rknn", "pose_landmarks_detector": "x"}), "no rknn entry"),
                 (dict(rknn={name: {"file": 7, "sha256": "x"} for name in models.NAMES}), "no rknn entry"),
                 (dict(rknn={name: {"file": f"{name}.rknn", "sha256": None} for name in models.NAMES}), "no rknn entry")]
        for sections, expected in cases:
            with self.subTest(sections=sections):
                write_models(self.temp.name, **sections)
                self.assertIn(expected, self.reason())
        self.assertFalse(models.check_passed({"check": "passed"}))
        self.assertFalse(models.check_passed([]))

    def test_rknnlite_version_must_match_the_accuracy_check(self):
        write_models(self.temp.name)
        reason = self.reason(lite_version=lambda: "2.5.0")
        self.assertIn("rknn-toolkit-lite2 2.5.0 but the accuracy check ran with 2.3.2", reason)
        self.assertIsNone(self.reason(lite_version=lambda: None))              # unknown here: not a reason
        self.assertIsNone(self.reason(forced=True, lite_version=lambda: "2.5.0"))


class FakeEngine:
    """Detector finds nothing unless told; landmark model returns a fixed pose."""

    def __init__(self, detection=None, flag=0.9, landmarks=None):
        self.detection = detection
        self.flag = flag
        self.raw = landmarks
        self.detector_calls = 0
        self.landmark_calls = 0
        self.closed = False

    def detector(self, crop):
        assert crop.shape == (224, 224, 3) and crop.dtype == np.uint8
        self.detector_calls += 1
        boxes = np.zeros((2254, 12), np.float32)
        scores = np.full((2254, 1), -10.0, np.float32)
        if self.detection is not None:
            index, raw = self.detection
            boxes[index] = raw
            scores[index] = 5.0
        return boxes, scores

    def landmarks(self, crop):
        assert crop.shape == (256, 256, 3) and crop.dtype == np.uint8
        self.landmark_calls += 1
        raw = self.raw if self.raw is not None else standing_raw()
        return raw, np.array([self.flag], np.float32), np.full((64, 64, 39), -10.0, np.float32)

    def close(self):
        self.closed = True


def standing_raw():
    """39 landmarks in 256-px crop units: an upright figure; aux 33 at the hips, 34 above the head."""
    raw = np.zeros((39, 5), np.float32)
    raw[:, 0], raw[:, 1] = 128, 128
    raw[:, 3], raw[:, 4] = 4.0, 4.0                     # sigmoid -> 0.982
    raw[0, :2] = (128, 60)
    raw[11, :2], raw[12, :2] = (150, 90), (106, 90)
    raw[23, :2], raw[24, :2] = (140, 150), (116, 150)
    raw[33, :2], raw[34, :2] = (128, 150), (128, 50)
    return raw.reshape(-1)


def centred_detection(width=640, height=480):
    """Raw detector output on the anchor nearest the image centre, keypoints 0/1 at the
    hips and above the head, 100 px apart vertically in the 224 input."""
    anchors = P.ssd_anchors()
    index = int(np.argmin(np.hypot(anchors[:, 0] - 0.5, anchors[:, 1] - 0.5)))
    raw = np.zeros(12, np.float32)
    raw[2:4] = 80                                       # w, h in 224 px
    raw[4:6] = (0, 30)                                  # keypoint 0 (x, y) relative to the anchor
    raw[6:8] = (0, -70)                                 # keypoint 1
    return index, raw


class CreateBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = []
        self.cpu = SimpleNamespace(name=backend.CPU_NAME, device="CPU", close=Mock())

    def create(self, env, probe=None, engine_factory=None):
        env = {"BAYMAX_POSE_MODEL_DIR": self.temp.name, **env}
        return backend.create_pose_backend(env=env, log=self.logs.append, cpu_factory=lambda: self.cpu,
                                           probe=probe or (lambda env, forced: None),
                                           engine_factory=engine_factory or (lambda d, l, c: FakeEngine()))

    def test_auto_off_the_rk3588_is_mediapipe_and_says_why(self):
        chosen = self.create({}, probe=lambda env, forced: "not Linux aarch64 (darwin arm64)")
        self.assertIs(chosen, self.cpu)
        self.assertEqual(self.logs, ["Pose backend: CPU (MediaPipe). NPU skipped: not Linux aarch64 (darwin arm64)."])

    def test_cpu_choice_never_probes(self):
        probe = Mock()
        self.assertIs(self.create({"BAYMAX_POSE_BACKEND": "cpu"}, probe=probe), self.cpu)
        probe.assert_not_called()
        self.assertIn("BAYMAX_POSE_BACKEND=cpu", self.logs[0])

    def test_npu_when_every_gate_passes(self):
        write_models(self.temp.name)
        engines = []
        def factory(detector, landmarks, core):
            engines.append((Path(detector).name, Path(landmarks).name, core))
            return FakeEngine()
        chosen = self.create({"BAYMAX_POSE_NPU_CORE": "1"}, engine_factory=factory)
        self.assertEqual((chosen.name, chosen.device), (backend.NPU_NAME, "NPU"))
        self.assertEqual(engines, [("pose_detector.rknn", "pose_landmarks_detector.rknn", "1")])
        self.assertIn("NPU (RK3588) on NPU core 1", self.logs[0])
        self.assertIn("rknn-toolkit2 2.3.2", self.logs[0])
        self.assertIsNone(chosen.detect(np.zeros((480, 640, 3), np.uint8), 1))
        chosen.close()

    def test_default_core_is_zero(self):
        write_models(self.temp.name)
        cores = []
        self.create({}, engine_factory=lambda d, l, c: cores.append(c) or FakeEngine())
        self.assertEqual(cores, ["0"])

    def test_init_runtime_failure_falls_back_with_reason(self):
        write_models(self.temp.name)
        def failing(detector, landmarks, core):
            raise RuntimeError("init_runtime(pose_detector, core 0) returned -1")
        with patch.object(backend, "driver_hint", return_value="kernel 6.12.0; no rknpu driver"):
            self.assertIs(self.create({}, engine_factory=failing), self.cpu)
        self.assertIn("NPU skipped: NPU init failed (RuntimeError: init_runtime(pose_detector, core 0) returned -1); "
                      "kernel 6.12.0; no rknpu driver.", self.logs[0])

    def test_warm_up_failure_closes_the_engine(self):
        write_models(self.temp.name)
        engine = FakeEngine()
        engine.landmarks = Mock(side_effect=RuntimeError("RKNN inference returned nothing"))
        with patch.object(backend, "driver_hint", return_value="kernel x"):
            self.assertIs(self.create({}, engine_factory=lambda d, l, c: engine), self.cpu)
        self.assertTrue(engine.closed)

    def test_forced_npu_passes_forced_to_the_probe(self):
        seen = []
        self.create({"BAYMAX_POSE_BACKEND": "npu"}, probe=lambda env, forced: seen.append(forced) or "x")
        self.assertEqual(seen, [True])

    def test_probe_errors_mean_mediapipe(self):
        def broken(env, forced):
            raise AttributeError("'str' object has no attribute 'get'")
        self.assertIs(self.create({}, probe=broken), self.cpu)
        self.assertEqual(self.logs, ["Pose backend: CPU (MediaPipe). NPU skipped: NPU probe failed "
                                     "(AttributeError: 'str' object has no attribute 'get')."])

    def test_unreadable_rknn_with_the_real_probe_means_mediapipe(self):
        write_models(self.temp.name)
        probe = functools.partial(backend.npu_unavailable_reason, system="linux", machine="aarch64",
                                  rk3588=lambda: True, rknnlite=Mock(), lite_version=lambda: "2.3.2")
        factory = Mock()
        with patch.object(models, "sha256", side_effect=PermissionError(13, "Permission denied")):
            self.assertIs(self.create({}, probe=probe, engine_factory=factory), self.cpu)
        factory.assert_not_called()
        self.assertIn("NPU skipped: cannot read", self.logs[0])
        for sections in (dict(check="passed"), dict(rknn="x")):
            write_models(self.temp.name, **sections)
            self.assertIs(self.create({}, probe=probe, engine_factory=factory), self.cpu)

    def test_malformed_tools_section_still_opens_the_npu(self):
        write_models(self.temp.name, tools="2.3.2")
        chosen = self.create({})
        self.assertEqual(chosen.device, "NPU")
        self.assertIn("rknn-toolkit2 ?", self.logs[0])

    def test_runtime_change_since_the_check_means_mediapipe_unless_forced(self):
        write_models(self.temp.name)                                   # checked on "API: 2.3.2 DRV: 0.9.8"
        def opened(sdk):
            engine = FakeEngine()
            engine.sdk_version = lambda: sdk
            self.logs.clear()
            return engine, (lambda d, l, c: engine)
        for same in ("API: 2.3.2   DRV: 0.9.8", "API: 2.3.2\n DRV: 0.9.8", None, ""):   # other spacing / unknown
            engine, factory = opened(same)
            self.assertEqual(self.create({}, engine_factory=factory).device, "NPU")
            self.assertEqual(len(self.logs), 1)
        engine, factory = opened("API: 2.3.2 DRV: 0.9.6")
        self.assertIs(self.create({}, engine_factory=factory), self.cpu)
        self.assertTrue(engine.closed)
        self.assertEqual(self.logs, ["Pose backend: CPU (MediaPipe). NPU skipped: the NPU runtime/driver "
                                     "(API: 2.3.2 DRV: 0.9.6) differs from the one the accuracy check ran on "
                                     "(API: 2.3.2 DRV: 0.9.8); re-run convert_rknn.py check "
                                     "(or BAYMAX_POSE_BACKEND=npu)."])
        engine, factory = opened("API: 2.3.2 DRV: 0.9.6")
        chosen = self.create({"BAYMAX_POSE_BACKEND": "npu"}, engine_factory=factory)
        self.assertEqual(chosen.device, "NPU")
        self.assertFalse(engine.closed)
        self.assertIn("Warning: the NPU runtime/driver (API: 2.3.2 DRV: 0.9.6) differs", self.logs[1])
        write_models(self.temp.name, check={"passed": True, "rknn_toolkit_lite2": "2.3.2"})   # no sdk recorded
        engine, factory = opened("API: 2.3.2 DRV: 0.9.6")
        self.assertEqual(self.create({}, engine_factory=factory).device, "NPU")

    def test_the_port_uses_the_runtimes_confidences(self):
        write_models(self.temp.name)
        options = SimpleNamespace(min_pose_detection_confidence=0.7, min_pose_presence_confidence=0.6,
                                  min_tracking_confidence=0.8, base_options=SimpleNamespace(model_asset_path=None))
        def chosen(env, options):
            with patch.object(engines, "OnnxEngine", lambda d, l: FakeEngine()):
                pose = backend.create_pose_backend(
                    mediapipe_options=options, env={"BAYMAX_POSE_MODEL_DIR": self.temp.name, **env},
                    log=self.logs.append, cpu_factory=lambda: self.cpu, probe=lambda env, forced: None,
                    engine_factory=lambda d, l, c: FakeEngine())
            pipeline = pose.active.pipeline
            return (pose.device, pipeline.min_detection_confidence, pipeline.min_presence_confidence,
                    pipeline.min_tracking_confidence)
        self.assertEqual(chosen({}, options), ("NPU", 0.7, 0.6, 0.8))
        self.assertEqual(chosen({"BAYMAX_POSE_BACKEND": "onnx"}, options), ("CPU", 0.7, 0.6, 0.8))
        self.assertEqual(chosen({}, None), ("NPU", 0.5, 0.5, 0.5))
        odd = SimpleNamespace(min_pose_detection_confidence=None, min_pose_presence_confidence=float("nan"),
                              min_tracking_confidence=True)
        self.assertEqual(chosen({}, odd), ("NPU", 0.5, 0.5, 0.5))

    def test_other_weights_in_the_runtime_task_are_logged(self):
        import zipfile
        write_models(self.temp.name)
        task = Path(self.temp.name) / "pose_landmarker_full.task"
        with zipfile.ZipFile(task, "w") as bundle:
            for name in models.NAMES:
                bundle.writestr(name + ".tflite", b"newer weights")
        options = SimpleNamespace(base_options=SimpleNamespace(model_asset_path=str(task)))
        backend.create_pose_backend(mediapipe_options=options, env={"BAYMAX_POSE_MODEL_DIR": self.temp.name},
                                    log=self.logs.append, cpu_factory=lambda: self.cpu,
                                    probe=lambda env, forced: None, engine_factory=lambda d, l, c: FakeEngine())
        self.assertIn("Warning: pose_landmarker_full.task has different weights", self.logs[1])
        self.assertFalse(models.task_matches_pinned(task))
        self.assertIsNone(models.task_matches_pinned(Path(self.temp.name) / "missing.task"))
        self.assertIsNone(backend.weights_warning(SimpleNamespace(base_options=SimpleNamespace(model_asset_path=None))))
        # The default lite .task (the CPU fallback) is a different model by choice: no warning.
        lite = Path(self.temp.name) / "pose_landmarker_lite.task"
        lite.write_bytes(task.read_bytes())
        self.assertIsNone(backend.weights_warning(model_path=str(lite)))
        self.assertIsNotNone(backend.weights_warning(model_path=str(task)))

    def test_child_pose_without_the_npu_never_probes(self):
        probe = Mock()
        cpu = SimpleNamespace(name=backend.CPU_NAME, device="CPU", close=Mock(),
                              detect=Mock(side_effect=lambda frame, ms: ms))
        with patch.dict(os.environ, {"BAYMAX_POSE_BACKEND": "npu"}), \
                patch.object(backend, "npu_unavailable_reason", probe), patch("builtins.print"):
            pose = backend.ChildPose(None, allow_npu=False, cpu_factory=lambda: cpu)
        probe.assert_not_called()
        self.assertEqual((pose.name, pose.device), (backend.CPU_NAME, "CPU"))
        stamps = [pose.detect("frame") for _ in range(3)]
        self.assertEqual(stamps, sorted(set(stamps)))           # strictly increasing ms
        pose.close()
        cpu.close.assert_called_once()

    def test_child_pose_factory_pickles_for_the_vision_process(self):
        import pickle
        factory = pickle.loads(pickle.dumps(functools.partial(backend.ChildPose, "x.task", allow_npu=False)))
        self.assertIs(factory.func, backend.ChildPose)
        self.assertEqual((factory.args, factory.keywords), (("x.task",), {"allow_npu": False}))

    def test_onnx_choice_opens_the_port_without_probing(self):
        write_models(self.temp.name)
        probe = Mock()
        opened = []
        def fake_onnx(detector, landmarks):
            opened.append((Path(detector).name, Path(landmarks).name))
            return FakeEngine()
        with patch.object(engines, "OnnxEngine", fake_onnx):
            chosen = self.create({"BAYMAX_POSE_BACKEND": "onnx"}, probe=probe)
        probe.assert_not_called()
        self.assertEqual((chosen.name, chosen.device), (backend.ONNX_NAME, "CPU"))
        self.assertEqual(opened, [("pose_detector.onnx", "pose_landmarks_detector.onnx")])
        self.assertEqual(self.logs, ["Pose backend: CPU (ONNX Runtime) (BAYMAX_POSE_BACKEND=onnx)."])

    def test_onnx_choice_without_models_or_onnxruntime_is_mediapipe(self):
        self.assertIs(self.create({"BAYMAX_POSE_BACKEND": "onnx"}), self.cpu)
        self.assertIn("ONNX Runtime skipped: ModelError: no converted models", self.logs[-1])
        write_models(self.temp.name)
        with patch.dict(sys.modules, {"onnxruntime": None}):
            self.assertIs(self.create({"BAYMAX_POSE_BACKEND": "onnx"}), self.cpu)
        self.assertIn("ONNX Runtime skipped: ModuleNotFoundError", self.logs[-1])

    def test_mediapipe_is_the_cpu_and_unknown_values_never_reach_the_npu(self):
        write_models(self.temp.name)                       # the NPU would pass every gate
        for value in ("mediapipe", " MediaPipe "):
            probe, factory = Mock(), Mock()
            self.logs.clear()
            self.assertIs(self.create({"BAYMAX_POSE_BACKEND": value}, probe=probe, engine_factory=factory), self.cpu)
            probe.assert_not_called()
            factory.assert_not_called()
            self.assertEqual(self.logs, ["Pose backend: CPU (MediaPipe) (BAYMAX_POSE_BACKEND=mediapipe)."])
        for value in ("gpu", "off", "0", "false", "none", "rknn"):
            probe, factory = Mock(), Mock()
            self.logs.clear()
            self.assertIs(self.create({"BAYMAX_POSE_BACKEND": value}, probe=probe, engine_factory=factory), self.cpu)
            probe.assert_not_called()
            factory.assert_not_called()
            self.assertEqual(self.logs[0], f"Warning: unknown BAYMAX_POSE_BACKEND={value!r} (use auto, cpu or "
                                           "mediapipe, npu, onnx); using CPU (MediaPipe), not the NPU.")
            self.assertEqual(len(self.logs), 2)
        for value in ("", "  ", "AUTO"):                    # unset or blank is the default, auto
            self.logs.clear()
            self.assertEqual(self.create({"BAYMAX_POSE_BACKEND": value}).device, "NPU")

    def test_npu_failure_during_use_switches_to_mediapipe(self):
        write_models(self.temp.name)
        engine = FakeEngine()
        cpu = Mock(name="cpu", device="CPU")
        cpu.name = backend.CPU_NAME
        cpu.detect.return_value = "cpu-landmarks"
        chosen = backend.create_pose_backend(env={"BAYMAX_POSE_MODEL_DIR": self.temp.name}, log=self.logs.append,
                                             cpu_factory=lambda: cpu, probe=lambda env, forced: None,
                                             engine_factory=lambda d, l, c: engine)
        engine.detector = Mock(side_effect=RuntimeError("NPU timeout"))
        frame = np.zeros((480, 640, 3), np.uint8)
        self.assertEqual(chosen.detect(frame, 5), "cpu-landmarks")
        self.assertEqual((chosen.name, chosen.device), (backend.CPU_NAME, "CPU"))
        self.assertTrue(engine.closed)
        self.assertIn("NPU (RK3588) failed during inference (RuntimeError: NPU timeout); switching to CPU (MediaPipe)",
                      self.logs[1])
        self.assertTrue(self.logs[2].startswith("Traceback of that failure:\nTraceback (most recent call last):"))
        self.assertIn("RuntimeError: NPU timeout", self.logs[2])
        chosen.detect(frame, 6)
        self.assertEqual(cpu.detect.call_count, 2)
        self.assertEqual(len(self.logs), 3)                      # logged once

    def test_the_switch_tells_the_vision_process_before_it_loads_mediapipe(self):
        # In the pose child that frame imports and loads MediaPipe (seconds on the board):
        # the vision process's watchdog must give it a start's time, not a frame's 5 s.
        write_models(self.temp.name)
        engine, order = FakeEngine(), []

        def cpu_factory():
            order.append("cpu")
            return SimpleNamespace(name=backend.CPU_NAME, device="CPU", detect=lambda frame, ms: None)
        chosen = backend.create_pose_backend(env={"BAYMAX_POSE_MODEL_DIR": self.temp.name}, log=self.logs.append,
                                             cpu_factory=cpu_factory, probe=lambda env, forced: None,
                                             engine_factory=lambda d, l, c: engine)
        frame = np.zeros((480, 640, 3), np.uint8)
        with patch.object(backend, "loading_model", side_effect=lambda: order.append("loading")):
            chosen.detect(frame, 5)                              # the NPU works: nothing to say
            self.assertEqual(order, [])
            engine.detector = Mock(side_effect=RuntimeError("NPU timeout"))
            chosen.detect(frame, 6)
            chosen.detect(frame, 7)
        self.assertEqual(order, ["loading", "cpu"])              # once, before the load

    def test_mediapipe_backend_makes_the_runtime_calls(self):
        landmarker = Mock()
        landmarker.detect_for_video.return_value = SimpleNamespace(pose_landmarks=[["lm"]])
        vision = SimpleNamespace(PoseLandmarker=SimpleNamespace(create_from_options=Mock(return_value=landmarker)))
        mp = SimpleNamespace(Image=Mock(return_value="image"), ImageFormat=SimpleNamespace(SRGB="srgb"))
        modules = {"mediapipe": mp, "mediapipe.tasks": SimpleNamespace(), "mediapipe.tasks.python": SimpleNamespace(vision=vision),
                   "mediapipe.tasks.python.vision": vision}
        with patch.dict(sys.modules, modules):
            cpu = backend.MediaPipeCpuBackend("options")
            frame = np.zeros((2, 2, 3), np.uint8)
            frame[0, 0] = (1, 2, 3)
            self.assertEqual(cpu.detect(frame, 42), ["lm"])
        vision.PoseLandmarker.create_from_options.assert_called_once_with("options")
        rgb = mp.Image.call_args.kwargs["data"]
        self.assertEqual(tuple(rgb[0, 0]), (3, 2, 1))   # BGR camera frame -> SRGB, as before
        landmarker.detect_for_video.assert_called_once_with("image", 42)
        landmarker.detect_for_video.return_value = SimpleNamespace(pose_landmarks=[])
        self.assertIsNone(cpu.detect(frame, 43))


class DefaultOffRk3588Tests(unittest.TestCase):
    def test_this_machine_uses_mediapipe_unless_it_is_an_rk3588(self):
        if pc_hardware.is_rk3588():
            self.skipTest("running on an RK3588; see NpuSelectionTests")
        with tempfile.TemporaryDirectory() as temp:
            cpu = object()
            logs = []
            env = {"BAYMAX_POSE_MODEL_DIR": temp}
            chosen = backend.create_pose_backend(env=env, log=logs.append, cpu_factory=lambda: cpu)
        self.assertIs(chosen, cpu)
        self.assertIn("NPU skipped", logs[0])
        self.assertNotIn("rknnlite", sys.modules)

    def test_device_tree_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compatible"
            path.write_bytes(b"xunlong,orangepi-5-max\0rockchip,rk3588\0")
            self.assertEqual(pc_hardware.device_tree_compatible(path), ("xunlong,orangepi-5-max", "rockchip,rk3588"))
            self.assertTrue(pc_hardware.is_rk3588(pc_hardware.device_tree_compatible(path)))
            self.assertTrue(pc_hardware.is_rk3588(("rockchip,rk3588s",)))
            self.assertFalse(pc_hardware.is_rk3588(("raspberrypi,5-model-b", "brcm,bcm2712")))
            self.assertEqual(pc_hardware.device_tree_compatible(Path(temp) / "missing"), ())
            drivers = Path(temp) / "drivers"
            for name in ("RKNPU", "rockchip-pinctrl"):
                (drivers / name).mkdir(parents=True)
            self.assertEqual(pc_hardware.npu_drivers(str(drivers)), ["RKNPU"])

    def test_rknnlite_is_imported_only_by_the_engine(self):
        offenders = []
        sources = []
        for folder, dirs, files in os.walk(ROOT):
            # Ember's own code only: not .venv*/.models (the converter's venvs), other venvs or caches.
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("venv", "node_modules", "site-packages", "__pycache__")]
            sources += [Path(folder) / f for f in files if f.endswith(".py")]
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(name.split(".")[0] == "rknnlite" for name in names):
                    offenders.append(path.relative_to(ROOT).as_posix())   # "/" on Windows too
        self.assertEqual(sorted(offenders), ["features/pose/engines.py"])
        engine = (ROOT / "features/pose/engines.py").read_text(encoding="utf-8")
        tree = ast.parse(engine)
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any("rknnlite" in ast.dump(n) for n in top_level))   # lazily, inside a function

    def test_importing_the_feature_loads_no_runtime(self):
        code = ("import sys; import features.pose.backend, features.pose.pipeline, features.pose.engines, "
                "features.pose.convert_rknn; "
                "print(sorted(m for m in ('rknnlite', 'rknn', 'onnxruntime', 'mediapipe', 'tensorflow') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class AnchorAndRoiTests(unittest.TestCase):
    def test_anchor_count_and_layout(self):
        anchors = P.ssd_anchors()
        self.assertEqual(anchors.shape, (2254, 2))
        self.assertTrue(np.allclose(anchors[0], (0.5 / 28, 0.5 / 28)))
        self.assertTrue(np.allclose(anchors[1], anchors[0]))           # two anchors per cell at stride 8
        self.assertTrue(np.allclose(anchors[1568], (0.5 / 14, 0.5 / 14)))
        self.assertTrue(np.allclose(anchors[1960:1966], (0.5 / 7, 0.5 / 7)))   # six at the three stride-32 layers
        self.assertTrue(np.allclose(anchors[-1], (6.5 / 7, 6.5 / 7)))

    def test_letterbox_of_the_camera_frame(self):
        roi, padding = P.detector_roi(640, 480)
        self.assertEqual(roi, (320.0, 240.0, 640.0, 640.0, 0.0))
        self.assertEqual(padding, (0.0, 0.125, 0.0, 0.125))
        roi, padding = P.detector_roi(480, 640)
        self.assertEqual(roi[2:4], (640.0, 640.0))
        self.assertEqual(padding, (0.125, 0.0, 0.125, 0.0))
        detection = P.Detection(0.9, (0.25, 0.125, 0.75, 0.875), [(0.5, 0.125), (0.5, 0.875), (0, 0), (0, 0)])
        clean = P.remove_letterbox(detection, (0.0, 0.125, 0.0, 0.125))
        self.assertTrue(np.allclose(clean.box, (0.25, 0.0, 0.75, 1.0)))
        self.assertTrue(np.allclose(clean.keypoints[:2], [(0.5, 0.0), (0.5, 1.0)]))

    def test_upright_person_roi(self):
        # Hips at the centre, keypoint 1 120 px above them on a 640x480 frame.
        rect = P.alignment_rect((0.5, 0.5), (0.5, 0.25), 640, 480)
        self.assertAlmostEqual(rect.rotation, 0.0)
        self.assertAlmostEqual(rect.width, 240 / 640)
        self.assertAlmostEqual(rect.height, 240 / 480)
        roi = P.transform_rect(rect, 640, 480)
        self.assertAlmostEqual(roi.width * 640, 300.0)                # x1.25, square in pixels
        self.assertAlmostEqual(roi.height * 480, 300.0)
        self.assertEqual((roi.x_center, roi.y_center), (0.5, 0.5))

    def test_lying_person_roi_is_rotated(self):
        rect = P.alignment_rect((0.5, 0.5), (0.75, 0.5), 640, 480)     # head to the right
        self.assertAlmostEqual(rect.rotation, math.pi / 2)
        rect = P.alignment_rect((0.5, 0.5), (0.25, 0.5), 640, 480)     # head to the left
        self.assertAlmostEqual(rect.rotation, -math.pi / 2)
        rect = P.alignment_rect((0.5, 0.5), (0.5, 0.75), 640, 480)     # upside down
        self.assertAlmostEqual(abs(rect.rotation), math.pi)
        for angle in (-7.0, -math.pi, 0.0, 3.5, 12.0):
            value = P.normalize_radians(angle)
            self.assertTrue(-math.pi <= value < math.pi)
            self.assertAlmostEqual(math.cos(value), math.cos(angle))

    def test_decode_weighted_nms_and_association(self):
        anchors = P.ssd_anchors()
        boxes = np.zeros((2254, 12), np.float32)
        scores = np.full((2254, 1), -10.0, np.float32)
        a, b, c = 300, 301, 1000                                       # a/b share a cell; c is elsewhere
        boxes[a, 2:4] = boxes[b, 2:4] = boxes[c, 2:4] = 40
        boxes[b, 0] = 2.0                                              # b shifted 2 px right
        boxes[a, 4:6] = (0, 10)
        boxes[b, 4:6] = (2, 10)
        scores[a], scores[b], scores[c] = 2.0, 0.0, 1.0
        scores[5] = -0.1                                               # sigmoid < 0.5: dropped
        detections = P.decode_detections(boxes, scores, anchors, 0.5)
        self.assertEqual(len(detections), 3)
        self.assertAlmostEqual(detections[0].box[2] - detections[0].box[0], 40 / 224, places=6)
        merged = P.weighted_nms(detections)
        self.assertEqual(len(merged), 2)
        wa, wb = 1 / (1 + math.exp(-2.0)), 0.5
        expected_x = anchors[a, 0] + (wb * 2.0 / 224) / (wa + wb)
        self.assertAlmostEqual(merged[0].keypoints[0, 0], expected_x, places=6)
        self.assertAlmostEqual(merged[0].score, wa, places=6)          # the top score is kept
        self.assertAlmostEqual(merged[1].keypoints[0, 0], anchors[c, 0], places=6)
        first = P.Rect(0.5, 0.5, 0.2, 0.2)
        overlapping = P.Rect(0.51, 0.5, 0.2, 0.2)
        apart = P.Rect(0.1, 0.1, 0.1, 0.1)
        self.assertEqual(P.associate([first, apart, overlapping], 0.5), [apart, overlapping])


class LandmarkMathTests(unittest.TestCase):
    def test_decode_uses_crop_pixels_and_sigmoid(self):
        raw = np.zeros((39, 5), np.float32)
        raw[0] = (128, 64, 25.6, 0.0, 2.0)
        out = P.decode_landmarks(raw.reshape(-1))
        self.assertTrue(np.allclose(out[0], (0.5, 0.25, 0.1, 0.5, 1 / (1 + math.exp(-2)))))

    def test_projection_onto_a_rotated_roi(self):
        landmarks = np.array([[0.5, 0.5, 0.1, 1, 1], [1.0, 0.5, 0.0, 1, 1], [0.5, 0.0, 0.0, 1, 1]])
        rect = P.Rect(0.4, 0.6, 0.5, 0.25, 0.0)
        out = P.project(landmarks, rect)
        self.assertTrue(np.allclose(out[0, :3], (0.4, 0.6, 0.05)))      # centre; z scales like x
        self.assertTrue(np.allclose(out[1, :2], (0.65, 0.6)))
        out = P.project(landmarks, P.Rect(0.4, 0.6, 0.5, 0.25, math.pi / 2))
        self.assertTrue(np.allclose(out[1, :2], (0.4, 0.725)))          # crop's right edge points down
        self.assertTrue(np.allclose(out[2, :2], (0.65, 0.6)))           # crop's top points right

    def test_heatmap_refinement(self):
        landmarks = np.zeros((39, 5))
        landmarks[:, :2] = 0.5
        landmarks[1, :2] = (-0.2, 0.5)                                  # outside: untouched
        heatmap = np.full((64, 64, 39), -10.0, np.float32)
        heatmap[30, 34, 0] = 10.0                                       # one confident cell near (32, 32)
        heatmap[:, :, 2] = 0.0                                          # sigmoid 0.5 everywhere: refined to the window centre
        heatmap[:, :, 3] = -1.0                                         # sigmoid 0.27 < 0.5: not refined
        out = P.refine_from_heatmap(landmarks.copy(), heatmap)
        self.assertAlmostEqual(out[0, 0], 34 / 64, places=3)
        self.assertAlmostEqual(out[0, 1], 30 / 64, places=3)
        self.assertTrue(np.allclose(out[1, :2], (-0.2, 0.5)))
        self.assertTrue(np.allclose(out[2, :2], (32 / 64, 32 / 64)))
        self.assertTrue(np.allclose(out[3, :2], (0.5, 0.5)))

    def test_engine_outputs_by_size_and_layout(self):
        boxes, scores = split_detector([np.zeros((1, 2254, 1)), np.ones((1, 2254, 12))])
        self.assertEqual((boxes.shape, scores.shape, boxes[0, 0], scores[0, 0]), ((2254, 12), (2254, 1), 1.0, 0.0))
        nhwc = np.arange(64 * 64 * 39, dtype=np.float32).reshape(1, 64, 64, 39)
        self.assertTrue(np.array_equal(heatmap_hwc(nhwc.transpose(0, 3, 1, 2)), nhwc[0]))
        raw, flag, heatmap = split_landmarks([nhwc, np.full((1, 1), 0.7), np.zeros((1, 195))])
        self.assertEqual((raw.shape, float(flag[0]), heatmap.shape), ((195,), np.float32(0.7), (64, 64, 39)))
        with self.assertRaises(RuntimeError):
            split_landmarks([np.zeros((1, 195))])


class SmoothingTests(unittest.TestCase):
    def test_one_euro_matches_mediapipe_rules(self):
        f = P.OneEuro(min_cutoff=0.05, beta=80.0, derivate_cutoff=1.0)
        self.assertEqual(float(f.apply(1_000_000, np.array([10.0]), 1.0)[0]), 10.0)      # first value passes
        self.assertEqual(float(f.apply(1_000_000, np.array([99.0]), 1.0)[0]), 99.0)      # same timestamp: raw
        still = f.apply(1_066_000_000, np.array([10.0]), 1.0)
        self.assertAlmostEqual(float(still[0]), 10.0)
        jump = float(f.apply(1_132_000_000, np.array([11.0]), 0.01)[0])
        self.assertTrue(10.0 < jump < 11.0)                                              # a small move is smoothed
        fast = P.OneEuro(0.05, 80.0, 1.0)
        fast.apply(1_000_000, np.array([10.0]), 1.0)
        self.assertGreater(float(fast.apply(67_000_000, np.array([11.0]), 1.0)[0]), jump)  # a big scaled move follows faster

    def test_visibility_survives_lost_tracking_but_positions_restart(self):
        smoother = P.LandmarkSmoother()
        rect = P.Rect(0.5, 0.5, 0.5, 0.5)
        pose = np.zeros((33, 5))
        pose[:, :2] = 0.5
        pose[:, 3] = 1.0
        smoother.apply(pose, 1000, 640, 480, rect)
        smoother.reset_position()                       # a frame without a pose
        moved = pose.copy()
        moved[:, 0] = 0.6
        moved[:, 3] = 0.0
        out = smoother.apply(moved, 200_000, 640, 480, rect)
        self.assertTrue(np.allclose(out[:, 0], 0.6))    # positions start over
        self.assertTrue(np.allclose(out[:, 3], 0.9))    # visibility: 0.1 * 0 + 0.9 * 1

    def test_tiny_roi_is_not_filtered(self):
        smoother = P.LandmarkSmoother()
        pose = np.full((33, 5), 0.5)
        out = smoother.apply(pose, 1000, 640, 480, P.Rect(0.5, 0.5, 0.0, 0.0))
        self.assertTrue(np.allclose(out, pose))


class PipelineFlowTests(unittest.TestCase):
    frame = np.full((480, 640, 3), 90, np.uint8)

    def test_detect_then_track_then_lose(self):
        engine = FakeEngine(detection=centred_detection())
        pose = P.PosePipeline(engine)
        first = pose.detect(self.frame, 10)
        self.assertEqual(len(first), 33)
        self.assertTrue(pose.detector_ran)
        landmark = first[0]
        for attribute in ("x", "y", "z", "visibility", "presence"):
            self.assertIsInstance(getattr(landmark, attribute), float)
        self.assertAlmostEqual(landmark.visibility, 1 / (1 + math.exp(-4.0)), places=5)
        # Upright: the nose is above the hips, shoulders above hips.
        self.assertLess(first[0].y, first[23].y)
        self.assertLess(first[11].y, first[23].y)
        self.assertEqual(engine.detector_calls, 1)
        second = pose.detect(self.frame, 76)
        self.assertFalse(pose.detector_ran)              # tracked from the auxiliary landmarks
        self.assertEqual(engine.detector_calls, 1)
        self.assertAlmostEqual(second[11].x, first[11].x, delta=0.01)   # new ROI, same person
        engine.flag = 0.4                                # pose flag below 0.5: lost
        self.assertIsNone(pose.detect(self.frame, 142))
        engine.flag = 0.9
        pose.detect(self.frame, 208)
        self.assertEqual(engine.detector_calls, 2)       # re-detected on the next frame

    def test_nothing_found(self):
        engine = FakeEngine()
        pose = P.PosePipeline(engine)
        self.assertIsNone(pose.detect(self.frame, 1))
        self.assertIsNone(pose.detect(self.frame, 2))
        self.assertEqual((engine.detector_calls, engine.landmark_calls), (2, 0))

    def test_timestamps_must_increase(self):
        pose = P.PosePipeline(FakeEngine())
        pose.detect(self.frame, 5)
        with self.assertRaises(ValueError):
            pose.detect(self.frame, 5)

    def test_tracking_roi_follows_the_auxiliary_landmarks(self):
        engine = FakeEngine(detection=centred_detection())
        pose = P.PosePipeline(engine)
        pose.detect(self.frame, 1)
        upright = pose.tracking_rect
        self.assertIsNotNone(upright)
        self.assertAlmostEqual(upright.rotation, 0.0, places=5)
        lying = standing_raw().reshape(39, 5).copy()
        lying[34, :2] = (228, 150)                        # keypoint above the head now to the right
        engine.raw = lying.reshape(-1)
        pose.detect(self.frame, 70)
        self.assertAlmostEqual(abs(pose.tracking_rect.rotation), math.pi / 2, delta=0.3)


class RuntimeWiringTests(unittest.TestCase):
    def test_camera_thread_runs_pose_in_the_vision_process(self):
        source = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")
        start = source.index("def _fall_detection_thread")
        loop = source[start:source.index("\n# ───", start)]
        self.assertIn("landmarks = landmarker.detect(frame)", loop)
        self.assertIn("fall_worker.share(landmarks", loop)
        self.assertIn("device=landmarker.device", loop)
        self.assertIn("shared_pose=POSE_TRACKING_ENABLED", loop)
        self.assertIn("_open_pose(force_cpu=True)", loop)       # a dead pose child restarts on the CPU
        self.assertIn("Remote(partial(ChildPose, _POSE_MODEL_PATH", source)
        self.assertIn('allow_npu=not force_cpu and _POSE_MODEL != "heavy"', source)
        # Pose never runs in Ember's own process (it held the GIL there): no MediaPipe
        # import and no in-process backend.
        self.assertNotIn("import mediapipe", source)
        self.assertNotIn("mp_vision", source)
        self.assertNotIn("create_pose_backend(", source)

    @staticmethod
    def fake_mediapipe():
        """mediapipe.tasks stand-ins that record the options they are given."""
        vision = SimpleNamespace(PoseLandmarkerOptions=lambda **options: SimpleNamespace(**options),
                                 RunningMode=SimpleNamespace(IMAGE="IMAGE", VIDEO="VIDEO", LIVE_STREAM="LIVE_STREAM"),
                                 PoseLandmarker=SimpleNamespace(create_from_options=Mock()))
        tasks_python = SimpleNamespace(BaseOptions=lambda **options: SimpleNamespace(**options), vision=vision)
        modules = {"mediapipe": SimpleNamespace(), "mediapipe.tasks": SimpleNamespace(python=tasks_python),
                   "mediapipe.tasks.python": tasks_python, "mediapipe.tasks.python.vision": vision}
        return vision, modules

    def test_the_camera_mediapipe_settings(self):
        # These were asserted on the camera loop's own PoseLandmarker before pose moved into
        # the vision process; default_mediapipe_options is now their only source.
        vision, modules = self.fake_mediapipe()
        with patch.dict(sys.modules, modules):
            options = backend.default_mediapipe_options(Path("x.task"))
        settings = vars(options)
        self.assertEqual(vars(settings.pop("base_options")), {"model_asset_path": "x.task"})
        self.assertEqual(settings, {"running_mode": "VIDEO",          # detect_for_video needs VIDEO
                                    "num_poses": 1, "min_pose_detection_confidence": 0.5,
                                    "min_pose_presence_confidence": 0.5, "min_tracking_confidence": 0.5})
        # The NPU/ONNX port gets no options from ChildPose: its defaults must match these.
        defaults = inspect.signature(P.PosePipeline).parameters
        confidences = backend.pipeline_confidences(options)
        self.assertEqual(len(confidences), 3)
        for keyword, value in confidences.items():
            self.assertEqual(defaults[keyword].default, value, keyword)

    def test_the_pose_process_gives_mediapipe_the_cameras_task(self):
        vision, modules = self.fake_mediapipe()
        with patch.dict(sys.modules, modules), patch("builtins.print"):
            pose = backend.ChildPose("pose_landmarker_lite.task", allow_npu=False)   # no cpu_factory: as Ember
        self.assertEqual(pose.device, "CPU")
        options = vision.PoseLandmarker.create_from_options.call_args.args[0]
        self.assertEqual(options.base_options.model_asset_path, "pose_landmarker_lite.task")
        self.assertEqual(options.running_mode, "VIDEO")
        self.assertEqual(options.num_poses, 1)

    def test_converter_without_its_environments_explains_the_setup(self):
        from features.pose import convert_rknn
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"BAYMAX_POSE_MODEL_DIR": temp}), \
                patch("builtins.print") as printed:
            self.assertEqual(convert_rknn.main(["all"]), 2)
        self.assertIn("setup_converter.sh", printed.call_args.args[0])

    def test_converter_skips_a_broken_runtime_task_and_downloads_the_pinned_one(self):
        from features.pose import convert_rknn
        import zipfile
        real_read = convert_rknn.read_task
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with zipfile.ZipFile(root / "pose_landmarker_full.task", "w") as bundle:
                bundle.writestr("pose_detector_v2.tflite", b"renamed members")          # KeyError
            (root / ".models").mkdir()
            (root / ".models" / "pose_landmarker_full.task").write_bytes(b"PK\x03\x04 cut short")   # BadZipFile
            pinned = root / "build" / "pose_landmarker_full.task"
            read = lambda path: {"pinned": b""} if Path(path) == pinned else real_read(path)
            with patch.object(convert_rknn, "ROOT", root), patch.object(convert_rknn, "read_task", read), \
                    patch.object(convert_rknn, "download", return_value=pinned) as download, \
                    patch("builtins.print") as printed:
                self.assertEqual(convert_rknn.find_task(None, root), (pinned, {"pinned": b""}))
                download.assert_called_once()
                logged = " ".join(c.args[0] for c in printed.call_args_list)
                self.assertIn("pose_landmarker_full.task: KeyError", logged)
                self.assertIn("pose_landmarker_full.task: BadZipFile", logged)
                with self.assertRaises(KeyError):                                  # given with --task: an error
                    convert_rknn.find_task(str(root / "pose_landmarker_full.task"), root)

    def test_converter_rejects_a_different_task(self):
        from features.pose import convert_rknn
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "other.task"
            with zipfile.ZipFile(task, "w") as bundle:
                for name in models.NAMES:
                    bundle.writestr(name + ".tflite", b"not the pinned model")
            with self.assertRaises(RuntimeError):
                convert_rknn.read_task(task)


class ToolSettingsTests(unittest.TestCase):
    def test_validate_judges_falls_with_the_runtimes_settings(self):
        from features.pose import validate
        source = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and len(node.targets) == 1
                     and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("FALL_")
                     and isinstance(node.value, ast.Constant)}
        expected = dict(angle_threshold=constants["FALL_ANGLE_THRESHOLD"],
                        ang_vel_threshold=constants["FALL_ANG_VEL_THRESHOLD"],
                        hip_vel_threshold=constants["FALL_HIP_VEL_THRESHOLD"],
                        history_window=constants["FALL_HISTORY_FRAMES"],
                        confirmation_frames=constants["FALL_CONFIRMATION_FRAMES"],
                        cooldown_seconds=constants["FALL_COOLDOWN_SECONDS"],
                        stay_down_s=constants["FALL_STAY_DOWN_SECONDS"])
        self.assertEqual(validate.FALL_SETTINGS, expected)
        self.assertEqual(constants["FALL_DETECTION_FPS"], 10)
        # validate.py's --fps default is the camera thread's rate (stop main() right after parsing).
        import argparse
        parsed = {}
        real = argparse.ArgumentParser.parse_args
        def stop_after_parsing(parser, *args, **kwargs):
            parsed.update(vars(real(parser, *args, **kwargs)))
            raise SystemExit(0)
        with patch.object(argparse.ArgumentParser, "parse_args", stop_after_parsing), self.assertRaises(SystemExit):
            validate.main(["--task", "t", "--onnx-dir", "d"])
        self.assertEqual(parsed["fps"], constants["FALL_DETECTION_FPS"])


class BenchmarkTests(unittest.TestCase):
    def test_paced_runs_sleep_to_each_frame_slot_like_the_camera_thread(self):
        from features.pose import benchmark
        now = [100.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(round(seconds, 4))
            now[0] += seconds
        def detect(frame, timestamp):
            now[0] += 0.01                                   # 10 ms of pose
            return None
        backend = SimpleNamespace(detect=detect)
        out, ms, cpu = benchmark.run(backend, [None] * 4, 1, fps=15, sleep=sleep, clock=lambda: now[0])
        self.assertEqual((len(out), len(ms), len(cpu)), (3, 3, 3))
        self.assertEqual(sleeps, [round(1 / 15 - 0.01, 4)] * 3)
        sleeps.clear()
        benchmark.run(backend, [None] * 4, 1, sleep=sleep, clock=lambda: now[0])   # default: back to back
        self.assertEqual(sleeps, [])
        with patch("builtins.print") as printed:
            benchmark.summary("NPU", [16.0, 17.0], 2, cpu=[8.0, 8.0], fps=15)
            benchmark.summary("NPU", [11.0], 1, cpu=[6.0])
        self.assertIn("process CPU   8.0 ms/frame = 12.0% of a core at 15 fps", printed.call_args_list[0].args[0])
        self.assertIn("process CPU   6.0 ms/frame (back to back)", printed.call_args_list[1].args[0])

    def test_pins_to_the_fast_cores_before_numpy_loads_like_the_runtime(self):
        # numpy sizes its OpenBLAS thread pool when it loads: on the fast cores, as in Ember.
        from features.pose import benchmark
        tree = ast.parse(Path(benchmark.__file__).read_text(encoding="utf-8"))
        def numpy_imports(nodes):
            return [n.lineno for n in nodes if isinstance(n, ast.Import) and any(a.name == "numpy" for a in n.names)
                    or isinstance(n, ast.ImportFrom) and (n.module or "").startswith("numpy")]
        self.assertEqual(numpy_imports(tree.body), [])
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        pins = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "use_performance_cpus"]
        loads = numpy_imports(ast.walk(main))
        self.assertEqual(len(pins), 1)
        self.assertTrue(loads)
        self.assertLess(pins[0], min(loads))

    def test_empty_scene_frames(self):
        from features.pose import benchmark
        frames = benchmark.frames_from(SimpleNamespace(empty=True, video=None, frames=5), Path("unused"))
        self.assertEqual(len(frames), 5)
        self.assertTrue(all(f.shape == (480, 640, 3) and f.dtype == np.uint8 for f in frames))
        self.assertLessEqual(int(np.abs(frames[0].astype(int) - 90).max()), 3)


class FakeRKNNLite:
    """Stand-in for rknnlite.api.RKNNLite (2.3.2/2.5.0 API) that records every call."""
    NPU_CORE_AUTO, NPU_CORE_0, NPU_CORE_1, NPU_CORE_2 = 0, 1, 2, 4
    NPU_CORE_0_1, NPU_CORE_0_1_2 = 3, 7
    instances = []
    load_ret = {}             # model file name -> load_rknn return value
    init_ret = 0
    outputs = {}              # model file name -> list returned by inference (or None)

    def __init__(self, verbose=False):
        self.calls = []
        self.model = None
        self.released = False
        FakeRKNNLite.instances.append(self)

    def load_rknn(self, path):
        self.model = Path(path).name
        self.calls.append(("load_rknn", path))
        return FakeRKNNLite.load_ret.get(self.model, 0)

    def init_runtime(self, core_mask=None):
        self.calls.append(("init_runtime", core_mask))
        return FakeRKNNLite.init_ret

    def inference(self, inputs=None, data_format=None):
        self.calls.append(("inference", inputs, data_format))
        return FakeRKNNLite.outputs.get(self.model)

    def get_sdk_version(self):
        return "RKNN VERSION:\n  API: 2.3.2 (abc)\n  DRV: rknnrt: 2.3.2"

    def release(self):
        self.released = True


class RknnEngineTests(unittest.TestCase):
    """RknnEngine against a stub rknnlite (the real one only exists on the board)."""

    def setUp(self):
        FakeRKNNLite.instances = []
        FakeRKNNLite.load_ret, FakeRKNNLite.init_ret = {}, 0
        FakeRKNNLite.outputs = {
            "det.rknn": [np.zeros((1, 2254, 1), np.float32), np.ones((1, 2254, 12), np.float32)],
            "lmk.rknn": [np.zeros((1, 39, 64, 64), np.float32), np.full((1, 1), 0.8, np.float32),
                         np.arange(195, dtype=np.float32).reshape(1, 195)]}
        api = SimpleNamespace(RKNNLite=FakeRKNNLite)
        stub = patch.dict(sys.modules, {"rknnlite": SimpleNamespace(api=api), "rknnlite.api": api})
        stub.start()
        self.addCleanup(stub.stop)

    def engine(self, core="0"):
        return engines.RknnEngine("/models/det.rknn", "/models/lmk.rknn", core)

    def test_core_names_map_to_rknnlite_masks(self):
        expected = {"auto": 0, "0": 1, "1": 2, "2": 4, "0_1": 3, "0_1_2": 7, "all": 7}
        self.assertEqual(set(expected), set(engines.NPU_CORES))
        for name, mask in expected.items():
            FakeRKNNLite.instances = []
            engine = self.engine(name)
            self.assertEqual(engine.core_mask, mask)
            self.assertEqual([i.calls[:2] for i in FakeRKNNLite.instances],
                             [[("load_rknn", "/models/det.rknn"), ("init_runtime", mask)],
                              [("load_rknn", "/models/lmk.rknn"), ("init_runtime", mask)]])
            engine.close()
            self.assertTrue(all(i.released for i in FakeRKNNLite.instances))
        FakeRKNNLite.instances = []
        with self.assertRaises(ValueError):
            self.engine("3")
        self.assertEqual(FakeRKNNLite.instances, [])

    def test_load_or_init_failure_raises_and_releases_every_runtime(self):
        FakeRKNNLite.load_ret = {"lmk.rknn": -1}
        with self.assertRaisesRegex(RuntimeError, r"load_rknn\(/models/lmk.rknn\) returned -1"):
            self.engine()
        self.assertEqual(len(FakeRKNNLite.instances), 2)
        self.assertTrue(all(i.released for i in FakeRKNNLite.instances))
        FakeRKNNLite.instances, FakeRKNNLite.load_ret, FakeRKNNLite.init_ret = [], {}, -1
        with self.assertRaisesRegex(RuntimeError, r"init_runtime\(pose_detector, core 0\) returned -1"):
            self.engine()
        self.assertEqual(len(FakeRKNNLite.instances), 1)
        self.assertTrue(FakeRKNNLite.instances[0].released)

    def test_inputs_are_uint8_nhwc_batches_and_outputs_are_found_by_size(self):
        engine = self.engine()
        bgr = np.random.default_rng(1).integers(0, 256, (224, 224, 3), dtype=np.uint8)
        boxes, scores = engine.detector(bgr[:, :, ::-1])              # a non-contiguous view, as cvtColor might not give
        call, (inputs,), data_format = FakeRKNNLite.instances[0].calls[-1]
        self.assertEqual(call, "inference")
        self.assertEqual(data_format, "nhwc")
        self.assertEqual((inputs.shape, inputs.dtype), ((1, 224, 224, 3), np.uint8))
        self.assertTrue(inputs.flags["C_CONTIGUOUS"])
        self.assertTrue(np.array_equal(inputs[0], bgr[:, :, ::-1]))
        self.assertEqual((boxes.shape, scores.shape, float(boxes[0, 0]), float(scores[0, 0])), ((2254, 12), (2254, 1), 1.0, 0.0))
        raw, flag, heatmap = engine.landmarks(np.zeros((256, 256, 3), np.uint8))
        self.assertEqual(FakeRKNNLite.instances[1].calls[-1][1][0].shape, (1, 256, 256, 3))
        self.assertEqual((raw.shape, float(raw[194]), float(flag[0]), heatmap.shape),
                         ((195,), 194.0, np.float32(0.8), (64, 64, 39)))
        self.assertEqual(engine.sdk_version(), "RKNN VERSION: API: 2.3.2 (abc) DRV: rknnrt: 2.3.2")

    def test_missing_or_broken_outputs_raise(self):
        engine = self.engine()
        FakeRKNNLite.outputs["det.rknn"] = None
        with self.assertRaisesRegex(RuntimeError, "returned nothing for pose_detector"):
            engine.detector(np.zeros((224, 224, 3), np.uint8))
        FakeRKNNLite.outputs["lmk.rknn"] = [np.zeros((1, 195), np.float32)]
        with self.assertRaisesRegex(RuntimeError, "missing sizes"):
            engine.landmarks(np.zeros((256, 256, 3), np.uint8))
        FakeRKNNLite.outputs["lmk.rknn"] = [np.full((1, 195), np.nan, np.float32), np.ones((1, 1), np.float32),
                                            np.zeros((1, 64, 64, 39), np.float32)]
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            engine.landmarks(np.zeros((256, 256, 3), np.uint8))
        FakeRKNNLite.instances[0].get_sdk_version = Mock(side_effect=RuntimeError("no"))
        self.assertIsNone(engine.sdk_version())

    def test_nan_from_the_npu_switches_the_backend_to_mediapipe(self):
        FakeRKNNLite.outputs = {"pose_detector.rknn": [np.full((1, 2254, 1), np.nan, np.float32),
                                                       np.ones((1, 2254, 12), np.float32)],
                                "pose_landmarks_detector.rknn": FakeRKNNLite.outputs["lmk.rknn"]}
        with tempfile.TemporaryDirectory() as temp:
            write_models(temp)
            logs = []
            cpu = Mock(device="CPU")
            cpu.name = backend.CPU_NAME
            chosen = backend.create_pose_backend(env={"BAYMAX_POSE_MODEL_DIR": temp}, log=logs.append,
                                                 cpu_factory=lambda: cpu, probe=lambda env, forced: None,
                                                 engine_factory=lambda d, l, c: engines.RknnEngine(d, l, c))
        self.assertIs(chosen, cpu)                                    # the warm-up already failed
        self.assertIn("NPU init failed (RuntimeError: pose detector returned non-finite values)", logs[0])
        self.assertTrue(all(i.released for i in FakeRKNNLite.instances))


class ConverterCheckTests(unittest.TestCase):
    """convert_rknn.py check with fake engines: what it compares and when it may pass."""

    def setUp(self):
        from features.pose import convert_rknn
        self.convert = convert_rknn
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        write_models(self.temp.name, passed=True)
        import cv2
        self.image = Path(self.temp.name) / "still.png"
        cv2.imwrite(str(self.image), np.full((667, 1000, 3), 120, np.uint8))

    def run_check(self, npu_factory, cpu_factory=None):
        cpu_factory = cpu_factory or (lambda d, l: FakeEngine(detection=centred_detection()))
        with patch.dict(os.environ, {"BAYMAX_POSE_MODEL_DIR": self.temp.name}), \
                patch.object(self.convert, "sample_image", return_value=self.image), \
                patch.object(engines, "OnnxEngine", cpu_factory), patch.object(engines, "RknnEngine", npu_factory), \
                patch("builtins.print"), patch("traceback.print_exc"):
            code = self.convert.main(["check"])
        return code, models.load_manifest(self.temp.name)["check"]

    def test_tracked_sequence(self):
        frames = self.convert.tracked_sequence(np.full((667, 1000, 3), 200, np.uint8))
        self.assertEqual(len(frames), self.convert.SEQUENCE_FRAMES)
        self.assertTrue(all(f.shape == (480, 640, 3) and f.dtype == np.uint8 for f in frames))
        blank = [i for i, f in enumerate(frames) if (f == 40).all()]
        self.assertEqual(blank, [24, 25, 26])                          # the person leaves for 3 frames
        def extent(frame):                                             # bounding box of the (bright) picture
            ys, xs = np.nonzero((frame == 200).all(axis=2))
            return xs.max() - xs.min(), ys.max() - ys.min()
        width, height = extent(frames[0])
        self.assertGreater(width, height)                              # upright: the 3:2 picture lies wide
        width, height = extent(frames[-1])
        self.assertLess(width, height)                                 # fallen: turned by 90 degrees

    def test_matching_engines_pass_on_tracked_frames(self):
        code, check = self.run_check(lambda d, l, c: FakeEngine(detection=centred_detection()))
        self.assertEqual(code, 0)
        self.assertTrue(check["passed"])
        self.assertEqual(check["failures"], [])
        self.assertEqual(check["frames"], {"still": 3, "tracked": 72, "video": 0})
        self.assertGreaterEqual(check["pipeline"]["tracked_frames"], self.convert.CHECK_MIN_TRACKED_FRAMES)
        self.assertEqual(check["pipeline"]["landmarks_compared"], 33 * check["pipeline"]["pose_frames"])
        self.assertEqual(check["raw"]["crops"], 76)                   # 75 frames + one of random noise
        self.assertEqual(check["limits"]["pose_flag_max"], self.convert.CHECK_POSE_FLAG_MAX)
        self.assertEqual((check["limits"]["raw_visibility_max"], check["limits"]["visibility_agreement"]), (0.05, 0.98))
        self.assertEqual((check["raw"]["visibility_max"], check["pipeline"]["visibility_agreement"]), (0.0, 1.0))
        self.assertEqual(check["pipeline"]["visibility_agreement_key"], {str(i): 1.0 for i in (0, 2, 5, 11, 12, 23, 24)})

    def test_wrong_visibility_on_the_npu_fails(self):
        hidden = standing_raw().reshape(39, 5).copy()
        hidden[[11, 12, 23, 24], 3] = -4.0                            # shoulders/hips: visibility 0.98 -> 0.02
        code, check = self.run_check(lambda d, l, c: FakeEngine(detection=centred_detection(),
                                                                landmarks=hidden.reshape(-1)))
        self.assertEqual((code, check["passed"]), (1, False))
        self.assertIn("raw visibility (0-32) 0.96403 (limit <= 0.05)", check["failures"])
        self.assertTrue(any(f.startswith("visibility agreement at 0.5 0.87") for f in check["failures"]))
        self.assertEqual(check["pipeline"]["visibility_agreement_key"]["11"], 0.0)
        self.assertEqual(check["pipeline"]["visibility_agreement_key"]["0"], 1.0)
        self.assertEqual(check["pipeline"]["landmark_error_max"], 0.0)              # x/y alone would pass
        slightly = standing_raw().reshape(39, 5).copy()
        slightly[:33, 3] = 3.0                                        # visibility 0.982 -> 0.953: still visible
        code, check = self.run_check(lambda d, l, c: FakeEngine(detection=centred_detection(),
                                                                landmarks=slightly.reshape(-1)))
        self.assertEqual((code, check["pipeline"]["visibility_agreement"]), (0, 1.0))
        self.assertAlmostEqual(check["raw"]["visibility_max"], 0.0294, places=3)

    def test_tracking_landmarks_33_34_are_compared(self):
        shifted = standing_raw().reshape(39, 5).copy()
        shifted[34, 0] += 5.0                                         # only the tracking keypoint differs
        code, check = self.run_check(lambda d, l, c: FakeEngine(detection=centred_detection(),
                                                                landmarks=shifted.reshape(-1)))
        self.assertEqual(code, 1)
        self.assertFalse(check["passed"])
        self.assertAlmostEqual(check["raw"]["aux_landmark_xy_max"], 5.0, places=4)
        self.assertIn("raw landmark x/y (0-34) 5.0 (limit <= 3.0)", check["failures"])

    def test_pose_flag_counts(self):
        code, check = self.run_check(lambda d, l, c: FakeEngine(detection=centred_detection(), flag=0.8))
        self.assertEqual(code, 1)
        self.assertTrue(any(f.startswith("raw pose flag 0.1") for f in check["failures"]))

    def test_nothing_compared_cannot_pass(self):
        nobody = lambda *args: FakeEngine()                           # neither engine ever finds a pose
        code, check = self.run_check(nobody, cpu_factory=nobody)
        self.assertEqual(code, 1)
        self.assertEqual(check["pipeline"]["detection_agreement"], 1.0)
        self.assertIsNone(check["pipeline"]["landmark_error_p95"])
        self.assertIn("frames with a pose in both 0 (limit >= 50)", check["failures"])
        self.assertIn("crops compared 0 (limit >= 10)", check["failures"])

    def test_engine_errors_are_a_recorded_failure(self):
        class Broken(FakeEngine):
            def landmarks(self, crop):
                return split_landmarks([np.full(195, np.nan), np.ones(1), np.zeros((64, 64, 39))])
        code, check = self.run_check(lambda d, l, c: Broken(detection=centred_detection()))
        self.assertEqual(code, 1)
        self.assertFalse(check["passed"])
        self.assertIn("error during the check: RuntimeError: pose landmark model returned non-finite values",
                      check["failures"])
        def no_runtime(d, l, c):
            raise RuntimeError("init_runtime(pose_detector, core 0) returned -1")
        code, check = self.run_check(no_runtime)
        self.assertEqual((code, check["passed"]), (1, False))
        self.assertIn("init_runtime", check["failures"][0])


class ParityWithMediaPipeTests(unittest.TestCase):
    """Needs converted ONNX models, onnxruntime, mediapipe, the .task and the converter's
    cached sample image; skipped when any is missing (e.g. in CI or on a fresh checkout)."""

    def test_port_matches_mediapipe_on_the_sample_image(self):
        directory = models.model_dir()
        image_path = directory / "build" / "pose.jpg"
        tasks = [ROOT / "pose_landmarker_full.task", ROOT / ".models" / "pose_landmarker_full.task",
                 directory / "build" / "pose_landmarker_full.task"]
        task = next((t for t in tasks if t.is_file()), tasks[0])
        try:
            import onnxruntime  # noqa: F401
            import mediapipe  # noqa: F401
            import cv2
            paths = models.verified_files(directory, models.load_manifest(directory), "onnx")
        except (ImportError, models.ModelError) as error:
            self.skipTest(f"parity needs converted models and runtimes: {error}")
        if not image_path.is_file() or not task.is_file():
            self.skipTest("parity needs the converter's sample image and the runtime's .task")
        from features.pose.engines import OnnxEngine
        image = cv2.imread(str(image_path))
        cpu = backend.MediaPipeCpuBackend(backend.default_mediapipe_options(task))
        port = backend.PipelineBackend(OnnxEngine(paths["pose_detector"], paths["pose_landmarks_detector"]), "port", "CPU")
        try:
            for timestamp in (1, 67, 133):
                a, b = cpu.detect(image, timestamp), port.detect(image, timestamp)
                self.assertIsNotNone(a)
                self.assertIsNotNone(b)
                error = [math.hypot(p.x - q.x, p.y - q.y) for p, q in zip(a, b)]
                self.assertLess(float(np.median(error)), 0.003)
                self.assertLess(max(error), 0.02)
                self.assertEqual([p.visibility >= 0.5 for p in a], [q.visibility >= 0.5 for q in b])
        finally:
            cpu.close()
            port.close()


if __name__ == "__main__":
    unittest.main()
