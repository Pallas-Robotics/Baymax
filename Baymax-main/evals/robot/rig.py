"""Robot test rig (Layer 2): the real runtime on a dedicated test robot, a scripted "person" on a second speaker.

Starts the branch's real `run_mvp.py` with the robot's own microphone, echo
cancellation, speaker and camera. A second USB speaker about 2 m away plays
synthetic speech and noise, a tablet in front of the camera shows synthetic cards,
and the rig checks what the runtime reports in `.runtime-status.json` and its log.

    python evals/robot/rig.py --root ../pr/Baymax-main --dry-run      # plan and gating; touches nothing
    python evals/robot/rig.py --root ../pr/Baymax-main --yes          # on the marked test robot only
    python evals/robot/rig.py --calibrate --spl 61                    # level calibration (README)
    python evals/robot/rig.py --root ../pr/Baymax-main --cleanup      # stop a leftover runtime, delete recordings
    python evals/robot/rig.py --root ../pr/Baymax-main --yes --repeat 20 --release   # pass rates, release gates
    python evals/robot/rig.py --root ../pr/Baymax-main --yes --virtual --repeat 50   # virtual devices (README)

Refuses to run unless /etc/ember/test-robot exists or EMBER_TEST_ROBOT=1. Posting
the report to the PR needs --post-comment. See evals/robot/README.md.
"""
import argparse
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import live_eval  # noqa: E402
from evals.robot import audio, checks, fixtures, measure, observe, report, safety, stats, timeline  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path(os.getenv("EMBER_RIG_CONFIG", "/etc/ember/robot-rig.json"))
REPORT_DIR = ROOT / ".local-review" / "robot"
POLL_S = 0.25
PAD_S = 0.3                      # audio.pad's silence either side of a line.
SAMPLE_S = 1.0
SAY_TIMEOUT_S = 30.0
SETTLE_S = 2.5
OBSERVE_S = 8.0
SCENARIO_TIMEOUT_S = 600.0
STARTUP_TIMEOUT_S = 180.0
TARGET_SPL_DBA = 60.0            # Conversational speech at 1 m, at the robot's microphone.
CALIBRATION_TOLERANCE_DB = 4.0


class RigError(Exception):
    """A problem with the rig or the runtime process, rather than an expectation."""


class StepTimeout(RigError):
    pass


class SuiteTimeout(RigError):
    pass


class RuntimeExited(RigError):
    pass


class RigFault(RigError):
    """The rig could not test fairly (screen check, fixtures, devices): not a product result."""


class NoHook:
    """Light, fault or power hook that is not configured: scenarios needing it are skipped."""

    def set(self, percent):
        raise RigFault("no light configured")

    def start(self):
        raise RigFault("hook not configured")

    stop = start


