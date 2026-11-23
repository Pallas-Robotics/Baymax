"""Ember's voice volume: the 1-10 level, the voice tool, saving it, and the gain applied
to Ember's voice on the speaker callback. No device, no Gemini."""
import json
import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

import numpy as np

from features.conversational_ability import volume
from features.conversational_ability.volume import GAINS, VoiceVolume
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


def pcm(*values):
    return np.array(values, dtype=np.int16).tobytes()


class VoiceVolumeTests(unittest.TestCase):
    def test_live_tool_dispatch_changes_saved_level_and_pcm_gain(self):
        from features.toolkits import gemini_tools
        with tempfile.TemporaryDirectory() as directory:
            control = VoiceVolume(Path(directory) / 'voice_volume.json')
            session = AsyncMock()
            with patch.object(volume, 'voice_volume', control):
                for level in [4, 6]:
                    call = SimpleNamespace(id=f'volume-{level}', name=volume.NAME, args={'level': level})
                    asyncio.run(gemini_tools.handle_tool_call(session, SimpleNamespace(function_calls=[call])))
                    response = session.send_tool_response.call_args.kwargs['function_responses'][0]
                    self.assertEqual(response.response['level'], level)
                    self.assertEqual(json.loads(control.path.read_text())['level'], level)
                    block = pcm(*([1000] * 480))
                    control.apply(block)  # Finish the gain ramp before comparing blocks.
                    amplitude = np.frombuffer(control.apply(block),dtype=np.int16).mean()
                    if level == 4:
                        quiet = amplitude
                    else:
                        self.assertGreater(amplitude,quiet * 1.4)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.file = self.dir / "voice_volume.json"
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BAYMAX_VOICE_VOLUME", None)
        printed = patch("builtins.print")
        printed.start()
        self.addCleanup(printed.stop)

    def vv(self):
        return VoiceVolume(self.file)

    def test_usual_level_leaves_the_voice_byte_for_byte_unchanged(self):
        v = self.vv()
        self.assertEqual(v.level, 7)
        chunk = pcm(0, 1, -1, 12000, -32768, 32767)
        self.assertIs(v.apply(chunk), chunk)

    def test_louder_quieter_default_and_exact_levels(self):
        v = self.vv()
        self.assertEqual(v.execute({"change": "louder"})["level"], 8)
        self.assertEqual(v.execute({"change": "quieter"})["level"], 7)
        self.assertEqual(v.execute({"level": 3})["level"], 3)
        self.assertEqual(v.execute({"level": "9"})["level"], 9)
        self.assertEqual(v.execute({"change": "default"})["level"], 7)
        status = v.execute({"change": "status"})
        self.assertEqual((status["level"], status["of"], status["usual"], status["changed"]), (7, 10, 7, False))

    def test_never_beyond_10_and_never_muted(self):
        v = self.vv()
        self.assertEqual(v.execute({"level": 50})["level"], 10)
        result = v.execute({"change": "louder"})
        self.assertEqual((result["level"], result["loudest"], result["changed"]), (10, True, False))
        self.assertEqual(v.execute({"level": 0})["level"], 1)
        self.assertEqual(v.execute({"level": -5})["level"], 1)
        result = v.execute({"change": "quieter"})
        self.assertEqual((result["level"], result["quietest"]), (1, True))
        self.assertGreater(GAINS[1], 0)

    def test_bad_arguments_change_nothing(self):
        v = self.vv()
        for args in ({}, {"change": "mute"}, {"level": "loud"}, {"level": float("nan")}, {"level": True}, "louder", None):
            with self.subTest(args=args):
                self.assertIn("error", v.execute(args))
        self.assertEqual(v.level, 7)
        self.assertFalse(self.file.exists())

    def test_the_level_survives_a_restart(self):
        self.vv().execute({"level": 9})
        self.assertEqual(json.loads(self.file.read_text()), {"level": 9})
        self.assertEqual(self.vv().level, 9)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["voice_volume.json"])  # no temp files left

    def test_env_sets_the_starting_level_until_someone_changes_it(self):
        os.environ["BAYMAX_VOICE_VOLUME"] = "8"
        self.assertEqual(self.vv().level, 8)
        self.vv().execute({"level": 5})
        self.assertEqual(self.vv().level, 5)  # the saved choice wins
        os.environ["BAYMAX_VOICE_VOLUME"] = "loud"
        self.file.unlink()
        self.assertEqual(self.vv().level, 7)

    def test_unreadable_saved_file_falls_back(self):
        for content in ("not json", '{"level": "x"}', "[]"):
            with self.subTest(content=content):
                self.file.write_text(content)
                self.assertEqual(self.vv().level, 7)

    def test_unsavable_level_still_applies_until_restart(self):
        v = VoiceVolume(self.dir / "missing" / "dir" / "voice_volume.json")
        with patch.object(v, "_save", side_effect=PermissionError("read-only")):
            result = v.execute({"change": "louder"})
        self.assertEqual((result["level"], result["saved"]), (8, False))

    def test_gain_scales_the_voice(self):
        v = self.vv()
        v.execute({"level": 3})
        v.apply(pcm(0))  # settle the ramp
        out = np.frombuffer(v.apply(pcm(10000, -10000, 100)), dtype=np.int16)
        np.testing.assert_allclose(out, [4000, -4000, 40], atol=1)

    def test_louder_levels_soft_limit_instead_of_clipping(self):
        v = self.vv()
        v.execute({"level": 10})
        v.apply(pcm(0))
        ramp = np.linspace(-32768, 32767, 4096).astype(np.int16)
        out = np.frombuffer(v.apply(ramp.tobytes()), dtype=np.int16).astype(np.int32)
        self.assertTrue(np.all(np.diff(out) >= 0))  # monotonic: no wrap-around or folding
        self.assertLess(out.max(), 32767)           # never pinned at full scale
        quiet = np.frombuffer(v.apply(pcm(1000)), dtype=np.int16)
        self.assertEqual(int(quiet[0]), 2000)       # quiet speech gets the full +6 dB

    def test_a_level_change_ramps_across_the_next_block(self):
        v = self.vv()
        v.apply(pcm(0))
        v.execute({"level": 1})
        out = np.frombuffer(v.apply(pcm(*([10000] * 1000))), dtype=np.int16)
        self.assertEqual(int(out[0]), 10000)
        self.assertEqual(int(out[-1]), 2000)
        self.assertTrue(np.all(np.diff(out.astype(np.int32)) <= 0))


