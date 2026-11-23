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
    if result.get("error"):
        return "error"
    return "pass" if not any(step["failures"] for step in result.get("steps", [])) else "FAIL"


def render(results, meta):
    """The report. Holds only what the rig played and what the runtime transcribed of it."""
    counts = {}
    for result in results.values():
        counts[outcome(result)] = counts.get(outcome(result), 0) + 1
    verdict = "PASS" if not counts.get("FAIL") and not counts.get("error") and not meta.get("rig_error") else "FAIL"
    lines = [f"## Ember robot test: {verdict}", "",
             f"- Commit: `{meta.get('commit', 'unknown')}`" + (f" (PR #{meta['pr']})" if meta.get("pr") else ""),
             f"- Robot: {meta.get('robot', 'test robot')}; runtime `{meta.get('core') or 'unknown'}`",
             f"- Started {meta.get('started', '')}, {meta.get('minutes', 0):.1f} min; "
             + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
             f"- Calibration: {meta.get('calibration', 'not checked')}",
             f"- Runtime CPU (whole run): {_cpu(meta.get('cpu'))}",
             f"- Recordings deleted after the run: {meta.get('recordings_deleted', 'unknown')}"]
    if meta.get("rig_error"):
        lines += ["", f"**Rig problem (not necessarily the PR):** {meta['rig_error']}"]
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
        note = _clip(result.get("error") or (failed[0] if failed else ""), 90).replace("|", "/")
        lines.append(f"| {name} | {state} | {first} | {note} |")
        if state != "pass":
            detail += ["", f"### {name}"]
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


def _cpu(summary):
    if not summary:
        return "no samples"
    return (f"mean {summary['mean_cpu_percent']}%, peak {summary['peak_cpu_percent']}% of all cores; "
            f"peak memory {summary['peak_rss_mb']} MB")


def write(path, markdown, results, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results}, indent=2, default=str),
                                         encoding="utf-8")


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
