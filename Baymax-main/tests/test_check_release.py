import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dotenv
from dotenv import dotenv_values, load_dotenv  # bound before the gate starts, as in a module

import check_release
from check_release import collect_suites, local_settings_ignored, render_report, run_sections

# Documented in .env.example; each failed a test written for the defaults.
ROBOT_SETTINGS = {"BAYMAX_VOICE_VOLUME_TOOL": "0", "BAYMAX_CHECKIN_MORNING": "07:00-10:00",
                  "BAYMAX_CHECKIN_AFTERNOON": "13:00-15:00", "BAYMAX_CHECKIN_UPTIME_S": "600",
                  "EMBER_DISCOVERY_ENABLED": "0", "BAYMAX_TOOLKIT_ENABLED": "0"}


class ReviewGateTests(unittest.TestCase):
    def test_missing_required_files_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                collect_suites(Path(directory))

    def test_failed_and_empty_sections_cannot_pass(self):
        class Broken(unittest.TestCase):
            def runTest(self):
                self.fail("simulated regression")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            outcomes = run_sections({"core": unittest.TestSuite([Broken()]), "toolkit": unittest.TestSuite()})
        self.assertFalse(outcomes["core"]["passed"])
        self.assertFalse(outcomes["toolkit"]["passed"])
        self.assertEqual(outcomes["core"]["failures"], 1)

    def test_offline_pass_does_not_grant_live_or_lead_approval(self):
        outcomes = {section: {"passed": True, "tests": 1, "failures": 0, "errors": 0, "skipped": 0}
                    for section in ("core", "toolkit")}
        with patch("check_release.subprocess.run", side_effect=OSError):
            report = render_report(outcomes)
        self.assertIn("core: PASS", report)
        self.assertIn("toolkit: PASS", report)
        self.assertIn("not live validation or release approval", report)
        self.assertIn("[ ] Start Ember", report)
        self.assertIn("approval: PENDING", report)

    def test_robots_skip_the_repository_bookkeeping_checks_that_ci_runs(self):
        # A test file someone forgot to list in a suite.json failed this gate, and a robot
        # rolls back when it fails. Robots keep the source, chat UI and sampler checks.
        repository = check_release.ROOT.parent
        if not (repository / "tests/overall").is_dir():
            self.skipTest("only the application folder is here, without the repository's checks")

        def collected(github_actions):
            files = set()

            def discover(loader, start_dir, pattern="test*.py", top_level_dir=None):
                files.update(path.relative_to(repository).as_posix() for path in Path(start_dir).glob(pattern))
                return unittest.TestSuite()
            with patch.dict(os.environ), patch.object(unittest.TestLoader, "discover", discover):
                os.environ.pop("GITHUB_ACTIONS", None)
                if github_actions:
                    os.environ["GITHUB_ACTIONS"] = "true"  # set by GitHub Actions
                collect_suites(check_release.ROOT / "tests")
            return files
        robot, ci = collected(False), collected(True)
        for checks in ("Baymax-main/tests/test_check_release.py", "tests/overall/test_repository.py",
                       "devops/performance/test_performance.py"):
            self.assertIn(checks, robot)
        self.assertNotIn("tests/overall/test_validation.py", robot)
        self.assertEqual(ci - robot, {"tests/overall/test_validation.py"})


