"""Face and voice recognition in the live runtime (features/person_recognition): child
processes, the mic feed, tools, spoken prompts and registration. No camera, mic, models
or Gemini: children run fake models; sessions are fakes."""
import asyncio
import functools
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.person_recognition import face_process, people as people_module, voice_process  # noqa: E402
from features.person_recognition.people import People, arrival_prompt, describe_away, speaker_answer  # noqa: E402
from features.vision_process import Remote, Stopped  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

FAKE = textwrap.dedent('''
    class Echo:
        """A model that replies with its own result (raw_results), placed as voice."""
        vision_role = "voice"
        raw_results = True
        device = "CPU"

        def detect(self, item):
            return {"echo": item, "pid": __import__("os").getpid()}
''')


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class RawResultsChildTests(unittest.TestCase):
    """vision_process: a raw_results model's replies come back as they are (dicts, not Points)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "fake_people_model.py").write_text(FAKE)
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = directory.name + (os.pathsep + old if old else "")
        self.addCleanup(lambda: os.environ.__setitem__("PYTHONPATH", old) if old else os.environ.pop("PYTHONPATH"))
        sys.path.insert(0, directory.name)
        self.addCleanup(sys.path.remove, directory.name)

    def test_raw_results_and_own_process(self):
        import fake_people_model
        with Remote(fake_people_model.Echo) as remote:
            reply = remote.call(("audio", [b"\x00\x01", 1.5]))
            self.assertEqual(reply["echo"], ("audio", [b"\x00\x01", 1.5]))
            self.assertNotEqual(reply["pid"], os.getpid())
            self.assertEqual(remote.device, "CPU")

    def test_voice_and_face_children_go_to_the_slow_cores(self):
        import pc_hardware
        self.assertEqual(pc_hardware.VISION_PLACES["voice"], ("slow", 10))
        self.assertEqual(pc_hardware.VISION_PLACES["face"], ("slow", 10))
        self.assertEqual(face_process.FaceChild.vision_role, "face")
        self.assertEqual(voice_process.VoiceChild.vision_role, "voice")
        self.assertTrue(face_process.FaceChild.raw_results and voice_process.VoiceChild.raw_results)


class FakeRemote:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.closed = False
        self.fail = False

    def call(self, item, timeout=None):
        self.calls.append(item)
        if self.fail:
            raise Stopped("vision process stopped (exited)")
        handler = self.replies.get(item[0])
        return handler(item) if callable(handler) else handler

    def close(self):
        self.closed = True


class RemoteFaceModelsTests(unittest.TestCase):
    def test_the_frame_crosses_once_per_cycle(self):
        remote = FakeRemote({"detect": ["det"], "refine": "refined", "fingerprint": "look", "embed": "emb"})
        models = face_process.RemoteFaceModels(paths=None, remote=remote)
        frame = object()
        self.assertEqual(models.detect(frame, 0.5), ["det"])
        self.assertEqual(models.fingerprint(frame, "det"), "look")
        self.assertEqual(models.refine(frame, "det"), "refined")
        self.assertEqual(models.embed(frame, "refined"), "emb")
        self.assertEqual(remote.calls, [("detect", frame, 0.5), ("fingerprint", "det"), ("refine", "det"),
                                        ("embed", "refined")])
        models.close()
        self.assertTrue(remote.closed)

    def test_child_keeps_the_frame_for_follow_up_calls(self):
        class Models:
            def detect(self, frame, scale):
                return [("found in", frame, scale)]

            def embed(self, frame, det):
                return ("embedded", frame, det)
        child = face_process.FaceChild.__new__(face_process.FaceChild)
        child.models, child.frame = Models(), None
        self.assertIsNone(child.detect(("embed", "det")))       # nothing to work on yet
        self.assertEqual(child.detect(("detect", "F", 0.5)), [("found in", "F", 0.5)])
        self.assertEqual(child.detect(("embed", "det")), ("embedded", "F", "det"))
        with self.assertRaises(ValueError):
            child.detect(("nonsense", None))


def reply(results=(), learned=(), in_speech=False):
    return {"results": list(results), "learned": list(learned), "in_speech": in_speech, "learning": False}


class VoiceFeedTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.remote = FakeRemote({"audio": reply(), "learn": reply()})
        self.seen, self.learned = [], []
        self.feed = voice_process.VoiceFeed(lambda: self.remote, on_result=self.seen.append,
                                            on_learned=self.learned.append, clock=self.clock)
        self.feed.remote = self.remote

    def test_feed_is_an_append_and_drops_the_oldest_when_full(self):
        for i in range(voice_process.MAX_QUEUED + 5):
            self.feed.feed(bytes([i % 256]), ember_speaking=i % 2)
        self.assertEqual(len(self.feed.queue), voice_process.MAX_QUEUED)
        self.assertEqual(self.feed.dropped, 5)
        self.assertEqual(self.feed.queue[0][0], bytes([5]))
        self.assertIs(self.feed.queue[1][1], False)

    def test_batches_go_to_the_child_and_results_come_back(self):
        result = {"level": "sure", "person_id": "p_1", "name": "Stella", "score": 0.7, "seconds": 2.1,
                  "ms": 90, "stage": "early"}
        self.remote.replies["audio"] = reply([result], in_speech=True)
        self.feed.feed(b"a", False)
        self.feed.feed(b"b", True)
        self.feed._step()
        (command, batch), = self.remote.calls
        self.assertEqual(command, "audio")
        self.assertEqual([(c, s) for c, s, _ in batch], [(b"a", False), (b"b", True)])
        self.assertEqual(self.seen, [result])
        self.assertTrue(self.feed.in_speech)
        self.assertEqual(self.feed.latest[0], result)
        self.feed._step()                       # nothing queued: no call
        self.assertEqual(len(self.remote.calls), 1)

    def test_learning_command_and_its_result(self):
        self.remote.replies["learn"] = reply(learned=[{"status": "saved", "name": "Stella"}])
        self.feed.learn("Stella")
        self.feed._step()
        self.assertEqual(self.remote.calls, [("learn", "Stella", False)])
        self.assertEqual(self.learned, [{"status": "saved", "name": "Stella"}])

    def test_speaker_waits_for_an_utterance_in_progress_and_ignores_old_results(self):
        self.assertIsNone(self.feed.speaker(wait=0))
        result = {"level": "maybe", "person_id": "p_1", "name": "Stella"}
        self.feed.in_speech = True

        def finish():
            time.sleep(0.05)
            with self.feed.changed:
                self.feed.latest, self.feed.results, self.feed.in_speech = (result, self.clock()), 1, False
                self.feed.changed.notify_all()
        threading.Thread(target=finish).start()
        self.assertEqual(self.feed.speaker(wait=2), result)
        self.clock.now += 60
        self.assertIsNone(self.feed.speaker(wait=0))

    def test_a_stopped_child_is_restarted_later_not_at_once(self):
        self.remote.fail = True
        self.feed.feed(b"a", False)
        stop = self.feed.stop
        calls = iter([False, True])
        with patch.object(stop, "wait", side_effect=lambda timeout: next(calls)), \
                patch("builtins.print"):
            self.feed._run()
        self.assertIsNone(self.feed.remote)
        self.assertTrue(self.remote.closed)
        self.assertEqual(self.feed.failed_at, self.clock.now)


class FakeIdentifier:
    def __init__(self, known=None, in_view=()):
        self.known = known or {}
        self.in_view = list(in_view)
        self.tool_calls = []
        self.arrivals = []
        self.introductions = []

    def describe_in_view(self):
        return "Stella is in front of you." if self.in_view else "Nobody is in front of your camera right now."

    def describe_visits(self):
        return None

    def present_names(self):
        return list(self.in_view)

    def find_person(self, name):
        return self.known.get(name)

    def handle_tool_call(self, name, args):
        self.tool_calls.append((name, args))
        return {"status": "remembered", "name": args.get("name")}

    def pending_arrival(self):
        return self.arrivals.pop(0) if self.arrivals else None

    def pending_introduction(self):
        return self.introductions.pop(0) if self.introductions else None


class FakeVoice:
    def __init__(self, result=None):
        self.result = result
        self.learning = []

    def speaker(self, wait=1.5):
        return self.result

    def learn(self, name, allow_new=False):
        self.learning.append(name)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.people = People(clock=Clock())

    def test_not_ready_yet(self):
        self.assertIn("error", self.people.execute("who_is_here", {}))

    def test_who_is_here_answers_from_latest_results(self):
        self.people.identifier = FakeIdentifier(in_view=["Stella"])
        self.people.voice = FakeVoice({"level": "sure", "name": "Stella"})
        answer = self.people.execute("who_is_here", {})
        self.assertEqual(answer["in_front_of_you"], "Stella is in front of you.")
        self.assertEqual(answer["talking"], {"speaker": "Stella", "confidence": "sure"})

    def test_identify_speaker(self):
        self.people.identifier = FakeIdentifier()
        self.assertIn("note", self.people.execute("identify_speaker", {}))      # voice off: faces only
        self.people.voice = FakeVoice({"level": "maybe", "name": "Stella"})
        self.assertEqual(self.people.execute("identify_speaker", {})["confidence"], "not sure")
        self.people.voice = FakeVoice(None)
        self.assertIsNone(self.people.execute("identify_speaker", {})["speaker"])

    def test_remember_voice_needs_a_known_face_in_view(self):
        stella = {"id": "p_1", "name": "Stella"}
        self.people.identifier = FakeIdentifier(known={"Stella": stella})
        self.people.voice = voice = FakeVoice()
        self.assertEqual(self.people.execute("remember_voice", {"name": "Bob"})["status"], "not_saved")
        self.assertEqual(self.people.execute("remember_voice", {"name": "Stella"})["status"], "not_saved")
        self.people.identifier.in_view = ["Stella"]
        self.assertEqual(self.people.execute("remember_voice", {"name": "Stella"})["status"], "listening")
        self.assertEqual(voice.learning, ["Stella"])
        stella["voice_embeddings"] = [[0.1]]            # adding to a known voice: no face needed
        self.people.identifier.in_view = []
        self.assertEqual(self.people.execute("remember_voice", {"name": "Stella"})["status"], "listening")

    def test_no_saving_under_a_placeholder_name(self):
        self.people.identifier = identifier = FakeIdentifier(known={"Stella": {"id": "p_1", "name": "Stella"}},
                                                             in_view=["Stella"])
        self.people.voice = voice = FakeVoice()
        for name in ("User", "the user", " someone ", "Unknown.", "", None, "me"):
            with self.subTest(name=name):
                self.assertEqual(self.people.execute("remember_person", {"name": name})["status"], "not_saved")
                self.assertEqual(self.people.execute("remember_voice", {"name": name})["status"], "not_saved")
        self.assertEqual(identifier.tool_calls, [])
        self.assertEqual(voice.learning, [])
        self.people.execute("remember_person", {"name": "Useria"})       # a real name that starts like one
        self.assertEqual(identifier.tool_calls, [("remember_person", {"name": "Useria"})])

    def test_face_tools_go_to_the_identifier(self):
        self.people.identifier = identifier = FakeIdentifier()
        self.people.execute("remember_person", {"name": "Stella"})
        self.assertEqual(identifier.tool_calls, [("remember_person", {"name": "Stella"})])

    def test_voice_results_are_logged_without_words(self):
        from io import StringIO
        out = StringIO()
        with patch("sys.stdout", out):
            people_module.log_voice_result({"level": "maybe", "name": "Stella", "score": 0.31, "stage": "final",
                                            "seconds": 3.2, "ms": 950})
        self.assertEqual(out.getvalue().strip(),
                         "[VOICE] might be Stella (score 0.31, final, 3.2 s of speech, 950 ms)")

    def test_answers_and_prompts(self):
        self.assertEqual(speaker_answer(None)["speaker"], None)
        self.assertEqual(speaker_answer({"level": "unknown"})["speaker"], None)
        self.assertEqual(describe_away(None), None)
        self.assertEqual(describe_away(30 * 3600), "yesterday")
        self.assertEqual(describe_away(5 * 3600), "about 5 hours ago")
        text = arrival_prompt({"name": "Stella", "label": "Stella (Margaret's daughter)", "away": 7200,
                               "last_seen": 1.0, "is_primary": False})
        self.assertTrue(text.startswith("[ARRIVED] Stella (Margaret's daughter) just came into view."))
        self.assertIn("You last saw Stella about 2 hours ago.", text)
        self.assertIn("first time", arrival_prompt({"name": "Stella", "away": None, "last_seen": None}))


class FakeSession:
    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns, turn_complete):
        self.sent.append((turns["parts"][0]["text"], turn_complete))


class SpokenPromptTests(unittest.TestCase):
    def run_task(self, people, seconds, can_speak):
        session, stop = FakeSession(), asyncio.Event()

        async def main():
            task = asyncio.create_task(people.session_task(session, stop))
            await asyncio.sleep(seconds)
            stop.set()
            await task
        with patch.object(people_module.People, "_can_speak", lambda self: can_speak()), \
                patch.object(people_module, "monitor"), patch("builtins.print"), \
                patch.object(people_module.asyncio, "sleep", side_effect=self.fast_sleep):
            asyncio.run(main())
        return session.sent

    _real_sleep = asyncio.sleep

    async def fast_sleep(self, seconds):
        await SpokenPromptTests._real_sleep(0.001)

    def test_arrival_waits_for_a_free_moment_and_is_spoken(self):
        people = People(clock=Clock())
        people.identifier = FakeIdentifier()
        people.identifier.arrivals = [{"name": "Stella", "label": "Stella", "away": None, "last_seen": None}]
        allowed = {"now": False}
        sent = []

        async def main():
            session, stop = FakeSession(), asyncio.Event()
            task = asyncio.create_task(people.session_task(session, stop))
            await asyncio.sleep(0.02)
            self.assertEqual(session.sent, [])            # busy: held
            allowed["now"] = True
            await asyncio.sleep(0.02)
            stop.set()
            await task
            sent.extend(session.sent)
        with patch.object(people_module.People, "_can_speak", lambda self: allowed["now"]), \
                patch.object(people_module, "monitor"), patch("builtins.print"), \
                patch.object(people_module.asyncio, "sleep", side_effect=self.fast_sleep):
            asyncio.run(main())
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("[ARRIVED] Stella just came into view."))
        self.assertIs(sent[0][1], True)

    def test_an_arrival_too_old_to_greet_is_dropped(self):
        clock = Clock()
        people = People(clock=clock)
        people.identifier = FakeIdentifier()
        people.identifier.arrivals = [{"name": "Stella", "label": "Stella", "away": None, "last_seen": None}]

        def can_speak():
            clock.now += people_module.ARRIVAL_WAIT_S + 1   # time passes while Ember is busy
            return False
        self.assertEqual(self.run_task(people, 0.05, can_speak), [])

    def test_introductions_and_learned_voices(self):
        people = People(clock=Clock())
        people.identifier = FakeIdentifier()
        people.identifier.introductions = ["[NEW PERSON] Someone you don't recognize..."]
        people._voice_learned({"status": "saved", "name": "Stella"})
        sent = self.run_task(people, 0.05, lambda: True)
        self.assertEqual(len(sent), 1)                       # one opener per OPENER_GAP_S
        self.assertTrue(sent[0][0].startswith("[VOICE LEARNED] You've learned Stella's voice."))

    def test_can_speak_respects_the_gap_and_disengagement(self):
        clock = Clock()
        people = People(clock=clock)
        with patch("features.toolkits.everyday.tools.available_to_speak", return_value=True), \
                patch.dict(os.environ, {"BAYMAX_PEOPLE_GREETINGS": "1"}):
            self.assertTrue(people._can_speak())
            people.last_opener = clock.now
            self.assertFalse(people._can_speak())
            clock.now += people_module.OPENER_GAP_S
            self.assertTrue(people._can_speak())
            with patch.dict(os.environ, {"BAYMAX_PEOPLE_GREETINGS": "0"}):
                self.assertFalse(people._can_speak())


class RegistrationTests(unittest.TestCase):
    def test_tools_and_instruction_only_when_enabled(self):
        from features.toolkits import gemini_tools
        config = {"system_instruction": "base", "tools": []}
        for flag, expected in (("1", True), ("0", False)):
            with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": flag}):
                result = gemini_tools.extend_config(config)
            names = {d["name"] for tool in result["tools"] for d in tool.get("function_declarations", [])}
            self.assertEqual(people_module.NAMES <= names, expected)
            self.assertEqual("[RECOGNIZING PEOPLE]" in result["system_instruction"], expected)

    def test_execute_routes_to_people(self):
        from features.toolkits import gemini_tools
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "1"}), \
                patch.object(people_module.people, "execute", return_value={"ok": 1}) as execute:
            self.assertEqual(gemini_tools.execute("who_is_here", {}), {"ok": 1})
        execute.assert_called_once_with("who_is_here", {})

    def test_hooks_are_registered_and_cheap(self):
        from features.toolkits.fall_detection import frame_observers, session_tasks
        self.assertIn(people_module.people.observe, frame_observers)
        self.assertIn(people_module.people.session_task, session_tasks)
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "0"}):
            people = People()
            people.observe("frame")                       # off: no thread, nothing kept
            self.assertIsNone(people.thread)
            self.assertIsNone(people.frame)
        people.feed_audio(b"x", False)                    # no voice yet: nothing happens

    def test_mic_callback_feeds_voice_recognition(self):
        source = (ROOT / "realtime_gemini_10.py").read_text()
        start = source.index("def audio_callback(")
        callback = source[start:source.index("NOISE SUPPRESSION DISABLED", start)]
        self.assertIn("_people.feed_audio(raw_bytes, _gemini_speaking or bool(_playback_buffer))", callback)
        self.assertIn('os.environ.setdefault("BAYMAX_PEOPLE_RECOGNITION", "1")', (ROOT / "run_mvp.py").read_text())


if __name__ == "__main__":
    unittest.main()
