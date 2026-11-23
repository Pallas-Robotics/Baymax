"""Robot scenarios: validation, requirement gating and expectation checks.

The format extends evals/scenarios.json (Layer 1). The `heard`, `reply`,
`not_reply`, `tools`, `not_tools`, `no_tools`, `max_words` and `max_latency_s`
expectations mean the same thing and are checked by live_eval.check. The rest only
make sense on hardware.
"""
import json
from pathlib import Path
import re

from evals import live_eval

SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"
STEP_KINDS = ("say", "show", "show_image", "clear_display", "noise", "wait", "restart")
LAYER1_KEYS = live_eval.EXPECT_KEYS
ROBOT_KEYS = {
    "silent",              # Ember says nothing in the window.
    "max_false_wakes",     # Ember replies + engage events in the window (noise periods).
    "no_user_speech",      # No user transcript at all: nothing reached Gemini (disengaged).
    "log", "not_log",      # Regex that must / must not appear in a runtime log line.
    "startup_log",         # Regex in the log since the runtime last started (e.g. "[EYES] Ready").
    "event", "not_event",  # Regex over "state: message" status events.
    "states",              # Conversation states that must appear in this order.
    "engaged",             # Engagement ('Ember disengage' / 'Ember engage') at the end of the window.
    "no_echo",             # No echo event and no user transcript started while Ember spoke.
    "max_cpu_percent",     # Mean CPU of the runtime, % of all cores.
    "max_cpu_peak_percent",
    "max_rss_mb",
    "ready_within_s",      # restart: seconds until "Connected; listening".
}
EXPECT_KEYS = LAYER1_KEYS | ROBOT_KEYS
REQUIRE_KEYS = {"tools", "any_tools", "prompt", "files", "env", "env_defaults", "display"}
REGEX_KEYS = ("heard", "reply", "not_reply", "log", "not_log", "startup_log", "event", "not_event")


def load_scenarios(path=SCENARIOS):
    scenarios = json.loads(Path(path).read_text(encoding="utf-8"))
    names = set()
    for scenario in scenarios:
        validate(scenario)
        if scenario["name"] in names:
            raise ValueError(f"Duplicate scenario {scenario['name']}")
        names.add(scenario["name"])
    return scenarios


def validate(scenario):
    name = scenario.get("name")
    if not name or not scenario.get("steps"):
        raise ValueError(f"Scenario needs a name and steps: {scenario}")
    unknown = set(scenario.get("requires", {})) - REQUIRE_KEYS
    if unknown:
        raise ValueError(f"{name}: unknown requirements {sorted(unknown)}")
    for step in scenario["steps"]:
        kinds = [k for k in STEP_KINDS if k in step]
        if len(kinds) != 1:
            raise ValueError(f"{name}: each step needs exactly one of {STEP_KINDS}: {step}")
        expect = step.get("expect", {})
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            raise ValueError(f"{name}: unknown expectations {sorted(unknown)}")
        for key in REGEX_KEYS:
            if key in expect:
                re.compile(expect[key])
        if "noise" in step and step["noise"] not in ("pink", "tv", "music"):
            raise ValueError(f"{name}: unknown noise {step['noise']!r}")
        if "noise" in step and not step.get("seconds"):
            raise ValueError(f"{name}: a noise step needs seconds")
        wait_for = step.get("wait_for", {})
        if set(wait_for) - {"state", "log", "event", "timeout_s", "after_s"}:
            raise ValueError(f"{name}: unknown wait_for keys in {wait_for}")


# ── Requirement gating ───────────────────────────────────────────────────────
def effective_env(probe, name, defaults):
    """The value the runtime will see: rig .env / launcher defaults (from the probe), else the code default."""
    env = probe.get("env") or {}
    return env.get(name, defaults.get(name))


def missing_requirements(scenario, probe, root, display_available=True):
    """Why this scenario does not apply to the branch under test, else None.

    `probe` is what probe.py read from the branch: its declared tools, system prompt
    and the environment its launchers and the rig's .env give the runtime.
    """
    needs = scenario.get("requires", {})
    if needs.get("display") and not display_available:
        return "no rig display configured"
    absent = [path for path in needs.get("files", []) if not (Path(root) / path).exists()]
    if absent:
        return "file not on this branch: " + ", ".join(absent)
    if needs.get("tools") or needs.get("any_tools") or needs.get("prompt"):
        if probe.get("error"):
            return "could not read the branch's session config: " + probe["error"]
        config = {"tools": [{"function_declarations": [{"name": n} for n in probe.get("tools", [])]}],
                  "system_instruction": probe.get("prompt", "")}
        reason = live_eval.missing_requirements(scenario, config)
        if reason:
            return reason
        any_tools = needs.get("any_tools", [])
        if any_tools and not set(any_tools) & set(probe.get("tools", [])):
            return "none of these tools on this branch: " + ", ".join(any_tools)
    defaults = needs.get("env_defaults", {})
    for name, wanted in needs.get("env", {}).items():
        value = effective_env(probe, name, defaults)
        if value != wanted:
            return f"{name}={value!r} on this branch (scenario needs {wanted!r})"
    return None


