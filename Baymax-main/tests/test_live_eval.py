"""Gemini-in-the-loop harness, offline: real config building, scenario checks, a fake Live session."""
import asyncio
import contextlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from evals import live_eval
from features import persona


def server(heard=None, said=None, audio=None, complete=False, tool=None):
    if tool is not None:
        call = SimpleNamespace(id="1", name=tool[0], args=tool[1])
        return SimpleNamespace(tool_call=SimpleNamespace(function_calls=[call]), server_content=None)
    parts = [SimpleNamespace(inline_data=SimpleNamespace(mime_type="audio/pcm;rate=24000", data=audio))] if audio else []
    content = SimpleNamespace(
        input_transcription=SimpleNamespace(text=heard) if heard else None,
        output_transcription=SimpleNamespace(text=said) if said else None,
        model_turn=SimpleNamespace(parts=parts) if parts else None, turn_complete=complete)
    return SimpleNamespace(tool_call=None, server_content=content)


class FakeSession:
    """Answers each user turn with a scripted list of server messages."""

    def __init__(self, replies):
        self.replies, self.inbox = list(replies), asyncio.Queue()
        self.audio, self.video, self.texts, self.tool_responses = 0, [], [], []
        self.speaking_done = self.spoke = False

    async def send_realtime_input(self, audio=None, video=None):
        if audio is not None:
            silent = not any(audio.data)
            if not silent:
                self.spoke = True
            elif self.spoke and not self.speaking_done and self.replies:  # The speaker stopped: Gemini answers.
                self.speaking_done = True
                await self.queue_reply()
            self.audio += len(audio.data)
        if video is not None:
            self.video.append(video)

    async def send_client_content(self, turns, turn_complete):
        self.texts.append(turns["parts"][0]["text"])
        await self.queue_reply()

    async def queue_reply(self):
        for message in self.replies.pop(0):
            await self.inbox.put(message)

    async def send_tool_response(self, function_responses):
        self.tool_responses += function_responses
        await self.queue_reply()

    async def receive(self):
        while True:
            yield await self.inbox.get()


def connector(session, seen):
    @contextlib.asynccontextmanager
    async def connect(model, config):
        seen.append((model, config))
        yield session
    return connect


def turn(**fields):
    return {"heard": "", "reply": "", "tools": [], "audio_bytes": 10, "latency_s": 1.0, "timed_out": False, **fields}


SPEECH = lambda text, voice=None: np.full(live_eval.CHUNK, 500, dtype=np.int16)


