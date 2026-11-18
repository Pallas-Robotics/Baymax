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
        for path in ['Baymax-main/realtime_gemini_10.py', 'unknown_shared_module.py',
                     'Baymax-main/features/toolkits/gemini_tools.py']:  # the toolkit registry
            self.assertEqual(validation.selected_suites(self.suites, [path], self.policy), sorted(self.suites))

    def test_changed_test_selects_its_feature(self):
        self.assertEqual(validation.selected_suites(self.suites, ['Baymax-main/tests/test_voice_volume.py'], self.policy), ['conversational_ability', 'overall', 'performance'])

    def test_a_module_also_selects_the_suites_whose_tests_use_it(self):
        # Tests are owned by file name, so a suite also lists the modules its tests
        # construct, call, patch or read, also through a realtime_gemini_10.py function
        # they run (gemini_tools.py, the toolkit registry many suites call, selects them all), and the other
        # suites' test modules they import.
        for path, owners in [
                ('Baymax-main/features/toolkits/fall_detection.py',
                 ['conversational_ability', 'everyday', 'eyes', 'family', 'information', 'media',
                  'person_recognition', 'pose']),
                # test_toolkit_vision_process (pose) runs the eye-contact face model.
                ('Baymax-main/features/toolkits/proactive.py', ['conversational_ability', 'information', 'pose']),
                ('Baymax-main/features/conversational_ability/processing.py', ['conversational_ability', 'eyes', 'media']),
                ('Baymax-main/features/toolkits/wakeword.py', ['conversational_ability', 'information', 'media']),
                # test_memory_saving runs the real receive loop and between_turns with an AddressGate.
                ('Baymax-main/features/conversational_ability/addressing.py', ['conversational_ability', 'memory_palace', 'runtime']),
                # Imported by test_v10_runtime, test_gemini_preflight, test_memory_saving and
                # test_toolkit_family (whether Ember sends notes without turn_complete), and
                # by test_toolkit's search fallback (web_search uses in_use and adapt).
                ('Baymax-main/features/conversational_ability/live_models.py',
                 ['conversational_ability', 'family', 'information', 'memory_palace', 'runtime', 'startup']),
                # test_engage, test_live_eval, test_v10_runtime and test_toolkit_eyes read the persona.
                ('Baymax-main/features/persona.py', ['conversational_ability', 'eyes', 'languages', 'runtime']),
                # test_language_eval imports the live eval and test_live_eval's fake session.
                ('Baymax-main/evals/live_eval.py', ['languages', 'runtime']),
                ('Baymax-main/tests/test_live_eval.py', ['languages', 'runtime']),
                # test_bootstrap patches pc_hardware.arm_linux; the runtime's camera thread imports it.
                # test_person_recognition_runtime checks the face/voice children's places.
                ('Baymax-main/pc_hardware.py', ['device_settings', 'person_recognition', 'pose', 'runtime', 'startup']),
                # test_v10_runtime's OpenPoseTests run the real _open_pose and vision process;
                # test_toolkit_proactive patches its Remote.
                # test_person_recognition_runtime runs a raw_results child.
                ('Baymax-main/features/vision_process.py', ['information', 'person_recognition', 'pose', 'runtime']),
                ('Baymax-main/features/pose/backend.py', ['pose', 'runtime']),
                # test_memory_saving reads the service's stop timeout and the eyes' join.
                ('devops/mvp/install_autostart.py', ['memory_palace', 'startup']),
                ('Baymax-main/features/eyes/controller.py', ['conversational_ability', 'eyes', 'memory_palace']),
                ('Baymax-main/tests/test_public_core_integration.py', ['conversational_ability', 'memory_palace', 'runtime']),
                ('Baymax-main/tests/test_waiting_cue.py', ['conversational_ability', 'runtime'])]:
            with self.subTest(path=path):
                self.assertEqual(validation.selected_suites(self.suites, [path], self.policy),
                                 sorted(owners + ['overall', 'performance']))

    def test_docs_select_graph_checks(self):
        self.assertEqual(validation.selected_suites(self.suites, ['docs/features/FEATURE_LOG.md'], self.policy), ['graphify', 'overall', 'performance'])


if __name__ == '__main__':
    unittest.main()