class Rig:
    def __init__(self, runtime, speaker, display, clock, sampler, speak=live_eval.synthesize, gain_db=0.0,
                 voice=None, startup_timeout_s=STARTUP_TIMEOUT_S, deadline=None, log=print, scenario_dir=HERE,
                 light=None, faults=None, recorder=None, power=None, fixture_paths=None, seed=0):
        self.runtime, self.speaker, self.display, self.clock = runtime, speaker, display, clock
        self.sampler_factory, self.sampler, self.speak = sampler, None, speak
        self.gain_db, self.voice, self.startup_timeout_s = gain_db, voice, startup_timeout_s
        self.deadline, self.scenario_deadline, self.log = deadline, None, log
        self.scenario_start = clock.time()
        self.scenario_dir = scenario_dir
        self.observer = observe.Observer(runtime.status_path, runtime.log_path, clock,
                                         getattr(runtime, "trace_path", None))
        self.last_sample = 0.0
        self.clipped = []
        # Optional rig hardware (README): smart bulb, fault hooks, reference microphone, smart plug.
        self.light, self.faults, self.recorder, self.power = light or NoHook(), dict(faults or {}), recorder, power
        self.fixture_paths, self.seed, self.round = dict(fixture_paths or {}), seed, 0
        self.played = []               # [{"id", "start" (rig time), "samples"}] this scenario's person audio
        self.step_index = 0

    # ── Time ─────────────────────────────────────────────────────────────────
    def tick(self):
        self.observer.poll()
        now = self.clock.time()
        if self.sampler is not None and now - self.last_sample >= SAMPLE_S:
            self.observer.sample(self.sampler.sample())
            self.last_sample = now
        if not self.runtime.alive():
            raise RuntimeExited(f"runtime exited with code {self.runtime.exit_code()}")
        if self.deadline is not None and now > self.deadline:
            raise SuiteTimeout("suite time limit reached")
        if self.scenario_deadline is not None and now > self.scenario_deadline:
            raise StepTimeout("scenario time limit reached")

    def pump(self, seconds):
        end = self.clock.time() + seconds
        while True:
            self.tick()
            left = end - self.clock.time()
            if left <= 0:
                return
            self.clock.sleep(min(POLL_S, left))

    def pump_until(self, predicate, timeout):
        end = self.clock.time() + timeout
        while True:
            self.tick()
            if predicate():
                return True
            if self.clock.time() >= end:
                return False
            self.clock.sleep(POLL_S)

    # ── Runtime ──────────────────────────────────────────────────────────────
    def start_runtime(self):
        started = self.runtime.start()
        self.observer.runtime_started(started)
        self.sampler = self.sampler_factory(self.runtime.pid)
        self.last_sample = 0.0
        self.log(f"[RIG] Runtime started (pid {self.runtime.pid}); waiting for Gemini to connect")
        if not self.pump_until(lambda: self.observer.window(started).first("listening") is not None,
                               self.startup_timeout_s):
            raise RigError(f"runtime not listening after {self.startup_timeout_s:.0f}s")
        ready = round(self.observer.window(started).first("listening") - started, 1)
        self.log(f"[RIG] Runtime ready after {ready}s")
        return ready

    def stop_runtime(self):
        code = self.runtime.stop()
        self.sampler = None
        self.observer.poll()
        return code

    def quiet(self, seconds=2.0):
        """Wait until the runtime is listening and nothing changed for `seconds` (max 30 s)."""
        def calm():
            polled = self.observer.polled
            return bool(polled) and polled[-1][1] == "listening" and self.clock.time() - self.observer.last_change >= seconds
        return self.pump_until(calm, 30.0)

    # ── Steps ────────────────────────────────────────────────────────────────
    def play(self, samples, label, record_id=None):
        start = self.clock.time()
        duration = self.speaker.start(samples, label)
        if record_id is not None:
            self.played.append({"id": record_id, "start": start, "samples": samples})
        try:
            self.pump(duration + 0.2)
        finally:
            self.speaker.stop()
        return start

    def level(self, step):
        if "level_db" in step:
            return float(step["level_db"])
        if "distance_m" in step:
            return audio.distance_db(float(step["distance_m"]))
        return 0.0

    def leveled(self, samples, step, label):
        out, clipped = audio.at_level(samples, self.level(step), self.gain_db)
        if clipped:
            self.clipped.append(label)
        return audio.pad(out)

    def wait_for(self, spec):
        """Until the condition holds. Log/event conditions count from the scenario's start (playback can
        start during the request step); a state condition also accepts the current state."""
        window_start = self.scenario_start

        def met():
            window = self.observer.window(window_start)
            polled = self.observer.polled
            current = bool(polled) and polled[-1][1] == spec.get("state")
            if "state" in spec and not current and window.first(spec["state"]) is None:
                return False
            if "log" in spec and not any(re.search(spec["log"], line) for line in window.log_lines()):
                return False
            if "event" in spec and not any(re.search(spec["event"], line) for line in window.event_lines()):
                return False
            return True
        ok = self.pump_until(met, float(spec.get("timeout_s", 30)))
        if ok:
            self.pump(float(spec.get("after_s", 0)))
        return ok

    def reply_done(self, since):
        window = self.observer.window(since)
        polled = self.observer.polled
        return (window.first("speaking") is not None and polled and polled[-1][1] == "listening"
                and self.clock.time() - self.observer.last_change >= SETTLE_S)

    def run_step(self, step):
        start = self.clock.time()
        expect = step.get("expect", {})
        failures, speech_end, ready_s = [], None, None
        if step.get("wait_for") and not self.wait_for(step["wait_for"]):
            failures.append(f"wait_for {step['wait_for']} not met")
        if "say" in step:
            label = step["say"]
            samples = self.leveled(self.speak(label, step.get("voice", self.voice)), step, label)
            started = self.play(samples, label, record_id=f"step{self.step_index}")
            # The end of the line itself (before the trailing pad), not when the rig stopped waiting for it.
            speech_end = started + len(samples) / audio.RATE - PAD_S
            if any(k in expect for k in ("silent", "max_false_wakes", "no_user_speech")):
                self.pump(float(step.get("observe_s", OBSERVE_S)))
            elif not self.pump_until(lambda: self.reply_done(speech_end), float(step.get("timeout_s", SAY_TIMEOUT_S))):
                self.log(f"[RIG] No completed reply to {label!r} before the step timeout")
        elif "noise" in step:
            label = f"{step['noise']} noise {step['seconds']}s"
            samples = audio.noise(step["noise"], float(step["seconds"]), self.speak)
            self.play(self.leveled(samples, step, label), label)
            self.pump(float(step.get("observe_s", 5.0)))
        elif ("show" in step or "show_image" in step) and not getattr(self.display, "available", True):
            self.log("[RIG] No display configured; card not shown")  # Scenarios that need it require display.
        elif "show" in step or "show_image" in step:
            jpeg = live_eval.card(step["show"]) if "show" in step else (self.scenario_dir / step["show_image"]).read_bytes()
            self.display.show(jpeg)
            self.pump(float(step.get("hold_s", 2.0)))  # The camera sends frames at about 1 fps.
        elif "clear_display" in step:
            self.display.clear()
        elif "wait" in step:
            self.pump(float(step["wait"]))
        elif "restart" in step:
            self.stop_runtime()
            ready_s = self.start_runtime()
        window = self.observer.window(start)
        turn = checks.observe_turn(window, speech_end)
        failures += checks.check(expect, window, turn, ready_s=ready_s) if expect else []
        kind = next(k for k in checks.STEP_KINDS if k in step)
        return {"id": f"step{self.step_index}", "expect": expect,
                "step": f"{kind}: {step[kind]}" if kind not in ("clear_display", "restart") else kind,
                "heard": turn["heard"], "reply": turn["reply"], "tools": [t["name"] for t in turn["tools"]],
                "states": turn["states"], "latency_s": turn["latency_s"], "latency_source": turn["latency_source"],
                "ready_s": ready_s,
                "failures": failures}

    # ── Optional hardware used by timelines ──────────────────────────────────
    def fixture_path(self, name):
        if name not in self.fixture_paths:
            raise RigFault(f"fixture {name} was not verified before the run")
        return self.fixture_paths[name]

    def fixture_audio(self, name):
        return live_eval.read_wav(self.fixture_path(name))

    def power_cycle(self):
        if self.power is None:
            raise RigFault("no smart plug configured")
        return self.power.cycle(self.clock)

    # ── Scenarios ────────────────────────────────────────────────────────────
    def run_scenario(self, scenario):
        self.scenario_start = self.clock.time()
        self.scenario_deadline = self.scenario_start + float(scenario.get("timeout_s", SCENARIO_TIMEOUT_S))
        seed = timeline.trial_seed(self.seed, scenario["name"], self.round)
        steps, result, self.played = [], {"seed": seed}, []
        recording = False
        try:
            self.quiet()
            if self.recorder is not None:
                self.recorder.start()
                recording = True
            if "timeline" in scenario:
                result.update(self.run_timeline(scenario, seed))
                steps = result["steps"]
            else:
                for index, step in enumerate(scenario["steps"]):
                    self.step_index = index
                    steps.append(self.run_step(step))
                result["steps"] = steps
        except SuiteTimeout:
            raise
        except RigFault as error:
            result.update(steps=steps, rig_fault=str(error))
        except Exception as error:
            result.update(steps=steps, error=f"{type(error).__name__}: {error}", log_tail=self.observer.tail())
        finally:
            self.scenario_deadline = None
            try:
                self.display.clear()
            except Exception:
                pass
            if recording:
                self.measure_reference(scenario, result)
        return result

    def run_timeline(self, scenario, seed):
        player = timeline.Player(self, scenario, seed)
        digest = player.prepare()
        player.run()
        steps = []
        for check in scenario.get("checks", []):
            origin = self.scenario_start if check.get("from", "start") == "start" else \
                player.cue_rig_time(check["from"], check.get("edge", "start"))
            if origin is None:
                steps.append({"id": f"check:{check.get('from')}", "step": f"check from {check.get('from')}",
                              "heard": "", "reply": "", "tools": [], "states": [], "latency_s": None,
                              "failures": [f"cue {check['from']} never fired"]})
                continue
            window = self.observer.window(origin, min(origin + float(check["within_s"]), self.clock.time()))
            speech = player.cue_rig_time(check["from"], "end") if check.get("from", "start") != "start" else None
            turn = checks.observe_turn(window, speech if "_samples" in player.by_id.get(check.get("from"), {}) else None)
            expect = check.get("expect", {})
            steps.append({"id": f"check:{check.get('from', 'start')}", "expect": expect,
                          "step": f"check {check.get('from', 'start')} +{check['within_s']}s",
                          "heard": turn["heard"], "reply": turn["reply"], "tools": [t["name"] for t in turn["tools"]],
                          "states": turn["states"], "latency_s": turn["latency_s"],
                          "latency_source": turn.get("latency_source"),
                          "failures": checks.check(expect, window, turn) if expect else []})
        boot = [e.get("boot_ready_s") for e in player.log if "boot_ready_s" in e]
        if "boot_ready_within_s" in scenario:
            limit = float(scenario["boot_ready_within_s"])
            ok = bool(boot) and boot[0] is not None and boot[0] <= limit
            steps.append({"id": "boot", "step": "power cycle to ready", "heard": "", "reply": "", "tools": [],
                          "states": [], "latency_s": None, "ready_s": boot[0] if boot else None,
                          "failures": [] if ok else [f"ready after {boot[0] if boot else None}s, max {limit}s"]})
        if player.failures:
            steps.append({"id": "timeline", "step": "timeline", "heard": "", "reply": "", "tools": [], "states": [],
                          "latency_s": None, "failures": list(player.failures)})
        self.played.extend(player.played)
        result = {"steps": steps, "cues": player.log, "stimulus_sha256": digest, "player": player}
        if boot:
            result["boot_ready_s"] = boot[0]
        if player.rig_faults:
            result["rig_fault"] = "; ".join(player.rig_faults)
        return result

    def measure_reference(self, scenario, result):
        """Analyse the reference recording, keep only numbers, and delete the recording."""
        player = result.pop("player", None)
        try:
            recording = self.recorder.stop()
        except Exception as error:
            result.setdefault("rig_fault", f"reference microphone failed: {type(error).__name__}: {error}")
            return
        try:
            if recording is None:
                result.setdefault("rig_fault", "reference microphone returned no audio")
                return
            samples, rate, started = recording
            numbers = reference_numbers(scenario, self.played, samples, rate, started, player)
            result["measured"] = numbers
            by_id = {step.get("id"): step for step in result.get("steps", [])}
            for entry in numbers:
                step = by_id.get(entry.get("step"))
                if step is not None and "latency_s" in entry:
                    step["ref_latency_s"] = entry["latency_s"]
                limit = entry.get("max_s")
                if limit is not None and entry.get("latency_s") is not None and entry["latency_s"] > limit:
                    result["steps"].append({"id": f"measure:{entry['id']}", "step": f"measured {entry['kind']}",
                                            "heard": "", "reply": "", "tools": [], "states": [], "latency_s": None,
                                            "failures": [f"reference mic: {entry['kind']} {entry['latency_s']:.2f}s, "
                                                         f"max {limit}s"]})
                if entry.get("error", "").startswith("no speech") and self.ember_spoke(entry):
                    result.setdefault("rig_fault", "reference microphone heard no speech while the runtime "
                                                   "reported Ember speaking (check its position and level)")
        finally:
            self.recorder.discard()

    def ember_spoke(self, entry):
        start = entry.get("rig_start")
        return start is not None and (self.observer.window(start).first("speaking") is not None
                                      or self.observer.playback_after(start) is not None)

    def run_trials(self, plan, repeat=1, shuffle=False, log_order=True):
        """Run `plan` `repeat` times (random order per round when `shuffle` or repeat > 1,
        from the rig's seed). Returns [{"scenario", "repeat", "order", "result"}]."""
        rng = random.Random(self.seed)
        trials = []
        for round_index in range(repeat):
            self.round = round_index
            order = list(plan)
            if shuffle or repeat > 1:
                rng.shuffle(order)
            if log_order:
                self.log(f"[RIG] Round {round_index + 1}/{repeat}: " + ", ".join(s["name"] for s in order))
            results = self.run_suite(order, {})
            for position, scenario in enumerate(order):
                if scenario["name"] in results:
                    trials.append({"scenario": scenario["name"], "repeat": round_index, "order": position,
                                   "result": results[scenario["name"]]})
            if any("time limit" in r.get("skipped", "") for r in results.values()):
                break
        return trials

    def run_suite(self, plan, results=None):
        results = {} if results is None else results
        restart = not self.runtime.alive()
        for index, scenario in enumerate(plan):
            name = scenario["name"]
            try:
                if restart or scenario.get("fresh_runtime"):
                    if self.runtime.alive():
                        self.stop_runtime()
                    self.start_runtime()
                result = self.run_scenario(scenario)
            except SuiteTimeout:
                for rest in plan[index:]:
                    results[rest["name"]] = {"skipped": "not run: suite time limit reached"}
                break
            except RigError as error:
                result = {"steps": [], "error": f"{type(error).__name__}: {error}", "log_tail": self.observer.tail()}
            results[name] = result
            state = report.outcome(result)
            self.log(f"[RIG] {name}: {state}")
            restart = state != "pass"  # A failure must not leak state into the next scenario.
        return results

    def calibration_level(self, seconds=audio.CALIBRATION_SECONDS):
        """Median robot mic level (dBFS, from [MIC LEVEL] lines) while pink noise plays at level 0."""
        start = self.clock.time()
        self.play(self.leveled(audio.pink_noise(seconds), {}, "calibration noise"), "calibration noise")
        levels = self.observer.window(start + 1.5).mic_levels()
        self.pump(2.0)
        if not levels:
            return None
        return round(20 * math.log10(max(statistics.median(levels), 1) / 32768.0), 1)


