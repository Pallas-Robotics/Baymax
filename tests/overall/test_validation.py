import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('repository_validation', ROOT / 'tests/validation.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


class SuiteSelectionTests(unittest.TestCase):
    def setUp(self):
        self.suites = validation.catalog()
        self.policy = json.loads((ROOT / 'tests/policy.json').read_text())

    def test_every_application_test_has_one_owner(self):
        owned = {}
        for name, suite in self.suites.items():
            for selection in suite['python']:
                for pattern in selection['patterns']:
                    for path in (ROOT / selection['directory']).glob(pattern):
                        owned.setdefault(path.resolve(), []).append(name)
        for path in (ROOT / 'Baymax-main/tests').glob('test_*.py'):
            self.assertEqual(len(owned.get(path.resolve(), [])), 1, (path.name, owned.get(path.resolve())))

    def test_single_feature_selects_it_and_overall(self):
        self.assertEqual(validation.selected_suites(self.suites, ['Baymax-main/features/eyes/driver.py'], self.policy), ['eyes', 'overall', 'performance'])

    def test_shared_and_unknown_changes_require_full_validation(self):
        for path in ['Baymax-main/realtime_gemini_10.py', 'unknown_shared_module.py']:
            self.assertEqual(validation.selected_suites(self.suites, [path], self.policy), sorted(self.suites))

    def test_changed_test_selects_its_feature(self):
        self.assertEqual(validation.selected_suites(self.suites, ['Baymax-main/tests/test_voice_volume.py'], self.policy), ['conversational_ability', 'overall', 'performance'])

    def test_a_module_also_selects_the_suites_whose_tests_use_it(self):
        # Tests are owned by file name, so a suite also lists the modules its tests
        # construct, call or patch (not the shared registries such as gemini_tools.py).
        for path, owners in [
                ('Baymax-main/features/toolkits/fall_detection.py', ['conversational_ability', 'eyes', 'information', 'media', 'pose']),
                ('Baymax-main/features/conversational_ability/processing.py', ['conversational_ability', 'eyes', 'media']),
                ('Baymax-main/features/toolkits/wakeword.py', ['conversational_ability', 'information', 'media']),
                ('Baymax-main/features/conversational_ability/addressing.py', ['conversational_ability', 'runtime']),
                ('Baymax-main/pc_hardware.py', ['device_settings', 'pose'])]:
            with self.subTest(path=path):
                self.assertEqual(validation.selected_suites(self.suites, [path], self.policy),
                                 sorted(owners + ['overall', 'performance']))

    def test_docs_select_graph_checks(self):
        self.assertEqual(validation.selected_suites(self.suites, ['docs/features/FEATURE_LOG.md'], self.policy), ['graphify', 'overall', 'performance'])


if __name__ == '__main__':
    unittest.main()