class VoiceVolumeWiringTests(unittest.TestCase):
    def test_the_speaker_scales_embers_voice_before_mixing_and_the_echo_reference(self):
        calls = []
        enhancer = Mock(full_duplex=True)
        enhancer.output_latency_ms = 0
        enhancer.mix_output.side_effect = lambda chunk: calls.append(("mix", chunk)) or chunk + b""
        enhancer.rendered.side_effect = lambda chunk, info: calls.append(("rendered", chunk))
        loud = Mock()
        loud.apply.side_effect = lambda chunk: calls.append(("voice", chunk)) or b"\x01\x00" * (len(chunk) // 2)
        namespace = {"_playback_buffer": pcm(5, 5), "_playback_lock": threading.Lock(), "_waiting_cue": None,
                     "_audio_enhancer": enhancer, "np": np, "OUTPUT_CHANNELS": 1, "voice_volume": loud,
                     "monitor": Mock(), "RECEIVE_SAMPLE_RATE": 48000}
        with patch("test_public_core_integration.CORE", V10):
            callback = load_function("_output_callback", namespace)
        out = np.empty((4, 1), dtype="int16")
        callback(out, 4, None, None)
        self.assertEqual([c[0] for c in calls], ["voice", "mix", "rendered"])
        self.assertEqual(calls[0][1], pcm(5, 5, 0, 0))     # the queued reply, padded to the block
        self.assertEqual(calls[2][1], b"\x01\x00" * 4)     # the echo reference is what was played

    def test_tool_is_offered_and_answered_even_with_the_toolkit_off(self):
        from features.toolkits import gemini_tools
        with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "0"}), \
                patch.object(volume, "voice_volume", Mock(execute=Mock(return_value={"level": 8}))):
            config = gemini_tools.extend_config({"system_instruction": "base", "tools": []})
            names = [d["name"] for tool in config["tools"] for d in tool.get("function_declarations", [])]
            self.assertIn(volume.NAME, names)
            self.assertIn("[VOICE VOLUME]", config["system_instruction"])
            self.assertEqual(gemini_tools.execute(volume.NAME, {"change": "louder"}), {"level": 8})

    def test_playback_volume_stays_with_the_audiobook_and_listen_tools(self):
        # "Louder" about a book, station or recording is that player's volume, not Ember's voice.
        from features.toolkits import gemini_tools
        with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LISTEN_ENABLED": "1"}):
            config = gemini_tools.extend_config({"system_instruction": "base", "tools": []})
        names = {d["name"] for tool in config["tools"] for d in tool.get("function_declarations", [])}
        self.assertLessEqual({volume.NAME, "audiobook", "listen"}, names)
        self.assertIn("radio station or recording plays", volume.INSTRUCTION)
        self.assertIn("audiobook or listen tool", volume.INSTRUCTION)
        self.assertIn("radio's", volume.DECLARATION["description"])

    def test_tool_can_be_switched_off(self):
        with patch.dict(os.environ, {"BAYMAX_VOICE_VOLUME_TOOL": "0"}):
            config = {"system_instruction": "base", "tools": []}
            self.assertIs(volume.extend_config(config), config)

    def test_offered_once(self):
        once = volume.extend_config({"system_instruction": "base"})
        self.assertIs(volume.extend_config(once), once)


if __name__ == "__main__":
    unittest.main()
