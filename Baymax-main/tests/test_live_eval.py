"""Gemini-in-the-loop harness, offline: real config building, scenario checks, a fake Live session."""
import asyncio
import contextlib
import os
from pathlib import Path
import tempfile
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

    def test_speech_goes_in_the_runtimes_own_mic_blocks(self):
        self.assertEqual(live_eval.CHUNK, 960)          # v10: two whole 10 ms frames at 48 kHz
        self.assertEqual(live_eval.chunk_size(live_eval.ROOT / "realtime_gemini_8.py"), 1024)

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

    def test_notes_sent_between_turns_are_background_that_needs_no_reply(self):
        # Recalled memories (between_turns) and the family's profile update go without
        # turn_complete. The prompt said every bracket-labelled message "always need[s] a
        # response" and only to use memories "naturally", so the note sent mixed signals.
        import device_config
        real = device_config.get
        settings = {"conversation.only_when_addressed": True, "memory.retrieval_enabled": True}
        with patch.object(device_config, "get", lambda key: settings.get(key, real(key))):
            model, config = live_eval.core_session(live_eval.runtime())
        prompt = " ".join(config["system_instruction"].split())
        memory = prompt[prompt.index("[MEMORY CONTEXT]"):prompt.index("[WHEN TO SPEAK]")]
        self.assertIn("Treat them as things you know about them", memory)
        self.assertIn("The note itself needs no reply", memory)
        when = prompt[prompt.index("[WHEN TO SPEAK]"):]
        self.assertIn("always need a response, except background notes such as [MEMORY CONTEXT] or "
                      "[ABOUT THE PERSON], which need no reply of their own", when)

    def test_launcher_defaults_come_from_run_mvp_and_run_pc(self):
        defaults = live_eval.launcher_defaults()
        self.assertEqual(defaults["BAYMAX_WAIT_CUE_ENABLED"], "0")
        self.assertEqual(defaults["BAYMAX_AUDIO_ENHANCEMENT"], "1")


class ScenarioTests(unittest.TestCase):
    def test_shipped_scenarios_are_valid(self):
        scenarios = live_eval.load_scenarios()
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))

    def test_the_hindi_name_scenario_lists_only_spellings_the_address_gate_knows(self):
        # A spelling the scenario hears but the gate doesn't know would pass the eval and still
        # drop Ember's reply on a robot with BAYMAX_ADDRESS_GATE=1.
        from features.conversational_ability import addressing
        shipped = {s["name"]: s for s in live_eval.load_scenarios()}["address-gate-hindi-name"]
        for spelling in shipped["steps"][0]["expect"]["heard"].split("|"):
            self.assertTrue(addressing.names_ember(spelling), spelling)

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


    def test_a_tag_before_the_reply_is_not_checked_as_speech(self):
        # Gemini 2.5 writes "[FALL ALERT]" before its answer in the transcript but doesn't say
        # it, so language-fall-alert-hindi's not_reply failed every correct Hindi reply.
        expect = {"reply": "[\\u0900-\\u097F]{2,}", "not_reply": "fall alert", "max_words": 5}
        for reply in ("[FALL ALERT] अरे! क्या आप ठीक हैं?", " [PRESENCE] [FALL ALERT]  अरे! क्या आप ठीक हैं?"):
            self.assertEqual(live_eval.check(expect, turn(reply=reply)), [], reply)
        # Said out loud, or a tag later in the reply, still counts.
        for reply in ("Fall alert! क्या आप ठीक हैं?", "क्या आप ठीक हैं? [FALL ALERT]"):
            failures = live_eval.check(expect, turn(reply=reply))
            self.assertTrue(any("matches forbidden" in failure for failure in failures), reply)
        self.assertEqual(live_eval.check({}, turn(reply="[FALL ALERT]", audio_bytes=0)), ["empty reply"])

    def test_hindi_replies_count_in_devanagari_romanized_or_urdu_script(self):
        # Replies to the fall alert in a Hindi conversation, as in the live runs of 2026-11-07:
        # the first three are right and failed only on the tag or the script; the last is English.
        alert = {s["name"]: s for s in live_eval.load_scenarios()}["language-fall-alert-hindi"]["steps"][1]["expect"]
        for reply in ("[FALL ALERT] अरे! क्या आप ठीक हैं? क्या आपको किसी मदद की ज़रूरत है?",
                      "[FALL ALERT] Kya aap theek hain? Mujhe lagta hai ki aap gir gayi hain. Kya aapko madad ki "
                      "zaroorat hai?",
                      "[PRESENCE] میں سن رہی ہوں، کیا آپ کچھ بتانا چاہتی ہیں؟"):
            self.assertEqual(live_eval.check(alert, turn(reply=reply)), [], reply)
        self.assertTrue(live_eval.check(alert, turn(reply="Oh no! Are you okay? Do you need any help?")))


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

    def test_a_note_needs_no_reply_and_the_spoken_turn_after_it_is_checked(self):
        # As the runtime gives recalled memories between turns: client content without turn_complete.
        class Session(FakeSession):
            async def send_client_content(self, turns, turn_complete):
                self.texts.append((turns["parts"][0]["text"], turn_complete))
                if turn_complete:
                    await self.queue_reply()
        note = "[MEMORY CONTEXT] Here are things you remember about this user:\n- grows tomatoes"
        scenario = {"name": "memory", "steps": [{"note": note}, {"say": "What do I grow?",
                                                                   "expect": {"reply": "tomato", "max_latency_s": 2}}]}
        session = Session([[server(said="Cherry tomatoes."), server(audio=b"\0" * 8, complete=True)]])
        with patch.object(live_eval, "TRAILING_SILENCE_S", 0.01), patch.object(live_eval, "LEAD_IN_S", 0.01), \
                patch.object(live_eval, "ROOM_NOISE", 0):
            turns = asyncio.run(live_eval.run_scenario(connector(session, []), "m", {}, scenario,
                                                       speak=SPEECH, turn_timeout=3))
        self.assertEqual(session.texts, [(note, False)])
        self.assertEqual(len(turns), 1)                  # the note is not a turn of its own
        self.assertEqual(turns[0]["failures"], [])
        self.assertIsNotNone(turns[0]["latency_s"])
        shipped = {s["name"]: s for s in live_eval.load_scenarios()}["memory-note-then-speech"]
        self.assertEqual([next(k for k in live_eval.STEP_KINDS if k in step) for step in shipped["steps"]],
                         ["say", "note", "say"])
        # The scenario sends the runtime's own note for that memory.
        from test_public_core_integration import load_function
        note = load_function("_memory_context", {})
        self.assertEqual(shipped["steps"][1]["note"],
                         note([{"document": "The user grows cherry tomatoes on the balcony", "distance": 0.2}]))

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


