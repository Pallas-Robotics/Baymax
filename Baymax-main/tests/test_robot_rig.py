"""Robot test rig (Layer 2), offline: fakes for the speaker, display, runtime and clock.

No microphone, camera or speaker is opened, nothing contacts Gemini or GitHub, and
the only processes started are a tiny stand-in runtime script and the probe.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import urllib.request

import numpy as np

from evals.robot import audio, checks, fakes, hardware, observe, report, rig, safety
from evals.robot import probe as probe_module

ROOT = Path(__file__).resolve().parents[1]
DISENGAGE = {"on": "disengage", "do": [{"user": "Ember, disengage."}, {"reply": "Okay, talk later."},
                                         {"log": "[ENGAGE] Disengaged; only 'Ember engage' is heard."},
                                         {"event": ["engagement", "Disengaged by voice; only 'Ember engage' is heard"]}]}
ENGAGE = {"on": "^Ember engage", "do": [{"log": "[ENGAGE] Engaged (heard 'ember engage')."},
                                        {"event": ["engagement", "Engaged (heard 'ember engage')"]},
                                        {"reply": "I'm here."}]}


def turn(on, heard, reply, tools=(), seconds=2.0, extra=()):
    return {"on": on, "do": [{"user": heard}] + [{"tool": t} for t in tools] + [{"reply": reply, "seconds": seconds}]
            + list(extra)}


# An ideal robot for every scenario in evals/robot/scenarios.json.
IDEAL = [
    DISENGAGE, ENGAGE,
    turn("how are you", "Hi Ember, how are you doing today?", "I'm doing well, thank you!"),
    turn("goldfish", "What is a good name for a goldfish?", "How about Bubbles?"),
    turn("lighthouse", "Tell me a long story about a lighthouse keeper.", "Once there was a keeper...", seconds=40),
    {"on": "Wait, stop", "do": [{"user": "Wait, stop for a moment please."},
                                {"state": ["listening", "Reply interrupted; listening"]}]},
    turn("friendly cat", "Please tell me a short story about a friendly cat.", "A friendly cat named Moss...", seconds=8),
    turn("Remind me", "Remind me in one minute to stretch my legs.", "Okay, in one minute I'll remind you.",
         tools=["reminder"], extra=[{"after": 50, "log": "[ENGAGE] Engaged (reminder due)."},
                                    {"event": ["engagement", "Engaged (reminder due)"]},
                                    {"reply": "Time to stretch your legs!"}]),
    turn("look closely", "Can you look closely and read this label?", "It says Examplol 10 mg, twice daily.",
         tools=["look_closely"]),
    turn("jazz radio", "Play a jazz radio station.", "Playing Smooth Jazz.", tools=["listen"],
         extra=[{"event": ["radio", "Playing Smooth Jazz Test"]}]),
    {"on": r"^Stop\.$", "do": [{"log": "[WAKE] heard stop(0.95,61dB) | narration 52dB -> stop"}, {"reply": "Yes?"}]},
    turn("radio off", "Please turn the radio off now.", "Okay, it's off.", tools=["listen"]),
    turn("Pride and Prejudice", "Please read me Pride and Prejudice.", "Starting Pride and Prejudice.",
         tools=["audiobook"], extra=[{"event": ["audiobook", "Playing Pride and Prejudice"]}]),
    {"on": "^Ember stop", "do": [{"event": ["audiobook", "Paused by 'ember stop'"]}, {"reply": "Yes?"}]},
    turn("stop the book", "Please stop the book for today.", "Okay, stopped.", tools=["audiobook"]),
    turn("favourite colour", "Hi Ember, what is your favourite colour?", "I like blue."),
    turn("in front of you", "What can you see in front of you right now?", "A red apple and a blue cup."),
    turn("two plus two", "What is two plus two?", "Four."),
    turn("Hello again", "Hello again, Ember. Are you there?", "Yes, I'm here."),
    turn("from over here", "Can you hear me from over here?", "Yes, I can hear you."),
]
FULL_PROBE = {"core": "realtime_gemini_8.py", "tools": ["reminder", "look_closely", "listen", "audiobook"],
              "prompt": "... [ENGAGE AND DISENGAGE] ...", "env": {"BAYMAX_INTERRUPTION_ENABLED": "1"}}
BRANCH_FILES = ("features/conversational_ability/engage.py", "features/toolkits/wakeword.py",
                "features/eyes/controller.py")


class Bench:
    """A rig wired to fakes in a temporary folder."""

    def __init__(self, rules=IDEAL, sampler=None, **robot):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        for name in BRANCH_FILES:
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_text("")
        self.clock = fakes.FakeClock()
        robot.setdefault("startup_log", ["[EYES] Ready: expressive eyes; thinking after 2 seconds."])
        self.robot = fakes.FakeRobot(self.path, self.clock, rules, **robot)
        self.speaker, self.display = fakes.FakeSpeaker(self.robot), fakes.FakeDisplay()
        self.messages = []
        self.rig = rig.Rig(self.robot, self.speaker, self.display, self.clock, sampler or fakes.FakeSampler(),
                           speak=fakes.fake_speak, log=self.messages.append)

    def plan(self, names=None, probe=FULL_PROBE):
        scenarios = checks.load_scenarios()
        plan, skipped = rig.plan_scenarios(scenarios, probe, self.path, True, names)
        return plan, skipped

    def run(self, names=None, probe=FULL_PROBE):
        plan, skipped = self.plan(names, probe)
        return self.rig.run_suite(plan, dict(skipped))

    def close(self):
        self.folder.cleanup()


class ScenarioFileTests(unittest.TestCase):
    def test_robot_scenarios_are_valid_and_cover_the_hardware_checks(self):
        names = {s["name"] for s in checks.load_scenarios()}
        for needed in ("conversation-latency", "barge-in", "disengage-tv-engage", "reminder-reengages",
                       "look-closely-card", "radio-stop", "audiobook-stop", "echo-own-voice", "eye-states",
                       "cpu-ceiling", "restart-engaged", "restart-recovers"):
            self.assertIn(needed, names)

    def test_two_minutes_of_tv_and_both_engage_distances(self):
        scenario = next(s for s in checks.load_scenarios() if s["name"] == "disengage-tv-engage")
        noise = [s for s in scenario["steps"] if "noise" in s]
        self.assertEqual((noise[0]["noise"], noise[0]["seconds"], noise[0]["expect"]["max_false_wakes"]), ("tv", 120, 0))
        self.assertEqual([s["distance_m"] for s in scenario["steps"] if s.get("say") == "Ember engage."], [1, 3])

    def test_validation_rejects_bad_scenarios(self):
        for bad in ({"name": "x", "steps": [{"say": "a", "wait": 1}]},
                    {"name": "x", "steps": [{"say": "a", "expect": {"nonsense": 1}}]},
                    {"name": "x", "steps": [{"noise": "rain", "seconds": 3}]},
                    {"name": "x", "steps": [{"noise": "tv"}]},
                    {"name": "x", "requires": {"gpu": True}, "steps": [{"wait": 1}]},
                    {"name": "x", "steps": [{"say": "a", "expect": {"log": "("}}]}):
            with self.assertRaises(Exception):
                checks.validate(bad)

    def test_layer1_expectation_keys_are_reused(self):
        self.assertTrue(checks.LAYER1_KEYS <= checks.EXPECT_KEYS)


class GatingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def missing(self, requires, probe=None, display=True):
        probe = FULL_PROBE if probe is None else probe
        return checks.missing_requirements({"name": "x", "requires": requires, "steps": [{"wait": 1}]}, probe,
                                           self.root, display)

    def test_tools_prompt_files_display_and_env(self):
        self.assertIsNone(self.missing({"tools": ["listen"], "prompt": ["[ENGAGE AND DISENGAGE]"]}))
        self.assertIn("tool not on this branch", self.missing({"tools": ["brain_game"]}))
        self.assertIn("prompt section", self.missing({"prompt": ["[GAMES]"]}))
        self.assertIn("file not on this branch", self.missing({"files": ["features/engage.py"]}))
        (self.root / "here.py").write_text("")
        self.assertIsNone(self.missing({"files": ["here.py"]}))
        self.assertIn("display", self.missing({"display": True}, display=False))
        self.assertIn("none of these tools", self.missing({"any_tools": ["radio", "tv"]}))

    def test_env_uses_branch_value_then_code_default(self):
        mvp = dict(FULL_PROBE, env={"BAYMAX_INTERRUPTION_ENABLED": "0"})
        needs = {"env": {"BAYMAX_INTERRUPTION_ENABLED": "1"}, "env_defaults": {"BAYMAX_INTERRUPTION_ENABLED": "1"}}
        self.assertIn("'0'", self.missing(needs, mvp))
        self.assertIsNone(self.missing(needs, dict(FULL_PROBE, env={})))  # Unset: the code default applies.
        self.assertIn("None", self.missing({"env": {"BAYMAX_X": "1"}}, dict(FULL_PROBE, env={})))

    def test_unreadable_branch_config_skips_instead_of_guessing(self):
        reason = self.missing({"tools": ["listen"]}, {"error": "SyntaxError: bad", "tools": [], "prompt": ""})
        self.assertIn("could not read", reason)

    def test_probe_reads_this_branch_offline(self):
        done = subprocess.run([sys.executable, str(ROOT / "evals" / "robot" / "probe.py"), "--root", str(ROOT)],
                              capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, EMBER_PROBE_ENV_BAYMAX_TEST="1"))
        probe = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", probe, probe.get("error"))
        self.assertEqual(probe["core"], probe_module.runtime_core(ROOT))  # What run_pc.py starts (v10 on main).
        self.assertRegex(probe["core"], r"^realtime_gemini_\d+\.py$")
        self.assertIn("get_weather", probe["tools"])
        self.assertEqual(probe["env"].get("BAYMAX_INTERRUPTION_ENABLED"), "0")  # run_mvp.py's default.
        self.assertEqual(probe["env"].get("BAYMAX_TEST"), "1")
        self.assertFalse(any("KEY" in name for name in probe["env"]))
        plan, skipped = rig.plan_scenarios(checks.load_scenarios(), probe, ROOT, False)
        self.assertIn("barge-in", skipped)  # The MVP turns interruptions off.
        self.assertIn("conversation-latency", [s["name"] for s in plan])


class SuiteTests(unittest.TestCase):
    def test_every_scenario_passes_against_an_ideal_robot(self):
        bench = Bench()
        steps = [s["name"] for s in checks.load_scenarios() if "steps" in s]  # Timelines: test_robot_rig_av.py.
        try:
            results = bench.run(steps)
        finally:
            bench.close()
        failures = {name: [f for s in r.get("steps", []) for f in s["failures"]] + [r.get("error")]
                    for name, r in results.items() if report.outcome(r) != "pass"}
        self.assertEqual(failures, {})
        self.assertEqual(len(results), len(steps))
        latency = results["conversation-latency"]["steps"][0]["latency_s"]
        self.assertTrue(0 < latency < 2, latency)
        self.assertEqual(results["restart-recovers"]["steps"][0]["ready_s"], 3.0)

    def test_played_levels_follow_distance(self):
        bench = Bench()
        try:
            bench.run(["disengage-tv-engage"])
            levels = {p["label"]: p["rms_dbfs"] for p in bench.speaker.played}
            near = [p["rms_dbfs"] for p in bench.speaker.played if p["label"] == "Ember engage."]
        finally:
            bench.close()
        self.assertAlmostEqual(near[0] - near[1], 9.54, delta=0.3)  # 1 m vs 3 m.
        self.assertIn("tv noise 120s", levels)

    def test_a_false_wake_during_tv_fails(self):
        rules = [{"on": "tv noise", "do": [{"after": -60, "user": "members engaged"}, {"reply": "Yes?"}]}] + IDEAL
        bench = Bench(rules)
        try:
            results = bench.run(["disengage-tv-engage"])
        finally:
            bench.close()
        failures = " ".join(results["disengage-tv-engage"]["steps"][1]["failures"])
        self.assertIn("false wakes", failures)
        self.assertIn("nothing should have reached it", failures)

    def test_answering_its_own_voice_fails_the_echo_scenario(self):
        echo = {"on": "friendly cat", "do": [{"user": "Tell me a story about a cat."},
                                              {"reply": "A friendly cat named Moss...", "seconds": 8},
                                              {"after": 2, "user": "named Moss"},
                                              {"event": ["audio", "Possible speaker echo: transcript overlaps"]},
                                              {"after": 7, "reply": "Yes, Moss is a lovely name."}]}
        bench = Bench([echo] + IDEAL)
        try:
            results = bench.run(["echo-own-voice"])
        finally:
            bench.close()
        failures = " ".join(f for s in results["echo-own-voice"]["steps"] for f in s["failures"])
        self.assertIn("runtime flagged echo", failures)
        self.assertIn("own voice", failures)

    def test_slow_reply_fails_latency(self):
        slow = [turn("how are you", "Hi Ember, how are you?", "Fine.", extra=[])]
        slow[0]["do"][1]["after"] = 7
        bench = Bench(slow + IDEAL)
        try:
            results = bench.run(["conversation-latency"])
        finally:
            bench.close()
        self.assertIn("first audio after", results["conversation-latency"]["steps"][0]["failures"][0])

    def test_reminder_that_never_reengages_fails(self):
        rules = [turn("Remind me", "Remind me in one minute to stretch.", "Okay.", tools=["reminder"])] + IDEAL
        bench = Bench(rules)
        try:
            results = bench.run(["reminder-reengages"])
        finally:
            bench.close()
        failures = " ".join(results["reminder-reengages"]["steps"][2]["failures"])
        self.assertIn("wait_for", failures)
        self.assertIn("engaged is False", failures)

    def test_runtime_crash_is_an_error_and_the_next_scenario_gets_a_fresh_runtime(self):
        crash = {"on": "goldfish", "do": [{"exit": 1}]}
        bench = Bench([crash] + IDEAL)
        try:
            results = bench.run(["conversation-latency", "eye-states"])
            starts = bench.robot.starts
        finally:
            bench.close()
        self.assertEqual(report.outcome(results["conversation-latency"]), "error")
        self.assertIn("RuntimeExited", results["conversation-latency"]["error"])
        self.assertEqual(report.outcome(results["eye-states"]), "pass")
        self.assertEqual(starts, 2)

    def test_cpu_ceiling_fails_when_the_runtime_is_busy(self):
        bench = Bench(sampler=fakes.FakeSampler(cpu=95.0, rss=4000))
        try:
            results = bench.run(["cpu-ceiling"])
        finally:
            bench.close()
        failures = " ".join(results["cpu-ceiling"]["steps"][1]["failures"])
        self.assertIn("mean CPU 95%", failures)
        self.assertIn("memory 4000 MB", failures)

    def test_restart_must_come_back_engaged(self):
        sticky = {"on": "two plus two", "do": [{"log": "[ENGAGE] Disengaged; only 'Ember engage' is heard."}]}
        bench = Bench([sticky] + IDEAL)
        try:
            results = bench.run(["restart-engaged"])
        finally:
            bench.close()
        failures = " ".join(results["restart-engaged"]["steps"][2]["failures"])
        self.assertIn("engaged is False", failures)
        self.assertIn("forbidden", failures)

    def test_runtime_that_never_connects_is_a_rig_error(self):
        bench = Bench(ready_after=10_000)
        bench.rig.startup_timeout_s = 20
        try:
            results = bench.run(["conversation-latency"])
        finally:
            bench.close()
        self.assertIn("not listening after 20s", results["conversation-latency"]["error"])

    def test_suite_time_limit_skips_the_rest(self):
        bench = Bench()
        bench.rig.deadline = bench.clock.time() + 60
        try:
            results = bench.run(["conversation-latency", "disengage-tv-engage", "eye-states"])
        finally:
            bench.close()
        self.assertEqual(report.outcome(results["conversation-latency"]), "pass")
        self.assertIn("time limit", results["disengage-tv-engage"]["skipped"])
        self.assertIn("time limit", results["eye-states"]["skipped"])

    def test_display_shows_the_card_and_clears_it(self):
        bench = Bench()
        try:
            bench.run(["look-closely-card"])
            shown, cleared = bench.display.shown, bench.display.cleared
        finally:
            bench.close()
        self.assertEqual(len(shown), 1)
        self.assertTrue(shown[0].startswith(b"\xff\xd8"))  # A JPEG from live_eval.card.
        self.assertGreaterEqual(cleared, 1)


class TimingSourceTests(unittest.TestCase):
    def test_latency_uses_the_runtimes_playback_time_when_reported(self):
        rules = [turn("how are you", "Hi Ember, how are you?", "Fine.")]
        rules[0]["do"][1]["playback_s"] = 0.6  # Audio reaches the speaker 0.6 s after 'speaking'.
        for timing in (True, False):
            bench = Bench(rules + IDEAL, timing=timing)
            try:
                step = bench.run(["conversation-latency"])["conversation-latency"]["steps"][0]
            finally:
                bench.close()
            self.assertEqual(step["latency_source"], "runtime playback time" if timing else "speaking state")
            if timing:
                with_playback = step["latency_s"]
            else:
                without = step["latency_s"]
        self.assertAlmostEqual(with_playback - without, 0.6, delta=0.3)

    # The example in Pallas-Robotics/Baymax#10's features/harness/README.md ("Schema (version 1)").
    HARNESS_EXAMPLE = """\
{"seq":1,"t":5021.3,"wall":1763900000.10,"kind":"trace_start","session":0,"schema":1,"pid":4242,"text_enabled":false}
{"seq":2,"t":5021.9,"wall":1763900000.70,"kind":"connect_start","session":1,"attempt":1,"model":"gemini-…"}
{"seq":4,"t":5022.4,"wall":1763900001.20,"kind":"connected","session":1,"attempt":1}
{"seq":5,"t":5022.4,"wall":1763900001.20,"kind":"state","session":1,"state":"listening"}
{"seq":6,"t":5022.42,"wall":1763900001.22,"kind":"mic_chunk_sent","session":1,"bytes":2048,"queue_ms":0.4,"send_ms":0.2}
{"seq":310,"t":5029.1,"wall":1763900007.90,"kind":"input_transcription","session":1,"chars":19}
{"seq":311,"t":5029.1,"wall":1763900007.90,"kind":"state","session":1,"state":"user_speech"}
{"seq":330,"t":5029.8,"wall":1763900008.60,"kind":"tool_call","session":1,"calls":[{"id":"w1","name":"get_weather"}]}
{"seq":332,"t":5029.8,"wall":1763900008.60,"kind":"tool_start","session":1,"id":"w1","name":"get_weather","arg_keys":["units"],"blocking":true,"limit_s":8.0}
{"seq":333,"t":5030.3,"wall":1763900009.10,"kind":"tool_end","session":1,"id":"w1","name":"get_weather","outcome":"ok","duration_ms":512.7,"error":null}
{"seq":335,"t":5030.3,"wall":1763900009.10,"kind":"tool_response_sent","session":1,"ids":["w1"],"count":1}
{"seq":352,"t":5031.0,"wall":1763900009.80,"kind":"audio_received","session":1,"bytes":9600,"duration_ms":200.0}
{"seq":401,"t":5033.2,"wall":1763900012.00,"kind":"turn_complete","session":1}
"""

    def observer_for(self, folder, text):
        trace = Path(folder) / "trace.jsonl"
        trace.write_text(text)
        clock = fakes.FakeClock(start=1763900013.0)
        observer = observe.Observer(Path(folder) / ".runtime-status.json", Path(folder) / "runtime.log", clock, trace)
        observer.runtime_started(1763900000.0)
        observer.poll()
        return observer, trace

    def test_reads_the_harness_schema_v1_example(self):
        with tempfile.TemporaryDirectory() as folder:
            observer, trace = self.observer_for(folder, self.HARNESS_EXAMPLE)
            window = observer.window(1763900000.0)
            self.assertEqual(window.first("listening"), 1763900001.20)  # The runtime's wall time, not the read time.
            self.assertEqual(window.first("user_speech"), 1763900007.90)
            self.assertIn("trace: tool_end get_weather ok", window.event_lines())
            self.assertIn("trace: turn_complete", window.event_lines())
            self.assertEqual(observer.reply_audio_after(1763900008.0), 1763900009.80)
            self.assertEqual(observer.trace.schema, 1)
            self.assertEqual(observer.trace.counts["mic_chunk_sent"], 1)
            turn = checks.observe_turn(window, speech_end=1763900008.0)  # No conversation_timing in this folder.
            self.assertEqual((turn["latency_s"], turn["latency_source"]), (1.8, "trace reply audio"))
            # Appended lines are read incrementally; a partial line waits; malformed lines are skipped.
            with trace.open("a") as out:
                out.write('not json\n{"kind": "state", "state": "speaking"}\n'
                          '{"seq":402,"t":5034.0,"wall":1763900012.8,"kind":"state","session":1,"state":"spea')
            observer.poll()
            self.assertIsNone(observer.window(1763900012.5).first("speaking"))
            with trace.open("a") as out:
                out.write('king"}\n')
            observer.poll()
            self.assertEqual(observer.window(1763900012.5).first("speaking"), 1763900012.8)

    def test_another_trace_schema_falls_back_to_status_and_log(self):
        with tempfile.TemporaryDirectory() as folder:
            observer, _ = self.observer_for(folder, self.HARNESS_EXAMPLE.replace('"schema":1', '"schema":2'))
            self.assertEqual(observer.trace.schema, 2)
            self.assertFalse(observer.trace.active)
            self.assertEqual(observer.events, [])
            self.assertIsNone(observer.reply_audio_after(0))

    def test_fake_runtime_trace_gives_reply_audio_latency_without_conversation_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            bench = Bench(IDEAL, timing=False, trace_path=Path(folder) / "trace.jsonl")
            bench.rig.observer = observe.Observer(bench.robot.status_path, bench.robot.log_path, bench.clock,
                                                  Path(folder) / "trace.jsonl")
            try:
                step = bench.run(["conversation-latency"])["conversation-latency"]["steps"][0]
            finally:
                bench.close()
        self.assertEqual(step["latency_source"], "trace reply audio")
        self.assertTrue(0 < step["latency_s"] < 2, step["latency_s"])


class CalibrationTests(unittest.TestCase):
    def test_calibration_check_passes_then_detects_drift(self):
        bench = Bench()
        try:
            bench.rig.start_runtime()
            measured = bench.rig.calibration_level()
            self.assertIsNotNone(measured)
            self.assertAlmostEqual(measured, audio.REFERENCE_DBFS - 6.0, delta=1.5)
            ok, message = rig.check_calibration(bench.rig, {"mic_dbfs": measured, "date": "2026-11-04"})
            self.assertTrue(ok, message)
            bench.robot.coupling_db = -16.0  # Speaker turned down or moved.
            ok, message = rig.check_calibration(bench.rig, {"mic_dbfs": measured})
            self.assertFalse(ok)
            self.assertIn("check speaker volume", message)
            self.assertTrue(rig.check_calibration(bench.rig, None)[0])
        finally:
            bench.close()

    def test_calibrate_saves_only_on_target(self):
        bench = Bench()
        try:
            path = bench.path / "calibration.json"
            config = {"calibration_file": str(path)}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(rig.calibrate(bench.rig, config, 66.0, input), 1)
            self.assertIn("gain_db to -6.0", out.getvalue())
            self.assertFalse(path.exists())
            bench.rig.stop_runtime()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rig.calibrate(bench.rig, config, 60.4, input), 0)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["spl_dba"], 60.4)
            self.assertIn("mic_dbfs", saved)
        finally:
            bench.close()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.clock = fakes.FakeClock()
        self.observer = observe.Observer(self.path / "status.json", self.path / "log.txt", self.clock)

    def tearDown(self):
        self.folder.cleanup()

    def status(self, **values):
        (self.path / "status.json").write_text(json.dumps(dict({"heartbeat": self.clock.time()}, **values)))

    def test_merges_events_once_and_ignores_the_previous_session(self):
        now = self.clock.time()
        self.observer.runtime_started(now)
        self.status(heartbeat=now - 30, events=[{"time": now - 30, "state": "speaking", "message": "old"}])
        self.observer.poll()
        self.assertEqual(self.observer.events, [])
        events = [{"time": now + 1, "state": "listening", "message": "Connected to Gemini; listening"}]
        self.status(state="listening", events=events)
        self.observer.poll()
        self.observer.poll()
        self.assertEqual(len(self.observer.events), 1)
        tool = {"id": "tool-1", "time": now + 2, "state": "toolkit", "name": "get_weather", "finished_at": None}
        self.status(state="thinking", events=events + [tool])
        self.observer.poll()
        self.status(state="thinking", events=events + [dict(tool, finished_at=now + 3)])
        self.observer.poll()
        self.assertEqual(self.observer.window(now, now + 10).tools(), ["get_weather"])

    def test_log_lines_are_read_incrementally_including_partial_lines(self):
        log = self.path / "log.txt"
        log.write_text("[USER] hel")
        self.observer.poll()
        self.assertEqual(self.observer.log, [])
        with log.open("a") as stream:
            stream.write("lo\n[MIC LEVEL] rms=820  peak=4000  (max=32767)\n")
        self.observer.poll()
        self.assertEqual([line for _, line in self.observer.log][0], "[USER] hello")
        self.assertEqual(self.observer.window(0).mic_levels(), [820])

    def test_engagement_resets_on_restart(self):
        self.observer.runtime_started(self.clock.time())
        self.observer.log.append((self.clock.time() + 1, "[ENGAGE] Disengaged; only 'Ember engage' is heard."))
        self.assertFalse(self.observer.window(0, self.clock.time() + 2).engaged())
        self.observer.runtime_started(self.clock.time() + 3)
        self.assertTrue(self.observer.window(0, self.clock.time() + 4).engaged())


class AudioTests(unittest.TestCase):
    def test_levels_and_distance(self):
        self.assertAlmostEqual(audio.distance_db(3), -9.54, places=2)
        self.assertEqual(audio.distance_db(1), 0)
        tone = fakes.fake_speak("one two three")
        leveled, clipped = audio.at_level(tone, -10, 2)
        self.assertAlmostEqual(audio.rms_dbfs(leveled), audio.REFERENCE_DBFS - 8, delta=0.2)
        self.assertFalse(clipped)
        self.assertTrue(audio.at_level(tone, 40)[1])

    def test_noises_are_synthetic_and_the_right_length(self):
        for kind in ("pink", "music", "tv"):
            samples = audio.noise(kind, 3, fakes.fake_speak)
            self.assertEqual(len(samples), 3 * audio.RATE)
            self.assertGreater(np.std(samples), 0)
        with self.assertRaises(ValueError):
            audio.noise("radio", 1)
        self.assertTrue(any("members engaged" in line.lower() for line in audio.TV_LINES))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def event(self, **changes):
        event = {"action": "labeled", "label": {"name": "robot-test"}, "sender": {"login": "lead"},
                 "pull_request": {"state": "open", "author_association": "COLLABORATOR",
                                  "head": {"repo": {"full_name": "JoeKojima/Baymax"}}}}
        for key, value in changes.items():
            if key in ("author_association", "state"):
                event["pull_request"][key] = value
            elif key == "head_repo":
                event["pull_request"]["head"]["repo"] = value and {"full_name": value}
            else:
                event[key] = value
        return event

    def allowed(self, event, permission="write"):
        return safety.check_event(event, "JoeKojima/Baymax", lambda login: permission)

    def test_only_collaborators_same_repo_robot_test_label(self):
        self.assertEqual(self.allowed(self.event()), (True, "ok"))
        self.assertIn("forks never run", self.allowed(self.event(head_repo="stranger/Baymax"))[1])
        self.assertIn("forks never run", self.allowed(self.event(head_repo=None))[1])
        self.assertIn("label", self.allowed(self.event(label={"name": "bug"}))[1])
        self.assertIn("label", self.allowed(self.event(action="opened"))[1])
        self.assertIn("not a collaborator", self.allowed(self.event(author_association="CONTRIBUTOR"))[1])
        self.assertIn("only collaborators", self.allowed(self.event(), permission="read")[1])
        self.assertIn("not open", self.allowed(self.event(state="closed"))[1])
        self.assertFalse(safety.check_event(self.event(), "JoeKojima/Baymax", None)[0])

    def test_branch_submissions_need_write_access_and_a_plain_branch(self):
        def submit(permission="write", repo="JoeKojima/Baymax", **inputs):
            event = {"inputs": {"branch": "svanik/weather-direct", **inputs},
                     "repository": {"full_name": repo}, "sender": {"login": "dev"}}
            return safety.check_dispatch(event, "JoeKojima/Baymax", lambda login: permission)
        self.assertEqual(submit(), (True, "ok"))
        self.assertEqual(submit(scenarios="radio-stop,eye-states", pr="27"), (True, "ok"))
        self.assertIn("only collaborators", submit(permission="read")[1])
        for branch in ("refs/pull/9/head", "a/../b", "-x", "", "a b"):
            self.assertIn("branch", submit(branch=branch)[1])
        self.assertIn("scenarios", submit(scenarios="radio; rm -rf /")[1])
        self.assertIn("pr", submit(pr="27x")[1])
        self.assertIn("another repository", submit(repo="stranger/Baymax")[1])

    def test_marker(self):
        self.assertFalse(safety.is_test_robot({}, self.root / "missing"))
        self.assertTrue(safety.is_test_robot({"EMBER_TEST_ROBOT": "1"}, self.root / "missing"))
        (self.root / "test-robot").write_text("")
        self.assertTrue(safety.is_test_robot({}, self.root / "test-robot"))

    def test_guard_cli_refuses_off_the_test_robot(self):
        event = self.root / "event.json"
        event.write_text(json.dumps(self.event()))
        with patch.object(safety, "MARKER", self.root / "missing"), patch.dict(os.environ, {"EMBER_TEST_ROBOT": "0"}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(safety.main(["--event", str(event), "--repository", "JoeKojima/Baymax"]), 1)

    def test_prepared_env_keeps_the_key_out_of_env_and_restores(self):
        base = self.root / "robot.env"
        base.write_text("BAYMAX_MIC_DEVICE=3\nGOOGLE_API_KEY=old-production-key\nBAYMAX_SPEAKER_NAME=Jieli\n")
        (self.root / ".env").write_text("MINE=1\n")
        with safety.PreparedEnv(self.root, base, "test-key-123", {"BAYMAX_X": "1"}) as prepared:
            written = (self.root / ".env").read_text()
            key_file = Path(prepared.values["GOOGLE_API_KEY_FILE"])
            self.assertEqual(key_file.read_text(), "test-key-123")
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("key-123", written)
        self.assertNotIn("old-production-key", written)
        self.assertIn("BAYMAX_MIC_DEVICE=3", written)
        self.assertIn("BAYMAX_AUDIO_RETENTION_DAYS=0.01", written)
        self.assertIn("BAYMAX_VOICE_ANALYSIS=0", written)
        self.assertFalse(key_file.exists())
        self.assertEqual((self.root / ".env").read_text(), "MINE=1\n")
        # The runtime's recordings, transcripts and memories go to a private folder that is removed.
        data = prepared.data_dir
        self.assertIn(f"BAYMAX_DATA_DIR={data}", written)
        self.assertIn(f"EMBER_TRACE={data / 'trace.jsonl'}", written)
        self.assertFalse(data.exists())
        with self.assertRaises(RuntimeError):
            safety.PreparedEnv(self.root, base, "").__enter__()

    def test_recordings_since_the_run_are_deleted(self):
        (self.root / "day_utterance").mkdir()
        old = self.root / "day_utterance" / "seg_old.wav"
        old.write_bytes(b"x")
        os.utime(old, (1_000_000, 1_000_000))
        for name in ("day_utterance/seg_1.wav", "session_1.wav", "transcript_1.txt", "notes.txt"):
            (self.root / name).write_bytes(b"x")
        removed = safety.delete_recordings(self.root, since=2_000_000)
        self.assertEqual(sum(removed.values()), 3)
        self.assertTrue(old.exists())  # Not from this run.
        self.assertTrue((self.root / "notes.txt").exists())
        self.assertEqual(safety.leftover_recordings(self.root, 2_000_000), [])


class ReportTests(unittest.TestCase):
    RESULTS = {"a": {"steps": [{"step": "say: hi", "heard": "hi", "reply": "hello", "tools": [], "states": [],
                                "latency_s": 1.2, "failures": []}]},
               "b": {"steps": [{"step": "say: x", "heard": "", "reply": "", "tools": [], "states": [],
                                "latency_s": None, "failures": ["no reply"]}], "log_tail": ["[USER] x", "[EYES] Ready"]},
               "c": {"skipped": "tool not on this branch: listen"}}

    def test_render(self):
        markdown, verdict = report.render(self.RESULTS, {"commit": "abc", "pr": 7, "recordings_deleted": 4})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("| a | pass | 1.20s |", markdown)
        self.assertIn("| c | skipped |", markdown)
        self.assertIn("### b", markdown)
        self.assertNotIn("### a", markdown)
        self.assertIn("PR #7", markdown)
        self.assertIn("not release approval", markdown)
        self.assertNotIn("[USER] x", markdown)  # Transcript lines stay out of the log excerpt.
        self.assertEqual(report.render({"a": self.RESULTS["a"]}, {})[1], "PASS")
        # A rig problem is inconclusive, not a product failure.
        self.assertEqual(report.render({"a": self.RESULTS["a"]}, {"rig_error": "drift"})[1], "RIG FAULT")
        self.assertEqual(report.render(self.RESULTS, {"rig_error": "drift"})[1], "FAIL")

    def test_post_comment_request(self):
        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            sent.append(request)
            return Response(b'{"html_url": "https://github.com/x/pull/7#c"}')
        url = report.post_comment("x" * 70000, "JoeKojima/Baymax", 7, "token", opener)
        self.assertEqual(url, "https://github.com/x/pull/7#c")
        self.assertEqual(sent[0].full_url, "https://api.github.com/repos/JoeKojima/Baymax/issues/7/comments")
        self.assertLessEqual(len(json.loads(sent[0].data)["body"]), report.MAX_COMMENT)
        with self.assertRaises(RuntimeError):
            report.post_comment("x", "JoeKojima/Baymax", None, "token", opener)

    def test_post_cli_for_the_workflow(self):
        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            sent.append(json.loads(request.data)["body"])
            return Response(b'{"html_url": "u"}')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            path = Path(folder) / "robot-test.md"
            self.assertEqual(report.main(["--post", str(path), "--pr", "7", "--repository", "a/b"], opener), 1)
            path.write_text("## Ember robot test: PASS\n")
            self.assertEqual(report.main(["--post", str(path), "--pr", "7", "--repository", "a/b"], opener), 0)
        self.assertEqual(sent, ["## Ember robot test: PASS\n"])


class MainTests(unittest.TestCase):
    """rig.main end to end with fake hardware: guards, the .env/key lifecycle, cleanup and posting."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "branch"
        self.root.mkdir()
        self.config = Path(self.folder.name) / "rig.json"
        base = Path(self.folder.name) / "robot.env"
        base.write_text("BAYMAX_MIC_DEVICE=1\nBAYMAX_SPEAKER_NAME=Jieli USB\n")
        self.config.write_text(json.dumps({"robot_name": "test-robot-1", "base_env": str(base),
                                           "display": {"kind": "none"}}))
        self.seen = {}

    def tearDown(self):
        self.folder.cleanup()

    def factory(self, config, root, prepared, log_path):
        self.seen["env"] = (root / ".env").read_text()
        self.seen["forbidden"] = safety.ember_speaker_names(prepared.values)
        clock = self.clock

        class Robot(fakes.FakeRobot):
            def start(inner):
                (root / "day_utterance").mkdir(exist_ok=True)
                (root / "day_utterance" / f"seg_{inner.starts}.wav").write_bytes(b"RIFF")
                return super().start()
        robot = Robot(root, clock, IDEAL)
        robot.log_path = log_path
        self.robot = robot
        return fakes.FakeSpeaker(robot), fakes.FakeDisplay(), robot, fakes.FakeSampler()

    def main(self, *extra, env=None, opener=None):
        self.clock = fakes.FakeClock(start=__import__("time").time())
        argv = ["--root", str(self.root), "--config", str(self.config), "--report-dir",
                str(Path(self.folder.name) / "report"), "--only", "conversation-latency"] + list(extra)
        environ = {"EMBER_TEST_ROBOT": "1", "GEMINI_TEST_KEY": "rig-test-key"} if env is None else env
        with patch.dict(os.environ, environ), patch.object(rig, "run_probe", return_value=FULL_PROBE), \
                patch.object(rig, "make_speak", return_value=fakes.fake_speak), \
                patch.object(rig.signal, "signal"), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = rig.main(argv, hardware_factory=self.factory, clock=self.clock, confirm=lambda _: "n",
                            opener=opener)
        return code, out.getvalue()

    def test_full_run_cleans_up_and_reports(self):
        code, out = self.main("--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("GOOGLE_API_KEY_FILE=", self.seen["env"])
        self.assertNotIn("rig-test-key", self.seen["env"])
        self.assertIn("Jieli USB", self.seen["forbidden"])
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual(list((self.root / "day_utterance").glob("*.wav")), [])
        saved = json.loads((Path(self.folder.name) / "report" / "robot-test.json").read_text())
        self.assertEqual(saved["meta"]["recordings_deleted"], 1)
        self.assertEqual(saved["meta"]["calibration"], "not calibrated (levels are relative; run --calibrate)")
        self.assertEqual(report.outcome(saved["results"]["conversation-latency"]), "pass")
        self.assertIn("Ember robot test: PASS", out)
        self.assertFalse(self.robot.alive())

    def test_refuses_without_the_marker_or_confirmation(self):
        with patch.object(safety, "MARKER", Path(self.folder.name) / "missing"):
            code, _ = self.main("--yes", env={"EMBER_TEST_ROBOT": "0", "GEMINI_TEST_KEY": "k"})
        self.assertEqual(code, 2)
        self.assertEqual(self.seen, {})
        code, out = self.main()  # Not --yes, and the operator answers "n".
        self.assertEqual(code, 1)
        self.assertIn("Nothing was started", out)
        self.assertEqual(self.seen, {})

    def test_dry_run_touches_nothing(self):
        with patch.object(safety, "is_test_robot", side_effect=AssertionError("no guard needed")):
            code, out = self.main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.seen, {})
        self.assertIn("conversation-latency", out)

    def test_posts_only_with_the_flag(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise AssertionError("must not post")
        self.main("--yes", "--pr", "7", "--repository", "JoeKojima/Baymax", opener=opener)
        self.assertEqual(calls, [])

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def poster(request, timeout):
            calls.append(request)
            return Response(b'{"html_url": "u"}')
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            code, out = self.main("--yes", "--pr", "7", "--repository", "JoeKojima/Baymax", "--post-comment",
                                  opener=poster)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Posted: u", out)

    def test_cleanup_removes_rig_env_and_recordings_but_signals_only_ember(self):
        (self.root / ".env").write_text("GOOGLE_API_KEY_FILE=/tmp/ember-rig-key-x/key\n")
        (self.root / "day_utterance").mkdir()
        (self.root / "day_utterance" / "seg_1.wav").write_bytes(b"x")
        (self.root / ".runtime-status.json").write_text(json.dumps({"pid": os.getpid(), "state": "listening"}))
        with patch.object(hardware, "other_runtimes", return_value=[]), patch.object(rig.os, "kill") as kill, \
                patch.object(rig.tempfile, "gettempdir", return_value=self.folder.name):
            rig.cleanup(self.root, since=0, log=lambda *_: None)
        kill.assert_not_called()
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual(list((self.root / "day_utterance").iterdir()), [])

    def test_missing_key_is_reported_and_nothing_starts(self):
        code, out = self.main("--yes", env={"EMBER_TEST_ROBOT": "1", "GEMINI_TEST_KEY": ""})
        self.assertEqual(code, 1)
        self.assertEqual(self.seen, {})
        self.assertIn("No Gemini test key", out)


class HardwareTests(unittest.TestCase):
    """Real hardware classes with stand-ins: a fake sounddevice module, localhost HTTP, a dummy process."""

    def test_person_speaker_is_chosen_by_name_and_never_embers_own(self):
        class FakeSD:
            def __init__(self):
                self.played = []

            def query_devices(self):
                return [{"name": "Built-in Mic", "max_output_channels": 0},
                        {"name": "UACDemoV1.0 (Jieli)", "max_output_channels": 2},
                        {"name": "Rig Speaker USB", "max_output_channels": 2},
                        {"name": "Rig Speaker HDMI", "max_output_channels": 2}]

            def play(self, samples, samplerate, device, blocking):
                self.played.append((device, samplerate, len(samples)))

            def stop(self):
                pass
        sd = FakeSD()
        speaker = hardware.SoundDeviceSpeaker("rig speaker usb", sd=sd)
        self.assertEqual(speaker.start(np.zeros(4800, dtype=np.int16)), 0.1)
        self.assertEqual(sd.played, [(2, 48000, 4800)])
        with self.assertRaisesRegex(RuntimeError, "matched 2"):
            hardware.SoundDeviceSpeaker("Rig Speaker", sd=sd)
        with self.assertRaisesRegex(RuntimeError, "Ember's own speaker"):
            hardware.SoundDeviceSpeaker("UACDemo", forbidden=["Jieli"], sd=sd)
        with self.assertRaisesRegex(ValueError, "wav"):
            hardware.CommandSpeaker(["aplay"])

    def test_http_display_serves_the_current_card(self):
        display = hardware.HttpDisplay("127.0.0.1", 0)
        try:
            def get(path):
                with urllib.request.urlopen(display.url + path, timeout=5) as response:
                    return response.read()
            self.assertIn(b"card.jpg", get(""))
            display.show(b"\xff\xd8card")
            self.assertEqual(get("version"), b"1")
            self.assertEqual(get("card.jpg"), b"\xff\xd8card")
            display.clear()
            self.assertEqual(get("card.jpg"), b"")
            with self.assertRaises(Exception):
                get("../.env")
        finally:
            display.close()

    def test_finds_other_runtimes(self):
        listing = textwrap.dedent("""\
            101 /home/ember/.venv/bin/python run_mvp.py
            102 python3 evals/robot/rig.py --root x
            103 /usr/bin/python3 -m http.server
            104 python realtime_gemini_10.py
        """)
        self.assertEqual([pid for pid, _ in hardware.other_runtimes(ps=lambda: listing)], [101, 104])

    def run_dummy(self, honours_stop_file):
        folder = tempfile.TemporaryDirectory()
        root = Path(folder.name)
        (root / "dummy_runtime.py").write_text(textwrap.dedent(f"""\
            import json, os, signal, sys, time
            from pathlib import Path
            if not {honours_stop_file}:
                signal.signal(signal.SIGTERM, lambda *a: sys.exit(3))
            print("[BOOT] dummy", flush=True)
            while True:
                stop = Path(".runtime-stop.json")
                if {honours_stop_file} and stop.exists() and json.loads(stop.read_text())["pid"] == os.getpid():
                    stop.unlink()
                    sys.exit(0)
                time.sleep(0.05)
        """))
        runtime = hardware.ProcessRuntime(root, sys.executable, root / "log.txt", command=["dummy_runtime.py"])
        with patch.object(hardware, "other_runtimes", return_value=[]):
            runtime.start()
        try:
            self.assertTrue(runtime.alive())
            code = runtime.stop(grace_s=1.0 if not honours_stop_file else 10.0, term_s=5.0)
        finally:
            if runtime.alive():
                runtime.process.kill()
            log = (root / "log.txt").read_text()
            folder.cleanup()
        return code, log

    def test_runtime_stops_through_the_stop_file(self):
        code, log = self.run_dummy(True)
        self.assertEqual(code, 0)
        self.assertIn("[BOOT] dummy", log)

    def test_runtime_that_ignores_the_stop_file_gets_sigterm(self):
        self.assertEqual(self.run_dummy(False)[0], 3)

    def test_refuses_when_another_runtime_runs(self):
        runtime = hardware.ProcessRuntime(Path(tempfile.gettempdir()), sys.executable, Path("unused.log"))
        with patch.object(hardware, "other_runtimes", return_value=[(9, "python run_mvp.py")]):
            with self.assertRaisesRegex(RuntimeError, "never stops production"):
                runtime.start()

    def test_runtime_gets_no_tokens_or_test_key(self):
        env = hardware.runtime_environment({"GITHUB_TOKEN": "t", "ACTIONS_RUNTIME_TOKEN": "t", "GH_TOKEN": "t",
                                            "GEMINI_TEST_KEY": "k", "GOOGLE_API_KEY": "k", "PATH": "/bin",
                                            "ACTIONS_ID_TOKEN_REQUEST_URL": "u"})
        self.assertEqual(env, {"PATH": "/bin", "PYTHONUNBUFFERED": "1"})

    def test_sampler_reads_this_process(self):
        sample = hardware.ProcessSampler(os.getpid()).sample()
        self.assertIsNotNone(sample)
        self.assertGreater(sample["rss_mb"], 1)

    def test_sampler_reuses_the_performance_check_on_linux(self):
        performance = hardware.load_performance()
        self.assertIsNotNone(performance)  # devops/performance/check.py of this checkout.

        class Stub:
            def sample(self, pid, previous):
                return ({"process_cpu_percent": 160.0, "rss_mib": 812.34, "cpu_percent": 41.0,
                         "system_used_mib": 2048.0, "temperature_c": 61.25, "npu_percent": None}, {"time": 1})
        sampler = hardware.ProcessSampler(os.getpid(), performance=Stub())
        sampler.cores = 8
        with patch.object(hardware.Path, "is_dir", return_value=True):
            sample = sampler.sample()
        self.assertEqual(sample, {"cpu_percent": 20.0, "rss_mb": 812.3, "system_cpu_percent": 41.0,
                                  "system_used_mib": 2048.0, "temperature_c": 61.2})


if __name__ == "__main__":
    unittest.main()