class ConfigTests(unittest.TestCase):
    def test_builds_the_mvp_config_from_the_runtime_source(self):
        with patch.dict(os.environ, {}, clear=False):
            for name in live_eval.launcher_defaults():
                os.environ.pop(name, None)
            model, config = live_eval.mvp_session()
            self.assertEqual(os.environ["BAYMAX_INTERRUPTION_ENABLED"], "0")  # run_mvp.py default.
            self.assertNotIn("BAYMAX_HARDWARE_PROFILE", os.environ)
        source = live_eval.CORE.read_text(encoding="utf-8")
        self.assertIn(f'MODEL = "{model}"', source)
        self.assertIn(persona.PERSONA, config["system_instruction"])
        # Replies follow the person's language: the instruction reaches Gemini, and no
        # fixed speech language overrides the native-audio model's own detection.
        self.assertIn("[LANGUAGE]\nAlways reply in the language the person is speaking to you.",
                      config["system_instruction"])
        self.assertNotIn("language_code", config.get("speech_config", {}))
        # Nor a transcription language: Hindi is transcribed as Hindi, which the address
        # gate and "Ember disengage" read. The offline pause words stay English.
        self.assertEqual(config["input_audio_transcription"], {})
        self.assertEqual(config["output_audio_transcription"], {})
        self.assertIn('they can say "Hey Ember" or "stop", say those words in English',
                      " ".join(config["system_instruction"].split()))
        self.assertEqual(config["response_modalities"], ["AUDIO"])
        self.assertEqual(config["realtime_input_config"]["activity_handling"], "NO_INTERRUPTION")
        self.assertIn("get_weather", live_eval.declared_tools(config))

    def test_uses_the_runtime_the_launcher_runs(self):
        import tempfile
        from pathlib import Path
        import run_pc
        self.assertEqual(live_eval.CORE, run_pc.RUNTIME)  # realtime_gemini_10.py on the Orange Pi line.
        with tempfile.TemporaryDirectory() as folder:   # An older MVP checkout for --root: v8, no RUNTIME.
            (Path(folder) / "run_pc.py").write_text('runpy.run_path("realtime_gemini_8.py")\n')
            self.assertEqual(live_eval.runtime(folder).name, "realtime_gemini_8.py")
            self.assertEqual(live_eval.runtime(Path(folder) / "missing").name, "realtime_gemini_8.py")

    def test_v10_config_has_its_device_switched_prompt_and_proactive_audio(self):
        # v10 adds these in top-level `if` blocks from device.toml; they reach Gemini on the robot.
        import device_config
        real = device_config.get
        for addressed, memory in ((True, True), (False, False)):
            settings = {"conversation.only_when_addressed": addressed, "memory.retrieval_enabled": memory}
            with patch.object(device_config, "get", lambda key: settings.get(key, real(key))):
                model, config = live_eval.core_session(live_eval.runtime())
            prompt = config["system_instruction"]
            self.assertEqual("[WHEN TO SPEAK]" in prompt, addressed)
            self.assertEqual("[MEMORY CONTEXT]" in prompt, memory)
            self.assertEqual(config.get("proactivity"), {"proactive_audio": True} if addressed else None)
            self.assertTrue(prompt.startswith(persona.PERSONA))
            self.assertIn("session_resumption", config)
        # The v8 runtime still builds for --root on an older MVP checkout.
        model, config = live_eval.core_session(live_eval.ROOT / "realtime_gemini_8.py")
        self.assertIn(persona.PERSONA, config["system_instruction"])

    def test_launcher_defaults_come_from_run_mvp_and_run_pc(self):
        defaults = live_eval.launcher_defaults()
        self.assertEqual(defaults["BAYMAX_WAIT_CUE_ENABLED"], "0")
        self.assertEqual(defaults["BAYMAX_AUDIO_ENHANCEMENT"], "1")


class ScenarioTests(unittest.TestCase):
    def test_shipped_scenarios_are_valid(self):
        scenarios = live_eval.load_scenarios()
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))

    def test_invalid_steps_are_rejected(self):
        for scenario in ({"name": "x", "steps": [{"say": "hi", "text": "hi"}]},
                         {"name": "x", "steps": [{"say": "hi", "expect": {"tool": ["a"]}}]},
                         {"name": "x", "steps": []}):
            with self.assertRaises(ValueError):
                live_eval.validate(scenario)

    def test_feature_scenarios_skip_when_their_pr_is_not_on_the_branch(self):
        config = {"system_instruction": "base", "tools": [{"function_declarations": [{"name": "get_weather"}]}]}
        self.assertIsNone(live_eval.missing_requirements({"requires": {"tools": ["get_weather"]}}, config))
        self.assertIn("brain_game", live_eval.missing_requirements({"requires": {"tools": ["brain_game"]}}, config))
        self.assertIn("ENGAGE", live_eval.missing_requirements({"requires": {"prompt": ["[ENGAGE]"]}}, config))

    def test_checks(self):
        expect = {"heard": "ember", "reply": "hello", "not_reply": "gemini", "tools": ["a"], "not_tools": ["b"],
                  "max_words": 3, "max_latency_s": 2}
        self.assertEqual(live_eval.check(expect, turn(heard="Hi Ember", reply="Hello there", tools=[{"name": "a"}])), [])
        failures = live_eval.check(expect, turn(heard="hi", reply="I am Gemini, not hello at all",
                                                tools=[{"name": "b"}], latency_s=5))
        self.assertEqual(len(failures), 6)
        self.assertEqual(live_eval.check({"no_tools": True}, turn(tools=[{"name": "a"}])),
                         ["expected no tool calls, got ['a']"])
        self.assertEqual(live_eval.check({}, turn(audio_bytes=0, timed_out=True)), ["no reply before timeout"])


