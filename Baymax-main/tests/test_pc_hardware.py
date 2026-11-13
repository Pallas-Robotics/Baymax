import ast
from contextlib import ExitStack
import os
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pc_hardware
from pc_hardware import camera_candidates, open_camera, select_audio_devices


ROOT = Path(__file__).resolve().parents[1]


class ParityTests(unittest.TestCase):
    def test_shared_runtime_functions_are_unchanged(self):
        original = (ROOT / "upstream/realtime_gemini_8.py").read_text(encoding="utf-8")
        runtime = (ROOT / "realtime_gemini_8.py").read_text(encoding="utf-8")
        camera_adapter = ('    from pc_hardware import camera_candidates, open_camera\n'
                          '    for cam_idx in (camera_candidates() if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc" else [0, 1, 2]):\n'
                          '        test = open_camera(cv2, cam_idx) if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc" else cv2.VideoCapture(cam_idx)')
        self.assertIn(camera_adapter, runtime)
        runtime = runtime.replace(camera_adapter, '    for cam_idx in [0, 1, 2]:\n        test = cv2.VideoCapture(cam_idx)')
        # Remove only the three explicit toolkit hooks; all original processing
        # functions and the base prompt/models still must match upstream.
        runtime = runtime.replace('    from features.toolkits.gemini_tools import extend_config\n    return extend_config({**_BASE_CONFIG, "system_instruction": system_instruction})',
                                  '    return {**_BASE_CONFIG, "system_instruction": system_instruction}')
        runtime = runtime.replace('                if response.tool_call:\n                    from features.toolkits.gemini_tools import handle_tool_call\n                    await handle_tool_call(session, response.tool_call)\n', '')
        # Session resumption/GoAway hook (features/conversational_ability/session.py).
        runtime = runtime.replace('                if response.session_resumption_update or response.go_away:\n                    from features.conversational_ability.session import on_server_message\n                    on_server_message(response)\n', '')
        runtime = runtime.replace('    from features.toolkits.gemini_tools import prewarm\n    prewarm()\n', '')
        # Normalize only the documented optional fall hooks; inference stays outside the core.
        alert_hook = '                        tg.create_task(fall_worker.alert_monitor(live_session, _shutdown_event))\n'
        self.assertEqual(runtime.count(alert_hook), 1)
        runtime = runtime.replace(alert_hook, '')
        camera_message = 'f"— fall detection: {fall_worker.status()[\'state\']}."'
        self.assertIn(camera_message, runtime)
        runtime = runtime.replace(camera_message, 'f"— fall detection disabled."')
        runtime = runtime.replace('print("Fall detection:   " + fall_worker.status()["state"])',
                                  'print("Fall detection:   DISABLED (demo latency) — camera feed only")')
        # Normalize only guarded conversational ability/waiting-cue hooks; the disabled
        # audio path must still recover the original upstream functions exactly.
        runtime = runtime.replace('if not buffer_empty and not (_audio_enhancer is not None and _audio_enhancer.full_duplex):', 'if not buffer_empty:')
        class RemoveMonitorHooks(ast.NodeTransformer):
            def visit_If(self, node):
                if any(isinstance(n, ast.Name) and n.id in {'_audio_enhancer', '_waiting_cue'} for n in ast.walk(node.test)):
                    return None
                return self.generic_visit(node)

            def visit_Expr(self, node):
                if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and (node.value.func.value.id == 'monitor' or
                         (node.value.func.value.id == 'fall_worker' and node.value.func.attr in {'start', 'submit', 'stop'}))):
                    return None
                return self.generic_visit(node)
        trees = [ast.parse(original), RemoveMonitorHooks().visit(ast.parse(runtime))]
        functions = [{node.name: ast.dump(node, include_attributes=False) for node in tree.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                     for tree in trees]
        self.assertEqual(set(functions[0]), set(functions[1]))
        for name in functions[0]:
            if name != "get_default_device_id":
                self.assertEqual(functions[0][name], functions[1][name], name)

    def test_prompt_models_and_sample_rates_match_upstream(self):
        names = {"MODEL", "SUMMARY_MODEL", "_BASE_SYSTEM_INSTRUCTION", "SEND_SAMPLE_RATE", "RECEIVE_SAMPLE_RATE"}
        values = []
        for file in (ROOT / "upstream/realtime_gemini_8.py", ROOT / "realtime_gemini_8.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            values.append({target.id: ast.dump(node.value) for node in tree.body if isinstance(node, ast.Assign)
                           for target in node.targets if isinstance(target, ast.Name) and target.id in names})
        self.assertEqual(set(values[0]), names)
        self.assertEqual(values[0], values[1])