def reference_numbers(scenario, played, samples, rate, started, player=None):
    """Latency numbers from one reference recording (`started`: rig time of its first sample)."""
    starts = sorted(entry["start"] for entry in played)

    def until(entry):
        later = [s for s in starts if s > entry["start"] + 0.01]
        return (later[0] - started - 0.05) if later else None

    def latency(entry, kind, limit, step=None):
        found = measure.response_latency(samples, rate, entry["samples"], audio.RATE, hint_s=entry["start"] - started,
                                         until_s=until(entry))
        row = {"id": entry["id"], "kind": kind, "rig_start": entry["start"], "max_s": limit, "step": step}
        row.update({k: v for k, v in found.items() if k in ("latency_s", "error", "score")})
        return row

    numbers = []
    if "timeline" not in scenario:
        for entry in played:
            index = int(entry["id"][4:]) if entry["id"].startswith("step") else None
            step = scenario["steps"][index] if index is not None and index < len(scenario["steps"]) else {}
            if "say" in step:
                numbers.append(latency(entry, "response latency", step.get("expect", {}).get("max_ref_latency_s"),
                                       entry["id"]))
        return numbers
    by_id = {entry["id"]: entry for entry in played}
    for spec in scenario.get("measure", []):
        kind = next(k for k in timeline.MEASURE_KINDS if k in spec)
        cue_id = spec[kind]
        limit = spec.get("max_s")
        if kind == "latency":
            if cue_id not in by_id:
                numbers.append({"id": cue_id, "kind": "response latency", "error": "cue did not play"})
                continue
            numbers.append(latency(by_id[cue_id], "response latency", limit))
        elif kind == "speech_after":
            cue = player.by_id.get(cue_id) if player else None
            if cue is None or "_actual" not in cue:
                numbers.append({"id": cue_id, "kind": "speech after", "error": "cue did not fire"})
                continue
            offset = float(spec.get("offset_s", 0.0))
            if spec.get("mark"):
                offset += float(cue.get("marks", {}).get(spec["mark"], 0.0))
            at = player.t0 + cue["_actual"] + offset
            later = [s for s in starts if s > at]
            found = measure.speech_after(samples, rate, at - started,
                                         until_s=(later[0] - started) if later else None)
            row = {"id": cue_id, "kind": "speech after", "rig_start": at, "max_s": limit}
            row.update({k: v for k, v in found.items() if k in ("latency_s", "error")})
            numbers.append(row)
        else:
            entry = by_id.get(cue_id)
            cue = player.by_id.get(cue_id) if player else None
            if entry is None or cue is None:
                numbers.append({"id": cue_id, "kind": "barge-in offset", "error": "cue did not play"})
                continue
            since = player.by_id.get(cue.get("after", {}).get("since"))
            ember_from = (player.t0 + since["_actual"] + since["_seconds"]) if since and "_actual" in since \
                else player.t0
            found = measure.barge_offset(samples, rate, entry["samples"], audio.RATE, entry["start"] - started,
                                         ember_from - started)
            row = {"id": cue_id, "kind": "barge-in offset", "rig_start": ember_from, "intended_ms":
                   cue.get("after", {}).get("ms")}
            if "offset_s" in found:
                row["offset_s"] = found["offset_s"]
            if "error" in found:
                row["error"] = found["error"]
            numbers.append(row)
    return numbers


