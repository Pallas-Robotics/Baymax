"""Ember's voice volume: the 1-10 level, the voice tool, saving it, and the gain applied
to Ember's voice on the speaker callback. No device, no Gemini."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from features.conversational_ability import volume
from features.conversational_ability.volume import GAINS, VoiceVolume
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


def pcm(*values):
    return np.array(values, dtype=np.int16).tobytes()


class VoiceVolumeTests(unittest.TestCase):
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


class FakeMixer:
    """The USB speaker on the Orange Pi 5 Max: PCM, -28.37 to -0.94 dB, at -17.37 dB."""
    card, control = 4, "PCM"

    def __init__(self, db=-17.37, low=-28.37, loud=-0.94, fail=None):
        self.db, self.low_db, self.loud_db, self.fail, self.sets = db, low, loud, fail, []

    def get_db(self):
        if self.fail == "get":
            raise RuntimeError("amixer: no such device")
        return self.db

    def set_db(self, db):
        if self.fail == "set":
            raise RuntimeError("amixer: no such device")
        self.db = min(self.loud_db, max(self.low_db, db))
        self.sets.append(round(self.db, 2))
        return self.db


class SpeakerVolumeTests(VoiceVolumeTests.__base__):
    """With the speaker's own volume (pc_hardware.speaker_mixer) the level moves it."""
    setUp = VoiceVolumeTests.setUp
    vv = VoiceVolumeTests.vv

    def test_its_present_volume_is_the_starting_level_and_nothing_changes(self):
        vv, mixer = self.vv(), FakeMixer()
        vv.use_mixer(mixer)
        self.assertEqual((vv.level, mixer.sets), (5, []))  # -17.37 dB is nearest level 5
        self.assertFalse(self.file.exists())

    def test_levels_move_the_speaker_3_db_each_up_to_its_loudest(self):
        vv, mixer = self.vv(), FakeMixer()
        vv.use_mixer(mixer)
        result = vv.execute({"change": "louder"})
        self.assertEqual((vv.level, mixer.sets[-1], result["speaker_db"]), (6, -12.94, -12.9))
        self.assertEqual(json.loads(self.file.read_text())["level"], 6)
        result = vv.execute({"level": 10})
        self.assertEqual((mixer.sets[-1], result["loudest"]), (-0.94, True))
        self.assertEqual(vv.execute({"change": "louder"})["changed"], False)  # already the loudest
        vv.execute({"change": "default"})
        self.assertEqual(mixer.sets[-1], -9.94)

    def test_the_voice_itself_is_not_scaled_or_limited(self):
        vv = self.vv()
        vv.use_mixer(FakeMixer())
        vv.execute({"level": 10})
        block = pcm(30000, -30000, 1000)
        self.assertEqual(vv.apply(block), block)

    def test_a_saved_or_configured_level_is_set_on_the_speaker_at_start(self):
        self.file.write_text(json.dumps({"level": 8}))
        mixer = FakeMixer()
        self.vv().use_mixer(mixer)
        self.assertEqual(mixer.sets, [-6.94])  # a reboot restores the card's own setting
        self.file.unlink()
        os.environ["BAYMAX_VOICE_VOLUME"] = "9"
        mixer = FakeMixer()
        self.vv().use_mixer(mixer)
        self.assertEqual(mixer.sets, [-3.94])

    def test_the_quietest_level_stays_within_the_speakers_range(self):
        vv, mixer = self.vv(), FakeMixer(low=-20.0)
        vv.use_mixer(mixer)
        vv.execute({"level": 1})
        self.assertEqual(mixer.sets[-1], -20.0)  # never below the card's lowest, never muted

    def test_a_speaker_that_fails_falls_back_to_scaling_the_voice(self):
        vv = self.vv()
        vv.use_mixer(FakeMixer(fail="get"))
        self.assertIsNone(vv.mixer)
        mixer = FakeMixer()
        vv.use_mixer(mixer)
        mixer.fail = "set"
        result = vv.execute({"level": 10})
        self.assertIsNone(vv.mixer)
        self.assertNotIn("speaker_db", result)
        self.assertEqual(vv.level, 10)
        vv.apply(pcm(1000, 1000))  # ramps to the new gain over one block
        self.assertEqual(vv.apply(pcm(1000)), pcm(2000))  # Ember's voice gain does it now (level 10: x2)


class VoiceVolumeWiringTests(unittest.TestCase):
    def test_the_speaker_scales_embers_voice_before_mixing_and_the_echo_reference(self):
        calls = []
        enhancer = Mock(full_duplex=True)
        enhancer.output_latency_ms = 0
        enhancer.mix_output.side_effect = lambda chunk: calls.append(("mix", chunk)) or chunk + b""
        enhancer.rendered.side_effect = lambda chunk, info: calls.append(("rendered", chunk))
        loud = Mock()
        loud.apply.side_effect = lambda chunk: calls.append(("voice", chunk)) or b"\x01\x00" * (len(chunk) // 2)
        namespace = {"_playback_buffer": pcm(5, 5), "_playback_lock": threading.Lock(), "_playback_taken": 0,
                     "_waiting_cue": None,
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
        self.assertIn("radio station or recording keeps its own level", volume.DECLARATION["description"])

    def test_tool_can_be_switched_off(self):
        with patch.dict(os.environ, {"BAYMAX_VOICE_VOLUME_TOOL": "0"}):
            config = {"system_instruction": "base", "tools": []}
            self.assertIs(volume.extend_config(config), config)

    def test_offered_once(self):
        once = volume.extend_config({"system_instruction": "base"})
        self.assertIs(volume.extend_config(once), once)


if __name__ == "__main__":
    unittest.main()
