"""Camera models run in a child process: frames in, plain landmarks out, errors survive."""
from functools import partial
import io
import os
from pathlib import Path
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.vision_process import Point, Remote
from features.pose.backend import ChildPose

FAKE = textwrap.dedent('''
    from types import SimpleNamespace

    class Model:
        def detect(self, frame):
            print("library chatter on stdout")  # Must not corrupt the reply pipe.
            if frame == "boom":
                raise ValueError("bad frame")
            if frame is None:
                return None
            return [SimpleNamespace(x=frame, y=0.5, z=0.0, visibility=None, presence=0.9)]

    class NativeChatter:
        """Writes to file descriptor 1 as a C library does (librknnrt logs while it loads)."""
        def __init__(self):
            import os
            os.write(1, b"I RKNN: init done\\n")

        def detect(self, frame):
            import os
            os.write(1, b"W RKNN: query\\n")
            return None

    class Broken:
        def __init__(self):
            raise ImportError("no mediapipe")

    class Switching:
        """Starts on the NPU; a frame can make it fail over to the CPU, as FallbackBackend does."""
        device = "NPU"

        def detect(self, frame):
            if frame == "switch":
                self.device = "CPU"
            return None

    class FakeCpu:
        """create_pose_backend's cpu_factory: echoes each timestamp in x."""
        name, device = "CPU (fake)", "CPU"

        def detect(self, frame, timestamp_ms):
            return [SimpleNamespace(x=float(timestamp_ms), y=0.0, z=0.0, visibility=1.0, presence=1.0)] * 33

        def close(self):
            pass

    class Placed:
        """Its device: the roles the child asked pc_hardware.place_vision_child for before
        building it (recorded by spy_on_placement instead of placing anything)."""
        asked = []

        def __init__(self):
            self.device = list(Placed.asked)

        def detect(self, frame):
            return None

    class PlacedFace(Placed):
        vision_role = "face"

    def spy_on_placement(factory):
        import pc_hardware
        pc_hardware.place_vision_child = Placed.asked.append
        return factory

    class Spied:
        """Unpickles as spy_on_placement(factory): the child installs the spy as it reads the factory."""
        def __init__(self, factory):
            self.factory = factory

        def __reduce__(self):
            return spy_on_placement, (self.factory,)
''')


