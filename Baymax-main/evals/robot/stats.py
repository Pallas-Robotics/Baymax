"""Repeated runs: pass rates with Wilson intervals, release gates and failure classes.

Each trial (one scenario, one repeat) ends as one of:
- `pass`
- `product`: an expectation failed, or the runtime crashed or hung. Evidence about
  the branch under test.
- `rig_fault`: the rig could not run a fair test (calibration or screen check failed,
  a device or fixture was missing or changed, the display did not confirm a cue, a cue
  fired late, playback clipped, the reference mic heard nothing while the runtime
  reported speech). Not a trial of the product: excluded from pass rates.
- `skipped`: the scenario does not apply to this branch or rig.

Across repeats, a scenario with both passes and product failures is **flaky**; one
whose every valid trial failed is a **product** failure. One failure in one run is
reported as product failure "(1 run)", since a single run cannot tell the two apart.

Gates (rig config `gates`) say what must hold for a release candidate, e.g. safety
scenarios pass at least 19 of 20 valid runs:

    "gates": {"default": {"min_pass_rate": 1.0},
              "safety":  {"min_trials": 20, "min_pass_rate": 0.95},
              "latency": {"min_trials": 10, "min_wilson_lower": 0.8}}

A scenario uses the gate named by its `gate` field, else `default`. Too few valid
trials is "insufficient", which does not pass. The configured gates apply with
`rig.py --release`; an ordinary run uses `DEFAULT_GATE` (every valid run passes).
Standard library only.
"""
import math

DEFAULT_GATE = {"min_pass_rate": 1.0}
GATE_KEYS = {"min_trials", "min_pass_rate", "min_wilson_lower"}


def wilson(passes, trials, z=1.959964):
    """95% Wilson score interval (lower, upper) for passes/trials; (0, 1) for no trials."""
    if trials <= 0:
        return 0.0, 1.0
    p = passes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def trial_outcome(result):
    if "skipped" in result:
        return "skipped"
    if result.get("rig_fault"):
        return "rig_fault"
    if result.get("error") or any(step.get("failures") for step in result.get("steps", [])):
        return "product"
    return "pass"


def summarize(trials):
    """{scenario: counts, rate, Wilson interval and class} from [{"scenario", "result", ...}]."""
    summary = {}
    for trial in trials:
        entry = summary.setdefault(trial["scenario"], {"runs": 0, "passes": 0, "product": 0, "rig_faults": 0,
                                                       "skipped": 0})
        entry["runs"] += 1
        outcome = trial_outcome(trial["result"])
        key = {"pass": "passes", "product": "product", "rig_fault": "rig_faults", "skipped": "skipped"}[outcome]
        entry[key] += 1
    for entry in summary.values():
        valid = entry["passes"] + entry["product"]
        entry["valid"] = valid
        entry["pass_rate"] = round(entry["passes"] / valid, 3) if valid else None
        low, high = wilson(entry["passes"], valid)
        entry["wilson95"] = [round(low, 3), round(high, 3)]
        entry["class"] = scenario_class(entry)
    return summary


def scenario_class(entry):
    if entry["valid"] == 0:
        return "rig fault" if entry["rig_faults"] else "skipped"
    if entry["product"] == 0:
        return "pass"
    if entry["passes"] > 0:
        return "flaky"
    return "product" if entry["valid"] > 1 else "product (1 run)"


def failure_class(trial, summary):
    """product / flaky / rig fault for one failed trial (None for a pass or skip)."""
    outcome = trial_outcome(trial["result"])
    if outcome == "rig_fault":
        return "rig fault"
    if outcome != "product":
        return None
    return "flaky" if summary[trial["scenario"]]["passes"] > 0 else "product"


def check_gate(entry, gate):
    """(state, reason): state is pass, fail or insufficient."""
    unknown = set(gate) - GATE_KEYS
    if unknown:
        raise ValueError(f"unknown gate keys {sorted(unknown)}")
    valid, passes = entry["valid"], entry["passes"]
    needed = int(gate.get("min_trials", 1))
    if valid < needed:
        return "insufficient", f"{valid} valid runs, gate needs {needed}"
    rate = passes / valid
    if "min_pass_rate" in gate and rate + 1e-9 < float(gate["min_pass_rate"]):
        return "fail", f"{passes}/{valid} passed, gate needs {float(gate['min_pass_rate']):.0%}"
    if "min_wilson_lower" in gate:
        lower = wilson(passes, valid)[0]
        if lower + 1e-9 < float(gate["min_wilson_lower"]):
            return "fail", f"Wilson lower bound {lower:.2f}, gate needs {float(gate['min_wilson_lower']):.2f}"
    return "pass", f"{passes}/{valid} passed"


def evaluate_gates(summary, scenarios, gates=None):
    """{scenario: {"gate", "state", "reason"}} for every scenario that ran or faulted."""
    gates = dict(gates or {})
    by_name = {s["name"]: s for s in scenarios}
    verdicts = {}
    for name, entry in summary.items():
        if entry["class"] == "skipped":
            continue
        gate_name = by_name.get(name, {}).get("gate", "default")
        gate = gates.get(gate_name, gates.get("default", DEFAULT_GATE))
        state, reason = check_gate(entry, gate)
        verdicts[name] = {"gate": gate_name, "state": state, "reason": reason}
    return verdicts


def verdict(summary, gate_results, rig_error=None):
    """PASS; FAIL (a gate failed on product evidence); RIG FAULT (the rig could not test
    fairly, so nothing is concluded); INCOMPLETE (too few runs for a gate)."""
    if any(result["state"] == "fail" for result in gate_results.values()):
        return "FAIL"
    short = [name for name, result in gate_results.items() if result["state"] == "insufficient"]
    if rig_error or any(summary[name]["rig_faults"] for name in short):
        return "RIG FAULT"
    if short:
        return "INCOMPLETE"
    return "PASS"
