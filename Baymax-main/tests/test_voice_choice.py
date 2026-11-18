"""Choosing Ember's voice (features/conversational_ability/voice_choice.py): the tool,
saving the choice, persona.voice taking it, the switch to a fresh session with a recap,
and trying the new voice out. No device, no Gemini."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from features import persona
from features.conversational_ability import voice_choice
from features.conversational_ability.voice_choice import VoiceChoice
from test_public_core_integration import load_function


def voice_of(config):
    return config["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"]


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.file = self.dir / "ember_voice.json"
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BAYMAX_VOICE", None)
        printed = patch("builtins.print")
        printed.start()
        self.addCleanup(printed.stop)

    def vc(self):
        return VoiceChoice(self.file)

    def test_the_usual_voice_until_someone_chooses(self):
        v = self.vc()
        self.assertIsNone(v.chosen)
        self.assertEqual(v.current(), persona.DEFAULT_VOICE)
        os.environ["BAYMAX_VOICE"] = "Puck"
        self.assertEqual(v.current(), "Puck")
        self.assertFalse(v.switch_due())

    def test_choosing_saves_it_and_asks_for_a_switch(self):
        v = self.vc()
        result = v.execute({"voice": "charon"})
        self.assertEqual((result["changed"], result["voice"], result["style"]), (True, "Charon", "informative"))
        self.assertTrue(v.switch_due())
        self.assertEqual(json.loads(self.file.read_text())["voice"], "Charon")
        self.assertEqual(self.vc().current(), "Charon")                  # outlasts a restart

    def test_typos_forgiven_unknown_refused(self):
        v = self.vc()
        self.assertEqual(v.execute({"voice": "Archernar"})["voice"], "Achernar")
        self.assertIn("error", v.execute({"voice": "Darth Vader"}))
        self.assertEqual(v.current(), "Achernar")
        self.assertIn("error", v.execute({}))
        self.assertIn("error", v.execute("Charon"))

    def test_same_voice_is_no_switch(self):
        v = self.vc()
        result = v.execute({"voice": persona.DEFAULT_VOICE})
        self.assertFalse(result["changed"])
        self.assertFalse(v.switch_due())

    def test_default_goes_back_to_the_installers_voice(self):
        os.environ["BAYMAX_VOICE"] = "Kore"
        v = self.vc()
        v.execute({"voice": "Charon"})
        v.start_switch()
        result = v.execute({"voice": "default"})
        self.assertEqual((result["changed"], result["voice"]), (True, "Kore"))
        self.assertIsNone(v.chosen)
        self.assertIsNone(json.loads(self.file.read_text())["voice"])

    def test_status(self):
        v = self.vc()
        v.execute({"voice": "Sulafat"})
        self.assertEqual(v.execute({"voice": "status"}),
                         {"voice": "Sulafat", "style": "warm", "usual": persona.DEFAULT_VOICE})

    def test_unreadable_or_unknown_file_is_ignored(self):
        self.file.write_text("{not json")
        self.assertIsNone(self.vc().chosen)
        self.file.write_text(json.dumps({"voice": "Nobody"}))
        self.assertIsNone(self.vc().chosen)

    def test_unsaved_choice_still_lasts_until_restart(self):
        v = VoiceChoice(self.dir / "missing" / "\0bad" / "ember_voice.json")
        with patch.object(VoiceChoice, "_save", side_effect=OSError("read-only")):
            result = v.execute({"voice": "Orus"})
        self.assertEqual((result["voice"], result["saved"]), ("Orus", False))
        self.assertEqual(v.current(), "Orus")

    def test_persona_voice_takes_the_choice_over_the_env(self):
        base = {"speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}}}
        self.assertEqual(voice_of(persona.voice(base, {"BAYMAX_VOICE": "Kore"}, chosen="Charon")), "Charon")
        self.assertEqual(voice_of(persona.voice(base, {"BAYMAX_VOICE": "Kore"}, chosen="")), "Kore")
        with patch.object(voice_choice.choice, "chosen", "Despina"):
            self.assertEqual(voice_of(persona.voice(base, {"BAYMAX_VOICE": "Kore"})), "Despina")


class CatalogueTests(unittest.TestCase):
    def test_every_prebuilt_voice_once(self):
        grouped = sum(voice_choice.VOICE_GROUPS.values(), [])
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(grouped), persona.VOICES)
        self.assertEqual(set(voice_choice.VOICE_STYLE), persona.VOICES)

    def test_instruction_names_every_voice(self):
        text = voice_choice.instruction()
        for name in persona.VOICES:
            self.assertIn(name, text)
        self.assertIn("change_voice", text)
        self.assertIn("Never change your voice unless someone asks", text)


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        printed = patch("builtins.print")
        printed.start()
        self.addCleanup(printed.stop)
        self.v = VoiceChoice(self.dir / "ember_voice.json", default="Autonoe")

    def test_recap_carries_the_conversation_into_the_fresh_session(self):
        self.v.remember_turn("user", "My granddaughter loves dinosaurs.")
        self.v.remember_turn("gemini", "  A six-year-old  paleontologist!  ")
        self.v.remember_turn("user", "")
        self.v.execute({"voice": "Charon"})
        self.assertEqual(self.v.start_switch(), ("Autonoe", "Charon"))
        self.assertFalse(self.v.switch_due())
        config = self.v.extend_config({"system_instruction": "base", "tools": []})
        self.assertIn("[CONVERSATION SO FAR]", config["system_instruction"])
        self.assertIn("Them: My granddaughter loves dinosaurs.\nYou: A six-year-old paleontologist!",
                      config["system_instruction"])
        self.v.settled()
        self.assertNotIn("[CONVERSATION SO FAR]", self.v.extend_config({"system_instruction": "base"})["system_instruction"])

    def test_recap_keeps_the_latest_turns_within_its_size(self):
        for i in range(40):
            self.v.remember_turn("user", f"turn {i} " + "x" * 600)
        self.v.execute({"voice": "Kore"})
        self.v.start_switch()
        carry = self.v.carry
        self.assertLessEqual(len(carry), voice_choice.RECAP_CHARS + 50)
        self.assertIn("turn 39", carry)
        self.assertNotIn("turn 0 ", carry)

    def test_no_recap_without_a_switch(self):
        self.v.remember_turn("user", "hello")
        config = self.v.extend_config({"system_instruction": "base"})
        self.assertNotIn("[CONVERSATION SO FAR]", config["system_instruction"])
        self.assertIsNone(self.v.start_switch())

    def test_tool_offered_once_and_can_be_switched_off(self):
        once = self.v.extend_config({"system_instruction": "base"})
        names = [d["name"] for tool in once["tools"] for d in tool["function_declarations"]]
        self.assertEqual(names, [voice_choice.NAME])
        self.assertEqual(self.v.extend_config(once)["tools"], once["tools"])
        with patch.dict(os.environ, {"BAYMAX_VOICE_CHOICE": "0"}):
            off = self.v.extend_config({"system_instruction": "base"})
        self.assertNotIn("tools", off)
        self.assertEqual(off["system_instruction"], "base")

    def test_new_voice_is_tried_out_once_then_the_recap_retires(self):
        self.v.execute({"voice": "Charon"})
        self.v.start_switch()
        session = Mock(send_client_content=AsyncMock())
        stop = threading.Event()
        handles = iter([None, None, 10 ** 12])
        with patch.object(voice_choice, "choice", self.v):
            asyncio.run(voice_choice.session_task(session, stop, can_speak=lambda: True, poll=0,
                                                  handle_at=lambda: next(handles)))
            text = session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
            self.assertIn("[VOICE CHANGED]", text)
            self.assertIn("Charon", text)
            self.assertTrue(session.send_client_content.call_args.kwargs["turn_complete"])
            self.assertIsNone(self.v.carry)                       # settled on the new handle
            asyncio.run(voice_choice.session_task(session, stop, can_speak=lambda: True, poll=0))
        self.assertEqual(session.send_client_content.await_count, 1)   # only once

    def test_a_failed_try_out_is_kept_for_the_next_connection(self):
        self.v.execute({"voice": "Charon"})
        self.v.start_switch()
        session = Mock(send_client_content=AsyncMock(side_effect=ConnectionError("gone")))
        with patch.object(voice_choice, "choice", self.v), self.assertRaises(ConnectionError):
            asyncio.run(voice_choice.session_task(session, threading.Event(), can_speak=lambda: True, poll=0))
        self.assertEqual(self.v.take_announcement(), "Charon")


class ToolkitAndRuntimeTests(unittest.TestCase):
    def test_tool_is_offered_and_answered_even_with_the_toolkit_off(self):
        from features.toolkits import gemini_tools
        with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "0"}), \
                patch.object(voice_choice, "choice", Mock(execute=Mock(return_value={"voice": "Kore"}), chosen=None,
                                                          extend_config=voice_choice.choice.extend_config)):
            config = gemini_tools.extend_config({"system_instruction": "base", "tools": []})
            names = [d["name"] for tool in config["tools"] for d in tool.get("function_declarations", [])]
            self.assertIn(voice_choice.NAME, names)
            self.assertIn("[CHANGING YOUR VOICE]", config["system_instruction"])
            self.assertEqual(gemini_tools.execute(voice_choice.NAME, {"voice": "Kore"}), {"voice": "Kore"})

    def test_session_task_is_registered(self):
        from features.toolkits.fall_detection import session_tasks
        self.assertIn(voice_choice.session_task, session_tasks)

    def test_transcripts_feed_the_recap(self):
        fake = Mock()
        cls = load_function("_TurnTranscript", {"_notify_webapp": Mock(),
                                                "voice_choice": Mock(choice=fake)})
        said = cls("user")
        said.add("Hel")
        said.add("lo there")
        said.flush()
        fake.remember_turn.assert_called_once_with("user", "Hello there")

    def test_runtime_moves_to_a_fresh_session_at_a_quiet_moment(self):
        """between_turns: a pending voice change ends this connection once it's quiet,
        after dropping the resume handle (a resumed session keeps its old voice)."""
        class Handover(Exception):
            pass
        choice = Mock(switch_due=Mock(return_value=True), start_switch=Mock(return_value=("Autonoe", "Charon")))
        quiet = {"now": False}
        ns = {"asyncio": asyncio, "time": __import__("time"), "BETWEEN_TURNS_POLL_S": 0,
              "MEMORY_NOTES_ENABLED": False, "live_models": Mock(), "_user_turn": None, "_looked_up": None,
              "_memory_embedder": None, "_recalled": None, "_go_away_at": None,
              "_between_turns": lambda now: quiet["now"], "_handover_due": lambda now: False,
              "_SessionHandover": Handover, "_recall_memories": Mock()}
        between = load_function("between_turns", ns)

        async def run():
            task = asyncio.create_task(between(Mock()))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())                        # not while Ember is talking
            choice.start_switch.assert_not_called()
            quiet["now"] = True
            with self.assertRaises(Handover):
                await asyncio.wait_for(task, 2)

        with patch("features.conversational_ability.session.forget") as forget, patch("builtins.print"), \
                patch.object(voice_choice, "choice", choice):
            asyncio.run(run())
        forget.assert_called_once()
        choice.start_switch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