def check_calibration(rig, calibration, tolerance=CALIBRATION_TOLERANCE_DB):
    """(ok, message): the robot hears the rig at the calibrated level, so failures are about the PR."""
    if not calibration:
        return True, "not calibrated (levels are relative; run --calibrate)"
    measured = rig.calibration_level()
    if measured is None:
        return False, "no [MIC LEVEL] lines from the runtime during the calibration noise"
    expected = calibration["mic_dbfs"]
    if abs(measured - expected) > tolerance:
        return False, (f"robot mic heard the rig at {measured} dBFS, calibrated {expected} dBFS "
                       f"(tolerance {tolerance} dB): check speaker volume, position and the mic")
    return True, f"ok ({measured} dBFS, calibrated {expected} dBFS on {calibration.get('date', '?')})"


# ── Plan ─────────────────────────────────────────────────────────────────────
def run_probe(python, root, extra_env, timeout=120):
    command = [str(python), str(HERE / "probe.py"), "--root", str(root)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, **{f"EMBER_PROBE_ENV_{k}": v for k, v in extra_env.items()}))
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        return {"error": f"probe failed: {type(error).__name__}", "tools": [], "prompt": "", "env": {}}


def plan_scenarios(scenarios, probe, root, display_available, only=None, capabilities=None):
    plan, skipped = [], {}
    for scenario in scenarios:
        if only and scenario["name"] not in only:
            continue
        reason = checks.missing_requirements(scenario, probe, root, display_available, capabilities)
        if reason:
            skipped[scenario["name"]] = {"skipped": reason}
        else:
            plan.append(scenario)
    return plan, skipped


