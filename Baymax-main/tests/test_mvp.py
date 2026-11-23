"""MVP first-run, saved configuration and startup checks; no capture or providers."""
import importlib.util
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dev_settings import save_configuration
import run_mvp

CATALOG = {"microphones": [{"index": 2, "name": "Fixed mic", "hostapi": "ALSA"}],
           "speakers": [{"index": 3, "name": "Fixed speaker", "hostapi": "ALSA"}],
           "cameras": [{"index": 0, "name": "Fixed camera"}]}


class MvpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = Path(self.temp.name)
        self.patch = patch.object(run_mvp, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.eyes_patch = patch.object(run_mvp, "eyes_session", side_effect=nullcontext)
        self.eyes_patch.start()
        self.addCleanup(self.eyes_patch.stop)
        self.pin_patch = patch.object(run_mvp, "use_performance_cpus")   # never pin the test process
        self.pin = self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.payload = dict(key="fake-key", microphone="2", speaker="3", camera="0")

    def save(self, **changes):
        save_configuration(self.root / ".env", {**self.payload, **changes}, CATALOG)

    def test_key_alone_does_not_skip_device_configuration(self):
        (self.root / ".env").write_text("GOOGLE_API_KEY=fake-key\n")
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))

    def test_saved_devices_and_explicit_no_camera_are_ready(self):
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))
        self.save(camera="none")
        self.assertTrue(run_mvp.configuration_ready(self.root / ".env"))
        self.save(camera="")
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))

    def test_first_setup_then_next_start_skips_setup(self):
        def setup():
            self.save()
            return True
        with patch.object(run_mvp, "configure_once", side_effect=setup) as configure, patch(
                "run_pc.main", return_value=0) as start:
            self.assertEqual(run_mvp.main([]), 0)
            self.assertEqual(run_mvp.main([]), 0)
        self.assertEqual(configure.call_count, 1)
        self.assertEqual(start.call_count, 2)

    def test_runtime_failure_does_not_reopen_setup_or_rewrite_devices(self):
        self.save()
        before = (self.root / ".env").read_bytes()
        with patch.object(run_mvp, "configure_once") as configure, patch("run_pc.main", return_value=1):
            self.assertEqual(run_mvp.main([]), 1)
        configure.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_explicit_setup_reopens_ui(self):
        self.save()
        with patch.object(run_mvp, "configure_once", return_value=True) as configure, patch("run_pc.main", return_value=0):
            self.assertEqual(run_mvp.main(["--setup"]), 0)
        configure.assert_called_once()

    def test_saved_eye_and_cue_overrides_are_loaded_before_feature_start(self):
        self.save()
        with (self.root / ".env").open("a") as config:
            config.write("BAYMAX_EYES_ENABLED=0\nBAYMAX_WAIT_CUE_ENABLED=0\nBAYMAX_EYES_PORT=/dev/custom\nBAYMAX_INTERRUPTION_ENABLED=1\nBAYMAX_TURN_END_SILENCE_MS=1200\n")
        def features():
            self.assertEqual(os.environ["BAYMAX_EYES_ENABLED"], "0")
            self.assertEqual(os.environ["BAYMAX_WAIT_CUE_ENABLED"], "0")
            self.assertEqual(os.environ["BAYMAX_EYES_PORT"], "/dev/custom")
            self.assertEqual(os.environ["BAYMAX_INTERRUPTION_ENABLED"], "1")
            self.assertEqual(os.environ["BAYMAX_TURN_END_SILENCE_MS"], "1200")
            return nullcontext()
        with patch.object(run_mvp, "eyes_session", side_effect=features), patch("run_pc.main", return_value=0):
            self.assertEqual(run_mvp.main([]), 0)

    def test_booting_eyes_start_before_setup_and_runtime_and_stop_after(self):
        order = []
        @contextmanager
        def features():
            order.append('eyes start')
            yield
            order.append('eyes stop')
        def setup():
            order.append('setup')
            self.save()
            return True
        self.pin.side_effect = lambda: order.append('pin with ' + os.environ.get('BAYMAX_CPU_PINNING', 'auto'))
        self.save()
        with (self.root / ".env").open("a", encoding="utf-8") as env:
            env.write("BAYMAX_CPU_PINNING=off\n")
        os.environ.pop("BAYMAX_CPU_PINNING", None)
        with patch.object(run_mvp, "eyes_session", side_effect=features), patch.object(
                run_mvp, "configure_once", side_effect=setup), patch(
                "run_pc.main", side_effect=lambda: order.append('runtime') or 0):
            self.assertEqual(run_mvp.main(["--setup"]), 0)
        # Fast-core pinning first (after .env, so its override applies): the eyes and
        # network threads and numpy's thread pool then start on the fast cores too.
        self.assertEqual(order, ['pin with off', 'eyes start', 'setup', 'runtime', 'eyes stop'])

    def test_runtime_failure_code_is_reported_to_the_eyes(self):
        self.save()
        for code, expected in [(1, True), (0, False)]:
            eyes = SimpleNamespace(exit_failed=False)
            with patch.object(run_mvp, "eyes_session", side_effect=lambda: nullcontext(eyes)), \
                    patch("run_pc.main", return_value=code):
                self.assertEqual(run_mvp.main([]), code)
            self.assertEqual(eyes.exit_failed, expected, code)

    def test_mvp_skips_shutdown_voice_analysis_unless_saved_override(self):
        self.save()
        os.environ.pop('BAYMAX_VOICE_ANALYSIS', None)
        seen = []
        with patch('run_pc.main', side_effect=lambda: seen.append(os.environ['BAYMAX_VOICE_ANALYSIS']) or 0):
            self.assertEqual(run_mvp.main([]), 0)
            with (self.root / ".env").open("a") as config:
                config.write("BAYMAX_VOICE_ANALYSIS=1\n")
            self.assertEqual(run_mvp.main([]), 0)
        self.assertEqual(seen, ['0', '1'])
        source = (Path(run_mvp.__file__).parent / "realtime_gemini_8.py").read_text(encoding="utf-8")
        self.assertIn('if os.getenv("BAYMAX_VOICE_ANALYSIS", "1") != "0":\n                _run_voice_analysis()', source)

    def test_mvp_leaves_the_waiting_hmm_off_unless_enabled(self):
        source = Path(run_mvp.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ.setdefault("BAYMAX_WAIT_CUE_ENABLED", "0")', source)

    def test_mvp_prevents_self_interruption_and_uses_400ms_by_default(self):
        self.save()
        os.environ.pop('BAYMAX_INTERRUPTION_ENABLED', None)
        os.environ.pop('BAYMAX_TURN_END_SILENCE_MS', None)
        def runtime():
            self.assertEqual(os.environ['BAYMAX_INTERRUPTION_ENABLED'], '0')
            self.assertEqual(os.environ['BAYMAX_TURN_END_SILENCE_MS'], '400')
            return 0
        with patch('run_pc.main', side_effect=runtime):
            self.assertEqual(run_mvp.main([]), 0)

    def test_mvp_sends_room_noise_not_digital_silence_by_default(self):
        # With interruptions off the mic is muted while Ember speaks; zeros then held up
        # Gemini's next turn. A value saved in .env still wins.
        self.save()
        os.environ.pop('BAYMAX_COMFORT_NOISE', None)
        seen = []
        def runtime():
            seen.append(os.environ.get('BAYMAX_COMFORT_NOISE'))
            return 0
        with patch('run_pc.main', side_effect=runtime):
            self.assertEqual(run_mvp.main([]), 0)
            with (self.root / ".env").open("a") as config:
                config.write("BAYMAX_COMFORT_NOISE=0\n")
            os.environ.pop('BAYMAX_COMFORT_NOISE', None)
            self.assertEqual(run_mvp.main([]), 0)
        self.assertEqual(seen, ['1', '0'])

    def test_cancelled_setup_never_starts_capture(self):
        with patch.object(run_mvp, "configure_once", return_value=False), patch("run_pc.main") as start:
            self.assertEqual(run_mvp.main([]), 0)
        start.assert_not_called()

    def test_lock_rejects_second_launcher_and_releases(self):
        path = self.root / ".mvp.lock"
        with run_mvp.runtime_lock(path):
            with self.assertRaisesRegex(RuntimeError, "already"):
                with run_mvp.runtime_lock(path):
                    self.fail("second launcher entered")
        with run_mvp.runtime_lock(path):
            pass

    def test_existing_runtime_prevents_setup_and_capture(self):
        with patch.object(run_mvp, "read_status", return_value={"state": "listening"}), patch.object(
                run_mvp, "configure_once") as configure, patch("run_pc.main") as start:
            self.assertEqual(run_mvp.main([]), 0)
        configure.assert_not_called()
        start.assert_not_called()

    def test_status_from_a_killed_runtime_does_not_block_restart(self):
        live = {"state": "listening", "pid": 4242}
        self.assertTrue(run_mvp.runtime_running(live, alive=lambda pid: True))
        self.assertFalse(run_mvp.runtime_running(live, alive=lambda pid: False))
        self.assertTrue(run_mvp.runtime_running(live, alive=lambda pid: None))  # Unknown: keep refusing.
        self.assertFalse(run_mvp.runtime_running({"state": "offline", "pid": 4242}, alive=lambda pid: True))
        self.save()
        with patch.object(run_mvp, "read_status", return_value=live), \
                patch.object(run_mvp, "pid_alive", return_value=False), patch("run_pc.main", return_value=0) as start:
            self.assertEqual(run_mvp.main([]), 0)
        start.assert_called_once()
        if os.name != "nt":
            self.assertTrue(run_mvp.pid_alive(os.getpid()))
        self.assertIsNone(run_mvp.pid_alive(None))

    def test_autostart_is_user_scoped_and_points_to_private_python(self):
        source = Path(__file__).resolve().parents[2] / "devops/mvp/install_autostart.py"
        spec = importlib.util.spec_from_file_location("mvp_install", source)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        project = self.root / "Ember with spaces"
        python = project / "Baymax-main/.venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        config_home = self.root / "user-config"
        desktop = self.root / "Localized Desktop"
        with patch.object(installer.subprocess, "run") as command:
            command.return_value.stdout = str(desktop) + "\n"
            unit, entry = installer.install(project, self.root, config_home)
        service = unit.read_text()
        # systemd does not strip shell-style quotes from WorkingDirectory.
        working = next(line.split('=', 1)[1] for line in service.splitlines()
                       if line.startswith('WorkingDirectory='))
        self.assertEqual(working, str(project / "Baymax-main"))
        self.assertFalse(working.startswith('"'))
        self.assertIn(f'ExecStart={installer.systemd_quote(python)}', service)
        self.assertIn("run_mvp.py", service)
        self.assertIn("KillSignal=SIGTERM", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("TimeoutStopSec=15"+chr(10), service)  # Reboots never wait minutes for Ember.
        # Opt-in updater runs first under system python and can never block startup.
        pre = next(line for line in service.splitlines() if line.startswith("ExecStartPre="))
        self.assertTrue(pre.startswith("ExecStartPre=-"))
        self.assertIn("update.py", pre)
        self.assertNotIn(".venv", pre)
        post = next(line for line in service.splitlines() if line.startswith("ExecStopPost="))
        self.assertTrue(post.startswith("ExecStopPost=-"))
        self.assertIn("show.py", post)
        self.assertTrue(post.endswith("--after-exit"))
        self.assertIn("WantedBy=default.target", service)
        self.assertIn("start-session.sh", entry.read_text())
        monitor = (config_home / "systemd/user/ember-monitor.service").read_text()
        self.assertIn("--serve", monitor)
        self.assertIn("Restart=always", monitor)
        self.assertNotIn("PartOf=ember-mvp", monitor)
        shortcut = (desktop / "ember-monitor.desktop").read_text()
        self.assertIn("--monitor", shortcut)
        self.assertEqual(shortcut, (self.root / ".local/share/applications/ember-monitor.desktop").read_text())
        self.assertFalse((self.root / "etc").exists())

    def test_monitor_display_detection_and_explicit_open(self):
        source = Path(__file__).resolve().parents[2] / "devops/mvp/monitor.py"
        spec = importlib.util.spec_from_file_location("mvp_monitor", source)
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        connector = self.root / "card0-HDMI-A-1"
        connector.mkdir()
        status = connector / "status"
        status.write_text("disconnected\n")
        self.assertFalse(monitor.display_connected(self.root))
        status.write_text("connected\n")
        self.assertTrue(monitor.display_connected(self.root))
        with patch.object(monitor, "display_connected", return_value=False), patch.object(
                monitor.urllib.request, "urlopen") as request, patch.object(monitor.webbrowser, "open", return_value=True) as browser:
            self.assertEqual(monitor.open_monitor(automatic=True), 0)
            request.assert_not_called()
            request.return_value.__enter__.return_value.read.return_value = b"<title>Ember dev monitor</title>"
            self.assertEqual(monitor.open_monitor(), 0)
            browser.assert_called_once_with(monitor.URL)

    def test_monitor_starts_without_browser_or_runtime_configuration(self):
        source = Path(__file__).resolve().parents[2] / "devops/mvp/monitor.py"
        spec = importlib.util.spec_from_file_location("mvp_monitor", source)
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        with patch.object(run_mvp, "configuration_ready") as configuration, patch.object(monitor.os, "execv") as execute:
            monitor.serve()
        configuration.assert_not_called()
        arguments = execute.call_args.args[1]
        self.assertIn("--no-browser", arguments)
        self.assertIn("--monitor", arguments)


if __name__ == "__main__":
    unittest.main()
