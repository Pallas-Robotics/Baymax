"""Markdown + JSON report of a robot run, and (only behind --post-comment / --post) a PR comment.

    python evals/robot/report.py --post .local-review/robot/robot-test.md --pr 21 --repository Pallas-Robotics/Baymax

The workflow posts with this separate step, so the branch under test never runs in a
process that holds GITHUB_TOKEN. Standard library only.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request

if __package__ in (None, ""):  # Run as a script by the workflow's system python3.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.robot import stats  # noqa: E402  (standard library only, like this file)

MAX_COMMENT = 60000
SNIPPET = 160
# Transcript lines stay out of the log excerpt (the step lines already show what was heard).
PRIVATE_LOG = ("[USER]", "[GEMINI TXT]", "[GEMINI]", "[TRANSCRIPT]")


def _clip(text, size=SNIPPET):
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[:size - 1] + "…"


def outcome(result):
    if "skipped" in result:
        return "skipped"
    if result.get("rig_fault"):
        return "rig fault"
    if result.get("error"):
        return "error"
    return "pass" if not any(step["failures"] for step in result.get("steps", [])) else "FAIL"


def render(results, meta, trials=None, scenarios=None, gates=None):
    """The report. Holds only what the rig played, what the runtime transcribed of it, and
    numbers (latencies, rates); never audio. `trials` (rig.run_trials) adds repeat statistics.

    Verdict: PASS, FAIL (a gate failed on product evidence), RIG FAULT (the rig could not
    test fairly, so nothing is concluded) or INCOMPLETE (too few runs for a gate)."""
    trials = trials if trials is not None else [{"scenario": n, "result": r} for n, r in results.items()]
    summary = stats.summarize(trials)
    gate_results = stats.evaluate_gates(summary, scenarios or [], gates)
    verdict = stats.verdict(summary, gate_results, meta.get("rig_error"))
    counts = {}
    for result in results.values():
        counts[outcome(result)] = counts.get(outcome(result), 0) + 1
    lines = [f"## Ember robot test: {verdict}", "",
             f"- Commit: `{meta.get('commit', 'unknown')}`" + (f" (PR #{meta['pr']})" if meta.get("pr") else ""),
             f"- Robot: {meta.get('robot', 'test robot')}; runtime `{meta.get('core') or 'unknown'}`",
             f"- Started {meta.get('started', '')}, {meta.get('minutes', 0):.1f} min; "
             + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
             f"- Calibration: {meta.get('calibration', 'not checked')}",
             f"- Runtime CPU (whole run): {_cpu(meta.get('cpu'))}",
             f"- Recordings deleted after the run: {meta.get('recordings_deleted', 'unknown')}"]
    if meta.get("seed") is not None:
        lines.append(f"- Seed {meta['seed']}, {meta.get('repeat', 1)} round(s)"
                     + (" in random order" if meta.get("shuffled") else "")
                     + (f"; fixtures manifest `{meta['fixtures'][:12]}`" if meta.get("fixtures") else "")
                     + (f"; mode {meta['mode']}" if meta.get("mode") else "")
                     + ("; release gates" if meta.get("release") else ""))
    if meta.get("screen_check"):
        lines.append(f"- Screen check: {meta['screen_check']}")
    if meta.get("rig_error"):
        lines += ["", f"**Rig problem (not necessarily the PR):** {meta['rig_error']}"]
    if any(entry["runs"] > 1 for entry in summary.values()) or meta.get("release"):
        lines += ["", "| Scenario | Runs | Passed | Pass rate (95% CI) | Rig faults | Gate | Class |",
                  "|---|---|---|---|---|---|---|"]
        for name, entry in summary.items():
            gate = gate_results.get(name)
            rate = "" if entry["pass_rate"] is None else \
                f"{entry['pass_rate']:.0%} ({entry['wilson95'][0]:.0%}-{entry['wilson95'][1]:.0%})"
            lines.append(f"| {name} | {entry['runs']} | {entry['passes']}/{entry['valid']} | {rate} | "
                         f"{entry['rig_faults']} | {gate['state'] + ': ' + gate['reason'] if gate else ''} | "
                         f"{entry['class']} |")
    measured = measurements(trials)
    if measured:
        lines += ["", "| Scenario | Measurement | n | Median | p90 | Max | Source |", "|---|---|---|---|---|---|---|"]
        for (name, kind, source), values in measured.items():
            summary_row = _summary(values)
            if summary_row:
                lines.append(f"| {name} | {kind} | {summary_row['n']} | {summary_row['median_s']:.2f}s | "
                             f"{summary_row['p90_s']:.2f}s | {summary_row['max_s']:.2f}s | {source} |")
    lines += ["", "| Scenario | Result | First audio | Notes |", "|---|---|---|---|"]
    detail = []
    for name, result in results.items():
        state = outcome(result)
        if state == "skipped":
            lines.append(f"| {name} | skipped | | {result['skipped']} |")
            continue
        latencies = [s["latency_s"] for s in result.get("steps", []) if s.get("latency_s") is not None]
        first = f"{min(latencies):.2f}s" if latencies else ""
        failed = [f for s in result.get("steps", []) for f in s["failures"]]
        note = _clip(result.get("rig_fault") or result.get("error") or (failed[0] if failed else ""), 90).replace("|", "/")
        lines.append(f"| {name} | {state} | {first} | {note} |")
        if state != "pass":
            detail += ["", f"### {name}"]
            if result.get("rig_fault"):
                detail.append(f"- RIG FAULT (not counted as a run of the product): {_clip(result['rig_fault'], 300)}")
            if result.get("error"):
                detail.append(f"- ERROR: {_clip(result['error'], 300)}")
            for index, step in enumerate(result.get("steps", []), 1):
                mark = "FAIL" if step["failures"] else "ok"
                detail.append(f"- Step {index} {mark}: {step['step']} | heard {_clip(step.get('heard'))!r} | "
                              f"reply {_clip(step.get('reply'))!r} | tools {', '.join(step.get('tools') or []) or 'none'}"
                              f" | states {' > '.join(step.get('states') or [])}")
                detail += [f"  - {failure}" for failure in step["failures"]]
            if result.get("log_tail"):
                detail += ["  <details><summary>runtime log tail</summary>", "", "  ```"]
                detail += [f"  {_clip(line, 200)}" for line in result["log_tail"]
                           if not line.strip().startswith(PRIVATE_LOG)]
                detail += ["  ```", "  </details>"]
    footer = ["", "Synthetic speech, cards and noise only, through the robot's real microphone, echo "
              "cancellation, speaker and camera. Offline and robot passes are evidence for review; "
              "they are not release approval (TESTING_PROTOCOL.md)."]
    return "\n".join(lines + detail + footer) + "\n", verdict


def measurements(trials):
    """{(scenario, kind, source): [seconds]} across trials: runtime-reported and reference-mic latencies."""
    found = {}
    for trial in trials:
        result = trial["result"]
        for step in result.get("steps", []):
            if step.get("latency_s") is not None:
                found.setdefault((trial["scenario"], "first audio", step.get("latency_source") or "runtime"),
                                 []).append(step["latency_s"])
        for entry in result.get("measured", []):
            value = entry.get("latency_s", entry.get("offset_s"))
            if value is not None:
                found.setdefault((trial["scenario"], entry["kind"], "reference mic"), []).append(value)
    return found


def _summary(values):
    values = sorted(values)
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, int(round(q * (len(values) - 1))))]  # noqa: E731
    return {"n": len(values), "median_s": pick(0.5), "p90_s": pick(0.9), "max_s": values[-1]}


def _cpu(summary):
    if not summary:
        return "no samples"
    return (f"mean {summary['mean_cpu_percent']}%, peak {summary['peak_cpu_percent']}% of all cores; "
            f"peak memory {summary['peak_rss_mb']} MB")


def write(path, markdown, results, meta, trials=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    data = {"meta": meta, "results": results}
    if trials is not None:
        summary = stats.summarize(trials)
        data["trials"] = [dict(t, outcome=stats.trial_outcome(t["result"]),
                               failure_class=stats.failure_class(t, summary)) for t in trials]
        data["summary"] = summary
    path.with_suffix(".json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def post_comment(markdown, repository, pr, token, opener=urllib.request.urlopen):
    """POST the report as a PR comment. Called only when the operator passes --post-comment."""
    if not (repository and pr and token):
        raise RuntimeError("Posting needs --repository, --pr and GITHUB_TOKEN")
    body = markdown if len(markdown) <= MAX_COMMENT else markdown[:MAX_COMMENT - 40] + "\n\n…(truncated; see the run)\n"
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/issues/{int(pr)}/comments",
                                     data=json.dumps({"body": body}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json"})
    with opener(request, timeout=30) as response:
        return json.loads(response.read().decode()).get("html_url")


def main(argv=None, opener=urllib.request.urlopen):
    parser = argparse.ArgumentParser(description="Post a saved robot-test report as a PR comment")
    parser.add_argument("--post", type=Path, required=True, help="The report's Markdown file")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)
    if not args.post.is_file():
        print(f"No report at {args.post}; nothing posted.", file=sys.stderr)
        return 1
    url = post_comment(args.post.read_text(encoding="utf-8"), args.repository, args.pr,
                       os.getenv("GITHUB_TOKEN", ""), opener)
    print(f"Posted: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
