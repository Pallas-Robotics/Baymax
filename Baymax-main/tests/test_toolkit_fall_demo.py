"""Fall demo mode: synthetic replay through the worker's alert path, demo switch and dev-UI routes.

No camera, microphone, MediaPipe or Gemini: replays use made-up poses and a mocked session.
"""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
import urllib.request

import devUI
from fall_detection.detector import FallDetector
from features.toolkits import fall_detection as fall
from features.toolkits import fall_logic, fall_poses
import runtime_monitor as monitor

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.005)
    return predicate()


class DemoWorkerTests(unittest.TestCase):
    def setUp(self):
        record = patch.object(fall, "_record")
        self.record = record.start()
        self.addCleanup(record.stop)
        self.backend_factory = Mock()
        self.worker = fall.FallWorker(self.backend_factory)
        self.worker.replay_speed = 200.0
        self.addCleanup(self.worker.stop)

    def replay(self, name="fast_fall"):
        self.assertTrue(self.worker.replay(name))
        self.assertTrue(wait_for(lambda: not self.worker._replaying()))

    def test_synthetic_fall_reaches_alert_path_without_camera_or_model(self):
        self.worker.set_demo(True, speak=True)
        self.replay()
        self.backend_factory.assert_not_called()   # no MediaPipe model, no camera
        alert = self.worker.pop_alert()
        self.assertEqual(alert["source"], "synthetic replay")
        demo = self.worker.status()["demo"]
        self.assertEqual(len(demo["alerts"]), 1)
        self.assertTrue(demo["alerts"][0]["spoken"])
        self.assertTrue(any("never reaches the runtime's own fall path" in line
                            for line in demo["alerts"][0]["would_fire"]))
        live = demo["live"]
        self.assertEqual(live["posture"], "on floor (possible fall)")
        self.assertEqual(len(live["points"]), 33)
        self.assertTrue(live["source"].startswith("synthetic replay"))
        self.assertTrue(any("synthetic pose replay" in call.args[0] for call in self.record.call_args_list))

    def test_non_fall_replays_raise_no_alert(self):
        self.worker.set_demo(True)
        for name in ("bend_pick_up", "sitting_down", "lie_in_bed"):
            self.replay(name)
        self.assertIsNone(self.worker.pop_alert())
        self.assertEqual(self.worker.status()["demo"]["alerts"], [])

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaises(ValueError):
            self.worker.replay("../../etc/passwd")

    def test_demo_off_publishes_no_joint_points(self):
        self.replay()
        demo = self.worker.status()["demo"]
        self.assertFalse(demo["enabled"])
        self.assertIsNone(demo["live"])
        self.assertEqual(demo["alerts"], [])
        self.assertIsNotNone(self.worker.pop_alert())  # a replay still uses the normal alert path

    def test_quiet_demo_shows_alert_but_does_not_ask_gemini(self):
        self.worker.set_demo(True, speak=False)
        self.replay()
        shutdown = threading.Event()
        session = SimpleNamespace(send_client_content=AsyncMock())

        async def run():
            task = asyncio.create_task(self.worker.alert_monitor(session, shutdown))
            await asyncio.sleep(0.3)
            shutdown.set()
            await task
        with patch.object(monitor, "notify") as notify:
            asyncio.run(run())
        session.send_client_content.assert_not_called()
        notify.assert_called_once_with("fall_alert")
        self.assertFalse(self.worker.status()["demo"]["alerts"][0]["spoken"])

    def test_speaking_demo_sends_the_unchanged_check_in(self):
        self.worker.set_demo(True, speak=True)
        self.replay()
        shutdown = threading.Event()

        async def sent(**kwargs):
            shutdown.set()
        session = SimpleNamespace(send_client_content=AsyncMock(side_effect=sent))
        with patch.object(monitor, "notify"):
            asyncio.run(self.worker.alert_monitor(session, shutdown))
        text = session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("possible fall", text)
        self.assertIn("do not claim an emergency", text)

    def test_control_file_applies_only_to_this_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".runtime-fall-demo.json"
            path.write_text(json.dumps({"pid": os.getpid() + 1, "enabled": True, "speak": False}))
            self.worker.poll_demo_control(path)
            self.assertFalse(self.worker.demo["enabled"])     # stale file from another Ember process
            path.write_text(json.dumps({"pid": os.getpid(), "enabled": True, "speak": False}))
            self.worker.poll_demo_control(path)
            self.assertEqual(self.worker.demo, {"enabled": True, "speak": False})
            path.write_text("not json")
            self.worker.poll_demo_control(path)                # ignored, not raised
            self.assertTrue(self.worker.demo["enabled"])

    def test_control_file_replay_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".runtime-fall-demo.json"
            request = {"pid": os.getpid(), "enabled": True, "speak": True,
                       "replay": {"id": "a", "scenario": "fast_fall", "requested_at": time.time()}}
            path.write_text(json.dumps(request))
            with patch.object(self.worker, "replay") as replay:
                self.worker.poll_demo_control(path)
                self.worker.poll_demo_control(path)
                replay.assert_called_once_with("fast_fall")
                request["replay"].update(id="b", requested_at=time.time() - 60)  # stale request
                path.write_text(json.dumps(request))
                self.worker.poll_demo_control(path)
                replay.assert_called_once()

    def test_turning_demo_off_clears_screen_state(self):
        self.worker.set_demo(True)
        self.replay()
        self.worker.set_demo(False)
        demo = self.worker.status()["demo"]
        self.assertIsNone(demo["live"])
        self.assertEqual(demo["alerts"], [])

    def test_camera_frames_publish_posture_in_demo_mode(self):
        env = patch.dict(os.environ, {"BAYMAX_FALL_DETECTION_ENABLED": "1", "BAYMAX_FALL_FPS": "15"})
        env.start()
        self.addCleanup(env.stop)
        backend = SimpleNamespace(detect=Mock(return_value=fall_poses.body()), close=Mock())
        worker = fall.FallWorker(lambda: backend)   # real decision logic, fake pose backend
        self.addCleanup(worker.stop)
        worker.set_demo(True)
        worker.start()
        import numpy as np
        with patch("runtime_monitor.sensor_alive"):
            worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.assertTrue(wait_for(lambda: worker.status()["demo"]["live"] is not None))
        live = worker.status()["demo"]["live"]
        self.assertEqual((live["source"], live["posture"]), ("camera (fall toolkit)", "standing"))

    def test_replay_checks_in_even_when_the_built_in_detector_owns_camera_check_ins(self):
        # v10's own detector never sees synthetic frames, so the replay uses the toolkit's path.
        self.worker.builtin = True
        self.replay()
        self.assertEqual(self.worker.pop_alert()["source"], "synthetic replay")

    def feed_core(self, name, worker=None):
        """Frames through v10's own detector (as _fall_detection_thread builds it) and observe_core."""
        worker = worker or self.worker
        detector = FallDetector(**fall_logic.V10_SETTINGS)
        detected = []
        for moment, landmarks in fall_poses.scenario(name).frames:
            result = detector.update(landmarks, timestamp=moment)
            detected.append(result["fall_detected"])
            worker.observe_core(landmarks, result, detector, moment)
        return detected

    def test_core_detection_is_shown_only_in_demo_mode(self):
        self.feed_core("fast_fall")
        demo = self.worker.status()["demo"]
        self.assertIsNone(demo["live"])
        self.assertEqual(demo["alerts"], [])
        self.worker.set_demo(True)
        self.assertEqual(sum(self.feed_core("fast_fall")), 1)
        demo = self.worker.status()["demo"]
        self.assertEqual(demo["live"]["source"], "camera (Ember's built-in detection)")
        self.assertEqual(demo["live"]["posture"], "on floor (possible fall)")
        self.assertEqual(len(demo["alerts"]), 1)
        self.assertTrue(any("/api/fall" in line for line in demo["alerts"][0]["would_fire"]))
        # Display only: the built-in detector sends its own check-in and report, never this queue.
        self.assertIsNone(self.worker.pop_alert())

    def test_observe_core_never_raises(self):
        self.worker.set_demo(True)
        self.worker.observe_core(object(), {"fall_detected": True}, object(), "not a time")

    def test_runtime_feeds_its_own_results_to_the_demo_screen(self):
        source = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")
        update = "result = fall_detector.update(landmarks, timestamp=captured_at) if fall_detector else no_fall"
        hook = "fall_worker.observe_core(landmarks, result, fall_detector, captured_at)"
        self.assertLess(source.index(update), source.index(hook))
        # Replays and the demo never reach the dashboard/cloud fall report.
        for module in (fall, devUI):
            text = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("_notify_webapp", text)
            self.assertNotIn("baymax_cloud", text)

    def test_monitor_loop_polls_demo_without_raising(self):
        with patch.object(fall.worker, "poll_demo_control", side_effect=RuntimeError("boom")):
            monitor.poll_fall_demo()


class DemoHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env_path = root / ".env"
        self.env_path.write_text("GOOGLE_API_KEY=existing-fake-key\n")
        self.control = root / ".runtime-fall-demo.json"
        for patcher in (patch.object(devUI, "ENV", self.env_path), patch.object(devUI, "FALL_DEMO", self.control)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), devUI.Handler)
        self.server.token = "test-token"
        self.server.catalog = None
        self.server.settings_lock = threading.Lock()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, token="test-token"):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "Origin": self.url,
                                                  "X-devUI-Token": token})
        with urllib.request.urlopen(request) as response:
            return response.read()

    def test_page_has_token_controls_and_no_key(self):
        page = self.request("/fall-demo").decode()
        self.assertIn("test-token", page)
        self.assertIn("Replay synthetic fall", page)
        self.assertIn("Demo mode", page)
        self.assertNotIn("existing-fake-key", page)
        self.assertNotIn("getUserMedia", page)   # the page never opens a camera or microphone itself

    def test_offline_replay_returns_timeline_and_writes_nothing(self):
        with patch.object(devUI, "read_status", return_value={"state": "offline"}):
            data = json.loads(self.request("/api/fall-demo", {"action": "replay", "scenario": "fast_fall"}))
        self.assertFalse(data["runtime"])
        self.assertEqual(set(data["would_fire"]), {"replay", "camera"})
        steps = data["timeline"]["steps"]
        self.assertEqual(len(steps), len(fall_poses.scenario("fast_fall").frames))
        detected = [step["t"] for step in steps if step["fall_detected"]]
        self.assertEqual(len(detected), 1)
        self.assertTrue(any(step["countdown_s"] is not None for step in steps))
        self.assertFalse(self.control.exists())

    def test_running_ember_gets_pid_bound_request(self):
        with patch.object(devUI, "read_status", return_value={"state": "listening", "pid": 4242}):
            data = json.loads(self.request("/api/fall-demo", {"action": "replay", "scenario": "bend_pick_up",
                                                              "enabled": False, "speak": False}))
            self.assertTrue(data["runtime"])
            request = json.loads(self.control.read_text())
            self.assertEqual((request["pid"], request["enabled"], request["speak"]), (4242, True, False))
            self.assertEqual(request["replay"]["scenario"], "bend_pick_up")
            self.request("/api/fall-demo", {"action": "set", "enabled": False, "speak": True})
            request = json.loads(self.control.read_text())
            self.assertFalse(request["enabled"])
            self.assertNotIn("replay", request)

    def test_invalid_requests_are_rejected(self):
        for body in ({"action": "email"}, {"action": "replay", "scenario": "nope"},
                     {"action": "set", "enabled": "yes"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/fall-demo", body)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/fall-demo", {"action": "set"}, token="")
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.control.exists())


if __name__ == "__main__":
    unittest.main()