class FakeAudio:
    class default:
        device = (0, 1)

    def query_devices(self):
        return [{"max_input_channels": 1, "max_output_channels": 0},
                {"max_input_channels": 0, "max_output_channels": 2}]

    def check_input_settings(self, **kwargs):
        if kwargs["channels"] != 1:
            raise ValueError("Microphone supports mono only")

    def check_output_settings(self, **kwargs):
        pass


class HardwareTests(unittest.TestCase):
    def test_default_mono_mic_and_output_work(self):
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": ""}):
            self.assertEqual(select_audio_devices(FakeAudio()), (0, 1))

    def test_invalid_or_wrong_direction_device_is_rejected(self):
        for index in ("1", "99", "-1", "unknown"):
            with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": index, "BAYMAX_SPEAKER_DEVICE": ""}):
                with self.assertRaises(RuntimeError):
                    select_audio_devices(FakeAudio())

    def test_saved_alsa_device_is_found_after_card_renumbering(self):
        class Renumbered(FakeAudio):
            def query_hostapis(self):
                return [{"name": "ALSA"}]
            def query_devices(self):
                return [{"name": "USB Mic: Audio (hw:3,0)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
                        {"name": "HDA Intel PCH: ALC269 (hw:0,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
                        {"name": "UACDemoV1.0: USB Audio (hw:1,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}]
        saved = {"BAYMAX_MIC_DEVICE": "0", "BAYMAX_MIC_NAME": "USB Mic: Audio (hw:2,0)", "BAYMAX_MIC_HOSTAPI": "ALSA",
                 "BAYMAX_SPEAKER_DEVICE": "1", "BAYMAX_SPEAKER_NAME": "UACDemoV1.0: USB Audio (hw:2,0)",
                 "BAYMAX_SPEAKER_HOSTAPI": "ALSA"}
        with patch.dict(os.environ, saved):
            self.assertEqual(select_audio_devices(Renumbered()), (0, 2))
        with patch.dict(os.environ, {**saved, "BAYMAX_SPEAKER_NAME": "Other Speaker (hw:2,0)"}):
            with self.assertRaises(RuntimeError):
                select_audio_devices(Renumbered())

    def test_ambiguous_renumbered_alsa_device_is_still_rejected(self):
        class Twins(FakeAudio):
            def query_hostapis(self):
                return [{"name": "ALSA"}]
            def query_devices(self):
                return [{"name": "Mic (hw:0,0)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
                        {"name": "Speaker (hw:1,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
                        {"name": "Speaker (hw:3,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}]
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": "1",
                                     "BAYMAX_SPEAKER_NAME": "Speaker (hw:2,0)", "BAYMAX_SPEAKER_HOSTAPI": "ALSA"}):
            with self.assertRaises(RuntimeError):
                select_audio_devices(Twins())

    def test_linux_camera_accepts_stable_udev_path(self):
        path = "/dev/v4l/by-id/usb-Arducam_Technology_Co.__Ltd._Arducam-1080P-HDR_SN0001-video-index0"
        cv2 = SimpleNamespace(CAP_ANY=0, CAP_V4L2=200, VideoCapture=Mock(return_value="capture"))
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": path}):
            self.assertEqual(camera_candidates(), [path])
            self.assertEqual(open_camera(cv2, path), "capture")
            cv2.VideoCapture.assert_called_with(path, 200)
            open_camera(cv2, 0)
            cv2.VideoCapture.assert_called_with(0, 0)

    def test_missing_default_uses_an_available_input(self):
        audio = FakeAudio()
        audio.default = type("Defaults", (), {"device": (-1, 1)})
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": ""}):
            self.assertEqual(select_audio_devices(audio), (0, 1))


# Everything use_performance_cpus may set for the rest of Ember and what it starts, by the
# names realtime_gemini_10.py, the vision children and the libraries read.
FAST, SLOW = "BAYMAX_FAST_CPUS", "BAYMAX_SLOW_CPUS"
SPEAKER = ("BAYMAX_OUTPUT_BLOCKSIZE", "BAYMAX_OUTPUT_LATENCY")
FAST_CORE_ENV = (FAST, SLOW, "OPENBLAS_NUM_THREADS", "OPENCV_FOR_THREADS_NUM", "TOKENIZERS_PARALLELISM", *SPEAKER)


class BigLittleCpuTests(unittest.TestCase):
    """Orange Pi 5 (RK3588): cpu0-3 are Cortex-A55s, cpu4-7 Cortex-A76s. Every case sets
    sys.platform and platform.machine, so the results are the same on Windows, macOS,
    x86 Linux and the Pi itself, and BAYMAX_CPU_PINNING is set for every case, so the
    documented BAYMAX_CPU_PINNING=off in the environment or in .env (which test_toolkit
    loads) changes nothing. The test process itself is never pinned, reniced or given
    another GIL switch interval: setUp replaces those calls and checks afterwards."""
    RK3588 = {0: 414, 1: 414, 2: 414, 3: 414, 4: 1024, 5: 1024, 6: 1024, 7: 1024}

    def setUp(self):
        before = self.own_state()
        self.addCleanup(lambda: self.assertEqual(self.own_state(), before))   # runs last
        environment = patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "auto"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in (*FAST_CORE_ENV, "BAYMAX_GIL_SWITCH_MS", "BAYMAX_VISION_CPUS"):
            os.environ.pop(name, None)                  # the shell's own; restored by patch.dict
        guards = [patch.object(sys, "setswitchinterval"), patch.object(pc_hardware, "_warned", set())]
        for name in ("sched_setaffinity", "setpriority"):   # tests that expect calls patch them again
            if hasattr(os, name):
                guards.append(patch.object(pc_hardware.os, name))
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.switch = sys.setswitchinterval

    @staticmethod
    def own_state():
        return (sys.getswitchinterval(), os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, "getpriority") else None,
                os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None)

    def sysfs(self, values, name="cpu_capacity"):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        for cpu, value in values.items():
            path = Path(root.name, f"cpu{cpu}", name)
            path.parent.mkdir(parents=True)
            path.write_text(f"{value}\n")
        return root.name

    def machine(self, system="linux", machine="aarch64"):
        stack = ExitStack()
        stack.enter_context(patch.object(sys, "platform", system))
        stack.enter_context(patch.object(pc_hardware.platform, "machine", return_value=machine))
        return stack

    def fast(self, values, allowed=None, name="cpu_capacity", system="linux", machine="aarch64"):
        allowed = set(values) if allowed is None else allowed
        with self.machine(system, machine), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=allowed), create=True):
            return pc_hardware.performance_cpus(self.sysfs(values, name))

    def test_rk3588_fast_cores_are_the_a76s(self):
        self.assertEqual(self.fast(self.RK3588), {4, 5, 6, 7})
        self.assertEqual(self.fast(self.RK3588, machine="arm64"), {4, 5, 6, 7})
        self.assertEqual(self.fast(self.RK3588, allowed={0, 1, 2, 3, 4, 5}), {4, 5})   # respects an outer limit
        self.assertEqual(self.fast(self.RK3588, allowed={4, 5, 6, 7}), {4, 5, 6, 7})   # already pinned: same set
        self.assertIsNone(self.fast(self.RK3588, allowed={0, 1, 2, 3, 4}))             # never onto one core

    def test_max_frequency_is_used_without_capacity_on_arm(self):
        khz = {0: 1800000, 1: 1800000, 2: 1800000, 3: 1800000, 4: 2400000, 5: 2400000, 6: 2256000, 7: 2256000}
        self.assertEqual(self.fast(khz, name="cpufreq/cpuinfo_max_freq"), {4, 5, 6, 7})

    def test_x86_hybrid_cpus_are_left_to_the_scheduler(self):
        # x86 has no cpu_capacity; intel_pstate reports each core's own maximum, so an
        # i5-1340P (LattePanda Sigma) or i7-1260P reads 4.6/4.7 GHz P-cores and 3.4 GHz
        # E-cores, under the 0.8 cut-off. Pinning was measured only on the RK3588.
        hybrid = {**{cpu: 4700000 for cpu in range(8)}, **{cpu: 3400000 for cpu in range(8, 16)}}
        for machine in ("x86_64", "AMD64", "i686"):
            self.assertIsNone(self.fast(hybrid, name="cpufreq/cpuinfo_max_freq", machine=machine), machine)
        self.assertIsNone(self.fast(self.RK3588, machine="x86_64"))
        self.assertEqual(self.fast(hybrid, name="cpufreq/cpuinfo_max_freq"), set(range(8)))   # same tree on ARM

    def test_identical_cores_or_other_systems_are_left_alone(self):
        self.assertIsNone(self.fast({0: 1024, 1: 1024, 2: 1024, 3: 1024}))       # Pi 4, RK3566
        self.assertIsNone(self.fast({0: 414, 4: 1024, 5: 1024}, allowed={0}))   # fast cores not allowed
        for system, machine in (("darwin", "arm64"), ("darwin", "x86_64"), ("win32", "ARM64"), ("win32", "AMD64")):
            self.assertIsNone(self.fast(self.RK3588, system=system, machine=machine), system)

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_pinning_moves_every_thread_once_and_can_be_turned_off(self):
        setter = Mock()
        current = Mock(return_value={0, 1, 2, 3, 4, 5, 6, 7})
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100, 101, 102]), \
             patch.object(pc_hardware.os, "sched_setaffinity", setter, create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", current, create=True), \
             patch("builtins.print") as printed:
            with patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "off"}):
                self.assertIsNone(pc_hardware.use_performance_cpus())
            setter.assert_not_called()
            self.assertFalse(set(FAST_CORE_ENV) & set(os.environ))     # off: nothing else changes either
            self.switch.assert_not_called()
            with patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "auto"}):
                self.assertEqual(pc_hardware.use_performance_cpus(), {4, 5, 6, 7})
            # The caller, then every thread that already exists (eyes, network watch, OpenBLAS).
            self.assertEqual([c.args for c in setter.call_args_list],
                             [(0, {4, 5, 6, 7}), (100, {4, 5, 6, 7}), (101, {4, 5, 6, 7}), (102, {4, 5, 6, 7})])
            self.assertEqual(printed.call_count, 1)
            self.assertIn("[CPU] Running on the fast cores 4,5,6,7, GIL switch interval 1 ms", printed.call_args.args[0])
            current.return_value = {4, 5, 6, 7}                 # run_pc's call after run_mvp's: quiet
            with patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "auto"}):
                self.assertEqual(pc_hardware.use_performance_cpus(), {4, 5, 6, 7})
            self.assertEqual(printed.call_count, 1)
        setter.reset_mock()
        with patch.object(pc_hardware, "performance_cpus", return_value=None), \
             patch.object(pc_hardware.os, "sched_setaffinity", setter, create=True):
            self.assertIsNone(pc_hardware.use_performance_cpus())
        setter.assert_not_called()

    def test_auto_pins_only_on_the_rk3588(self):
        # Measured on the Orange Pi 5 Max only; the Orange Pi 4 Pro's A733 (2 fast cores
        # for Ember and both vision processes) stays unpinned unless =on.
        setter = Mock()
        with patch.object(pc_hardware, "is_rk3588", return_value=False), \
             patch.object(pc_hardware, "performance_cpus", return_value={6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", setter, create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
             patch("builtins.print") as printed:
            with patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "auto"}):
                self.assertIsNone(pc_hardware.use_performance_cpus())
            setter.assert_not_called()
            printed.assert_not_called()
            self.assertFalse(set(FAST_CORE_ENV) & set(os.environ))     # the 4 Pro by default: as before
            self.switch.assert_not_called()
            with patch.dict(os.environ, {"BAYMAX_CPU_PINNING": "on"}):
                self.assertEqual(pc_hardware.use_performance_cpus(), {6, 7})
                # Pose would share the two A76s with Ember; faces would go to the six A55s.
                self.assertEqual((os.environ[FAST], os.environ[SLOW]), ("6,7", "0,1,2,3,4,5"))
            self.assertEqual([c.args for c in setter.call_args_list], [(0, {6, 7}), (100, {6, 7})])
            self.assertIn("[CPU] Running on the fast cores 6,7", printed.call_args.args[0])

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_a_refused_pin_leaves_ember_on_all_cores(self):
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(side_effect=PermissionError(1, "denied")),
                          create=True), \
             patch.object(pc_hardware, "pin_threads") as others, patch("builtins.print") as printed:
            self.assertIsNone(pc_hardware.use_performance_cpus())
        others.assert_not_called()
        self.assertIn("Could not keep Ember on the fast cores", printed.call_args.args[0])
        self.assertFalse(set(FAST_CORE_ENV) & set(os.environ))         # not pinned: nothing else either
        self.switch.assert_not_called()

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_fast_core_settings_come_only_with_the_pinning(self):
        with patch.object(pc_hardware, "performance_cpus", return_value=None), \
             patch("builtins.print") as printed:                            # x86, macOS, Windows, all-alike cores
            self.assertIsNone(pc_hardware.use_performance_cpus())
        self.assertFalse(set(FAST_CORE_ENV) & set(os.environ))
        self.switch.assert_not_called()
        printed.assert_not_called()
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
             patch("builtins.print"), \
             patch.dict(os.environ, {"OPENBLAS_NUM_THREADS": "4"}):         # a value already set wins
            self.assertEqual(pc_hardware.use_performance_cpus(), {4, 5, 6, 7})
            # The speaker: 512-frame callbacks and low latency, measured on the 5 Max (42 ms
            # of output latency instead of about 96 ms, no underflows).
            self.assertEqual({name: os.environ.get(name) for name in FAST_CORE_ENV},
                             {FAST: "4,5,6,7", SLOW: "0,1,2,3", "OPENBLAS_NUM_THREADS": "4",
                              "OPENCV_FOR_THREADS_NUM": "1", "TOKENIZERS_PARALLELISM": "false",
                              "BAYMAX_OUTPUT_BLOCKSIZE": "512", "BAYMAX_OUTPUT_LATENCY": "low"})
        self.switch.assert_called_once_with(0.001)
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
             patch("builtins.print"), \
             patch.dict(os.environ, {"BAYMAX_OUTPUT_BLOCKSIZE": "1024"}):   # e.g. from .env: it wins
            pc_hardware.use_performance_cpus()
            self.assertEqual([os.environ.get(name) for name in SPEAKER], ["1024", "low"])

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_the_gil_switch_interval_can_be_changed_or_left_to_python(self):
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
             patch("builtins.print") as printed:
            for value, interval in (("", 0.001), (" 5 ", 0.005), ("0.5", 0.0005), ("0", None),
                                    ("fast", 0.001), ("-1", 0.001), ("0.01", 0.001), ("1000", 0.001), ("nan", 0.001)):
                self.switch.reset_mock()
                printed.reset_mock()
                with self.subTest(value=value), patch.dict(os.environ, {"BAYMAX_GIL_SWITCH_MS": value}):
                    pc_hardware.use_performance_cpus()
                    if interval is None:
                        self.switch.assert_not_called()                    # 0: Python's own (5 ms)
                    else:
                        self.switch.assert_called_once_with(interval)
                    warnings = [c.args[0] for c in printed.call_args_list if "Invalid BAYMAX_GIL_SWITCH_MS" in c.args[0]]
                    self.assertEqual(len(warnings), 1 if value in ("fast", "-1", "0.01", "1000", "nan") else 0)

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_an_invalid_gil_switch_interval_is_reported_once(self):
        # run_mvp.py pins Ember, then run_pc.py calls use_performance_cpus again: the warning
        # came twice, though a second call is meant to print nothing.
        current = Mock(return_value=set(range(8)))
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", current, create=True), \
             patch.dict(os.environ, {"BAYMAX_GIL_SWITCH_MS": "fast"}), patch("builtins.print") as printed:
            pc_hardware.use_performance_cpus()                              # run_mvp.py
            current.return_value = {4, 5, 6, 7}
            pc_hardware.use_performance_cpus()                              # run_pc.py
        self.assertEqual([c.args[0] for c in printed.call_args_list],
                         ["[CPU] Invalid BAYMAX_GIL_SWITCH_MS; using 1 ms.",
                          "[CPU] Running on the fast cores 4,5,6,7, GIL switch interval 1 ms "
                          "(BAYMAX_CPU_PINNING=off to use all)."])
        self.assertEqual([c.args for c in self.switch.call_args_list], [(0.001,), (0.001,)])

    @patch.object(pc_hardware, "is_rk3588", Mock(return_value=True))
    def test_children_may_use_the_cores_ember_had_before_it_narrowed_itself(self):
        allowed = Mock(return_value=set(range(8)))
        with patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
             patch.object(pc_hardware, "thread_ids", return_value=[100]), \
             patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
             patch.object(pc_hardware.os, "sched_getaffinity", allowed, create=True), \
             patch("builtins.print"):
            pc_hardware.use_performance_cpus()                              # run_mvp.py
            allowed.return_value = {4, 5, 6, 7}
            pc_hardware.use_performance_cpus()                              # run_pc.py, already narrowed
            self.assertEqual((os.environ[FAST], os.environ[SLOW]), ("4,5,6,7", "0,1,2,3"))
            del os.environ[FAST], os.environ[SLOW]
            pc_hardware.use_performance_cpus()                              # started under taskset -c 4-7
            self.assertEqual(os.environ[FAST], "4,5,6,7")
            self.assertNotIn(SLOW, os.environ)                              # so faces stay inside it too

    def test_vision_children_pose_stays_on_the_fast_cores_faces_go_to_the_slow_ones(self):
        setter = Mock()
        with patch.object(pc_hardware.os, "sched_setaffinity", setter, create=True), \
             patch.object(pc_hardware, "pin_threads") as pin, \
             patch.object(pc_hardware, "lower_priority") as lower, \
             patch("builtins.print") as printed:
            for role in ("pose", "face"):                                   # Ember not pinned: as before
                self.assertIsNone(pc_hardware.place_vision_child(role))
            for calls in (setter, pin, lower, printed):
                calls.assert_not_called()
            places = []
            def place(role, **env):
                with patch.dict(os.environ, env):
                    cores = pc_hardware.place_vision_child(role)
                places.append((role, cores, lower.call_args.args[0]))
                self.assertEqual(setter.call_args.args, (0, cores))         # this process, then its threads
                self.assertEqual(pin.call_args.args, (cores,))
            with patch.dict(os.environ, {FAST: "4,5,6,7", SLOW: "0,1,2,3"}):
                place("pose")
                place("face")
                self.assertIn("[CPU] Face process on the slow cores 0,1,2,3 at nice 10.", printed.call_args.args[0])
                place("something else")                                     # placed as pose
                place("pose", BAYMAX_VISION_CPUS="slow")                    # to measure: cores move, nice stays
                place("face", BAYMAX_VISION_CPUS="Fast")
                place("face", BAYMAX_VISION_CPUS="a55")                     # mistyped: the role's own cores
                self.assertTrue(any("unknown BAYMAX_VISION_CPUS=a55" in c.args[0] for c in printed.call_args_list))
            with patch.dict(os.environ, {FAST: "4,5,6,7"}):                 # Ember under taskset -c 4-7
                place("face")
        self.assertEqual(places, [("pose", {4, 5, 6, 7}, 5), ("face", {0, 1, 2, 3}, 10),
                                  ("something else", {4, 5, 6, 7}, 5), ("pose", {0, 1, 2, 3}, 5),
                                  ("face", {4, 5, 6, 7}, 10), ("face", {0, 1, 2, 3}, 10),
                                  ("face", {4, 5, 6, 7}, 10)])

    def test_vision_children_leave_alone_what_they_cannot_change(self):
        with patch.object(pc_hardware, "lower_priority") as lower, patch("builtins.print"):
            for value in ("4,x", "-1", "4,,5", " "):                        # not written by Ember
                with patch.dict(os.environ, {FAST: value}), \
                     patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True) as setter:
                    self.assertIsNone(pc_hardware.place_vision_child("pose"))
                setter.assert_not_called()
            with patch.dict(os.environ, {FAST: "4,5,6,7"}):
                with patch.object(pc_hardware, "os", SimpleNamespace(environ=os.environ, getenv=os.getenv)):
                    self.assertIsNone(pc_hardware.place_vision_child("pose"))   # macOS, Windows: no affinity
                with patch.object(pc_hardware.os, "sched_setaffinity", Mock(side_effect=OSError(22, "Invalid")),
                                  create=True):
                    self.assertIsNone(pc_hardware.place_vision_child("face"))   # refused: nice unchanged too
        lower.assert_not_called()

    def test_priority_is_only_ever_lowered(self):
        current = {100: 0, 101: 7}
        def getpriority(which, tid):
            if tid not in current:
                raise ProcessLookupError(3, "No such process")              # exited after the listing
            return current[tid]
        def setpriority(which, tid, value):
            current[tid] = value
        with patch.object(pc_hardware, "thread_ids", return_value=[100, 101, 102]), \
             patch.object(pc_hardware.os, "PRIO_PROCESS", 0, create=True), \
             patch.object(pc_hardware.os, "getpriority", getpriority, create=True), \
             patch.object(pc_hardware.os, "setpriority", setpriority, create=True):
            pc_hardware.lower_priority(5)
        self.assertEqual(current, {100: 5, 101: 7})                         # 101 was already lower

    def test_threads_that_exit_or_start_meanwhile(self):
        tasks = tempfile.TemporaryDirectory()
        self.addCleanup(tasks.cleanup)
        for tid in ("100", "101", "102"):
            Path(tasks.name, tid).mkdir()
        pinned = []
        def setaffinity(tid, cpus):
            if tid == 101:
                raise ProcessLookupError(3, "No such process")     # exited after the listing
            pinned.append(tid)
            if tid == 100 and not Path(tasks.name, "103").exists():
                Path(tasks.name, "103").mkdir()                  # started before 100 was moved
        with patch.object(pc_hardware.os, "sched_setaffinity", setaffinity, create=True):
            pc_hardware.pin_threads({4, 5}, tasks.name)
        self.assertEqual(pinned, [100, 102, 103])
        self.assertEqual(pc_hardware.thread_ids(str(Path(tasks.name, "missing"))), [0])

    def test_linux_arm_camera_keeps_only_the_newest_frame(self):
        cv2 = SimpleNamespace(CAP_PROP_BUFFERSIZE=38)
        cases = (("linux", "aarch64", [(38, 1)]), ("linux", "arm64", [(38, 1)]),
                 ("linux", "x86_64", []),                       # LattePanda, Docker image, x86 Linux PCs
                 ("darwin", "arm64", []), ("win32", "AMD64", []))
        for system, machine, expected in cases:
            capture = Mock()
            with self.machine(system, machine):
                pc_hardware.latest_frames_only(cv2, capture)
            self.assertEqual([c.args for c in capture.set.call_args_list], expected, (system, machine))

    def test_runtime_shortens_the_camera_queue_only_for_the_pc_profile(self):
        # The Orange Pi and the LattePanda MVP run the pc profile (run_pc.py); unit 001's
        # path (deploy/host/startup.sh) and the Docker image run realtime_gemini_10.py
        # without it. Within it, latest_frames_only's arm_linux() check (above) keeps
        # the x86 LattePanda on OpenCV's queue.
        source = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("latest_frames_only(cv2, cap)"), 1)
        self.assertIn("    if pc:  # pc profile (run_pc.py: the Orange Pi, the LattePanda MVP); only the Orange Pi (arm_linux) keeps 1 frame\n"
                      "        from pc_hardware import latest_frames_only\n"
                      "        latest_frames_only(cv2, cap)\n", source)

    def test_runtime_turns_off_opencvs_pool_only_on_the_fast_cores(self):
        # Right after import cv2, before anything in Ember uses OpenCV: run the real statement.
        tree = ast.parse((ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8"))
        imports = [i for i, node in enumerate(tree.body)
                   if isinstance(node, ast.Import) and [alias.name for alias in node.names] == ["cv2"]]
        self.assertEqual(len(imports), 1)
        statement = ast.Module(body=[tree.body[imports[0] + 1]], type_ignores=[])
        self.assertIsInstance(statement.body[0], ast.If)
        code = compile(statement, "realtime_gemini_10.py", "exec")
        for env, calls in (({}, []), ({FAST: "4,5,6,7"}, [(1,)])):
            cv2 = Mock()
            with patch.dict(os.environ, env):
                exec(code, {"os": os, "cv2": cv2})
            self.assertEqual([c.args for c in cv2.setNumThreads.call_args_list], calls, env)



class MicByteAlignmentTests(unittest.TestCase):
    """A USB mic stream that slipped one byte (Orange Pi 5 Max, 2026-11-13) is realigned."""

    @staticmethod
    def speech(seconds=4.0, seed=0):
        import numpy as np
        rng = np.random.default_rng(seed)
        t = np.arange(int(48000 * seconds)) / 48000
        x = 3000 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) + rng.normal(0, 150, len(t))
        return x.astype(np.int16)

    def run_blocks(self, align, samples):
        return [align.fix(samples[i:i + 960]) for i in range(0, len(samples) - 960 + 1, 960)]

    def test_a_slipped_stream_is_put_back_within_about_a_second(self):
        import numpy as np
        speech = self.speech()
        slipped = np.frombuffer(speech.tobytes()[1:-1], np.int16)
        align = pc_hardware.MicByteAlignment()
        out = self.run_blocks(align, slipped)
        self.assertEqual((align.flips, align.shifted), (1, True))
        flipped_at = next(i for i, block in enumerate(out) if not align.looks_misaligned(block))
        self.assertLessEqual(flipped_at, align.WINDOW)              # about a second of 20 ms blocks
        realigned = np.concatenate(out[flipped_at + 1:])
        start = (flipped_at + 1) * 960
        np.testing.assert_array_equal(realigned, speech[start:start + len(realigned)])

    def test_normal_audio_is_never_touched(self):
        import numpy as np
        align = pc_hardware.MicByteAlignment()
        speech = self.speech(seed=1)
        out = self.run_blocks(align, speech)
        self.assertEqual(align.flips, 0)
        np.testing.assert_array_equal(np.concatenate(out), speech[:len(out) * 960])
        quiet = np.zeros(48000, np.int16)
        self.run_blocks(align, quiet)
        self.assertEqual(align.flips, 0)

    def test_a_mistaken_shift_is_undone(self):
        import numpy as np
        align = pc_hardware.MicByteAlignment()
        align.shifted = True                    # e.g. after a burst of loud noise
        self.run_blocks(align, self.speech(seed=2))
        self.assertEqual((align.flips, align.shifted), (1, False))

    def test_a_brief_glitch_does_not_shift(self):
        import numpy as np
        speech = self.speech(seed=3)
        slipped = np.frombuffer(speech.tobytes()[1:-1], np.int16)
        glitchy = np.concatenate([speech[:48000], slipped[:960 * 10], speech[48000:]])
        align = pc_hardware.MicByteAlignment()
        self.run_blocks(align, glitchy)
        self.assertEqual(align.flips, 0)

if __name__ == "__main__":
    unittest.main()


CONTROLS = """numid=2,iface=MIXER,name='PCM Playback Switch'
numid=3,iface=MIXER,name='PCM Playback Volume'
numid=1,iface=PCM,name='Playback Channel Map'
"""
CGET = """numid=3,iface=MIXER,name='PCM Playback Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=147,step=0
  : values=59,59
  | dBminmax-min=-28.37dB,max=-0.94dB
"""
SGET = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 147
  Mono:
  Front Left: Playback 59 [40%] [-17.37dB] [on]
  Front Right: Playback 59 [40%] [-17.37dB] [on]
"""


class SpeakerMixerTests(unittest.TestCase):
    """The speaker's own volume through amixer, from the Orange Pi 5 Max's USB speaker."""

    def setUp(self):
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BAYMAX_SPEAKER_MIXER", None)
        self.calls = []
        printed = patch("builtins.print")
        printed.start()
        self.addCleanup(printed.stop)

    def amixer(self, args):
        self.calls.append(args)
        if args[-1] == "controls":
            return CONTROLS
        if args[2] == "cget":
            return CGET
        if args[2] == "sget":
            return SGET
        return ""

    def mixer(self, name="UACDemoV1.0: USB Audio (hw:4,0)", platform="linux"):
        with patch.object(pc_hardware.sys, "platform", platform):
            return pc_hardware.speaker_mixer(name, run=self.amixer)

    def test_finds_the_cards_playback_volume_and_its_db_range(self):
        mixer = self.mixer()
        self.assertEqual((mixer.card, mixer.control, mixer.low_db, mixer.loud_db, mixer.switch),
                         (4, "PCM", -28.37, -0.94, True))
        self.assertEqual(mixer.get_db(), -17.37)
        self.assertEqual(mixer.set_db(-9.94), -9.94)
        self.assertEqual(self.calls[-1], ["-q", "-c", "4", "sset", "PCM", "--", "-9.94dB", "unmute"])
        self.assertEqual(mixer.set_db(5), -0.94)  # never past the card's range
        self.assertEqual(mixer.set_db(-90), -28.37)

    def test_none_where_portaudio_does_not_open_an_alsa_card(self):
        self.assertIsNone(self.mixer(platform="darwin"))
        self.assertIsNone(self.mixer("pipewire"))  # PipeWire/Pulse: no (hw:N,M)
        self.assertIsNone(self.mixer("Speakers (Realtek(R) Audio)", platform="linux"))
        os.environ["BAYMAX_SPEAKER_MIXER"] = "off"
        self.assertIsNone(self.mixer())
        self.assertEqual(self.calls, [])  # nothing is run then

    def test_none_when_amixer_fails_or_the_control_has_no_db_scale(self):
        def missing(args):
            raise RuntimeError("amixer: [Errno 2] No such file or directory")
        with patch.object(pc_hardware.sys, "platform", "linux"):
            self.assertIsNone(pc_hardware.speaker_mixer("X (hw:4,0)", run=missing))
            no_scale = lambda args: CONTROLS if args[-1] == "controls" else "  : values=59,59\n"
            self.assertIsNone(pc_hardware.speaker_mixer("X (hw:4,0)", run=no_scale))

    def test_a_named_control_and_a_db_step_scale(self):
        controls = "numid=5,iface=MIXER,name='Master Playback Volume'\nnumid=3,iface=MIXER,name='PCM Playback Volume'\n"
        cget = "  ; type=INTEGER,access=rw---R--,values=2,min=0,max=30,step=0\n  | dBscale-min=-12.00dB,step=1.50dB,mute=0\n"
        run = lambda args: controls if args[-1] == "controls" else cget
        os.environ["BAYMAX_SPEAKER_MIXER"] = "Master"
        with patch.object(pc_hardware.sys, "platform", "linux"):
            mixer = pc_hardware.speaker_mixer("X (hw:2,0)", run=run)
        self.assertEqual((mixer.card, mixer.control, mixer.low_db, mixer.loud_db, mixer.switch),
                         (2, "Master", -12.0, 33.0, False))