class ModelTests(unittest.TestCase):
    """--model: another Live model, with Ember's config adapted as the runtime adapts it."""
    LATER = "gemini-3.8-live"

    def run_main(self, *argv, models=""):
        """main() up to the Gemini runs, which are recorded instead of made. `models` is
        BAYMAX_LIVE_MODELS (set, so a developer's .env can't change it)."""
        seen = []

        async def run_all(plan, model, config, repeat, real_tools, connect):
            seen.append((model, config))
            return {}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BAYMAX_LIVE_MODELS": models}), \
                patch.object(live_eval, "run_all", run_all), patch("builtins.print") as printed:
            report = Path(folder) / "eval.md"
            result = live_eval.main([*argv, "--only", "greeting", "--yes", "--report", str(report)],
                                    connect=connector(FakeSession([]), []))
            shown = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
            self.assertEqual(result, 0)
            return *seen[0], shown, report.read_text(encoding="utf-8")

    @staticmethod
    def behaviors(config):
        return {d.get("behavior") for tool in config["tools"] for d in tool.get("function_declarations", [])}

    def test_the_model_option_sends_the_config_the_runtime_would_send_that_model(self):
        model, config, shown, report = self.run_main("--model", self.LATER)
        self.assertEqual(model, self.LATER)
        self.assertNotIn("thinking_config", config)
        self.assertEqual(self.behaviors(config), {"BLOCKING"})
        self.assertIn(f"Model: {self.LATER}", shown)   # in what --dry-run and the prompt list
        self.assertIn(f"Model: {self.LATER}", report)

    def test_without_it_the_eval_uses_the_model_the_runtime_connects_with_first(self):
        runtime_model, runtime_config = live_eval.core_session()
        model, config, _, report = self.run_main()
        self.assertEqual(model, runtime_model)  # MODEL, gemini-2.5: its config as the runtime builds it
        self.assertEqual(config["thinking_config"], runtime_config["thinking_config"])
        self.assertEqual(self.behaviors(config), {None})
        self.assertIn(f"Model: {runtime_model}", report)
        model, config, _, _ = self.run_main(models=f"{self.LATER},{runtime_model}")
        self.assertEqual(model, self.LATER)
        self.assertNotIn("thinking_config", config)

    def test_a_branch_from_before_the_model_list_keeps_its_model_and_config(self):
        import sys
        import features.conversational_ability as package
        config = {"thinking_config": {"thinking_budget": 0}}
        with patch.dict(sys.modules, {"features.conversational_ability.live_models": None}), \
                patch.dict(package.__dict__), patch("builtins.print") as printed:
            package.__dict__.pop("live_models", None)  # --root on an older checkout: no live_models.py
            self.assertEqual(live_eval.for_model("m", config), ("m", config))
            printed.assert_not_called()
            self.assertEqual(live_eval.for_model("m", config, self.LATER), (self.LATER, config))
            self.assertIn("not adapted", printed.call_args.args[0])


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