def disclosure(plan, probe):
    lines = [safety.ROOM_NOTICE, "",
             f"Runtime: {probe.get('core')} ({probe.get('model', 'model unknown')}); "
             f"{len(probe.get('tools', []))} tools",
             "The rig plays (second speaker) / shows (tablet), all synthetic:"]
    for scenario in plan:
        lines.append(f"  {scenario['name']}:")
        for cue in scenario.get("timeline", []):
            lines.append(f"    {timeline.describe(cue)}")
        for step in scenario.get("steps", []):
            kind = next(k for k in checks.STEP_KINDS if k in step)
            detail = f"{step['seconds']}s" if kind == "noise" else step[kind]
            lines.append(f"    {kind}: {detail}")
    return "\n".join(lines)


def make_speak(config):
    """Text-to-speech: live_eval.synthesize, or a configured command (e.g. a Piper voice) writing {wav}."""
    command = config.get("tts_command")
    if not command:
        return live_eval.synthesize

    def speak(text, voice=None):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "speech.wav"
            subprocess.run([str(out) if part == "{wav}" else part for part in command], input=text, text=True,
                           check=True, timeout=60, capture_output=True)
            return live_eval.read_wav(out)
    return speak


def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise RigError(f"No rig config at {path} (copy evals/robot/rig.example.json there).")
    return json.loads(path.read_text(encoding="utf-8"))


def load_calibration(config):
    path = config.get("calibration_file")
    if path and Path(path).expanduser().is_file():
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return None


def commit_of(root):
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def rig_env(config):
    """Non-secret settings the runtime gets: the robot's devices plus the rig's privacy settings."""
    values = {k: v for k, v in safety.read_env_file(config.get("base_env")).items() if k not in safety.SECRET_NAMES}
    values.update(safety.RIG_ENV)
    values.update(config.get("env", {}))
    return values


def build_hardware(config, root, prepared, log_path):
    from evals.robot import hardware
    speaker = hardware.make_speaker(config, safety.ember_speaker_names(prepared.values))
    if config.get("_virtual") is not None:
        from evals.robot import virtual
        display = virtual.VirtualCamera(config["_virtual"])
    else:
        display = hardware.make_display(config)
    runtime = hardware.ProcessRuntime(root, hardware.python_for(root), log_path, trace_path=prepared.trace_path)
    return speaker, display, runtime, hardware.ProcessSampler


