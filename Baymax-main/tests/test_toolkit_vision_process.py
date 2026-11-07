"""Camera models run in a child process: frames in, plain landmarks out, errors survive."""
from functools import partial
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features import vision_process
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

    class Hangs:
        """Never answers the frame "hang", as librknnrt stuck in the NPU driver would not."""
        device = "NPU"

        def detect(self, frame):
            import time
            if frame == "hang":
                time.sleep(3600)
            return None

    class HangsLoading:
        def __init__(self):
            import time
            time.sleep(3600)

    class LoadsSlowly:
        """Takes 1.5 s to load: longer than a frame may take, far less than a start may."""
        device = "CPU"

        def __init__(self):
            import time
            time.sleep(1.5)

        def detect(self, frame):
            return None
''')


def quick_watchdog(test, timeout, start_timeout=60.0):
    """Remote's limits shortened for `test`, checked every 50 ms. create=True: the Remote
    from before the watchdog had no such settings and must fail by waiting, not by a
    TypeError."""
    for name, value in (("timeout", timeout), ("start_timeout", start_timeout), ("tick", 0.05)):
        patcher = patch.object(Remote, name, value, create=True)
        patcher.start()
        test.addCleanup(patcher.stop)


def finishes(test, call, within=10):
    """call() in a thread: its outcome ({"value"} or {"error"}) and the seconds it took.
    Fails `test`, rather than waiting for good, if the call is still blocked on a hung
    child after `within` seconds."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except Exception as error:
            outcome["error"] = error
    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(within)
    test.assertFalse(thread.is_alive(), "still waiting for a hung vision process")
    return outcome, time.monotonic() - started


class VisionProcessTests(unittest.TestCase):
    def setUp(self):
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


class HungVisionProcessTests(unittest.TestCase):
    """A child that hangs without exiting is killed by its watchdog, and the call raises
    RuntimeError like a dead child's, so the camera thread restarts pose on the CPU."""

    setUp = VisionProcessTests.setUp      # the same fake models (not the same tests)
    quick_watchdog = quick_watchdog
    finishes = finishes

    def test_a_frame_the_child_never_answers_stops_it_like_a_dead_one(self):
        self.quick_watchdog(timeout=0.5)
        model = Remote(self.fake.Hangs)
        self.addCleanup(model.close)
        self.addCleanup(lambda: model.child.poll() is None and model.child.kill())  # runs first
        self.assertIsNone(model.detect("frame"))
        outcome, took = self.finishes(lambda: model.detect("hang"))
        self.assertIsInstance(outcome.get("error"), RuntimeError)
        self.assertIn("vision process hung: no reply in 0.5 s, so it was stopped", str(outcome["error"]))
        self.assertLess(took, 3)
        self.assertIsNotNone(model.child.wait(timeout=2))      # killed, not left running

    def test_a_model_that_never_finishes_loading_is_stopped_after_the_start_limit(self):
        self.quick_watchdog(timeout=0.1, start_timeout=0.6)
        children = []

        class Processes:                # vision_process's subprocess, keeping the children
            PIPE, TimeoutExpired = subprocess.PIPE, subprocess.TimeoutExpired

            @staticmethod
            def Popen(*args, **kwargs):
                children.append(subprocess.Popen(*args, **kwargs))
                return children[-1]
        self.addCleanup(lambda: [child.kill() for child in children if child.poll() is None])
        with patch.object(vision_process, "subprocess", Processes):
            outcome, took = self.finishes(lambda: Remote(self.fake.HangsLoading))
        self.assertIsInstance(outcome.get("error"), RuntimeError)
        self.assertIn("no reply in 0.6 s", str(outcome["error"]))   # the start's limit, not a frame's
        self.assertLess(took, 5)
        self.assertIsNotNone(children[0].wait(timeout=2))         # no process left behind

    def test_loading_may_take_longer_than_a_frame(self):
        self.quick_watchdog(timeout=5.0)                            # checked every 50 ms
        with Remote(self.fake.LoadsSlowly, timeout=0.5) as model:   # loads in 1.5 s; start limit 60 s
            self.assertEqual(model.device, "CPU")
            self.assertIsNone(model.detect("frame"))
            self.assertIsNone(model.child.poll())

    def test_one_watchdog_per_child_and_no_thread_or_timer_per_frame(self):
        # A threading.Timer per frame doubled the parent's CPU per round trip (pose README).
        made = []

        class Threads:                  # vision_process's threading, keeping the threads it makes
            Lock, Event = threading.Lock, threading.Event

            @staticmethod
            def Thread(*args, **kwargs):
                made.append(threading.Thread(*args, **kwargs))
                return made[-1]

            @staticmethod
            def Timer(*args, **kwargs):
                made.append(threading.Timer(*args, **kwargs))
                return made[-1]
        with patch.object(vision_process, "threading", Threads):
            model = Remote(self.fake.Model)
            self.addCleanup(model.close)
            self.assertEqual([t.name for t in made], ["vision-watchdog"])
            for n in range(20):
                self.assertEqual(model.detect(n / 100)[0].x, n / 100)
            self.assertEqual(len(made), 1)                      # nothing more per frame
            model.close()
        made[0].join(2)
        self.assertFalse(made[0].is_alive())                    # close() ends it


if __name__ == "__main__":
    unittest.main()