# ── Expectations ─────────────────────────────────────────────────────────────
def observe_turn(window, speech_end=None):
    """A live_eval-style turn from one window of robot observations."""
    speaking = window.first("speaking", after=speech_end)
    latency, source = None, None
    playback = window.o.playback_after(speech_end) if speech_end is not None else None
    if playback is not None and playback <= window.end:
        # v10 conversation_timing: when reply audio was scheduled to the speaker, stamped by the runtime.
        latency, source = round(max(0.0, playback - speech_end), 3), "runtime playback time"
    elif speaking is not None and speech_end is not None:
        latency, source = round(max(0.0, speaking - speech_end), 2), "speaking state"
    return {"heard": window.heard(), "reply": window.reply(),
            "tools": [{"name": name} for name in window.tools()],
            "audio_bytes": 1 if speaking is not None or playback is not None else 0, "latency_s": latency,
            "latency_source": source, "timed_out": False,
            "states": window.states(), "replies": window.replies()}


def is_subsequence(wanted, seen):
    position = iter(seen)
    return all(state in position for state in wanted)


def check(expect, window, turn, ready_s=None):
    """Failed expectations for one step, as short sentences."""
    layer1 = {key: value for key, value in expect.items() if key in LAYER1_KEYS}
    # A spoken reply is required only when the step checks one.
    layer1.setdefault("silence_ok", not any(key in expect for key in ("reply", "max_words", "max_latency_s")))
    failures = live_eval.check(layer1, turn)
    if expect.get("silent") and (turn["reply"] or turn["audio_bytes"]):
        failures.append(f"expected silence, Ember said {turn['reply']!r}")
    if "max_false_wakes" in expect:
        engages = [line for line in window.log_lines() if line.startswith("[ENGAGE] Engaged")]
        wakes = window.replies() + len(engages)
        if wakes > expect["max_false_wakes"]:
            failures.append(f"{wakes} false wakes (replies {window.replies()}, engages {len(engages)}), "
                            f"max {expect['max_false_wakes']}")
    if expect.get("no_user_speech") and turn["heard"]:
        failures.append(f"Gemini heard {turn['heard']!r}; nothing should have reached it")
    lines, events = window.log_lines(), window.event_lines()
    for key, source, label in (("log", lines, "log"), ("event", events, "event")):
        if key in expect and not any(re.search(expect[key], line) for line in source):
            failures.append(f"no {label} line matches /{expect[key]}/")
        if "not_" + key in expect:
            hits = [line for line in source if re.search(expect["not_" + key], line)]
            if hits:
                failures.append(f"{label} matches forbidden /{expect['not_' + key]}/: {hits[0][:120]!r}")
    if "startup_log" in expect:
        since = max([s for s in window.o.starts if s <= window.end], default=0)
        if not any(re.search(expect["startup_log"], line) for when, line in window.o.log if since <= when <= window.end):
            failures.append(f"no log line since the runtime started matches /{expect['startup_log']}/")
    if "states" in expect and not is_subsequence(expect["states"], turn["states"]):
        failures.append(f"states {turn['states']} do not include {expect['states']} in order")
    if "engaged" in expect and window.engaged() != expect["engaged"]:
        failures.append(f"engaged is {window.engaged()}, expected {expect['engaged']}")
    if expect.get("no_echo"):
        failures += echo_failures(window)
    failures += cpu_failures(expect, window.cpu())
    if "ready_within_s" in expect and (ready_s is None or ready_s > expect["ready_within_s"]):
        failures.append(f"ready after {ready_s}s, max {expect['ready_within_s']}s")
    return failures


def echo_failures(window, tolerance_s=0.5):
    failures = [f"runtime flagged echo: {line[:100]!r}" for line in window.echo_events()]
    speaking = window.first("speaking")
    if speaking is not None:
        late = [t for t in window.transcripts("user") if t["time"] > speaking + tolerance_s]
        if late:
            failures.append(f"user transcript started while/after Ember spoke (own voice?): {late[0]['text'][:80]!r}")
    return failures


def cpu_failures(expect, samples):
    keys = ("max_cpu_percent", "max_cpu_peak_percent", "max_rss_mb")
    if not any(key in expect for key in keys):
        return []
    if not samples:
        return ["no CPU/memory samples of the runtime"]
    failures = []
    cpu = [s["cpu_percent"] for s in samples if s.get("cpu_percent") is not None]
    rss = [s["rss_mb"] for s in samples if s.get("rss_mb") is not None]
    if "max_cpu_percent" in expect and cpu and sum(cpu) / len(cpu) > expect["max_cpu_percent"]:
        failures.append(f"mean CPU {sum(cpu) / len(cpu):.0f}% of all cores, max {expect['max_cpu_percent']}%")
    if "max_cpu_peak_percent" in expect and cpu and max(cpu) > expect["max_cpu_peak_percent"]:
        failures.append(f"peak CPU {max(cpu):.0f}%, max {expect['max_cpu_peak_percent']}%")
    if "max_rss_mb" in expect and rss and max(rss) > expect["max_rss_mb"]:
        failures.append(f"memory {max(rss):.0f} MB, max {expect['max_rss_mb']} MB")
    return failures


def cpu_summary(samples):
    cpu = [s["cpu_percent"] for s in samples if s.get("cpu_percent") is not None]
    rss = [s["rss_mb"] for s in samples if s.get("rss_mb") is not None]
    if not cpu:
        return None
    return {"mean_cpu_percent": round(sum(cpu) / len(cpu), 1), "peak_cpu_percent": round(max(cpu), 1),
            "peak_rss_mb": round(max(rss), 1) if rss else None, "samples": len(cpu)}
