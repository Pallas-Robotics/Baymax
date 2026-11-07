"""Read-only Linux feature performance measurements; no capture or generated load."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
METRICS = ('cpu_percent', 'process_cpu_percent', 'rss_mib', 'system_used_mib',
           'temperature_c', 'npu_percent', 'recognition_ms', 'response_ms', 'audible_ms')


def read(path):
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def cpu_ticks(proc):
    # guest/guest_nice are already included in user/nice.
    values = [int(n) for n in read(proc / 'stat').splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def process_tree(proc, pid):
    processes = {}
    for path in proc.glob('[0-9]*/stat'):
        try:
            # comm may contain spaces and parentheses; fields after its final ')'
            # begin with field 3 (state).
            fields = read(path).rsplit(')', 1)[1].split()
            processes[int(path.parent.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[21]))
        except (ValueError, IndexError):
            continue  # process exited during this scan
    if pid not in processes:
        raise ValueError('Runtime PID is missing; start Ember and select its current PID.')
    selected = {pid}
    while True:
        children = {key for key, value in processes.items() if value[0] in selected}
        if children <= selected:
            break
        selected.update(children)
    # Per-process ticks let the sampler avoid negative deltas when children exit.
    return {key: processes[key][1] for key in selected}, sum(processes[key][2] for key in selected)


def sensors(sysroot, npu_file=None):
    temperatures = []
    for path in (sysroot / 'class/thermal').glob('thermal_zone*/temp'):
        try:
            value = float(read(path)) / 1000
            if math.isfinite(value) and -20 <= value <= 150:
                temperatures.append(value)
        except ValueError:
            continue
    paths = ([npu_file] if npu_file else list((sysroot / 'kernel/debug').glob('rknpu*/load'))
             + [sysroot / 'kernel/debug/viplite/core_loading'])
    loads = []
    for path in paths:
        loads.extend(float(n) for n in re.findall(r'(\d+(?:\.\d+)?)\s*%', read(path)))
    loads = [n for n in loads if 0 <= n <= 100]
    return {'temperature_c': max(temperatures) if temperatures else None,
            'npu_percent': max(loads) if loads else None}


def sample(pid, previous=None, proc=Path('/proc'), sysroot=Path('/sys'), npu_file=None):
    now = time.monotonic()
    total, idle = cpu_ticks(proc)
    ticks, pages = process_tree(proc, pid)
    memory = {match[0]: int(match[1]) for match in re.findall(r'^(MemTotal|MemAvailable):\s+(\d+)', read(proc / 'meminfo'), re.M)}
    result = dict(cpu_percent=None, process_cpu_percent=None,
                  rss_mib=pages * os.sysconf('SC_PAGE_SIZE') / 1048576,
                  system_used_mib=(memory['MemTotal'] - memory['MemAvailable']) / 1024,
                  **sensors(sysroot, npu_file))
    if previous is not None:
        elapsed = now - previous['time']
        delta = total - previous['total']
        if delta > 0 and elapsed > 0:
            result['cpu_percent'] = max(0, min(100, 100 * (1 - (idle - previous['idle']) / delta)))
            used = sum(max(0, value - previous['ticks'][key]) for key, value in ticks.items() if key in previous['ticks'])
            result['process_cpu_percent'] = used / os.sysconf('SC_CLK_TCK') / elapsed * 100
    return result, dict(time=now, total=total, idle=idle, ticks=ticks)


def monitor(path, pid, now):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        age = now - float(data['heartbeat'])
        if data.get('pid') != pid or not 0 <= age <= 5:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def timing_rows(data, start, end, turns):
    for row in data.get('conversation_timing', []):
        # Only turns whose detected speech ends inside this measurement window.
        # Overwrite observed turns as playback arrives; never store transcript text.
        if start <= row.get('speech_end', 0) <= end:
            turns[row['id']] = {key: row[key] for key in ('id', 'speech_end', 'playback', 'delays') if key in row}


def summary(values):
    values = sorted(value for value in values if isinstance(value, (int, float)) and math.isfinite(value))
    if not values:
        return None
    return dict(count=len(values), mean=round(statistics.mean(values), 2),
                p95=round(values[max(0, math.ceil(len(values) * .95) - 1)], 2), max=round(max(values), 2))


def report(samples, turns, baseline=None):
    metrics = {key: summary([row.get(key) for row in samples]) for key in METRICS[:6]}
    for key in METRICS[6:]:
        metrics[key] = summary([row.get('delays', {}).get(key) for row in turns.values()])
    deltas = {}
    if baseline:
        for key, value in metrics.items():
            old = baseline['metrics'].get(key)
            if value and old:
                deltas[key] = {stat: round(value[stat] - old[stat], 2) for stat in ('mean', 'p95', 'max')}
    return dict(format=1, metrics=metrics, delta_from_baseline=deltas,
                speech_turns=len(turns), completed_turns=sum('playback' in row for row in turns.values()),
                unavailable=[key for key, value in metrics.items() if value is None])


def violations(result, limits, min_turns=0, require_npu=False, max_added_latency=None):
    errors = []
    for metric, limit in limits.items():
        value = result['metrics'][metric]
        if value is None:
            errors.append(metric + ': required measurement unavailable')
        elif value['max'] > limit:
            errors.append(f'{metric}: peak {value["max"]} exceeds {limit}')
    if result['completed_turns'] < min_turns:
        errors.append(f'Only {result["completed_turns"]} completed speech turns; need {min_turns}')
    if min_turns and result['speech_turns'] > result['completed_turns']:
        errors.append('Speech turns without playback remain; extend the window and investigate')
    if require_npu and result['metrics']['npu_percent'] is None:
        errors.append('NPU telemetry required but unavailable')
    if max_added_latency is not None:
        delta = result['delta_from_baseline'].get('audible_ms')
        if delta is None:
            errors.append('Baseline and current speech-to-playback latency are required')
        elif delta['p95'] > max_added_latency:
            errors.append(f'Added p95 audible latency {delta["p95"]} ms exceeds {max_added_latency}')
    return errors


def validate_baseline(data, board):
    if (not isinstance(data, dict) or data.get('format') != 1
            or not isinstance(data.get('metrics'), dict) or data.get('status') == 'FAIL'):
        raise ValueError('Expected a successful format-1 performance baseline')
    if data.get('board') != board:
        raise ValueError('Baseline board does not match; collect both runs on the same board.')
    for metric, value in data['metrics'].items():
        if metric not in METRICS or value is None:
            continue
        if not isinstance(value, dict) or any(type(value.get(stat)) not in (int, float)
                or not math.isfinite(value[stat]) for stat in ('mean', 'p95', 'max')):
            raise ValueError('Invalid baseline metric: ' + metric)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feature', required=True, help='Feature/workload label, e.g. eyes or pose+conversation')
    parser.add_argument('--pid', type=int, help='Ember runtime PID; defaults to current status-file PID')
    parser.add_argument('--status-file', type=Path, default=ROOT / 'Baymax-main/.runtime-status.json')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / '.local-review/performance/report.json')
    parser.add_argument('--npu-load-file', type=Path, help='Optional vendor load file containing percentages; read only')
    parser.add_argument('--require-npu', action='store_true')
    parser.add_argument('--min-turns', type=int, default=0)
    for flag in ('cpu-percent', 'rss-mib', 'temp-c', 'audible-ms', 'npu-percent', 'added-latency-ms'):
        parser.add_argument('--max-' + flag, type=float)
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('Run this hardware check on the Linux Orange Pi, not the development desktop.')
    numbers = [args.seconds, args.interval] + [value for key, value in vars(args).items() if key.startswith('max_') and value is not None]
    if any(not math.isfinite(value) or value <= 0 for value in numbers) or args.interval > args.seconds or args.min_turns < 0:
        parser.error('Duration/interval/budgets must be finite and positive; interval <= duration; min-turns >= 0.')
    try:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8')) if args.baseline else None
        board = read(Path('/proc/device-tree/model')).strip('\x00\n') or os.uname().machine
        if baseline is not None:
            validate_baseline(baseline, board)
        if args.max_added_latency_ms is not None and baseline is None:
            raise ValueError('--max-added-latency-ms requires --baseline')
        pid = args.pid or json.loads(args.status_file.read_text(encoding='utf-8'))['pid']
        if type(pid) is not int or pid <= 0:
            raise ValueError('Expected a positive runtime PID')
        started, deadline = time.time(), time.monotonic() + args.seconds
        samples, turns, previous, stale = [], {}, None, 0
        while True:
            row, previous = sample(pid, previous, npu_file=args.npu_load_file)
            samples.append(row)
            data = monitor(args.status_file, pid, time.time())
            if data is None:
                stale += 1
            else:
                timing_rows(data, started, time.time(), turns)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
        result = report(samples, turns, baseline)
        result.update(feature=args.feature, platform=sys.platform, board=board, kernel=os.uname().release,
                      cpu_count=os.cpu_count(), duration_s=round(time.time() - started, 2),
                      recorded_utc=datetime.now(timezone.utc).isoformat(), samples=len(samples), stale_monitor_samples=stale)
        limits = {metric: getattr(args, flag) for metric, flag in (
            ('cpu_percent', 'max_cpu_percent'), ('rss_mib', 'max_rss_mib'),
            ('temperature_c', 'max_temp_c'), ('audible_ms', 'max_audible_ms'), ('npu_percent', 'max_npu_percent'))
            if getattr(args, flag) is not None}
        result['failures'] = violations(result, limits, args.min_turns, args.require_npu, args.max_added_latency_ms)
        if stale and (args.min_turns or args.max_audible_ms or args.max_added_latency_ms):
            result['failures'].append('Monitor was stale/missing or runtime restarted; latency coverage is incomplete')
        checked = bool(limits or args.min_turns or args.require_npu or args.max_added_latency_ms)
        result['status'] = 'FAIL' if result['failures'] else ('PASS' if checked else 'MEASURED')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(result['status'] + ': ' + args.feature + ' -> ' + str(args.output))
        print('Unavailable: ' + ', '.join(result['unavailable']))
        for failure in result['failures']:
            print(failure)
        return 1 if result['failures'] else 0
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
