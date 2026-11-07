import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from fall_detection import detector as upstream_detector
from features.toolkits import fall_detection as fall
import runtime_monitor as monitor
from dev_settings import save_configuration, read_configuration


def pose(angle):
    angle = math.radians(angle)
    shoulder = (0.5 + 0.25 * math.sin(angle), 0.6 - 0.25 * math.cos(angle))
    points = [SimpleNamespace(x=0.5, y=0.6, visibility=1.0) for _ in range(33)]
    for index, offset in ((11, -0.02), (12, 0.02)):
        points[index] = SimpleNamespace(x=shoulder[0]+offset, y=shoulder[1], visibility=1.0)
    return points


class FallAlgorithmTests(unittest.TestCase):
    def simulate(self, frames):
        detector = upstream_detector.FallDetector(angle_threshold=45, ang_vel_threshold=25,
                                                   hip_vel_threshold=0.12, confirmation_frames=2)
        results = []
        for moment, angle in frames:
            with patch.object(upstream_detector.time, "monotonic", return_value=moment):
                results.append(detector.update(pose(angle) if angle is not None else None))
        return results

    def test_standing_missing_pose_and_slow_lying_do_not_trigger(self):
        for frames in ([(i/10, 0) for i in range(50)], [(i/10, None) for i in range(50)],
                       [(i/10, i*1.8) for i in range(50)]):
            self.assertFalse(any(result["fall_detected"] for result in self.simulate(frames)))

    def test_fast_rotation_requires_stay_down_and_respects_cooldown(self):
        frames = [(i/10, 0) for i in range(13)] + [(1.3, 60), (1.4, 80)]
        frames += [(i/10, 90) for i in range(15, 55)]
        results = self.simulate(frames)
        self.assertTrue(any(result["fall_pending"] for result in results))
        detected = [moment for (moment, _), result in zip(frames, results) if result["fall_detected"]]
        self.assertEqual(len(detected), 1)
        self.assertGreaterEqual(detected[0], 3.4)

    def test_bending_and_returning_upright_cancels_pending_fall(self):
        frames = [(i/10, 0) for i in range(13)] + [(1.3, 60), (1.4, 80), (1.5, 0)]
        frames += [(i/10, 0) for i in range(16, 45)]
        results = self.simulate(frames)
        self.assertTrue(any(result["fall_pending"] for result in results))
        self.assertFalse(any(result["fall_detected"] for result in results))

    def test_capture_times_not_the_time_of_the_call_give_the_motion(self):
        # update() runs when the frame's pose comes back, a different time after each
        # capture; given each frame's capture time, the result is that of on-time calls.
        frames = [(i/10, 0) for i in range(13)] + [(1.3, 60), (1.4, 80)]
        frames += [(i/10, 90) for i in range(15, 55)]
        on_time = self.simulate(frames)
        detector = upstream_detector.FallDetector(angle_threshold=45, ang_vel_threshold=25,
                                                   hip_vel_threshold=0.12, confirmation_frames=2)
        late = []
        for number, (moment, angle) in enumerate(frames):
            pose_ms = (20, 90, 45, 70, 30)[number % 5]
            with patch.object(upstream_detector.time, "monotonic", return_value=moment + pose_ms / 1000):
                late.append(detector.update(pose(angle), timestamp=moment))
        self.assertEqual(late, on_time)
        self.assertEqual(sum(result["fall_detected"] for result in late), 1)

    def test_capture_times_matter_when_the_clock_runs_differently(self):
        # Ported from opi4-demo (ff168ca): the clock says the fall took 7 times as long.
        detector = upstream_detector.FallDetector(confirmation_frames=2)
        frames = [(i / 10, 0) for i in range(13)] + [(1.3, 70), (1.4, 85)]
        frames += [(i / 10, 90) for i in range(15, 36)]
        results = []
        for captured_at, angle in frames:
            with patch.object(upstream_detector.time, "monotonic", return_value=1000 + captured_at * 7):
                results.append(detector.update(pose(angle), timestamp=captured_at))
        self.assertTrue(results[14]["fall_pending"])
        self.assertEqual(sum(result["fall_detected"] for result in results), 1)
        self.assertFalse(results[33]["fall_detected"])
        self.assertTrue(results[34]["fall_detected"])          # stayed down 2 s by the capture times

    def test_upstream_detector_matches_recorded_source_hash(self):
        directory = Path(upstream_detector.__file__).parent
        manifest = json.loads((directory / "UPSTREAM.json").read_text())
        self.assertEqual(hashlib.sha256((directory / "detector.py").read_bytes()).hexdigest(), manifest["sha256"])


class FallWorkerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BAYMAX_FALL_DETECTION_ENABLED": "1", "BAYMAX_FALL_FPS": "15"})
        env.start()
        self.addCleanup(env.stop)
        record = patch.object(fall, "_record")
        self.record = record.start()
        self.addCleanup(record.stop)
        self.backend = SimpleNamespace(detect=Mock(return_value=None), close=Mock())
        self.detector = SimpleNamespace(update=Mock(return_value={"fall_detected": True, "fall_active": True,
                                                                   "fall_pending": False, "trunk_angle": 90.0}))
        self.factory = Mock(return_value=self.backend)
        self.worker = fall.FallWorker(self.factory, lambda **_: self.detector)
        self.addCleanup(self.worker.stop)

    def wait_for(self, predicate):
        end = time.monotonic() + 2
        while not predicate() and time.monotonic() < end:
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_disabled_does_not_load_model_or_copy_frames(self):
        frame = Mock()
        with patch.dict(os.environ, {"BAYMAX_FALL_DETECTION_ENABLED": "0"}):
            self.worker.start()
            self.worker.submit(frame)
        self.factory.assert_not_called()
        frame.copy.assert_not_called()
        with patch.dict(os.environ, {"BAYMAX_FALL_DETECTION_ENABLED": "0"}):
            self.assertEqual(self.worker.status()["state"], "Disabled")

    def test_latest_frame_replaces_backlog_without_opening_camera(self):
        self.worker.is_enabled = True
        self.worker.thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: None)
        self.worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.worker.submit(np.ones((2, 2, 3), dtype=np.uint8))
        self.assertEqual(self.worker.dropped, 1)
        self.assertEqual(int(self.worker.pending[1][0, 0, 0]), 1)

    def test_shared_core_pose_loads_no_second_model(self):
        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=1.0)] * 33
        frame = Mock()
        self.worker.start(shared_pose=True, angle_threshold=45)
        self.worker.submit(frame)                       # observers still run; the frame isn't queued
        frame.copy.assert_not_called()
        self.worker.share(landmarks, 12.5)
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        self.factory.assert_not_called()
        self.detector.update.assert_called_with(landmarks, timestamp=None)   # no capture time: the clock
        status = self.worker.status()
        self.assertEqual(status["backend"], "CPU (core pose)")
        self.assertEqual(status["inference_ms"], 12.5)
        self.assertEqual(status["state"], "Running on CPU (core pose)")
        self.record.assert_any_call("Fall detection running on the core's pose model; development mode")

    def test_shared_pose_on_the_npu_is_reported(self):
        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=1.0)] * 33
        self.worker.start(shared_pose=True, angle_threshold=45)
        self.worker.share(landmarks, 4.2, device="NPU")
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        status = self.worker.status()
        self.assertEqual(status["backend"], "NPU (core pose)")
        self.assertEqual(status["state"], "Running on NPU (core pose)")
        self.assertEqual(status["inference_ms"], 4.2)
        self.worker.share(landmarks, 30.0, device="CPU")       # the backend fell back to MediaPipe
        self.wait_for(lambda: self.worker.status()["inference_ms"] == 30.0)
        self.assertEqual(self.worker.status()["state"], "Running on CPU (core pose)")

    def test_shared_pose_is_timed_by_the_frames_capture(self):
        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=1.0)] * 33
        self.worker.start(shared_pose=True, angle_threshold=45)
        self.worker.share(landmarks, 30.0, device="NPU", captured_at=1234.5)
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        self.detector.update.assert_called_with(landmarks, timestamp=1234.5)

    def test_own_model_is_timed_by_when_the_frame_came_not_when_its_pose_did(self):
        def slow_pose(frame):
            time.sleep(0.2)
            return None
        self.backend.detect.side_effect = slow_pose
        self.worker.start(angle_threshold=45)
        before = time.monotonic()
        self.worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        submitted = time.monotonic()
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        timestamp = self.detector.update.call_args.kwargs.get("timestamp")
        self.assertIsNotNone(timestamp)
        self.assertTrue(before <= timestamp <= submitted)       # not 0.2 s later, after the pose

    def test_with_builtin_fall_detection_on_one_fall_gets_one_check_in(self):
        # v10's own detector sends its check-in; the toolkit's must not send a second one,
        # but keeps detecting and reporting for the Monitor.
        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=1.0)] * 33
        self.worker.start(shared_pose=True, builtin=True, angle_threshold=45)
        self.worker.share(landmarks, 4.2, device="NPU")
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        status = self.worker.status()
        self.assertEqual(status["state"], "Running on NPU (core pose)")
        self.assertEqual(status["backend"], "NPU (core pose)")
        self.assertEqual(status["inference_ms"], 4.2)
        self.assertEqual(status["last_result"]["fall_active"], True)
        self.assertIsNone(self.worker.pop_alert())
        self.record.assert_any_call("Possible fall detected locally; check-ins come from Ember's built-in fall detection.")
        session = SimpleNamespace(send_client_content=AsyncMock())
        shutdown = threading.Event()
        threading.Timer(0.3, shutdown.set).start()
        with patch.object(monitor, "notify") as notify:
            asyncio.run(self.worker._alerts(session, shutdown))     # the toolkit's check-in loop
        session.send_client_content.assert_not_awaited()
        notify.assert_not_called()

    def test_without_builtin_fall_detection_the_toolkit_checks_in(self):
        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=1.0)] * 33
        self.worker.start(shared_pose=True, builtin=False, angle_threshold=45)
        self.worker.share(landmarks, 4.2, device="NPU")
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        self.assertIsNotNone(self.worker.pop_alert())
        self.record.assert_any_call("Possible fall detected locally; awaiting a check-in. No external alert sent.")

    def test_with_its_own_model_the_toolkit_checks_in_even_with_builtin_on(self):
        # The core's fall detection only sees the core's pose; without shared_pose nobody else checks in.
        self.worker.start(builtin=True, angle_threshold=45)
        self.worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        self.assertIsNotNone(self.worker.pop_alert())
        self.record.assert_any_call("Possible fall detected locally; awaiting a check-in. No external alert sent.")

    def test_unshared_worker_ignores_core_landmarks(self):
        self.worker.start(angle_threshold=45)
        self.worker.share([SimpleNamespace()], 1.0)
        self.assertIsNone(self.worker.pending)
        self.worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        self.factory.assert_called_once()

    def test_cpu_worker_reports_result_and_closes_backend_on_stop(self):
        self.worker.start(angle_threshold=45)
        self.worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.wait_for(lambda: self.worker.status()["last_result"] is not None)
        alert = self.worker.pop_alert()
        self.assertIsNotNone(alert)
        self.assertEqual(self.worker.status()["backend"], "CPU")
        self.assertIsNotNone(self.worker.status()["inference_ms"])
        self.worker.stop()
        self.backend.close.assert_called_once()

    def test_initialization_failure_is_unavailable_not_running(self):
        self.factory.side_effect = RuntimeError("simulated model failure")
        with patch("builtins.print"):
            self.worker.start()
            self.wait_for(lambda: self.worker.status()["state"].startswith("Unavailable"))
        self.assertIsNone(self.worker.pop_alert())

    def test_invalid_fps_is_reported_without_loading_backend(self):
        for fps in ("bad", "0", "nan", "100"):
            with patch.dict(os.environ, {"BAYMAX_FALL_FPS": fps}):
                self.worker.start()
                self.assertTrue(self.worker.status()["state"].startswith("Unavailable"))
        self.factory.assert_not_called()

    def test_stale_frames_do_not_report_running_inference(self):
        self.worker.state = "Running on CPU"
        self.worker.samples.extend([time.monotonic()-4, time.monotonic()-3])
        status = self.worker.status()
        self.assertEqual(status["state"], "Waiting for fresh camera frames")
        self.assertIsNone(status["fps"])

    def test_a_check_in_that_cannot_reach_gemini_is_asked_again(self):
        # A reconnect only resends messages that reached Gemini, so the worker retries itself.
        worker = fall.FallWorker()
        worker.alerts.append({"time": time.time(), "recorded": time.monotonic() - 10})
        calls = []

        async def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ConnectionResetError()
            shutdown.set()

        async def run():
            await worker._alerts(SimpleNamespace(send_client_content=flaky), shutdown)
        shutdown = asyncio.Event()
        with patch("asyncio.sleep", AsyncMock()), patch.object(fall, "_record"), \
                patch("runtime_monitor.notify"):
            asyncio.run(run())
        self.assertEqual(len(calls), 2)

    def test_a_retried_check_in_still_says_that_playback_was_paused(self):
        # The first try paused the book; on the retry it is already paused, so
        # hold_for_answer has nothing to say, but Gemini still has to be told.
        from features.toolkits import audiobooks
        worker = fall.FallWorker()
        worker.alerts.append({"time": time.time(), "recorded": time.monotonic()})
        texts = []

        async def flaky(**kwargs):
            texts.append(kwargs["turns"]["parts"][0]["text"])
            if len(texts) == 1:
                raise ConnectionResetError()
            shutdown.set()

        shutdown = asyncio.Event()
        notes = iter([" The book is paused at chapter 2 so they can hear you and answer.", ""])
        with patch("asyncio.sleep", AsyncMock()), patch.object(fall, "_record"), \
                patch("runtime_monitor.notify"), \
                patch.object(audiobooks, "hold_for_answer", side_effect=lambda reason: next(notes)):
            asyncio.run(worker._alerts(SimpleNamespace(send_client_content=flaky), shutdown))
        self.assertEqual(len(texts), 2)
        for text in texts:
            self.assertTrue(text.endswith(" The book is paused at chapter 2 so they can hear you and answer."))

    def test_a_check_in_cut_off_by_a_cancelled_session_waits_for_the_next(self):
        worker = fall.FallWorker()
        worker.alerts.append({"time": time.time(), "recorded": time.monotonic()})

        async def hangs(**kwargs):
            raise asyncio.CancelledError()

        with patch("runtime_monitor.notify"), self.assertRaises(asyncio.CancelledError):
            asyncio.run(worker._alerts(SimpleNamespace(send_client_content=hangs), asyncio.Event()))
        self.assertIsNotNone(worker.pop_alert())  # fresh, so the next session sends it

    def test_stale_alert_is_not_delivered(self):
        self.worker.alerts.append({"recorded": time.monotonic()-20})
        self.assertIsNone(self.worker.pop_alert())

    def test_checkin_uses_uncertain_language_and_no_external_alert(self):
        shutdown = threading.Event()
        async def sent(**kwargs):
            shutdown.set()
        session = SimpleNamespace(send_client_content=AsyncMock(side_effect=sent))
        self.worker.alerts.append({"recorded": time.monotonic()})
        with patch.object(monitor, 'notify') as notify:
            asyncio.run(self.worker.alert_monitor(session, shutdown))
        notify.assert_called_once_with('fall_alert')
        text = session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("possible fall", text)
        self.assertIn("do not claim an emergency", text)
        self.assertNotIn("email", text)

    def test_failed_gemini_checkin_does_not_crash_conversation(self):
        shutdown = threading.Event()
        async def rejected(**kwargs):
            shutdown.set()
            raise RuntimeError("simulated provider failure")
        self.worker.alerts.append({"recorded": time.monotonic()})
        asyncio.run(self.worker.alert_monitor(SimpleNamespace(send_client_content=AsyncMock(side_effect=rejected)), shutdown))
        self.assertTrue(any("could not reach Gemini" in call.args[0] for call in self.record.call_args_list))


