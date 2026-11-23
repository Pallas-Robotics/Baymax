"""Robot rig audio/video upgrade, offline: timeline scheduler, fixture checksums, outside
measurement, statistics and gates, virtual-device commands and the video display handshake.

No microphone, camera or speaker is opened, nothing is played, no kernel module is
loaded and nothing contacts Gemini or GitHub. Only localhost HTTP is used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

import numpy as np

from evals.robot import checks, fakes, fixtures, hardware, measure, report, rig, stats, timeline, virtual

R = 48000


def speechlike(seconds, f0, seed, rate=R):
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    syllables = 0.5 * (1 + np.sin(2 * np.pi * 4 * t)) ** 2
    x = sum(np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6)) / k for k in range(1, 8)) * syllables
    return x / np.max(np.abs(x)) * 12000


class Bench:
    def __init__(self, rules=(), ack_delay=0.08):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.clock = fakes.FakeClock()
        self.robot = fakes.FakeRobot(self.path, self.clock, list(rules))
        self.speaker = fakes.FakeSpeaker(self.robot)
        self.display = fakes.FakeDisplay(self.clock, ack_delay)
        self.rig = rig.Rig(self.robot, self.speaker, self.display, self.clock, fakes.FakeSampler(),
                           speak=fakes.fake_speak, log=lambda *_: None)
        self.rig.light, self.rig.faults = fakes.FakeHook(self.clock), {"network_drop": fakes.FakeHook(self.clock)}
        self.rig.fixture_path = lambda name: self.path / name
        self.rig.start_runtime()

    def play(self, scenario, seed=1):
        timeline.validate(scenario)
        player = timeline.Player(self.rig, scenario, seed)
        player.prepare()
        player.run()
        return player

    def close(self):
        self.folder.cleanup()


STORY = {"on": "long story", "do": [{"user": "Tell me a long story."}, {"after": 0.7, "reply": "Once upon a time",
                                                                          "seconds": 20}]}


class TimelineTests(unittest.TestCase):
    def test_validation(self):
        good = {"name": "t", "duration_s": 10, "timeline": [{"at": 0, "id": "a", "say": "hi"},
                                                             {"after": {"cue": "a", "edge": "end", "ms": 200},
                                                              "light": 20}],
                "checks": [{"from": "a", "within_s": 5, "expect": {}}], "measure": [{"latency": "a"}]}
        timeline.validate(good)
        for bad in ({"timeline": [{"at": 0, "say": "x", "light": 3}]},
                    {"timeline": [{"say": "x"}]},
                    {"timeline": [{"at": 0, "after": {"cue": "a"}, "say": "x"}]},
                    {"timeline": [{"after": {"cue": "later"}, "say": "x"}, {"at": 0, "id": "later", "say": "y"}]},
                    {"timeline": [{"at": 0, "fault": "network_drop"}]},
                    {"timeline": [{"at": 0, "light": 300}]},
                    {"timeline": [{"at": 0, "noise": "rain", "seconds": 2}]},
                    {"timeline": [{"at": 0, "say": "x", "sha256": "abc"}]},
                    {"duration_s": 0},
                    {"checks": [{"from": "nope", "within_s": 3}]},
                    {"measure": [{"latency": "nope"}]}):
            scenario = dict(good, **bad)
            with self.assertRaises(timeline.TimelineError, msg=bad):
                timeline.validate(scenario)

    def test_fixed_audio_cues_are_mixed_sample_exactly_and_other_cues_fire_on_time(self):
        bench = Bench()
        try:
            player = bench.play({"name": "t", "duration_s": 30, "timeline": [
                {"at": 0, "noise": "pink", "seconds": 4, "level_db": -10},
                {"at": 1.5, "id": "q", "say": "hello there Ember"},
                {"after": {"cue": "q", "edge": "end", "ms": 500}, "id": "l", "light": 10},
                {"at": 2.0, "show": ["RIG", "CARD"], "id": "c"}]})
        finally:
            bench.close()
        self.assertEqual(len(bench.speaker.played), 1)  # One mixed track, not one play per cue.
        parts = {label: offset for offset, label, _ in bench.speaker.played[0]["parts"]}
        self.assertEqual(parts["hello there Ember"], 1.5)
        log = {entry["id"]: entry for entry in player.log}
        q_seconds = player.by_id["q"]["_seconds"]
        self.assertAlmostEqual(log["l"]["scheduled_s"], 1.5 + q_seconds + 0.5, places=3)
        self.assertLess(abs(log["l"]["late_ms"]), 60)
        self.assertAlmostEqual(log["c"]["late_ms"], 80, delta=60)  # The display's confirmation delay.
        self.assertEqual(bench.rig.light.calls[0][1], 10.0)
        self.assertEqual(player.rig_faults, [])

    def test_barge_in_fires_relative_to_ember_starting_to_speak(self):
        bench = Bench([STORY])
        try:
            player = bench.play({"name": "b", "duration_s": 60, "timeline": [
                {"at": 0, "id": "story", "say": "Tell me a long story please"},
                {"id": "stop", "after": {"state": "speaking", "since": "story", "ms": 800}, "say": "Wait stop"}]})
            speaking = bench.rig.observer.window(bench.robot.status_path and 0).first("speaking")
        finally:
            bench.close()
        stop = next(e for e in player.log if e["id"] == "stop")
        self.assertAlmostEqual(stop["actual_s"] - stop["anchor_s"], 0.8 + timeline.LEAD_S, delta=0.08)
        self.assertGreater(stop["anchor_s"], player.by_id["story"]["_seconds"])
        self.assertIsNotNone(speaking)
        self.assertEqual(len(bench.speaker.played), 2)  # Restarted from the current position with the cue mixed in.
        self.assertEqual(bench.speaker.played[1]["parts"][0][1], "Wait stop")

    def test_an_anchor_that_never_happens_is_a_failure_not_a_hang(self):
        bench = Bench()
        try:
            player = bench.play({"name": "b", "duration_s": 60, "timeline": [
                {"at": 0, "id": "q", "say": "Hello"},
                {"after": {"state": "speaking", "since": "q", "ms": 500, "timeout_s": 10}, "say": "Wait"}]})
        finally:
            bench.close()
        self.assertIn("did not happen", " ".join(player.failures))

    def test_faults_are_always_restored_and_video_is_confirmed(self):
        bench = Bench()
        try:
            player = bench.play({"name": "f", "duration_s": 40, "timeline": [
                {"at": 1, "fault": "network_drop", "seconds": 5, "id": "drop"},
                {"at": 2, "show_video": "fall.mp4", "id": "v"}]})
            calls = bench.rig.faults["network_drop"].calls
        finally:
            bench.close()
        self.assertEqual([c[0] for c in calls], ["start", "stop"])
        self.assertAlmostEqual(calls[1][2] - calls[0][2], 5.0, delta=0.1)
        video = next(e for e in player.log if e["id"] == "v")
        self.assertAlmostEqual(video["late_ms"], 80, delta=60)
        self.assertTrue(bench.display.videos[0].endswith("fall.mp4"))

    def test_display_that_never_confirms_is_a_rig_fault(self):
        bench = Bench(ack_delay=None)
        try:
            player = bench.play({"name": "v", "duration_s": 20, "timeline": [{"at": 0, "show_video": "x.mp4"}]})
        finally:
            bench.close()
        self.assertIn("did not confirm playing", " ".join(player.rig_faults))

    def test_seeds_are_deterministic(self):
        scenario = {"name": "n", "duration_s": 10, "timeline": [{"at": 0, "noise": "pink", "seconds": 2},
                                                                 {"at": 0.5, "say": "hello"}]}
        bench = Bench()
        try:
            digests = []
            for seed in (7, 7, 8):
                player = timeline.Player(bench.rig, scenario, seed)
                digests.append(player.prepare())
        finally:
            bench.close()
        self.assertEqual(digests[0], digests[1])
        self.assertNotEqual(digests[0], digests[2])
        self.assertEqual(timeline.trial_seed(3, "a", 1), timeline.trial_seed(3, "a", 1))
        self.assertNotEqual(timeline.trial_seed(3, "a", 1), timeline.trial_seed(3, "a", 2))


class FixtureTests(unittest.TestCase):
    def test_lock_verify_and_detect_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "fall.mp4").write_bytes(b"clip-1")
            with self.assertRaises(fixtures.FixtureError):
                fixtures.verify(folder, {"fall.mp4": None})  # No manifest yet.
            fixtures.lock(folder)
            digest = fixtures.sha256_file(folder / "fall.mp4")
            self.assertEqual(fixtures.verify(folder, {"fall.mp4": digest}), {"fall.mp4": folder / "fall.mp4"})
            for wanted, message in (({"other.mp4": None}, "not in manifest"),
                                    ({"fall.mp4": "0" * 64}, "pinned"),
                                    ({"../x": None}, "plain file names")):
                with self.assertRaisesRegex(fixtures.FixtureError, message):
                    fixtures.verify(folder, wanted)
            (folder / "fall.mp4").write_bytes(b"clip-2")
            with self.assertRaisesRegex(fixtures.FixtureError, "changed since"):
                fixtures.verify(folder, {"fall.mp4": None})
            (folder / "fall.mp4").unlink()
            with self.assertRaisesRegex(fixtures.FixtureError, "missing"):
                fixtures.verify(folder, {"fall.mp4": None})

    def test_scenario_fixtures_are_collected(self):
        scenario = {"timeline": [{"at": 0, "show_video": "a.mp4", "sha256": "f" * 64}, {"at": 1, "play": "b.wav"}]}
        self.assertEqual(timeline.fixtures_of(scenario), {"a.mp4": "f" * 64, "b.wav": None})


class MeasureTests(unittest.TestCase):
    def room(self, delay, noise=30.0, seed=1, rate=R):
        rng = np.random.default_rng(seed)
        utterance, ember = speechlike(1.6, 180, 2, rate), speechlike(2.0, 230, 3, rate)
        recording = rng.standard_normal(int(rate * 9)) * noise
        # Direct sound, then a diffuse tail from 3 ms (RT60 about 0.4 s, direct-to-reverberant about 0 dB).
        impulse = np.exp(-np.arange(int(0.4 * rate)) / (0.06 * rate)) * rng.standard_normal(int(0.4 * rate)) * 0.02
        impulse[:int(0.003 * rate)] = 0.0
        impulse[0] = 1.0
        at = 1.234 + rng.uniform(0, 0.2)
        heard = np.convolve(utterance * 0.4, impulse)
        recording[int(at * rate):int(at * rate) + len(heard)] += heard
        end = at + measure.speech_bounds(utterance, rate)[1]
        reply = np.convolve(ember * 0.5, impulse)
        start = int((end + delay) * rate)
        if start < len(recording):
            part = recording[start:start + len(reply)]
            part += reply[:len(part)]
        return recording.astype(np.int16), utterance.astype(np.int16), at

    def test_known_delays_are_recovered_within_20_ms(self):
        for delay in (0.45, 0.8, 1.73, 3.1):
            recording, utterance, at = self.room(delay, seed=int(delay * 100))
            result = measure.response_latency(recording, R, utterance, R, hint_s=at + 0.5)
            self.assertNotIn("error", result)
            self.assertAlmostEqual(result["latency_s"], delay, delta=0.02, msg=result)

    def test_noisy_room_and_a_16_khz_recording(self):
        recording, utterance, at = self.room(0.9, noise=300.0, seed=5)
        sixteen = measure.to_rate(recording, R).astype(np.int16)
        result = measure.response_latency(sixteen, 16000, utterance, R, hint_s=at)
        self.assertAlmostEqual(result["latency_s"], 0.9, delta=0.02, msg=result)

    def test_missing_utterance_or_silence_is_reported(self):
        rng = np.random.default_rng(0)
        silence = (rng.standard_normal(R * 5) * 30).astype(np.int16)
        result = measure.response_latency(silence, R, speechlike(1.0, 180, 2).astype(np.int16), R, hint_s=1)
        self.assertIn("not found", result["error"])
        recording, utterance, at = self.room(10.0)  # Ember answers after the recording ends.
        self.assertIn("no speech", measure.response_latency(recording, R, utterance, R, hint_s=at)["error"])

    def test_speech_after_a_video_mark_and_barge_offset(self):
        rng = np.random.default_rng(3)
        recording = rng.standard_normal(R * 10) * 30
        ember = speechlike(4.0, 230, 3)
        recording[int(2.0 * R):int(2.0 * R) + len(ember)] += ember * 0.5
        self.assertAlmostEqual(measure.speech_after(recording, R, 1.0)["latency_s"], 1.0, delta=0.02)
        barge = speechlike(1.0, 150, 9)
        recording[int(3.3 * R):int(3.3 * R) + len(barge)] += barge * 0.6
        result = measure.barge_offset(recording.astype(np.int16), R, barge.astype(np.int16), R, 3.4, 1.0)
        self.assertAlmostEqual(result["offset_s"], 1.3, delta=0.03, msg=result)

    def test_summary_and_stt_hook(self):
        self.assertEqual(measure.summary([1.0, None, 3.0, 2.0])["median_s"], 2.0)
        self.assertIsNone(measure.summary([]))
        self.assertIsNone(measure.transcribe(np.zeros(10, dtype=np.int16), R, None))
        seen = []

        def runner(argv, **kwargs):
            seen.append(argv)
            self.assertTrue(Path(argv[1]).is_file())
            return type("Done", (), {"stdout": " hello \n"})()
        self.assertEqual(measure.transcribe(np.zeros(10, dtype=np.int16), R, ["stt", "{wav}"], runner), "hello")
        self.assertFalse(Path(seen[0][1]).exists())  # The temporary WAV is gone.


def trial(name, outcome):
    result = {"pass": {"steps": [{"failures": []}]}, "product": {"steps": [{"failures": ["x"]}]},
              "error": {"steps": [], "error": "RuntimeExited"}, "rig": {"steps": [], "rig_fault": "screen check"},
              "skip": {"skipped": "n/a"}}[outcome]
    return {"scenario": name, "result": result}


class StatsTests(unittest.TestCase):
    def test_wilson_interval(self):
        low, high = stats.wilson(19, 20)
        self.assertAlmostEqual(low, 0.7639, places=3)
        self.assertAlmostEqual(high, 0.9911, places=3)
        self.assertEqual(stats.wilson(0, 0), (0.0, 1.0))
        self.assertAlmostEqual(stats.wilson(0, 10)[1], 0.2775, places=3)

    def test_classification(self):
        trials = ([trial("a", "pass")] * 3 + [trial("a", "product")] + [trial("b", "product")] * 2
                  + [trial("c", "rig")] + [trial("d", "error")] + [trial("e", "skip")])
        summary = stats.summarize(trials)
        self.assertEqual(summary["a"]["class"], "flaky")
        self.assertEqual(summary["b"]["class"], "product")
        self.assertEqual(summary["c"]["class"], "rig fault")
        self.assertEqual(summary["d"]["class"], "product (1 run)")
        self.assertEqual(summary["e"]["class"], "skipped")
        self.assertEqual(summary["a"]["pass_rate"], 0.75)
        self.assertEqual(stats.failure_class(trials[3], summary), "flaky")
        self.assertEqual(stats.failure_class(trials[4], summary), "product")
        self.assertEqual(stats.failure_class(trials[6], summary), "rig fault")
        self.assertIsNone(stats.failure_class(trials[0], summary))

    def test_gates(self):
        gates = {"default": {"min_pass_rate": 1.0}, "safety": {"min_trials": 20, "min_pass_rate": 0.95}}
        scenarios = [{"name": "fall", "gate": "safety"}, {"name": "chat"}]
        ok = stats.summarize([trial("fall", "pass")] * 19 + [trial("fall", "product")] + [trial("chat", "pass")])
        results = stats.evaluate_gates(ok, scenarios, gates)
        self.assertEqual(results["fall"]["state"], "pass")
        self.assertEqual(stats.verdict(ok, results), "PASS")
        bad = stats.summarize([trial("fall", "pass")] * 18 + [trial("fall", "product")] * 2)
        self.assertEqual(stats.evaluate_gates(bad, scenarios, gates)["fall"]["state"], "fail")
        # Rig faults are not trials: 19 passes + 1 rig fault is too few valid runs.
        short = stats.summarize([trial("fall", "pass")] * 19 + [trial("fall", "rig")])
        results = stats.evaluate_gates(short, scenarios, gates)
        self.assertEqual(results["fall"]["state"], "insufficient")
        self.assertEqual(stats.verdict(short, results), "RIG FAULT")
        few = stats.summarize([trial("fall", "pass")] * 5)
        self.assertEqual(stats.verdict(few, stats.evaluate_gates(few, scenarios, gates)), "INCOMPLETE")
        wilson = stats.check_gate({"valid": 10, "passes": 9}, {"min_wilson_lower": 0.8})
        self.assertEqual(wilson[0], "fail")
        with self.assertRaises(ValueError):
            stats.check_gate({"valid": 1, "passes": 1}, {"min_pass": 1})


class VirtualTests(unittest.TestCase):
    SPEC = virtual.spec_of({"virtual": {"load_modules": True}})

    def test_commands(self):
        load = virtual.load_commands(self.SPEC)
        self.assertEqual(load[0], ["sudo", "-n", "modprobe", "snd-aloop", "enable=1,1", "index=10,11",
                                   "id=EmberRigMic,EmberRigOut", "pcm_substreams=1,1"])
        self.assertEqual(load[1], ["sudo", "-n", "modprobe", "v4l2loopback", "devices=1", "video_nr=42",
                                   "card_label=EmberRigCam", "exclusive_caps=1"])
        self.assertEqual(virtual.unload_commands(self.SPEC, ["v4l2loopback"]),
                         [["sudo", "-n", "modprobe", "-r", "v4l2loopback"]])
        self.assertEqual(virtual.person_feed_command(self.SPEC),
                         ["aplay", "-q", "-D", "plughw:CARD=EmberRigMic,DEV=0", "{wav}"])
        self.assertIn("plughw:CARD=EmberRigOut,DEV=1", virtual.ember_tap_command(self.SPEC))
        video = virtual.camera_command(self.SPEC, "/f/fall.mp4", "video")
        self.assertEqual(video[-3:], ["-f", "v4l2", "/dev/video42"])
        self.assertIn("-re", video)
        self.assertIn("-an", video)
        self.assertIn("-loop", virtual.camera_command(self.SPEC, "/f/c.jpg", "image"))
        self.assertIn("lavfi", virtual.camera_command(self.SPEC, None, "blank"))
        with self.assertRaises(ValueError):
            virtual.camera_command(self.SPEC, None, "hologram")
        with self.assertRaises(virtual.VirtualError):
            virtual.spec_of({})
        self.assertEqual(virtual.spec_of({"virtual": {}})["sudo"], ["sudo", "-n"])

    def test_runtime_env_finds_the_loopback_devices(self):
        hostapis = [{"name": "ALSA"}, {"name": "JACK"}]
        devices = [{"name": "UACDemoV1.0: USB Audio (hw:2,0)", "hostapi": 0, "max_input_channels": 1,
                    "max_output_channels": 2},
                   {"name": "Loopback: PCM (hw:10,0)", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32},
                   {"name": "Loopback: PCM (hw:10,1)", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32},
                   {"name": "Loopback: PCM (hw:11,0)", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32},
                   {"name": "Loopback: PCM (hw:11,0)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 2}]
        env = virtual.runtime_env(self.SPEC, devices, hostapis)
        self.assertEqual((env["BAYMAX_MIC_DEVICE"], env["BAYMAX_MIC_NAME"]), ("2", "Loopback: PCM (hw:10,1)"))
        self.assertEqual((env["BAYMAX_SPEAKER_DEVICE"], env["BAYMAX_SPEAKER_HOSTAPI"]), ("3", "ALSA"))
        self.assertEqual(env["BAYMAX_CAMERA_DEVICE"], "/dev/video42")
        with self.assertRaises(virtual.VirtualError):
            virtual.runtime_env(self.SPEC, devices[:2], hostapis)

    def test_modules_load_only_when_configured_and_unload_only_their_own(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sysmod, dev, asound = root / "sys", root / "dev", root / "asound"
            for path in (sysmod / "v4l2loopback", dev, asound):
                path.mkdir(parents=True)
            ran = []

            def run(command, **kwargs):
                ran.append(command)
                if "snd-aloop" in command:
                    (asound / "EmberRigMic").mkdir(exist_ok=True)
                    (asound / "EmberRigOut").mkdir(exist_ok=True)
                return type("Done", (), {"returncode": 0, "stderr": ""})()
            (dev / "video42").write_text("")
            modules = virtual.Modules(self.SPEC, run, sysmod, dev, asound)
            modules.setup()
            self.assertEqual(len(ran), 1)  # v4l2loopback was already loaded by the operator.
            modules.teardown()
            self.assertEqual(ran[-1], ["sudo", "-n", "modprobe", "-r", "snd_aloop"])
            passive = virtual.Modules(virtual.spec_of({"virtual": {"load_modules": False}}), run, sysmod, root / "nodev", asound)
            ran.clear()
            with self.assertRaisesRegex(virtual.VirtualError, "video42"):
                passive.setup()
            self.assertEqual(ran, [])  # Without load_modules nothing is run.

    def test_virtual_camera_confirms_the_first_frame(self):
        started = []

        class Process:
            def __init__(self, argv, **kwargs):
                started.append(argv)
                self.stdout = io.StringIO("frame=0\nframe=1\nprogress=continue\nframe=2\nprogress=end\n")

            def poll(self):
                return 0

        camera = virtual.VirtualCamera(self.SPEC, Process, clock=lambda: 123.0)
        try:
            version = camera.play_video("/f/fall.mp4")
            for _ in range(100):
                if camera.ack(version, "ended"):
                    break
                __import__("time").sleep(0.01)
            self.assertEqual(camera.ack(version, "playing"), 123.0)
            self.assertIn("lavfi", started[0])  # Starts on a black frame.
            self.assertIn("/f/fall.mp4", started[1])
        finally:
            camera.close()


class HttpDisplayVideoTests(unittest.TestCase):
    def test_video_handshake_range_requests_and_acks(self):
        display = hardware.HttpDisplay("127.0.0.1", 0, clock=lambda: 1000.0)
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "fall.mp4"
            clip.write_bytes(bytes(range(256)) * 4)
            try:
                def get(path, headers=None):
                    request = urllib.request.Request(display.url + path, headers=headers or {})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, response.read(), response.headers

                def post(body):
                    request = urllib.request.Request(display.url + "ack", data=json.dumps(body).encode(),
                                                     method="POST")
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.read()
                self.assertIn(b"requestVideoFrameCallback", get("")[1])
                self.assertEqual(get("time")[1], b"1000.000000")
                with self.assertRaises(Exception):
                    get("clip")  # Nothing is playing.
                version = display.play_video(clip)
                self.assertEqual(json.loads(get("state")[1]), {"version": version, "kind": "video"})
                status, body, headers = get("clip", {"Range": "bytes=10-19"})
                self.assertEqual((status, body, headers["Content-Range"]), (206, bytes(range(10, 20)), "bytes 10-19/1024"))
                self.assertEqual(len(get("clip")[1]), 1024)
                self.assertIsNone(display.ack(version, "playing"))
                post({"version": version, "event": "playing", "at": 1000.25})
                self.assertEqual(display.ack(version, "playing"), 1000.25)
                post({"version": version, "event": "ended", "at": 5.0})  # Clock sync failed: arrival time.
                self.assertEqual(display.ack(version, "ended"), 1000.0)
                with self.assertRaises(Exception):
                    post({"version": "x"})
                with self.assertRaises(RuntimeError):
                    display.play_video(Path(folder) / "notes.txt")
                card = display.show(b"\xff\xd8card")
                self.assertEqual(json.loads(get("state")[1])["kind"], "card")
                self.assertEqual(get("card.jpg")[1], b"\xff\xd8card")
                self.assertGreater(card, version)
            finally:
                display.close()


def reply(on, heard, text, seconds=2.0, extra=(), after=0.5):
    return {"on": on, "do": [{"user": heard, "after": after}, {"reply": text, "seconds": seconds}] + list(extra)}


SCREEN = {"on": "read me the code", "do": [{"user": "Read me the code number."}, {"reply": "It says {display}."}]}
# An ideal robot for every timeline scenario in scenarios.json.
TIMELINE_IDEAL = [
    SCREEN,
    {"on": "fall-forward", "do": [{"after": 4.0, "log": "[FALL] Injecting fall alert into Gemini session..."},
                                  {"after": 1.5, "reply": "Are you okay? Do you need help?"}]},
    STORY,
    {"on": "Wait, stop", "do": [{"user": "Wait, stop for a moment please."},
                                {"state": ["listening", "Reply interrupted; listening"]}]},
    reply("tulips", "What month is best for planting tulips?", "October is a good month to plant tulips."),
    reply("spider", "How many legs does a spider have?", "A spider has eight legs."),
    reply("capital of France", "What is the capital of France?", "Paris."),
    reply("are you there", "Hi Ember, are you there?", "I'm here."),
    reply("getting dark", "Ember, is it getting dark in here?", "It does look a bit dark."),
]
FULL_PROBE = {"core": "realtime_gemini_10.py", "tools": ["look_closely"], "prompt": "",
              "env": {"BAYMAX_INTERRUPTION_ENABLED": "1"},
              "device": {"fall_detection.enabled": True, "camera.pose_tracking": True}}


class NetworkDrop:
    """A fault hook whose start makes the fake runtime lose and regain Gemini."""

    def __init__(self, robot, recover_s=20.0):
        self.robot, self.recover_s, self.calls = robot, recover_s, []

    def start(self):
        self.calls.append("start")
        self.robot.schedule(8.0, {"state": ["reconnecting", "Gemini receive connection needs recovery"]})

    def stop(self):
        self.calls.append("stop")
        self.robot.schedule(self.recover_s, {"state": ["listening", "Connected to Gemini; listening"]})


class FullBench:
    """A rig with every optional part faked: video display, light, fault hook, plug, reference mic."""

    def __init__(self, rules=TIMELINE_IDEAL, recorder=True, broken_mic=False, seed=11):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        (self.path / "features" / "toolkits").mkdir(parents=True)
        (self.path / "features" / "toolkits" / "fall_detection.py").write_text("")
        self.clock = fakes.FakeClock()
        self.robot = fakes.FakeRobot(self.path, self.clock, list(rules))
        self.speaker, self.display = fakes.FakeSpeaker(self.robot), fakes.FakeDisplay(self.clock)
        self.display.robot, self.robot.display = self.robot, self.display
        self.recorder = fakes.FakeRecorder(self.clock, self.speaker, self.robot, broken=broken_mic) if recorder else None
        self.light, self.drop, self.power = fakes.FakeHook(self.clock), NetworkDrop(self.robot), fakes.FakePower()
        clips = {name: self.path / name for name in ("fall-forward-01.mp4", "sit-down-01.mp4")}
        self.messages = []
        self.rig = rig.Rig(self.robot, self.speaker, self.display, self.clock, fakes.FakeSampler(),
                           speak=fakes.fake_speak, log=self.messages.append, light=self.light,
                           faults={"network_drop": self.drop}, recorder=self.recorder, power=self.power,
                           fixture_paths=clips, seed=seed)

    def plan(self, names=None):
        plan, skipped = rig.plan_scenarios(checks.load_scenarios(), FULL_PROBE, self.path, True, names,
                                           dict(checks.FULL_CAPABILITIES, faults={"network_drop"}))
        return plan, skipped

    def close(self):
        self.folder.cleanup()


class RigIntegrationTests(unittest.TestCase):
    def test_every_timeline_scenario_passes_against_an_ideal_robot(self):
        bench = FullBench()
        names = [s["name"] for s in checks.load_scenarios() if "timeline" in s]
        try:
            plan, skipped = bench.plan(names)
            self.assertEqual(skipped, {})
            trials = bench.rig.run_trials(plan)
        finally:
            bench.close()
        problems = {t["scenario"]: [f for st in t["result"]["steps"] for f in st["failures"]]
                    + [t["result"].get("error"), t["result"].get("rig_fault")]
                    for t in trials if report.outcome(t["result"]) != "pass"}
        self.assertEqual(problems, {})
        by_name = {t["scenario"]: t["result"] for t in trials}
        self.assertEqual(by_name["boot-to-ready"]["boot_ready_s"], 42.0)
        self.assertEqual(bench.drop.calls, ["start", "stop"])
        self.assertEqual([c[1] for c in bench.light.calls], [5.0, 100.0])
        fall = {m["kind"]: m for m in by_name["fall-clip-checkin"]["measured"]}["speech after"]
        self.assertAlmostEqual(fall["latency_s"], 4.0 + 1.5 + 0.04 - 3.0, delta=0.05)  # From the clip's fall mark.
        barge = {m["kind"]: m for m in by_name["barge-in-timed"]["measured"]}["barge-in offset"]
        self.assertEqual(barge["intended_ms"], 800)
        self.assertGreater(barge["offset_s"], 0.8)  # The anchor is polled, so the real offset is a little longer.
        self.assertTrue(all(len(t["result"]["stimulus_sha256"]) == 64 for t in trials))

    def test_reference_mic_latency_through_the_rig(self):
        bench = FullBench([reply("how are you", "Hi Ember, how are you?", "Fine.", after=0.7),
                           reply("goldfish", "A name for a goldfish?", "Bubbles.", after=1.3)])
        try:
            plan, _ = bench.plan(["conversation-latency"])
            result = bench.rig.run_trials(plan)[0]["result"]
            played, replies = bench.speaker.played, bench.robot.replies
        finally:
            bench.close()
        speech = [p for p in played if p["label"] != "calibration noise"]
        for step, play, (playback, _) in zip(result["steps"], speech, replies):
            tone = len(fakes.fake_speak(play["label"])) / 48000
            truth = playback - (play["at"] + 0.3 + tone)  # audio.pad adds 0.3 s of silence first.
            self.assertAlmostEqual(step["ref_latency_s"], truth, delta=0.02)
            self.assertAlmostEqual(step["latency_s"], truth, delta=0.05)  # The runtime's own playback time.
        self.assertEqual(bench.recorder.discarded, 1)  # The recording is deleted after the analysis.

    def test_a_deaf_reference_mic_is_a_rig_fault_not_a_product_failure(self):
        bench = FullBench([reply("how are you", "Hi", "Fine."), reply("goldfish", "Goldfish?", "Bubbles.")],
                          broken_mic=True)
        try:
            plan, _ = bench.plan(["conversation-latency"])
            result = bench.rig.run_trials(plan)[0]["result"]
        finally:
            bench.close()
        self.assertIn("reference microphone heard no speech", result["rig_fault"])
        self.assertEqual(report.outcome(result), "rig fault")

    def test_repeats_run_in_a_seeded_random_order(self):
        names = ["noisy-room", "dim-light-conversation", "fall-clip-control"]
        orders = []
        for seed in (3, 3, 4):
            bench = FullBench(seed=seed, recorder=False)
            try:
                plan, _ = bench.plan(names)
                trials = bench.rig.run_trials(plan, repeat=3)
            finally:
                bench.close()
            orders.append([t["scenario"] for t in trials])
        self.assertEqual(len(orders[0]), 9)
        self.assertEqual(orders[0], orders[1])
        self.assertNotEqual(orders[0], orders[2])
        rounds = [orders[0][i:i + 3] for i in (0, 3, 6)]
        self.assertTrue(all(sorted(r) == sorted(names) for r in rounds))
        self.assertGreater(len({tuple(r) for r in rounds}), 1)
        self.assertEqual(stats.summarize(trials)["noisy-room"]["runs"], 3)

    def test_screen_check(self):
        bench = FullBench()
        try:
            bench.rig.start_runtime()
            ok, message = rig.screen_check(bench.rig, seed=5)
            self.assertTrue(ok, message)
            bench.robot.rules = [{"on": "read me the code", "do": [{"user": "code?"}, {"reply": "I can't see it."}]}]
            ok, message = rig.screen_check(bench.rig, seed=5)
            self.assertFalse(ok)
            self.assertIn("did not read", message)
            bench.display.ack_delay = None
            ok, message = rig.screen_check(bench.rig, seed=5)
            self.assertIn("did not confirm", message)
        finally:
            bench.close()

    def test_spoken_digits(self):
        self.assertEqual(rig.spoken_digits("It says forty-eight twenty one."), "4821")
        self.assertEqual(rig.spoken_digits("four eight two one"), "4821")
        self.assertEqual(rig.spoken_digits("The code is 4821"), "4821")
        self.assertEqual(rig.spoken_digits("thirty, oh seven"), "3007")

    def test_capabilities_gate_scenarios(self):
        config = {"display": {"kind": "http"}, "faults": {"network_drop": {}}}
        physical = rig.capabilities_of(config)
        self.assertEqual((physical["video"], physical["light"], physical["power"], physical["mode"]),
                         (True, False, False, "physical"))
        plan, skipped = rig.plan_scenarios(checks.load_scenarios(), FULL_PROBE, Path("/nonexistent"), True,
                                           None, physical)
        self.assertIn("no dimmable light", skipped["dim-light-conversation"]["skipped"])
        self.assertIn("external controller", skipped["boot-to-ready"]["skipped"])
        virtual_mode = rig.capabilities_of({}, virtual_mode=True)
        _, skipped = rig.plan_scenarios(checks.load_scenarios(), FULL_PROBE, Path("/nonexistent"), True, None,
                                        virtual_mode)
        self.assertIn("virtual mode", skipped["echo-own-voice"]["skipped"])
        self.assertIn("fault hooks not configured", skipped["network-drop"]["skipped"])
        off = dict(FULL_PROBE, device={"fall_detection.enabled": False, "camera.pose_tracking": True})
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "features" / "toolkits").mkdir(parents=True)
            (Path(folder) / "features" / "toolkits" / "fall_detection.py").write_text("")
            self.assertIn("device.toml", checks.missing_requirements(
                next(s for s in checks.load_scenarios() if s["name"] == "fall-clip-checkin"), off, Path(folder), True,
                physical))


class RigMainTests(unittest.TestCase):
    """rig.main with fakes: fixtures, repeats and statistics, release gates, virtual mode."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.root = self.base / "branch"
        self.root.mkdir()
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        self.config = self.base / "rig.json"
        self.write_config({})
        self.started = []

    def tearDown(self):
        self.folder.cleanup()

    def write_config(self, extra):
        self.config.write_text(json.dumps(dict({"robot_name": "test-robot-1", "display": {"kind": "http"},
                                                "fixtures_dir": str(self.fixtures),
                                                "gates": {"safety": {"min_trials": 20, "min_pass_rate": 0.95}}},
                                               **extra)))

    def factory(self, config, root, prepared, log_path):
        self.robot = fakes.FakeRobot(root, self.clock, TIMELINE_IDEAL)
        self.robot.log_path = log_path
        display = fakes.FakeDisplay(self.clock)
        display.robot, self.robot.display = self.robot, display
        speaker = fakes.FakeSpeaker(self.robot)
        self.started.append(config)
        return speaker, display, self.robot, fakes.FakeSampler()

    def extras(self, config, prepared):
        return {"light": fakes.FakeHook(self.clock), "faults": {}, "power": None,
                "recorder": None}

    def main(self, *extra):
        self.clock = fakes.FakeClock(start=__import__("time").time())
        argv = ["--root", str(self.root), "--config", str(self.config), "--report-dir", str(self.base / "report"),
                "--yes"] + list(extra)
        with patch.dict(os.environ, {"EMBER_TEST_ROBOT": "1", "GEMINI_TEST_KEY": "rig-test-key"}), \
                patch.object(rig, "run_probe", return_value=FULL_PROBE), \
                patch.object(rig, "make_speak", return_value=fakes.fake_speak), \
                patch.object(rig.signal, "signal"), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = rig.main(argv, hardware_factory=self.factory, clock=self.clock, confirm=lambda _: "n",
                            extras_factory=self.extras)
        return code, out.getvalue() + err.getvalue()

    def saved(self):
        return json.loads((self.base / "report" / "robot-test.json").read_text())

    def test_changed_fixture_is_a_rig_fault_and_nothing_starts(self):
        (self.root / "features" / "toolkits").mkdir(parents=True)
        (self.root / "features" / "toolkits" / "fall_detection.py").write_text("")
        (self.fixtures / "fall-forward-01.mp4").write_bytes(b"clip")
        fixtures.lock(self.fixtures)
        (self.fixtures / "fall-forward-01.mp4").write_bytes(b"edited clip")
        code, out = self.main("--only", "fall-clip-checkin")
        self.assertEqual(code, 1)
        self.assertIn("Ember robot test: RIG FAULT", out)
        self.assertIn("changed since it was locked", out)
        self.assertEqual(self.started, [])

    def test_repeats_report_rates_and_release_gates(self):
        (self.root / "features" / "toolkits").mkdir(parents=True)
        (self.root / "features" / "toolkits" / "fall_detection.py").write_text("")
        for name in ("fall-forward-01.mp4", "sit-down-01.mp4"):
            (self.fixtures / name).write_bytes(name.encode())
        fixtures.lock(self.fixtures)
        code, out = self.main("--only", "noisy-room", "--only", "fall-clip-checkin", "--repeat", "3",
                              "--seed", "9", "--release")
        saved = self.saved()
        self.assertEqual(len(saved["trials"]), 6)
        self.assertEqual(saved["summary"]["noisy-room"]["passes"], 3)
        self.assertIn("| noisy-room | 3 | 3/3 |", out)
        self.assertIn("Screen check: ok", out)
        self.assertIn("Seed 9, 3 round(s) in random order", out)
        # Safety needs 20 valid runs for a release: 3 passes are not enough.
        self.assertIn("insufficient: 3 valid runs, gate needs 20", out)
        self.assertIn("Ember robot test: INCOMPLETE", out)
        self.assertEqual(code, 1)
        self.assertTrue(saved["meta"]["fixtures"])

    def test_virtual_mode_dry_run_prints_commands_and_runs_nothing(self):
        self.write_config({"virtual": {"load_modules": True}})
        with patch.object(rig.subprocess, "run", side_effect=AssertionError("nothing may run")):
            code, out = self.main("--virtual", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("modprobe snd-aloop", out)
        self.assertIn("plughw:CARD=EmberRigMic,DEV=0", out)
        self.assertIn("echo-own-voice: skipped (not meaningful in virtual mode", out)
        self.assertEqual(self.started, [])
        self.write_config({})
        code, out = self.main("--virtual", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("needs a 'virtual' section", out)

    def test_virtual_mode_points_the_runtime_at_the_loopback_devices(self):
        self.write_config({"virtual": {}})
        modules = []

        class Modules:
            def __init__(self, spec):
                self.spec = spec
                modules.append(self)

            def setup(self):
                self.ready = True

            def teardown(self):
                self.down = True
        env = {"BAYMAX_MIC_DEVICE": "7", "BAYMAX_CAMERA_DEVICE": "/dev/video42"}
        seen = {}

        def factory(config, root, prepared, log_path):
            seen["env"] = (root / ".env").read_text()
            seen["speaker"] = config["person_speaker"]["command"]
            return self.factory(config, root, prepared, log_path)
        self.clock = fakes.FakeClock(start=__import__("time").time())
        with patch.dict(os.environ, {"EMBER_TEST_ROBOT": "1", "GEMINI_TEST_KEY": "k"}), \
                patch.object(rig, "run_probe", return_value=FULL_PROBE), \
                patch.object(rig, "make_speak", return_value=fakes.fake_speak), \
                patch.object(rig, "virtual_runtime_env", return_value=env), \
                patch.object(rig.signal, "signal"), contextlib.redirect_stdout(io.StringIO()) as out:
            rig.main(["--root", str(self.root), "--config", str(self.config), "--report-dir",
                      str(self.base / "report"), "--yes", "--virtual", "--only", "conversation-latency"],
                     hardware_factory=factory, clock=self.clock, extras_factory=self.extras,
                     modules_factory=Modules)
        self.assertIn("BAYMAX_MIC_DEVICE=7", seen["env"])
        self.assertIn("BAYMAX_CAMERA_DEVICE=/dev/video42", seen["env"])
        self.assertIn("plughw:CARD=EmberRigMic,DEV=0", seen["speaker"])
        self.assertTrue(modules[0].ready and modules[0].down)
        self.assertIn("not needed in virtual-device mode", out.getvalue())


class RigHardwareHookTests(unittest.TestCase):
    def run_ok(self, calls, code=0):
        def run(argv, **kwargs):
            calls.append(argv)
            return type("Done", (), {"returncode": code, "stderr": "boom"})()
        return run

    def test_light_fault_and_power_commands(self):
        calls = []
        light = hardware.CommandLight(["kasa", "brightness", "{percent}"], 80, run=self.run_ok(calls))
        light.set(4.6)
        light.close()
        self.assertEqual(calls, [["kasa", "brightness", "5"], ["kasa", "brightness", "80"]])
        hook = hardware.CommandHook("network_drop", ["drop", "on"], ["drop", "off"], run=self.run_ok(calls))
        hook.start()
        hook.close()  # Restores an active fault.
        self.assertEqual(calls[-2:], [["drop", "on"], ["drop", "off"]])
        with self.assertRaises(RuntimeError):
            hardware.CommandHook("x", ["a"], ["b"], run=self.run_ok([], code=1)).start()
        with self.assertRaises(ValueError):
            hardware.CommandLight(["kasa"])
        ready = iter([1, 1, 0])

        def power_run(argv, **kwargs):
            calls.append(argv)
            return type("Done", (), {"returncode": next(ready) if argv == ["ready"] else 0})()
        clock = fakes.FakeClock()
        plug = hardware.CommandPower(["off"], ["on"], ["ready"], off_s=5, run=power_run)
        self.assertEqual(plug.cycle(clock), 2.0)

    def test_rig_hardware_from_config_and_ember_mic_is_refused(self):
        parts = hardware.make_rig_hardware({"light": {"command": ["l", "{percent}"]},
                                            "faults": {"network_drop": {"start": ["a"], "stop": ["b"]}},
                                            "power": {"off": ["o"], "on": ["n"], "ready": ["r"]},
                                            "reference_mic": {"command": ["arecord", "-D", "RefMic", "{wav}"]}})
        self.assertIsInstance(parts["light"], hardware.CommandLight)
        self.assertIn("network_drop", parts["faults"])
        self.assertIsNone(parts["power"])  # Only with external_controller: the rig cannot power itself off.
        self.assertIsInstance(parts["recorder"], hardware.CommandRecorder)
        with self.assertRaisesRegex(RuntimeError, "Ember's own microphone"):
            hardware.make_rig_hardware({"reference_mic": {"command": ["arecord", "-D", "UACDemo", "{wav}"]}},
                                       ["UACDemo"])

    def test_command_recorder_keeps_the_recording_private_and_deletes_it(self):
        class Process:
            def __init__(self, argv, **kwargs):
                self.path = Path(argv[-1])
                with __import__("wave").open(str(self.path), "wb") as out:
                    out.setnchannels(2)
                    out.setsampwidth(2)
                    out.setframerate(48000)
                    out.writeframes(np.zeros(9600, dtype=np.int16).tobytes())
                self.code = None

            def poll(self):
                return self.code

            def send_signal(self, sig):
                self.code = 0

            def wait(self, timeout):
                return 0

            def kill(self):
                self.code = -9
        recorder = hardware.CommandRecorder(["arecord", "{wav}"], popen=Process, clock=lambda: 100.0)
        recorder.start()
        folder = recorder.folder
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        samples, rate, started = recorder.stop()
        self.assertEqual((len(samples), rate, started), (4800, 48000, 100.0))  # Stereo mixed to mono.
        recorder.discard()
        self.assertFalse(folder.exists())
        self.assertEqual(recorder.discarded, 1)


if __name__ == "__main__":
    unittest.main()