def cleanup(root, since, log=print):
    """Stop a runtime the rig left in `root`, remove a rig .env and the recordings since `since`."""
    status = json.loads((root / ".runtime-status.json").read_text(encoding="utf-8")) \
        if (root / ".runtime-status.json").is_file() else {}
    pid = status.get("pid")
    from evals.robot import hardware
    running = {found for found, _ in hardware.other_runtimes()}
    if isinstance(pid, int) and pid in running:  # Never signal a PID that is not an Ember runtime now.
        try:
            (root / ".runtime-stop.json").write_text(json.dumps({"pid": pid, "requested_at": time.time()}))
            time.sleep(20)
            os.kill(pid, signal.SIGTERM)
            log(f"[RIG] Sent SIGTERM to leftover runtime {pid}")
        except (ProcessLookupError, PermissionError, OSError):
            pass
    env_path = root / ".env"
    if env_path.is_file() and "ember-rig-key-" in env_path.read_text(encoding="utf-8"):
        backup = root / ".env.rig-backup"
        os.replace(backup, env_path) if backup.exists() else env_path.unlink()
        log("[RIG] Removed the rig's .env")
    for folder in Path(tempfile.gettempdir()).glob("ember-rig-key-*"):
        for child in folder.iterdir():
            child.unlink(missing_ok=True)
        folder.rmdir()
    import shutil
    for folder in Path(tempfile.gettempdir()).glob("ember-rig-data-*"):  # An interrupted run's data folder.
        shutil.rmtree(folder, ignore_errors=True)
        log(f"[RIG] Removed leftover data folder {folder.name}")
    removed = safety.delete_recordings(root, since)
    log(f"[RIG] Recordings deleted: {removed}")
    return removed


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def capabilities_of(config, virtual_mode=False):
    """What this rig can do, for requirement gating (checks.missing_requirements)."""
    display = (config.get("display") or {}).get("kind", "none")
    power = config.get("power") or {}
    return {"display": virtual_mode or display != "none", "video": virtual_mode or display == "http",
            "light": not virtual_mode and bool((config.get("light") or {}).get("command")),
            "faults": set(config.get("faults") or {}),
            "power": bool(power.get("external_controller") and power.get("off")),
            "reference_mic": virtual_mode or bool((config.get("reference_mic") or {}).get("command")),
            "mode": "virtual" if virtual_mode else "physical"}


def build_extras(config, prepared):
    from evals.robot import hardware
    extras = hardware.make_rig_hardware(config, [prepared.values.get("BAYMAX_MIC_NAME", "")])
    spec = config.get("_virtual")
    if spec is not None:
        from evals.robot import virtual
        extras["recorder"] = hardware.CommandRecorder(virtual.ember_tap_command(spec))
    return extras


def virtual_runtime_env(spec, python):
    """Device settings pointing the runtime at the loopback cards (queried in the branch's interpreter)."""
    from evals.robot import virtual
    done = subprocess.run([str(python), "-c", virtual.QUERY_DEVICES], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise virtual.VirtualError("could not list audio devices: " + (done.stderr or "").strip()[-200:])
    listing = json.loads(done.stdout.strip().splitlines()[-1])
    return virtual.runtime_env(spec, listing["devices"], listing["hostapis"])


NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
                "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
                "eighteen": "18", "nineteen": "19"}
TENS = {"twenty": 2, "thirty": 3, "forty": 4, "fifty": 5, "sixty": 6, "seventy": 7, "eighty": 8, "ninety": 9}


def spoken_digits(text):
    """Digits in a reply, with number words read out: 'forty-eight twenty one' -> '4821'."""
    tokens = re.findall(r"[a-z]+|\d+", (text or "").lower().replace("-", " "))
    out, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token.isdigit() or token in NUMBER_WORDS:
            out.append(token if token.isdigit() else NUMBER_WORDS[token])
        elif token in TENS:
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            unit = NUMBER_WORDS.get(following)
            if unit is not None and len(unit) == 1 and unit != "0":
                out.append(f"{TENS[token]}{unit}")
                index += 1
            else:
                out.append(f"{TENS[token]}0")
        index += 1
    return "".join(out)


SCREEN_PROMPT = "Ember, please look at the screen in front of you and read me the code number on it."


def screen_check(rig, seed, attempts=2, prompt=SCREEN_PROMPT, settle_s=3.0):
    """(ok, message): the robot's camera reads a seeded code card through the real runtime.
    Fails as a rig fault, so a misplaced or dark screen never counts against a PR."""
    code = str(random.Random(seed).randrange(1000, 10000))
    for attempt in range(1, attempts + 1):
        version = rig.display.show(live_eval.card(["RIG CHECK", f"CODE {code}"]), f"RIG CHECK / CODE {code}")
        if hasattr(rig.display, "ack") and not rig.pump_until(lambda: rig.display.ack(version, "shown") is not None, 5.0):
            return False, "the display did not confirm the test card (page not open or not reachable)"
        rig.pump(settle_s)  # The camera sends frames at about 1 fps.
        start = rig.clock.time()
        rig.play(rig.leveled(rig.speak(prompt, rig.voice), {}, prompt), prompt)
        end = rig.clock.time()
        rig.pump_until(lambda: rig.reply_done(end), 40.0)
        reply = rig.observer.window(start).reply()
        if code in spoken_digits(reply):
            rig.display.clear()
            return True, f"ok (code read on attempt {attempt})"
        rig.log(f"[RIG] Screen check attempt {attempt}: code {code} not in the reply")
    rig.display.clear()
    return False, (f"the robot did not read the test card's code in {attempts} attempts: check the screen's "
                   "position, brightness and focus, and the camera")


def needs_screen(plan):
    return any(s.get("requires", {}).get("display") or s.get("requires", {}).get("video") for s in plan)