class FallModelAndSettingsTests(unittest.TestCase):
    def test_model_download_is_checksum_verified_and_cached(self):
        data=b"test-model"
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"pose.task"
            fetch=Mock(return_value=data)
            fall.ensure_model(path, fetch, hashlib.sha256(data).hexdigest())
            fall.ensure_model(path, fetch, hashlib.sha256(data).hexdigest())
            fetch.assert_called_once()
            self.assertEqual(path.read_bytes(), data)

    def test_the_own_model_is_fetched_before_its_pose_process_starts(self):
        # The vision process stops a child that has not loaded in 60 s; a slow first
        # download (~9.4 MB) must not count against that.
        order = []
        with patch.object(fall, "ensure_model", side_effect=lambda path: order.append("download") or Path("m.task")), \
                patch("features.vision_process.Remote", side_effect=lambda factory: order.append(factory.args)):
            fall.remote_pose()
            fall.remote_pose("given.task")                      # a given model is not fetched
        self.assertEqual(order, ["download", (Path("m.task"),), ("given.task",)])

    def test_bad_model_is_not_saved_or_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"pose.task"
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                fall.ensure_model(path, lambda: b"bad")
            self.assertFalse(path.exists())

    def test_toggle_round_trip_and_no_camera_validation(self):
        catalog={"microphones": [], "speakers": [], "cameras": [{"index": 0,"name":"Test camera"}]}
        payload={"key":"", "microphone":"", "speaker":"", "camera":"0", "fall_detection":True}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/".env"
            result=save_configuration(path, payload, catalog)
            self.assertTrue(result["fall_detection"])
            self.assertEqual(read_configuration(path)["BAYMAX_FALL_DETECTION_ENABLED"], "1")
            previous=path.read_bytes()
            for change in ({"camera":"none"}, {"fall_detection":"yes"}):
                with self.assertRaises(ValueError):
                    save_configuration(path, dict(payload, **change), catalog)
                self.assertEqual(path.read_bytes(), previous)
            self.assertFalse(save_configuration(path, dict(payload, fall_detection=False), catalog)["fall_detection"])

    def test_passive_fall_event_does_not_replace_listening_state(self):
        with patch.object(monitor, "events", monitor.deque(maxlen=80)), patch.object(monitor, "state", "listening"):
            monitor.record("fall_detection", "Possible fall")
            self.assertEqual(monitor.snapshot()["state"], "listening")
            self.assertEqual(monitor.snapshot()["events"][-1]["state"], "fall_detection")