class VisionProcessTests(unittest.TestCase):
    def setUp(self):
        # Children inherit this environment: never let them place themselves here.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("BAYMAX_FAST_CPUS", "BAYMAX_SLOW_CPUS", "BAYMAX_VISION_CPUS"):
            os.environ.pop(name, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "fake_vision_model.py").write_text(FAKE)
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = directory.name + (os.pathsep + old if old else "")
        self.addCleanup(lambda: os.environ.__setitem__("PYTHONPATH", old) if old else os.environ.pop("PYTHONPATH"))
        sys.path.insert(0, directory.name)
        self.addCleanup(sys.path.remove, directory.name)
        import fake_vision_model
        self.fake = fake_vision_model

    def test_landmarks_come_back_as_plain_points_and_errors_do_not_stop_the_process(self):
        with Remote(self.fake.Model) as model:
            self.assertEqual(model.detect(0.25), [Point(0.25, 0.5, 0.0, 0.0, 0.9)])
            self.assertIsNone(model.detect(None))
            with self.assertRaisesRegex(RuntimeError, "bad frame"):
                model.detect("boom")
            self.assertEqual(model.detect(0.75)[0].x, 0.75)
        self.assertIsNotNone(model.child.poll())  # Closed: the child has exited.

    def test_native_writes_to_stdout_do_not_corrupt_the_reply_pipe(self):
        with Remote(self.fake.NativeChatter) as model:
            self.assertIsNone(model.detect("frame"))
            self.assertIsNone(model.detect("frame"))

    def test_an_unreadable_reply_is_a_runtime_error_like_a_stopped_process(self):
        # The camera thread restarts the child on RuntimeError; anything else ended the thread.
        import threading
        for garbage in (b"I RKNN: init done\n", b"c no_such_module\nthing\n", b"\x80\x05K\x01."):
            model = Remote.__new__(Remote)
            model.lock = threading.Lock()
            model.child = type("Child", (), {"stdin": io.BytesIO(), "stdout": io.BytesIO(garbage)})()
            with self.subTest(garbage=garbage), self.assertRaises(RuntimeError):
                model.detect("frame")

    def test_a_model_that_cannot_load_raises_and_leaves_no_process(self):
        with self.assertRaisesRegex(RuntimeError, "no mediapipe"):
            Remote(self.fake.Broken)

    def test_the_models_device_comes_back_with_each_frame(self):
        with Remote(self.fake.Switching) as model:
            self.assertEqual(model.device, "NPU")
            self.assertIsNone(model.detect("frame"))
            self.assertEqual(model.device, "NPU")
            model.detect("switch")                 # read after detect: the switch happens inside it
            self.assertEqual(model.device, "CPU")
        with Remote(self.fake.Model) as model:     # a model without .device (fall toolkit's CpuPose)
            self.assertIsNone(model.device)
            model.detect(0.5)
            self.assertIsNone(model.device)

    def test_the_core_pose_backend_runs_in_the_child_with_increasing_timestamps(self):
        # =cpu makes it deterministic even on an RK3588 with converted models.
        with patch.dict(os.environ, {"BAYMAX_POSE_BACKEND": "cpu"}), \
                Remote(partial(ChildPose, None, cpu_factory=self.fake.FakeCpu)) as pose:
            self.assertEqual(pose.device, "CPU")
            stamps = []
            for _ in range(5):
                landmarks = pose.detect(None)
                self.assertEqual(len(landmarks), 33)
                self.assertIsInstance(landmarks[0], Point)
                stamps.append(landmarks[0].x)
            self.assertEqual(pose.device, "CPU")
        self.assertEqual(stamps, sorted(set(stamps)))   # strictly increasing
        self.assertIsNotNone(pose.child.poll())

    def test_the_child_places_itself_before_it_builds_the_model(self):
        # So the model's threads (MediaPipe's, the NPU runtime's) inherit the cores and nice value.
        with Remote(self.fake.Spied(self.fake.Placed)) as pose, Remote(self.fake.Spied(self.fake.PlacedFace)) as face:
            self.assertEqual(pose.device, ["pose"])
            self.assertEqual(face.device, ["face"])

    def test_pose_models_are_placed_as_pose_and_face_landmarks_as_face(self):
        import pc_hardware
        from features import vision_process
        from features.toolkits.fall_detection import CpuPose
        from features.toolkits.proactive import FaceLandmarks
        factories = (partial(ChildPose, "pose_landmarker_lite.task", allow_npu=True),   # the camera's, NPU or CPU
                     partial(CpuPose, None),                                          # the fall toolkit's own
                     FaceLandmarks, self.fake.Model)
        with patch.object(pc_hardware, "place_vision_child", return_value=None) as place:
            for factory in factories:
                vision_process._place(factory)
        self.assertEqual([c.args for c in place.call_args_list], [("pose",), ("pose",), ("face",), ("pose",)])

    def test_only_a_placed_child_turns_off_opencvs_pool_and_placement_never_stops_the_model(self):
        import pc_hardware
        from features import vision_process
        cv2 = SimpleNamespace(setNumThreads=Mock())
        with patch.dict(sys.modules, {"cv2": cv2}), patch("builtins.print") as printed:
            with patch.object(pc_hardware, "place_vision_child", return_value=None):         # Ember not pinned
                vision_process._place(self.fake.Model)
            cv2.setNumThreads.assert_not_called()
            with patch.object(pc_hardware, "place_vision_child", return_value={4, 5, 6, 7}):
                vision_process._place(self.fake.Model)
            cv2.setNumThreads.assert_called_once_with(1)
            printed.assert_not_called()
            with patch.object(pc_hardware, "place_vision_child", side_effect=ValueError("bad cores")):
                vision_process._place(self.fake.Model)
        self.assertIn("[VISION] Core placement skipped (ValueError: bad cores)", printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
