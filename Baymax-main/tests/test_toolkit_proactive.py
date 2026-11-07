"""Proactive greetings with fake faces, clocks and sessions; no camera, model or Gemini."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import runtime_monitor as monitor
from features.toolkits import fall_detection, proactive
from features.toolkits.proactive import EyeContact, Proactive, looking


def face(yaw=0.0, iris=0.5, span=0.14):
    """478 fake landmarks: eyes `span` apart, nose offset by `yaw`, irises at `iris` across each eye."""
    points = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
    left, right = 0.5 - span / 2, 0.5 + span / 2
    width = span * 0.3
    points[33].x, points[133].x = left, left + width          # Right eye (image left).
    points[362].x, points[263].x = right - width, right       # Left eye.
    points[468].x = left + width * iris
    points[473].x = right - width + width * iris
    points[1].x = 0.5 + yaw * span
    return points


class LookingTests(unittest.TestCase):
    def test_facing_camera_with_centred_irises_is_eye_contact(self):
        self.assertTrue(looking(face()))
        self.assertFalse(looking(None))
        self.assertFalse(looking(face()[:468]))      # No iris points.
        self.assertFalse(looking(face(yaw=0.3)))     # Head turned away.
        self.assertFalse(looking(face(iris=0.15)))   # Eyes looking sideways.
        self.assertFalse(looking(face(span=0.03)))   # Too far away to be talking to Ember.

    def test_new_eye_contact_needs_a_held_look_after_time_away(self):
        contact = EyeContact(hold=0.8, away=30)
        self.assertFalse(contact.update(0.0, True))
        self.assertTrue(contact.update(1.0, True))    # Held long enough: one event.
        self.assertFalse(contact.update(2.0, True))
        self.assertFalse(contact.update(3.0, False))  # Glanced away briefly...
        self.assertFalse(contact.update(4.0, True))
        self.assertFalse(contact.update(6.0, True))   # ...looking back is not new.
        contact.update(7.0, False)
        contact.update(40.0, True)
        self.assertTrue(contact.update(41.0, True))   # Back after 30 s away: new again.


    def test_started_and_ended_survive_a_blink(self):
        contact = EyeContact(hold=0.8, away=30, grace=1.0)
        changes = []
        for t, seen in [(0, True), (0.5, True), (1.0, True), (1.25, False), (1.5, True),
                        (2.0, False), (2.5, False), (3.1, False), (3.5, False)]:
            contact.update(t, seen)
            if contact.change:
                changes.append((t, contact.change))
        self.assertEqual(changes, [(1.0, "started"), (3.1, "ended")])


class StartupNoteTests(unittest.TestCase):
    def test_clock_uses_local_am_pm_timezone_and_daypart(self):
        for hour, minute, expected_time, greeting in [
            (0, 0, "12:00 AM", "Good evening"),
            (5, 0, "5:00 AM", "Good morning"),
            (11, 59, "11:59 AM", "Good morning"),
            (12, 0, "12:00 PM", "Good afternoon"),
            (18, 0, "6:00 PM", "Good evening"),
            (19, 53, "7:53 PM", "Good evening"),
        ]:
            with self.subTest(hour=hour, minute=minute):
                local = datetime(2026, 10, 27, hour, minute,
                                 tzinfo=timezone(timedelta(hours=-7), "PDT"))
                with patch.object(proactive, "datetime") as clock:
                    clock.fromtimestamp.return_value.astimezone.return_value = local
                    note = proactive.startup_note(123)
                clock.fromtimestamp.assert_called_once_with(123)
                data = json.loads(note.removeprefix(proactive.STARTUP_NOTE))
                self.assertEqual(data, {"greeting": greeting, "local_time": expected_time,
                                        "timezone": "PDT", "local_datetime": local.isoformat(timespec="seconds")})


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793155980.0
        self.book = False
        self.talked = 0.0
        self.greeter = Proactive(landmarks_factory=lambda: None, clock=lambda: self.now,
                                 wall=lambda: self.now, book_playing=lambda: self.book,
                                 last_conversation=lambda: self.talked)
        patcher = patch.object(monitor, "state", "listening")
        patcher.start()
        self.addCleanup(patcher.stop)

    def look(self, seconds=1.0):
        for _ in range(int(seconds / 0.25) + 1):
            self.greeter.step(face())
            self.now += 0.25

    def test_greets_on_new_eye_contact_only_when_quiet_and_not_in_a_book(self):
        self.talked = self.now - 30           # Talking recently: no greeting.
        self.look()
        self.assertFalse(self.greeter.wanted)
        self.greeter.step(None)
        self.now += 60
        self.talked = self.now - 600
        self.book = True                      # Book playing: no greeting.
        self.look()
        self.assertFalse(self.greeter.wanted)
        self.greeter.step(None)
        self.now += 60
        self.book = False
        self.look()
        self.assertTrue(self.greeter.wanted)

    def test_cooldown_and_busy_states_block_greetings(self):
        self.greeter.last_greeting = self.now - 60
        self.assertFalse(self.greeter.allowed())
        self.greeter.last_greeting = None
        with patch.object(monitor, "state", "speaking"):
            self.assertFalse(self.greeter.allowed())
        self.assertTrue(self.greeter.allowed())

    def test_session_task_says_hello_once_and_delivers_greetings(self):
        sent = []
        session = SimpleNamespace(send_client_content=AsyncMock(
            side_effect=lambda **kw: sent.append(kw["turns"]["parts"][0]["text"])))

        async def run_once(want=False):
            shutdown = asyncio.Event()
            self.greeter.wanted = want
            self.greeter.sleep = AsyncMock(side_effect=lambda s: shutdown.set())
            await self.greeter.session_task(session, shutdown)

        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            asyncio.run(run_once())
            asyncio.run(run_once())           # Reconnect: no second hello.
        self.assertEqual(sent, [proactive.startup_note(self.now)])
        self.greeter.last_greeting = None     # Past the cooldown.
        asyncio.run(run_once(want=True))
        self.assertEqual(sent[-1], proactive.PRESENCE_NOTE)

    def test_ambient_transcript_during_boot_does_not_suppress_startup_statement(self):
        sent = []
        session = SimpleNamespace(send_client_content=AsyncMock(
            side_effect=lambda **kw: sent.append(kw["turns"]["parts"][0]["text"])))
        shutdown = asyncio.Event()
        self.talked = self.now  # A noise fragment was transcribed during the four-second wait.
        self.greeter.sleep = AsyncMock(side_effect=lambda seconds: shutdown.set())
        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            asyncio.run(self.greeter.session_task(session, shutdown))
        self.assertEqual(sent, [proactive.startup_note(self.now)])

    def test_failed_startup_send_retries_on_the_next_session(self):
        failed = SimpleNamespace(send_client_content=AsyncMock(side_effect=ConnectionError))
        working = SimpleNamespace(send_client_content=AsyncMock())
        shutdown = asyncio.Event()
        self.greeter.sleep = AsyncMock(side_effect=lambda seconds: shutdown.set())
        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            asyncio.run(self.greeter.session_task(failed, shutdown))
            shutdown.clear()
            asyncio.run(self.greeter.session_task(working, shutdown))
        working.send_client_content.assert_awaited_once()
        self.assertTrue(self.greeter.greeted_startup)

    def test_a_delivered_hello_is_not_sent_again_by_the_greeter(self):
        # A hello that reached Gemini but was cut off by a drop is resent by the reconnect
        # (AddressGate.abandon); the greeter sending it too would greet twice.
        sent = []
        session = SimpleNamespace(send_client_content=AsyncMock(
            side_effect=lambda **kw: sent.append(kw["turns"]["parts"][0]["text"])))

        async def session_then_drop():
            shutdown = asyncio.Event()

            async def sleep(seconds):
                if seconds == 0.2:
                    raise asyncio.CancelledError  # the connection drops
            self.greeter.sleep = sleep
            try:
                await self.greeter.session_task(session, shutdown)
            except asyncio.CancelledError:
                pass

        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            asyncio.run(session_then_drop())
            asyncio.run(session_then_drop())
        self.assertEqual(sent, [proactive.startup_note(self.now)])
        self.assertTrue(self.greeter.greeted_startup)

    def test_hello_that_never_reached_gemini_is_retried_once(self):
        attempts = []

        async def unreachable(**kw):
            attempts.append(kw["turns"]["parts"][0]["text"])
            raise ConnectionResetError()
        session = SimpleNamespace(send_client_content=unreachable)

        async def failing_session():
            shutdown = asyncio.Event()

            async def sleep(seconds):
                if seconds == 0.2:
                    shutdown.set()
            self.greeter.sleep = sleep
            await self.greeter.session_task(session, shutdown)

        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}), patch("builtins.print"):
            for _ in range(3):
                asyncio.run(failing_session())
        self.assertEqual(len(attempts), 2)  # Retried once, then gives up.
        self.assertFalse(self.greeter.greeted_startup)

    def test_eye_contact_is_off_unless_enabled(self):
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("BAYMAX_PRESENCE_GREETING", None)  # Default: no face model, no thread.
            self.greeter.observe(object())
        with patch.dict("os.environ", {"BAYMAX_PRESENCE_GREETING": "0"}):
            self.greeter.observe(object())
        self.assertIsNone(self.greeter.thread)
        self.assertIsNone(self.greeter.frame)

    def test_unavailable_model_leaves_conversation_alone(self):
        def broken():
            raise ImportError("mediapipe")
        greeter = Proactive(landmarks_factory=broken)
        greeter._run()
        self.assertTrue(greeter.state.startswith("Unavailable"))

    def test_the_face_model_is_fetched_before_its_process_starts(self):
        # The vision process stops a child that has not loaded in 60 s; a slow first
        # download must not count against that.
        order = []
        with patch.object(proactive, "ensure_model", side_effect=lambda: order.append("download")), \
                patch("features.vision_process.Remote", side_effect=lambda factory: order.append("start")):
            proactive.remote_face()
        self.assertEqual(order, ["download", "start"])

    def test_face_checks_slow_to_once_a_second_while_nobody_is_in_view(self):
        faces = iter([None, None, object()])
        greeter = Proactive(landmarks_factory=lambda: SimpleNamespace(detect=lambda frame: next(faces)),
                            clock=lambda: 0.0)
        greeter.step = lambda face: None
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise StopIteration
            greeter.frame = object()
        greeter.frame = object()
        with patch.object(proactive.time, "sleep", sleep), self.assertRaises(StopIteration):
            greeter._run()
        self.assertEqual(sleeps, [1.0, 1.0, 1.0 / greeter.fps])


class HookTests(unittest.TestCase):
    def test_registered_on_existing_camera_frames_and_session(self):
        import features.toolkits.gemini_tools  # noqa: F401  (imports and registers proactive)
        self.assertIn(proactive.proactive.observe, fall_detection.frame_observers)
        self.assertIn(proactive.proactive.session_task, fall_detection.session_tasks)

    def test_no_session_task_blocks_the_event_loop_with_the_runtime_shutdown_event(self):
        # The runtime passes a threading.Event. A task that called its wait() froze
        # Ember's whole loop on ember-002: no hello, mic never reached Gemini.
        import threading
        import features.toolkits.gemini_tools  # noqa: F401  (registers every task)
        session = SimpleNamespace(send_client_content=AsyncMock())
        self.assertGreaterEqual(len(fall_detection.session_tasks), 2)
        for task in list(fall_detection.session_tasks):
            ticks = []

            async def run():
                shutdown = threading.Event()
                running = asyncio.create_task(task(session, shutdown))
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    ticks.append(1)
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

            asyncio.run(asyncio.wait_for(run(), 3))
            self.assertEqual(len(ticks), 10, getattr(task, "__qualname__", task))

    def test_fall_worker_forwards_frames_and_runs_session_tasks(self):
        seen, started = [], []

        def broken(frame):
            raise RuntimeError

        async def task(session, shutdown):
            started.append(session)
            await asyncio.sleep(10)

        worker = fall_detection.FallWorker()
        with patch.object(fall_detection, "frame_observers", [seen.append, broken]), \
                patch.object(fall_detection, "session_tasks", [task]):
            worker.submit("frame")
            worker.submit("frame2")
            self.assertEqual(seen, ["frame", "frame2"])
            self.assertEqual(fall_detection.frame_observers, [seen.append])  # Broken one removed.

            async def run():
                shutdown = asyncio.Event()
                asyncio.get_running_loop().call_later(0.3, shutdown.set)
                await worker.alert_monitor("session", shutdown)
            asyncio.run(run())
        self.assertEqual(started, ["session"])


if __name__ == "__main__":
    unittest.main()
