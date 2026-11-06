"""Feature-owned offline suites and an overall gate; no device capture or providers."""
import argparse
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'Baymax-main'


def catalog(root=ROOT):
    suites = {}
    for path in sorted((root / 'tests').rglob('suite.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        name = item['name']
        if not re.fullmatch(r'[a-z_]+', name) or name in suites:
            raise ValueError('Duplicate or invalid suite name: ' + name)
        for selection in item['python']:
            directory = (root / selection['directory']).resolve()
            if not directory.is_relative_to(root.resolve()) or not directory.is_dir():
                raise ValueError('Suite directory is missing or outside the repository: ' + str(directory))
            for pattern in selection['patterns']:
                if not re.fullmatch(r'test_[A-Za-z0-9_*]+\.py', pattern):
                    raise ValueError('Invalid test pattern: ' + pattern)
        suites[name] = item
    if 'overall' not in suites:
        raise ValueError('Overall validation suite is required')
    return suites


def selected_suites(suites, paths, policy):
    chosen = {'overall', 'performance'}
    for path in paths:
        path = path.replace('\\', '/')
        if any(fnmatch.fnmatchcase(path, p) for p in policy['full_validation_paths']):
            return sorted(suites)
        owners = set()
        for name, suite in suites.items():
            patterns = suite['sources'] + [selection['directory'] + '/' + pattern
                        for selection in suite['python'] for pattern in selection['patterns']]
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
                owners.add(name)
        if not owners and not any(fnmatch.fnmatchcase(path, p) for p in policy['documentation_paths']):
            return sorted(suites)  # Unknown shared changes must not silently skip coverage.
        chosen.update(owners)
    return sorted(chosen)


def changed_paths(base):
    if base.startswith('-'):
        raise ValueError('Expected a Git ref, not an option')
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', base + '^{commit}'], cwd=ROOT, text=True).strip()
    tracked = subprocess.check_output(['git', 'diff', '--name-only', commit, '--'], cwd=ROOT, text=True)
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT, text=True)
    return sorted(set((tracked + untracked).splitlines()))


def run_child(name, output):
    suites = catalog()
    sys.path.insert(0, str(APP))
    import check_release
    result_suite = unittest.TestSuite()
    with check_release.local_settings_ignored():
        for selection in suites[name]['python']:
            directory = ROOT / selection['directory']
            for pattern in selection['patterns']:
                if not list(directory.glob(pattern)):
                    raise ValueError('No tests match ' + selection['directory'] + '/' + pattern)
                result_suite.addTests(unittest.TestLoader().discover(str(directory), pattern=pattern))
        count = result_suite.countTestCases()
        if not count and not suites[name].get('note'):
            raise ValueError('Empty suite: ' + name)
        result = unittest.TextTestRunner(verbosity=1).run(result_suite)
    payload = dict(passed=result.wasSuccessful(), tests=result.testsRun,
                   failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                   note=suites[name].get('note'))
    output.write_text(json.dumps(payload), encoding='utf-8')
    return 0 if payload['passed'] else 1


def main(argv=None):
    suites = catalog()
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--feature', choices=sorted(suites), action='append', help='Run this feature and overall checks; repeat for several features.')
    selection.add_argument('--changed', metavar='BASE', help='Select suites from changes since a Git ref; shared/unknown changes select all.')
    parser.add_argument('--list', action='store_true', help='List suite ownership without running tests.')
    parser.add_argument('--report', type=Path, default=ROOT / '.local-review/validation/report.md')
    parser.add_argument('--child', choices=sorted(suites), help=argparse.SUPPRESS)
    parser.add_argument('--result', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        if args.result is None:
            parser.error('Internal child run requires a result file')
        return run_child(args.child, args.result)
    if args.list:
        for name, suite in suites.items():
            print(name + ': ' + ', '.join(suite['sources']))
        return 0
    policy = json.loads((ROOT / 'tests/policy.json').read_text(encoding='utf-8'))
    try:
        chosen = sorted(set(args.feature) | {'overall', 'performance'}) if args.feature else (
                 selected_suites(suites, changed_paths(args.changed), policy) if args.changed else sorted(suites))
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    logs = ROOT / '.local-review/validation'
    logs.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    for name in chosen:
        result_file = logs / (name + '.json')
        result_file.unlink(missing_ok=True)
        start = time.monotonic()
        process = subprocess.run([sys.executable, str(Path(__file__)), '--child', name,
                                  '--result', str(result_file)], cwd=APP, capture_output=True, text=True)
        log = logs / (name + '.log')
        log.write_text(process.stdout + process.stderr, encoding='utf-8')
        outcome = json.loads(result_file.read_text()) if result_file.exists() else dict(passed=False, tests=0, failures=0, errors=1, skipped=0)
        outcome['passed'] = bool(outcome['passed'] and process.returncode == 0)
        outcome['seconds'] = round(time.monotonic() - start, 2)
        outcomes[name] = outcome
        status = 'N/A' if outcome['passed'] and outcome['tests'] == 0 and outcome.get('note') else ('PASS' if outcome['passed'] else 'FAIL')
        print(f"{status} {name}: {outcome['tests']} tests, {outcome['skipped']} skipped ({outcome['seconds']} s)", flush=True)
        if outcome.get('note'):
            print('  ' + outcome['note'])
        if not outcome['passed']:
            print('\n'.join((process.stdout + process.stderr).splitlines()[-35:]))
            print('Full log:', log)
    report = ['# Repository validation', '', 'Generated UTC: ' + datetime.now(timezone.utc).isoformat(),
              '', '| Suite | Result | Tests | Failures | Errors | Skipped |', '|---|---|---:|---:|---:|---:|']
    for name, result in outcomes.items():
        status = 'N/A' if result['passed'] and result['tests'] == 0 and result.get('note') else ('PASS' if result['passed'] else 'FAIL')
        report.append(f"| {name} | {status} | {result['tests']} | {result['failures']} | {result['errors']} | {result['skipped']} |")
    report += ['', 'Offline results do not establish physical board compatibility or release approval.',
               'Explain skips and run the live checklist before deployment. Detailed logs: .local-review/validation/.']
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('Report:', args.report)
    return 0 if all(value['passed'] for value in outcomes.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