class LocalSettingsTests(unittest.TestCase):
    """The Orange Pi 5 Max's updater runs the gate before each start, next to the robot's
    .env; a setting there must not fail tests written for the defaults, or the updater
    rolls back every release."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "robot", "Baymax-main")
        self.root.mkdir(parents=True)
        robot_env = "".join(f"{name}={value}\n" for name, value in ROBOT_SETTINGS.items())
        (self.root / ".env").write_text(robot_env + "GOOGLE_API_KEY=robot-key\n")
        (self.root.parent / ".env").write_text("BAYMAX_VOICE=Puck\n")  # found by a bare load_dotenv() too
        self.own = Path(folder.name, "test-own.env")
        self.own.write_text("BAYMAX_TEST_OWN=1\n")
        environment = patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "off", "GOOGLE_API_KEY": "shell-key",
                                              "EMBER_TEST_UNRELATED": "", "NOT_EMBERS": "kept"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_machines_settings_and_env_files_are_ignored_while_the_checks_run(self):
        with local_settings_ignored(self.root):
            for name in ("BAYMAX_CPU_PINNING", "GOOGLE_API_KEY", "EMBER_TEST_UNRELATED"):
                self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["NOT_EMBERS"], "kept")
            for path in (self.root / ".env", self.root.parent / ".env", str(self.root / ".." / ".env")):
                self.assertFalse(load_dotenv(path, override=True))
                self.assertFalse(dotenv.load_dotenv(path, override=True))
                self.assertEqual(dotenv_values(path), {})
            self.assertFalse(set(os.environ) & {*ROBOT_SETTINGS, "GOOGLE_API_KEY", "BAYMAX_VOICE"})
            self.assertEqual(dotenv_values(self.own), {"BAYMAX_TEST_OWN": "1"})  # a test's own .env
            self.assertTrue(load_dotenv(self.own))
            self.assertEqual(os.environ["BAYMAX_TEST_OWN"], "1")
        # Afterwards: the machine's environment as it was, and its .env readable again.
        self.assertEqual(os.environ["BAYMAX_CPU_PINNING"], "off")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "shell-key")
        self.assertNotIn("BAYMAX_TEST_OWN", os.environ)
        self.assertEqual(dotenv_values(self.root / ".env")["BAYMAX_VOICE_VOLUME_TOOL"], "0")

    def test_the_model_folder_for_the_real_model_memory_tests_is_kept(self):
        # Not a setting of Ember: it points test_semantic_embedder's real-model tests at a
        # downloaded model. Removed, those tests never ran under this gate or tests/run.py.
        with patch.dict(os.environ, {"BAYMAX_MEMORY_MODEL_DIR": "/models/minilm", "BAYMAX_CPU_PINNING": "off"}), \
                local_settings_ignored(self.root):
            self.assertEqual(os.environ.get("BAYMAX_MEMORY_MODEL_DIR"), "/models/minilm")
            self.assertNotIn("BAYMAX_CPU_PINNING", os.environ)

    def test_the_new_features_pass_on_a_robot_that_changed_their_settings(self):
        names = ["test_voice_volume.VoiceVolumeWiringTests.test_tool_is_offered_and_answered_even_with_the_toolkit_off",
                 "test_voice_volume.VoiceVolumeWiringTests.test_playback_volume_stays_with_the_audiobook_and_listen_tools",
                 "test_toolkit_checkins.CheckInTests.test_only_inside_a_window",
                 "test_toolkit_checkins.CheckInTests.test_never_soon_after_boot_or_before_the_first_session",
                 "test_toolkit_brain_games.BrainGameToolkitTests"
                 ".test_on_by_default_with_discovery_and_routed_through_gemini_tools",
                 "test_toolkit_public.RegistryTests.test_discovery_is_on_by_default_without_the_public_toolkit"]
        with patch.dict(os.environ, ROBOT_SETTINGS), local_settings_ignored(self.root):
            load_dotenv(self.root / ".env")  # what baymax_cloud does on import on the robot
            suite = unittest.TestLoader().loadTestsFromNames(names)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertEqual(result.testsRun, len(names))
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_the_gate_collects_and_runs_the_checks_with_them_ignored(self):
        seen = []

        def step(*args):
            seen.append(os.environ.get("GOOGLE_API_KEY"))
            return {"core": {"passed": True}}
        with patch.object(check_release, "collect_suites", side_effect=step), \
                patch.object(check_release, "run_sections", side_effect=step), \
                patch.object(check_release, "render_report", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_release.main([]), 0)
        self.assertEqual(seen, [None, None])
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "shell-key")
