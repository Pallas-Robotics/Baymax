"""Offline review gate; never opens devices or contacts service providers."""
import argparse
import contextlib
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import platform
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent
# A machine's own settings: its .env (Setup, .env.example) or its shell.
LOCAL_SETTING_PREFIXES = ("BAYMAX_", "EMBER_")
LOCAL_SETTING_NAMES = {"GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "FLASK_SECRET_KEY"}
REQUIRED_TEST_FILES = {
    "core": {"test_dev_settings.py", "test_devUI_http.py", "test_pc_hardware.py", "test_runtime_monitor.py"},
    "toolkit": {"test_toolkit.py"},
}
MANUAL_CHECKLIST = """## Core: live checks (pending until a person records evidence)
- [ ] Fresh install, configure devices/key, save and reopen successfully.
- [ ] Test actual microphone, speaker and camera in devUI.
- [ ] Verify a rejected key stops startup before capture; restore a working key.
- [ ] Start Ember, confirm key/model verification, say hello and receive audible Gemini speech.
- [ ] Have a multi-turn conversation and interrupt a reply; record behavior.
- [ ] Verify live transcripts, listening and reply timing in the monitor.
- [ ] Stop and restart; confirm capture/audio stop and devices are released.
- [ ] Disable toolkit; confirm core conversation still works.

## Toolkit: live and integration checks (pending)
- [ ] Ask location; verify approximate city/region or a clear failure.
- [ ] Correct location with city/region/country; resolve ambiguity before acceptance.
- [ ] Ask weather; compare spoken result, units and time with provider data.
- [ ] Ask weather for another city; confirm saved user location stays unchanged.
- [ ] Verify one toolkit start/end message per call, then continue conversation.
- [ ] Record slow/unavailable provider behavior and confirm conversation recovery.
- [ ] If fall detection is enabled: verify disabled/loading/running/error states, actual FPS, safe positive/negative clips, camera loss, check-in and concurrent conversation on the target device.
- [ ] For each added/changed toolkit, record normal, invalid-input and failure cases,
      core integration, disabled behavior, dependencies and external data flows.

## Every feature: performance evidence (pending)
- [ ] Follow devops/performance/README.md: baseline/off, active/on and combined load on the target board.
- [ ] Record CPU/RAM peaks, NPU telemetry or justified N/A, temperature and recognition/reply/playback p95.
- [ ] Check explicit budgets and added p95 latency; record completed/missed turns, sensor gaps and repeat runs.

## Evidence and lead review
- Tester, date, OS, devices and runtime/deployed baseline:
- Core live result and evidence:
- Each toolkit result and evidence:
- Untested platforms, failures and justified N/A items:
- James's review/approval: PENDING (not granted by this command).

Do not paste API keys, private transcripts or recordings into a public PR.
"""


@contextlib.contextmanager
def local_settings_ignored(root=ROOT):
    """The checks test the shipped defaults, so they give the same result on every
    machine. The Orange Pi 5 Max's updater runs them before each start, in the folder
    that holds the robot's .env: a documented setting there (BAYMAX_VOICE_VOLUME_TOOL=0,
    a check-in window) failed tests written for the defaults once a module (baymax_cloud)
    had loaded that .env on import, and every release was rolled back.

    While the checks run, this machine's settings are out of the environment and its
    .env files (the one in `root` or a folder above, which a bare load_dotenv() finds)
    read as empty, through every python-dotenv entry point, even one imported earlier.
    A test's own .env in a temporary folder still loads."""
    import dotenv.main

    root = Path(root).resolve()
    ignored = {os.path.normcase(folder / ".env") for folder in (root, *root.parents)}
    real = dotenv.main.DotEnv

    class WithoutLocalEnv(real):
        def __init__(self, dotenv_path, stream=None, *args, **kwargs):
            if dotenv_path and os.path.normcase(Path(dotenv_path).expanduser().resolve()) in ignored:
                dotenv_path, stream = None, io.StringIO("")
            super().__init__(dotenv_path, stream, *args, **kwargs)

    saved = dict(os.environ)
    for name in [name for name in os.environ
                 if name.startswith(LOCAL_SETTING_PREFIXES) or name in LOCAL_SETTING_NAMES]:
        del os.environ[name]
    dotenv.main.DotEnv = WithoutLocalEnv
    try:
        yield
    finally:
        dotenv.main.DotEnv = real
        os.environ.clear()
        os.environ.update(saved)


def collect_suites(test_dir):
    paths = sorted(test_dir.glob("test_*.py"))
    names = {path.name for path in paths}
    missing = {section: sorted(required - names) for section, required in REQUIRED_TEST_FILES.items()}
    if any(missing.values()):
        raise ValueError(f"Required test files missing: {missing}")
    suites = {"core": unittest.TestSuite(), "toolkit": unittest.TestSuite()}
    for path in paths:
        section = "toolkit" if path.name.startswith("test_toolkit") else "core"
        # Discover every new test module, not only today's fixed list.
        suites[section].addTests(unittest.TestLoader().discover(str(test_dir), pattern=path.name))
    overall = ROOT.parent / 'tests/overall'
    if Path(test_dir).resolve() == (ROOT / 'tests').resolve() and overall.is_dir():
        suites['core'].addTests(unittest.TestLoader().discover(str(overall), pattern='test_*.py'))
        performance = ROOT.parent / 'devops/performance'
        if performance.is_dir():
            suites['core'].addTests(unittest.TestLoader().discover(str(performance), pattern='test_*.py'))
    return suites


def run_sections(suites):
    outcomes = {}
    for section in ("core", "toolkit"):
        suite = suites[section]
        count = suite.countTestCases()
        print(f"\n=== {section.upper()} OFFLINE CHECKS ({count}) ===", flush=True)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        outcomes[section] = {"passed": count > 0 and result.wasSuccessful(),
                             "tests": result.testsRun, "failures": len(result.failures),
                             "errors": len(result.errors), "skipped": len(result.skipped)}
    return outcomes


def render_report(outcomes):
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unavailable"
    lines = ["# Ember review checklist", "", f"Commit: {commit}",
             f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
             f"Platform: {platform.system()} {platform.release()}; Python {platform.python_version()}", "",
             "## Automated offline results", ""]
    for section, result in outcomes.items():
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(f"- {section}: {status}; {result['tests']} tests, {result['failures']} failures, "
                     f"{result['errors']} errors, {result['skipped']} skipped.")
    lines += ["", "Offline checks use simulated devices/providers. PASS is not live validation or release approval.",
              "Skipped tests must be explained in the PR. Local uncommitted changes are not identified by the commit ID.",
              "", MANUAL_CHECKLIST]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write a fresh Markdown report/checklist at this path (overwrites it).")
    args = parser.parse_args(argv)
    try:
        with local_settings_ignored():
            outcomes = run_sections(collect_suites(ROOT / "tests"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = render_report(outcomes)
    print("\n" + report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Report saved: {args.report}")
    return 0 if all(result["passed"] for result in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