class SessionTests(unittest.TestCase):
    def run_scenario(self, scenario, replies):
        session, seen = FakeSession(replies), []
        with patch.object(live_eval, "TRAILING_SILENCE_S", 0.01), patch.object(live_eval, "LEAD_IN_S", 0.01), \
                patch.object(live_eval, "ROOM_NOISE", 0):
            turns = asyncio.run(live_eval.run_scenario(connector(session, seen), "model", {"c": 1}, scenario,
                                                       speak=SPEECH, turn_timeout=3))
        return session, seen, turns

    def test_spoken_turn_with_a_stubbed_tool_call(self):
        scenario = {"name": "weather", "tool_results": {"get_weather": {"temperature": 71}},
                    "steps": [{"say": "Weather?", "expect": {"tools": ["get_weather"], "reply": "71"}}]}
        replies = [[server(heard="What's the weather?"), server(tool=("get_weather", {"units": "fahrenheit"}))],
                   [server(said="It's 71 degrees."), server(audio=b"\0" * 8, complete=True)]]
        session, seen, turns = self.run_scenario(scenario, replies)
        self.assertEqual(seen, [("model", {"c": 1})])
        self.assertEqual(session.tool_responses[0].response, {"temperature": 71})
        self.assertEqual(turns[0]["tools"], [{"name": "get_weather", "args": {"units": "fahrenheit"}}])
        self.assertEqual(turns[0]["heard"], "What's the weather?")
        self.assertEqual(turns[0]["failures"], [])
        self.assertIsNotNone(turns[0]["latency_s"])

    def test_cards_are_sent_as_camera_frames_before_the_question(self):
        scenario = {"name": "card", "steps": [{"card": ["EXAMPLOL 10 MG"]}, {"text": "What does it say?"}]}
        session, _, turns = self.run_scenario(scenario, [[server(said="Examplol."), server(audio=b"\0", complete=True)]])
        self.assertEqual(session.video[0].mime_type, "image/jpeg")
        self.assertEqual(session.texts, ["What does it say?"])
        self.assertEqual(len(turns), 1)

    def test_a_turn_without_a_reply_times_out_and_fails(self):
        scenario = {"name": "silent", "steps": [{"text": "Hello?"}]}
        with patch.object(live_eval, "TURN_TIMEOUT_S", 1):
            session, seen = FakeSession([[]]), []
            turns = asyncio.run(live_eval.run_scenario(connector(session, seen), "m", {}, scenario,
                                                       speak=SPEECH, turn_timeout=1))
        self.assertTrue(turns[0]["timed_out"])
        self.assertEqual(turns[0]["failures"], ["no reply before timeout"])


    def test_a_dropped_session_records_where_it_dropped(self):
        class Dropped(Exception):
            pass

        class DropAfterToolCall(FakeSession):
            async def send_tool_response(self, function_responses):
                raise Dropped("1011")
        scenario = {"name": "r", "steps": [{"text": "Remind me"}]}
        session = DropAfterToolCall([[server(heard="Remind me"), server(tool=("reminder", {}))]])
        results = asyncio.run(live_eval.run_all([scenario], "m", {}, 1, False, connector(session, []),
                                                speak=SPEECH, log=lambda line: None))
        run = results["r"]["runs"][0]
        self.assertIn("Dropped", run["error"])
        self.assertEqual(run["during"]["tools"], ["reminder"])
        self.assertIn("during:", live_eval.summarize(results))


class MainTests(unittest.TestCase):
    def test_dry_run_and_declined_confirmation_send_nothing(self):
        seen = []
        connect = connector(FakeSession([]), seen)
        with patch("builtins.print"):
            self.assertEqual(live_eval.main(["--dry-run", "--only", "greeting"], connect=connect), 0)
            self.assertEqual(live_eval.main(["--only", "greeting"], connect=connect, confirm=lambda prompt: "n"), 1)
        self.assertEqual(seen, [])

    def test_disclosure_lists_what_is_sent(self):
        text = live_eval.disclosure("m", {"system_instruction": "abc", "tools": [{"google_search": {}}]},
                                    [{"name": "s", "steps": [{"say": "Hi Ember"}]}], 2, False)
        self.assertIn("1 scenarios x 2 runs", text)
        self.assertIn("say: Hi Ember", text)
        self.assertIn("Stub tool results", text)

    def test_card_is_a_vga_jpeg(self):
        import cv2
        data = live_eval.card(["EXAMPLOL 10 MG", "TAKE 1 TABLET"])
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (480, 640, 3))

    def test_resample_to_the_robot_rate(self):
        self.assertEqual(len(live_eval.resample(np.ones(16000, dtype=np.int16), 16000)), live_eval.RATE)


if __name__ == "__main__":
    unittest.main()