def main(argv=None, hardware_factory=build_hardware, clock=None, confirm=input, opener=None,
         extras_factory=build_extras, modules_factory=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="Baymax-main of the branch under test")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--scenarios", type=Path, default=checks.SCENARIOS)
    parser.add_argument("--only", action="append")
    parser.add_argument("--dry-run", action="store_true", help="Plan and gating only; opens and contacts nothing")
    parser.add_argument("--yes", action="store_true", help="Unattended: skip the empty-room confirmation")
    parser.add_argument("--max-minutes", type=float, default=75.0)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--repeat", type=int, default=1, help="Run every scenario N times (random order)")
    parser.add_argument("--seed", type=int, help="Seed for the order and stimuli (default: from the clock; reported)")
    parser.add_argument("--shuffle", action="store_true", help="Random order even with --repeat 1")
    parser.add_argument("--release", action="store_true", help="Judge by the rig config's release gates")
    parser.add_argument("--virtual", action="store_true",
                        help="Virtual-device mode (needs the config's 'virtual' section; README)")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--spl", type=float, help="With --calibrate: SPL meter reading, dB(A) at the robot's mic")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--since-minutes", type=float, default=240.0, help="With --cleanup")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--post-comment", action="store_true", help="Post the report on --pr (needs GITHUB_TOKEN)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.cleanup:
        cleanup(root, time.time() - args.since_minutes * 60)
        return 0
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config) if not args.dry_run or args.config.is_file() else {}
    except (RigError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    spec = None
    if args.virtual:
        from evals.robot import virtual
        try:
            spec = virtual.spec_of(config)
        except virtual.VirtualError as error:
            print(error, file=sys.stderr)
            return 2
    seed = args.seed if args.seed is not None else int(time.time()) % 1_000_000
    scenarios = checks.load_scenarios(args.scenarios)
    if args.only and set(args.only) - {s["name"] for s in scenarios}:
        print(f"Unknown scenario in {args.only}", file=sys.stderr)
        return 2
    from evals.robot import hardware as hw
    probe = run_probe(hw.python_for(root), root, rig_env(config))
    capabilities = capabilities_of(config, spec is not None)
    plan, skipped = plan_scenarios(scenarios, probe, root, capabilities["display"], args.only, capabilities)
    wanted = {}
    for scenario in plan:
        wanted.update(timeline.fixtures_of(scenario))
    fixture_folder = fixtures.folder_of(config)
    if not args.calibrate:
        print(disclosure(plan, probe))
        for name, outcome in skipped.items():
            print(f"  {name}: skipped ({outcome['skipped']})")
        print(f"Seed {seed}; {args.repeat} round(s)" + (" in random order" if args.repeat > 1 or args.shuffle else ""))
        if wanted:
            print(f"Fixtures ({fixture_folder}): " + ", ".join(sorted(wanted)))
        if spec is not None:
            from evals.robot import virtual
            print("Virtual-device mode. Commands:")
            for command in (virtual.load_commands(spec) if spec.get("load_modules") else []):
                print("  " + " ".join(command))
            print("  person -> mic: " + " ".join(virtual.person_feed_command(spec)))
            print("  Ember's speaker -> reference: " + " ".join(virtual.ember_tap_command(spec)))
            print("  camera: " + " ".join(virtual.camera_command(spec, "<card or clip>", "video")))
    if args.dry_run:
        return 0
    if not safety.is_test_robot():
        print("Refusing: not a test robot (create /etc/ember/test-robot or set EMBER_TEST_ROBOT=1 on the "
              "dedicated test robot only).", file=sys.stderr)
        return 2
    print("\n" + safety.ROOM_NOTICE)
    if not args.yes and confirm("Is the room empty and the rig set up? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was started.")
        return 1
    key = os.getenv("GEMINI_TEST_KEY") or ""
    started_wall, started_text = time.time(), time.strftime("%Y-%m-%d %H:%M:%S %Z")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.report_dir / "runtime.log"
    log_path.unlink(missing_ok=True)
    clock = clock or hw.Clock()
    signal.signal(signal.SIGTERM, _interrupt)  # A cancelled job still stops the runtime and cleans up.
    meta = {"commit": commit_of(root), "pr": args.pr, "core": probe.get("core"), "started": started_text,
            "robot": config.get("robot_name") or socket.gethostname().split(".")[0], "seed": seed,
            "repeat": args.repeat, "shuffled": args.repeat > 1 or args.shuffle, "mode": capabilities["mode"],
            "release": args.release}
    results, trials, removed, prepared_env, modules = dict(skipped), [], None, None, None
    try:
        try:
            fixture_paths = fixtures.verify(fixture_folder, wanted)
            meta["fixtures"] = fixtures.manifest_digest(fixture_folder) if wanted else None
        except fixtures.FixtureError as error:
            raise RigFault(f"fixtures: {error}") from None
        extra_env = dict(config.get("env") or {})
        if spec is not None:
            from evals.robot import virtual
            modules = (modules_factory or virtual.Modules)(spec)
            modules.setup()
            extra_env.update(virtual_runtime_env(spec, hw.python_for(root)))
            config = dict(config, _virtual=spec,
                          person_speaker={"command": virtual.person_feed_command(spec)}, display={"kind": "virtual"})
        with safety.PreparedEnv(root, config.get("base_env"), key, extra_env, config.get("temp_dir")) as prepared:
            prepared_env = prepared
            speaker, display, runtime, sampler = hardware_factory(config, root, prepared, log_path)
            extras = extras_factory(config, prepared)
            calibration = None if spec is not None else load_calibration(config)
            rig = Rig(runtime, speaker, display, clock, sampler, make_speak(config),
                      gain_db=float((calibration or {}).get("gain_db", config.get("gain_db", 0.0))),
                      voice=config.get("voice"), startup_timeout_s=float(config.get("startup_timeout_s", STARTUP_TIMEOUT_S)),
                      deadline=clock.time() + args.max_minutes * 60, light=extras.get("light"),
                      faults=extras.get("faults"), recorder=extras.get("recorder"), power=extras.get("power"),
                      fixture_paths=fixture_paths, seed=seed)
            try:
                if args.calibrate:
                    return calibrate(rig, config, args.spl, confirm)
                rig.start_runtime()
                if spec is not None:
                    ok, message = True, "not needed in virtual-device mode (digital levels)"
                else:
                    ok, message = check_calibration(rig, calibration, float(config.get("calibration_tolerance_db",
                                                                                       CALIBRATION_TOLERANCE_DB)))
                meta["calibration"] = message
                if ok and needs_screen(plan) and (config.get("screen_check") or {}).get("enabled", True):
                    ok, message = screen_check(rig, seed)
                    meta["screen_check"] = message
                    if not ok:
                        meta["rig_error"] = "screen check failed: " + message
                elif not ok:
                    meta["rig_error"] = "out of calibration: " + message
                if ok:
                    trials = rig.run_trials(plan, args.repeat, args.shuffle)
                    for trial in trials:
                        results[trial["scenario"]] = trial["result"]  # The last round's result per scenario.
            except KeyboardInterrupt:
                meta["rig_error"] = "interrupted (job cancelled or timed out)"
            except (RigError, RuntimeError, OSError) as error:
                meta["rig_error"] = f"{type(error).__name__}: {error}"
            finally:
                rig.stop_runtime()
                for part in (speaker, display, extras.get("light"), extras.get("recorder"),
                             *(extras.get("faults") or {}).values()):
                    try:
                        if part is not None and hasattr(part, "close"):
                            part.close()
                    except Exception as error:  # Keep closing the rest (a fault must still be restored).
                        meta["rig_error"] = (meta.get("rig_error", "") + f" close failed: {error}").strip()
                meta["cpu"] = checks.cpu_summary([v for _, v in rig.observer.samples])
                if rig.clipped:
                    meta["rig_error"] = (meta.get("rig_error", "") + f" clipped playback: {rig.clipped[:3]}").strip()
                if extras.get("recorder") is not None:
                    meta["reference_recordings_deleted"] = getattr(extras["recorder"], "discarded", None)
    except (RigError, RuntimeError, OSError) as error:  # Setup: key, devices, display, fixtures, virtual devices.
        meta["rig_error"] = str(error)
    finally:
        if modules is not None:
            modules.teardown()
        removed = safety.delete_recordings(root, started_wall)
        left = safety.leftover_recordings(root, started_wall)
        meta["recordings_deleted"] = (sum(removed.values()) if removed else 0) + \
            (prepared_env.data_removed if prepared_env else 0)
        if left:
            meta["rig_error"] = (meta.get("rig_error", "") + f" recordings left behind: {left[:3]}").strip()
    if args.calibrate:
        return 0
    meta["minutes"] = (time.time() - started_wall) / 60
    ordered = {s["name"]: results[s["name"]] for s in scenarios if s["name"] in results}
    trials = trials + [{"scenario": name, "result": result} for name, result in skipped.items()]
    gates = config.get("gates") if args.release else None
    markdown, verdict = report.render(ordered, meta, trials, scenarios, gates)
    report.write(args.report_dir / "robot-test.md", markdown, ordered, meta, trials)
    print("\n" + markdown)
    print(f"Report: {args.report_dir / 'robot-test.md'} (the runtime log stays local: {log_path})")
    if args.post_comment:
        kwargs = {"opener": opener} if opener else {}
        url = report.post_comment(markdown, args.repository, args.pr, os.getenv("GITHUB_TOKEN", ""), **kwargs)
        print(f"Posted: {url}")
    return 0 if verdict == "PASS" else 1


def calibrate(rig, config, spl, confirm):
    """Measure how loud the robot hears level-0 rig audio, and record it with the SPL meter reading."""
    rig.start_runtime()
    print("Playing pink noise at the reference level. Read the SPL meter (A-weighted, slow) at the robot's mic.")
    measured = rig.calibration_level(audio.CALIBRATION_SECONDS * 2)
    if measured is None:
        print("No [MIC LEVEL] lines from the runtime; is the robot's microphone working?", file=sys.stderr)
        return 1
    if spl is None:
        try:
            spl = float(confirm("SPL meter reading in dB(A): "))
        except ValueError:
            print("No reading; nothing saved.")
            return 1
    target = float(config.get("target_spl_dba", TARGET_SPL_DBA))
    if abs(spl - target) > 1.0:
        print(f"Measured {spl} dB(A); target {target}. Set gain_db to {rig.gain_db + target - spl:+.1f} in the rig "
              "config (or change the speaker's volume) and calibrate again. Nothing saved.")
        return 1
    path = Path(config.get("calibration_file", "~/.config/ember/robot-rig-calibration.json")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"gain_db": rig.gain_db, "mic_dbfs": measured, "spl_dba": spl, "target_spl_dba": target,
                                "date": time.strftime("%Y-%m-%d")}, indent=2), encoding="utf-8")
    print(f"Calibrated: level 0 = {spl} dB(A) at the robot, mic {measured} dBFS. Saved {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
