import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feature_performance', Path(__file__).with_name('check.py'))
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class PerformanceTests(unittest.TestCase):
    def test_process_tree_includes_children_not_unrelated_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for pid, parent, ticks, pages in [(10, 1, 5, 20), (11, 10, 7, 30), (12, 11, 2, 4), (20, 1, 99, 999)]:
                fields = ['0'] * 22
                fields[0], fields[1], fields[11], fields[12], fields[21] = 'S', str(parent), str(ticks), '1', str(pages)
                (proc / str(pid)).mkdir()
                (proc / str(pid) / 'stat').write_text(f'{pid} (worker ) name) ' + ' '.join(fields))
            self.assertEqual(check.process_tree(proc, 10), ({10: 6, 11: 8, 12: 3}, 54))
            with self.assertRaises(ValueError):
                check.process_tree(proc, 404)

    def test_cpu_delta_and_memory_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'stat').write_text('cpu  100 0 100 600 100 0 0 100 999 999\n')
            (root / 'meminfo').write_text('MemTotal: 4096 kB\nMemAvailable: 1024 kB\n')
            previous = dict(time=1, total=900, idle=650, ticks={10: 100, 11: 500})
            with patch.object(check, 'process_tree', return_value=({10: 150, 12: 999}, 256)), \
                 patch.object(check.time, 'monotonic', return_value=2), \
                 patch.object(check.os, 'sysconf', side_effect=lambda key: 4096 if key == 'SC_PAGE_SIZE' else 100, create=True):
                row, _ = check.sample(10, previous, proc=root, sysroot=root)
            self.assertEqual(row['cpu_percent'], 50)
            self.assertEqual(row['process_cpu_percent'], 50)
            self.assertEqual(row['rss_mib'], 1)
            self.assertEqual(row['system_used_mib'], 3)

    def test_temperature_and_multicore_npu_unavailable_is_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(check.sensors(root), dict(temperature_c=None, npu_percent=None))
            thermal = root / 'class/thermal/thermal_zone0'
            thermal.mkdir(parents=True)
            (thermal / 'temp').write_text('65000')
            npu = root / 'load'
            npu.write_text('NPU load: Core0: 20%, Core1: 70%, Core2: 0%')
            self.assertEqual(check.sensors(root, npu), dict(temperature_c=65, npu_percent=70))
            vip = root / 'kernel/debug/viplite'
            vip.mkdir(parents=True)
            (vip / 'core_loading').write_text('NPU Loading -----> Core0:  0%')
            self.assertEqual(check.sensors(root), dict(temperature_c=65, npu_percent=0))

    def test_fresh_monitor_requires_same_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            path.write_text(json.dumps(dict(pid=10, heartbeat=100)))
            self.assertIsNotNone(check.monitor(path, 10, 104))
            for pid, now in [(11, 104), (10, 106), (10, 99)]:
                self.assertIsNone(check.monitor(path, pid, now))
            path.write_text('{broken')
            self.assertIsNone(check.monitor(path, 10, 104))

    def test_turns_exclude_old_rows_deduplicate_and_strip_text(self):
        turns = {}
        data = {'conversation_timing': [dict(id=1, speech_end=50),
                dict(id=2, speech_end=110, text='private words', delays={})], 'transcripts': ['private']}
        check.timing_rows(data, 100, 120, turns)
        data['conversation_timing'][1].update(playback=112, delays={'audible_ms': 2000})
        check.timing_rows(data, 100, 125, turns)
        self.assertEqual(list(turns), [2])
        self.assertNotIn('private', json.dumps(turns))
        self.assertEqual(check.report([], turns)['completed_turns'], 1)

    def test_percentiles_and_added_latency_budget(self):
        self.assertEqual(check.summary([1, 2, 3, 4, 100, None, float('nan')]),
                         dict(count=5, mean=22, p95=100, max=100))
        turns = {1: dict(playback=2, delays={'audible_ms': 1200})}
        baseline = {'metrics': {'audible_ms': check.summary([1000])}}
        result = check.report([{'rss_mib': 512}], turns, baseline)
        self.assertEqual(result['delta_from_baseline']['audible_ms']['p95'], 200)
        self.assertFalse(check.violations(result, {'rss_mib': 512}, min_turns=1, max_added_latency=200))
        self.assertEqual(len(check.violations(result, {'rss_mib': 511}, require_npu=True, max_added_latency=199)), 3)

    def test_missing_latency_and_unanswered_turns_fail_required_checks(self):
        result = check.report([], {1: {'delays': {}}})
        failures = check.violations(result, {'audible_ms': 2000}, min_turns=1, max_added_latency=100)
        self.assertEqual(len(failures), 4)
        self.assertIsNone(result['metrics']['npu_percent'])

    def test_baseline_rejects_other_board_failed_run_and_invalid_metrics(self):
        good = dict(format=1, board='OPi4', status='MEASURED', metrics={'rss_mib': check.summary([100])})
        self.assertEqual(check.validate_baseline(good, 'OPi4'), good)
        for data in [[], dict(good, board='OPi5'), dict(good, status='FAIL'),
                     dict(good, metrics={'rss_mib': {'mean': 'bad'}})]:
            with self.assertRaises(ValueError):
                check.validate_baseline(data, 'OPi4')

    def test_sampler_cli_writes_only_aggregate_report(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'report.json'
            with patch.object(check.sys, 'platform', 'linux'), \
                 patch.object(check.os, 'uname', return_value=type('OS', (), dict(machine='aarch64', release='test'))(), create=True), \
                 patch.object(check.time, 'monotonic', side_effect=[0, 2]), \
                 patch.object(check, 'sample', return_value=({'rss_mib': 100}, {})), \
                 patch.object(check, 'monitor', return_value=None):
                code = check.main(['--feature', 'eyes', '--pid', '10', '--seconds', '1', '--output', str(output), '--max-rss-mib', '200'])
            self.assertEqual(code, 0)
            data = json.loads(output.read_text())
            self.assertEqual(data['status'], 'PASS')
            self.assertEqual(data['metrics']['rss_mib']['max'], 100)
            self.assertNotIn('transcripts', data)
            self.assertNotIn('samples_raw', data)


if __name__ == '__main__':
    unittest.main()
