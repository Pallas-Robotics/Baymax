"""Fake-clock and decoded Firmata-wire checks; no serial device or cloud."""
import os
import unittest
from unittest.mock import Mock, patch

from features.eyes.controller import Eyes, OPEN, BLINK, BLANK, THINKING, eyes_session
from features.eyes.driver import LedControl
from features.eyes.patterns import EXPRESSIONS, SAD, SURPRISED, SLEEP_BASE
import runtime_monitor as monitor


class Pin:
    def __init__(self, board, number):
        self.board, self.number = board, number

    def write(self, value):
        board = self.board
        board.writes.append((self.number, value))
        board.levels[self.number] = value
        if self.number == 10 and value == 0:
            board.bits = []
        elif self.number == 13 and value == 1 and board.levels.get(10) == 0:
            board.bits.append(board.levels.get(6, 0))
        elif self.number == 10 and value == 1 and board.bits:
            assert len(board.bits) == 32
            words = [int(''.join(map(str, board.bits[i:i+16])), 2) for i in (0, 16)]
            for device, word in zip((1, 0), words):
                if word >> 8:
                    board.registers[device, word >> 8] = word & 255
            board.bits = []


class Board:
    def __init__(self):
        self.levels, self.registers = {}, {}
        self.bits, self.writes, self.pins = [], [], []
        self.closed = False

    def get_pin(self, spec):
        self.pins.append(spec)
        return Pin(self, int(spec.split(':')[1]))

    def exit(self):
        self.closed = True


class EyeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.eyes = Eyes(clock=lambda: self.now)

    def test_all_supplied_expression_frames_are_valid_and_keep_mounting(self):
        self.assertEqual(set(EXPRESSIONS), {'happy', 'sad', 'excited', 'confused', 'surprised', 'curious', 'love', 'sleep', 'dead'})
        self.assertEqual(SAD, (0, 0, 0, 66, 66, 36, 24, 0))
        for name, (frames, seconds) in EXPRESSIONS.items():
            self.assertGreater(seconds, 0)
            for left, right in frames:
                self.assertEqual(len(left), 8)
                self.assertEqual(len(right), 8)
                self.assertTrue(all(0 <= v <= 255 for v in left + right))
        self.assertEqual(len(EXPRESSIONS['confused'][0]), 4)
        for left, right in EXPRESSIONS['sleep'][0]:
            self.assertEqual(left, SLEEP_BASE)
        self.assertNotEqual(*EXPRESSIONS['excited'][0][0])

    def test_booting_shows_confused_until_gemini_first_connects(self):
        eyes = Eyes(clock=lambda: self.now, booting=True)
        self.assertEqual(eyes.mode(), 'confused')
        for event in ('connecting', 'reconnecting', 'stopping'):
            eyes.event(event)
            self.assertEqual(eyes.mode(), 'confused', event)
        eyes.event('fall_alert')
        self.assertEqual(eyes.mode(), 'surprised')
        self.now = 20
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'normal')
        eyes.event('reconnecting')
        self.assertEqual(eyes.mode(), 'confused')  # Ember is down but recovering by itself.
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'normal')

    def test_ready_eyes_wait_until_mic_audio_reaches_gemini(self):
        eyes = Eyes(clock=lambda: self.now, booting=True, wait_for_mic=True)
        eyes.event('mic_live')                      # Mic before any session: still booting.
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('listening')                     # Connected, but nothing sent yet.
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('user_speech')
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('mic_live')                      # Audio really going out: blink normally.
        self.assertEqual(eyes.mode(), 'normal')
        eyes.event('listening')                     # "Reply complete; listening" stays ready.
        self.assertEqual(eyes.mode(), 'normal')
        eyes.event('mic_stalled')                   # The 144 s deaf spell: show it.
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('mic_live')
        self.assertEqual(eyes.mode(), 'normal')
        eyes.event('reconnecting')
        eyes.event('mic_live')                      # Old session's audio does not count.
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('listening')
        eyes.event('mic_live')
        self.assertEqual(eyes.mode(), 'normal')
        self.assertEqual(self.eyes.mode(), 'normal')  # Default Eyes() is not booting.

    def test_expression_survives_tool_finish_speech_and_buffered_completion(self):
        self.eyes.event('user_speech')
        self.assertTrue(self.eyes.express('sad'))
        self.eyes.event('user_speech')  # Late transcription delta for this request.
        self.eyes.event('calling_tool')
        self.eyes.event('thinking')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.now = 3
        self.assertEqual(self.eyes.mode(), 'thinking')
        self.eyes.event('speaking')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.eyes.event('reply_complete')
        self.assertEqual(self.eyes.mode(), 'sad')
        self.eyes.event('reply_playback_done')
        self.assertEqual(self.eyes.mode(), 'normal')

    def test_fall_alert_overrides_model_expression_and_thinking(self):
        self.eyes.express('sad')
        self.eyes.event('fall_alert')
        self.eyes.event('thinking')
        self.now = 3
        self.eyes.express('happy')
        self.assertEqual(self.eyes.mode(), 'surprised')
        self.eyes.event('reply_playback_done')  # No check-in audio has arrived yet.
        self.assertEqual(self.eyes.mode(), 'surprised')
        self.eyes.event('speaking')
        self.eyes.event('reply_complete')
        self.eyes.event('reply_playback_done')
        self.assertEqual(self.eyes.mode(), 'normal')

    def test_expression_expiry_new_user_and_reconnect_do_not_leave_stuck_faces(self):
        for event in ('user_speech', 'listening', 'reconnecting', 'stopping'):
            self.eyes.express('dead')
            self.eyes.event(event)
            if event == 'reconnecting':
                self.assertEqual(self.eyes.mode(), 'confused')  # Recovering; the expression is cleared.
                self.eyes.event('listening')
            self.assertEqual(self.eyes.mode(), 'normal')
        self.eyes.event('listening')
        self.eyes.express('sleep')
        self.now = 31
        self.assertEqual(self.eyes.mode(), 'normal')
        self.eyes.event('fall_alert')
        self.now = 47
        self.assertEqual(self.eyes.mode(), 'normal')
        self.assertFalse(self.eyes.express('not-a-pattern'))
        self.eyes.stopping.set()
        self.assertFalse(self.eyes.express('happy'))

    def test_renderer_plays_supplied_dynamic_frames_in_order(self):
        for name in ('confused', 'sleep', 'excited', 'sad'):
            eyes = Eyes()
            eyes.express(name)
            expected = EXPRESSIONS[name][0]
            frames = []
            def draw(left, right=None):
                frames.append((left, left if right is None else right))
                if len(frames) == len(expected) + 1:
                    eyes.stopping.set()
            with patch.object(eyes, 'hold', return_value=True):
                eyes.render(Mock(draw_pair=draw))
            self.assertEqual(frames, [(OPEN, OPEN), *expected])

    def test_surprise_uses_the_supplied_pattern_and_fast_blink(self):
        eyes = Eyes()
        eyes.express('surprised')
        frames = []
        def draw(left, right=None):
            frames.append(left)
            if len(frames) == 4:
                eyes.stopping.set()
        with patch('features.eyes.controller.random.uniform', return_value=0), patch(
                'features.eyes.controller.random.random', return_value=0.4), patch.object(eyes, 'hold', return_value=True):
            eyes.render(Mock(draw_pair=draw))
        self.assertEqual(frames, [OPEN, SURPRISED, BLINK, SURPRISED])

    def test_monitor_waits_for_playback_drain_without_resetting_waiting_audio(self):
        cue = Mock()
        with patch.object(monitor, 'event_observer', self.eyes.event), patch.object(
                monitor, 'waiting_audio', cue), patch.object(monitor, 'state', 'speaking'), patch.object(monitor, 'detail', ''):
            self.eyes.express('sad')
            self.eyes.event('speaking')
            monitor.emit('listening', 'Reply complete; listening (buffered audio may still play)')
            monitor.snapshot({'playback_bytes': 96000})
            self.assertEqual(self.eyes.mode(), 'sad')
            monitor.snapshot({'playback_bytes': 0})
            self.assertEqual(self.eyes.mode(), 'normal')
        cue.event.assert_called_once_with('reply_complete')

    def test_thinking_only_strictly_after_two_seconds(self):
        self.eyes.event('user_speech')
        self.eyes.event('thinking')
        for moment in (0, 1.9, 2.0):
            self.now = moment
            self.assertFalse(self.eyes.thinking())
        self.now = 2.001
        self.assertTrue(self.eyes.thinking())

    def test_new_user_fragments_restart_delay_but_state_repeats_do_not(self):
        self.eyes.event('thinking')
        self.now = 1.5
        self.eyes.event('user_speech')
        self.now = 3
        self.eyes.event('thinking')
        self.assertFalse(self.eyes.thinking())
        self.now = 3.501
        self.assertTrue(self.eyes.thinking())

    def test_fast_reply_and_late_transcription_keep_normal_eyes(self):
        self.eyes.event('thinking')
        self.now = 1
        self.eyes.event('speaking')
        self.now = 10
        self.eyes.event('user_speech')
        self.eyes.event('thinking')
        self.assertFalse(self.eyes.thinking())

    def test_listening_interruption_reconnect_and_stop_clear_thinking(self):
        for event in ('listening', 'reconnecting', 'connecting', 'stopping', 'stopped'):
            self.eyes.event('thinking')
            self.now += 3
            self.assertTrue(self.eyes.thinking())
            self.eyes.event(event)
            self.assertFalse(self.eyes.thinking())

    def test_event_requests_do_not_touch_hardware(self):
        factory = Mock()
        eyes = Eyes(board_factory=factory)
        eyes.event('thinking')
        eyes.event('speaking')
        factory.assert_not_called()

    def test_single_dot_moves_across_six_positions(self):
        self.assertEqual(len(THINKING), 6)
        for index, pair in enumerate(THINKING):
            self.assertEqual(sum(v.bit_count() for eye in pair for v in eye), 4)
            self.assertEqual(pair[1 if index < 3 else 0], BLANK)

    def test_wire_protocol_mounting_brightness_and_no_servo(self):
        board = Board()
        leds = LedControl(board)
        leds.set_intensity(0, 3)
        leds.set_intensity(1, 3)
        leds.draw_pair(THINKING[0][0], BLANK)
        self.assertEqual(set(board.pins), {'d:6:o', 'd:13:o', 'd:10:o'})
        for device in (0, 1):
            self.assertEqual(board.registers[device, 15], 0)
            self.assertEqual(board.registers[device, 10], 3)
        self.assertEqual(tuple(board.registers[1, r+1] for r in range(8)), THINKING[0][0])
        self.assertEqual(tuple(board.registers[0, r+1] for r in range(8)), BLANK)
        count = len(board.writes)
        leds.draw_pair(THINKING[0][0], BLANK)
        self.assertEqual(len(board.writes), count)

    def test_orange_pi_spi_sends_the_same_registers_at_1_mhz(self):
        from features.eyes import driver
        writes, ioctls, closed = [], [], []
        with patch.object(driver.os, 'write', lambda fd, data: writes.append((fd, data)) or len(data)), \
                patch.object(driver.os, 'close', closed.append):
            leds = driver.SpiLedControl('/dev/spidev3.1', opener=lambda path, flags: 7,
                                        ioctl=lambda fd, request, arg: ioctls.append((fd, request, arg)))
            leds.set_intensity(0, 3)
            leds.set_intensity(1, 3)
            leds.draw_pair(THINKING[0][0], BLANK)
            leds.exit()
        self.assertEqual(ioctls, [(7, 0x40016B01, b'\x00'), (7, 0x40046B04, (1_000_000).to_bytes(4, 'little'))])
        registers = {}
        for fd, data in writes:
            self.assertEqual((fd, len(data)), (7, 4))          # one 32-bit transfer per register write
            for device, (opcode, value) in zip((1, 0), (data[0:2], data[2:4])):
                if opcode:                                       # 0 = no-op for the other matrix
                    registers[device, opcode] = value
        for device in (0, 1):
            self.assertEqual(registers[device, 15], 0)
            self.assertEqual(registers[device, 10], 3)
        self.assertEqual(tuple(registers[1, r + 1] for r in range(8)), THINKING[0][0])
        self.assertEqual(tuple(registers[0, r + 1] for r in range(8)), BLANK)
        self.assertEqual(closed, [7])

    def test_spidev_ports_use_spi_unless_a_test_board_is_given(self):
        from features.eyes import driver
        with patch.object(driver, 'SpiLedControl') as spi:
            leds, close = driver.open_leds('/dev/spidev3.1')
        spi.assert_called_once_with('/dev/spidev3.1')
        self.assertIs(close, spi.return_value.exit)
        board = Board()
        with patch.object(driver, 'SpiLedControl') as spi:
            leds, close = driver.open_leds('/dev/spidev3.1', lambda port: board)
        spi.assert_not_called()
        self.assertIsInstance(leds, LedControl)

    def test_failed_row_transfer_is_retried(self):
        leds = LedControl(Board())
        with patch.object(leds, 'transfer', side_effect=[OSError('disconnect'), None]) as transfer:
            with self.assertRaises(OSError):
                leds.set_row(0, 0, 42)
            leds.set_row(0, 0, 42)
        self.assertEqual(transfer.call_count, 2)

    def test_reply_interrupts_thinking_hold_and_restores_conversation_eyes(self):
        self.eyes.event('thinking')
        self.now = 3
        frames = []
        def draw(left, right=None):
            frames.append((left, left if right is None else right))
            if (left, right) == THINKING[0]:
                self.eyes.event('speaking')
            elif len(frames) > 1 and left == OPEN:
                self.eyes.stopping.set()
        self.eyes.render(Mock(draw_pair=draw))
        self.assertEqual(frames, [(OPEN, OPEN), THINKING[0], (OPEN, OPEN)])

    def test_normal_blinks_continue_while_speaking_and_listening(self):
        for event in ('speaking', 'listening'):
            eyes = Eyes(clock=lambda: 0)
            eyes.event(event)
            frames = []
            def draw(left, right=None):
                frames.append(left)
                if len(frames) == 3:
                    eyes.stopping.set()
            with patch('features.eyes.controller.random.uniform', return_value=0), patch(
                    'features.eyes.controller.random.random', return_value=0.5), patch.object(eyes, 'hold', return_value=True):
                eyes.render(Mock(draw_pair=draw))
            self.assertEqual(frames, [OPEN, BLINK, OPEN])

    def test_worker_shutdown_blanks_both_and_closes_connection(self):
        board = Board()
        eyes = Eyes(board_factory=lambda port: board)
        eyes.stopping.set()
        eyes.run()
        self.assertTrue(board.closed)
        self.assertEqual(board.registers[0, 12], 0)
        self.assertEqual(board.registers[1, 12], 0)

    def test_missing_hardware_is_nonfatal(self):
        eyes = Eyes(board_factory=Mock(side_effect=OSError('no Arduino')))
        eyes.start()
        eyes.stop()
        self.assertFalse(eyes.thread.is_alive())

    def test_unreachable_gemini_turns_to_x_eyes_and_recovers(self):
        eyes = Eyes(clock=lambda: self.now, booting=True, dead_after=120)
        self.now = 119
        self.assertEqual(eyes.mode(), 'confused')
        self.now = 121
        self.assertEqual(eyes.mode(), 'dead')  # Never connected: unplug and replug.
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'normal')
        self.assertTrue(eyes.connected_once)
        eyes.event('reconnecting')
        self.now = 200
        self.assertEqual(eyes.mode(), 'confused')  # Short reconnects: confused, no replug needed.
        eyes.event('reconnecting')  # Repeated reconnects keep the first timestamp.
        self.now = 242
        self.assertEqual(eyes.mode(), 'dead')
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'normal')
        self.assertEqual(Eyes(clock=lambda: self.now, booting=True).mode(), 'confused')  # Watchdog off.

    def test_network_watch_reports_lost_after_five_seconds_and_back_at_once(self):
        from features.eyes.network import NetworkWatch, GEMINI_HOST
        up, events, targets = [True], [], []
        def connect(address, timeout):
            targets.append((address, timeout))
            if not up[0]:
                raise OSError('no route')
            return Mock()
        watch = NetworkWatch(events.append, connect=connect, clock=lambda: self.now)
        watch.check()
        self.assertEqual(targets[0], ((GEMINI_HOST, 443), 2.0))
        up[0] = False
        for second in range(5):
            self.now = 10 + second
            watch.check()
        self.assertEqual(events, [])  # 0..4 s of failures: not yet.
        self.now = 15
        watch.check()
        self.assertEqual(events, ['network_lost'])
        self.now = 16
        watch.check()
        self.assertEqual(events, ['network_lost'])  # Reported once.
        up[0] = True
        watch.check()
        self.assertEqual(events, ['network_lost', 'network_ok'])
        up[0] = False
        self.now = 20
        watch.check()
        up[0] = True
        watch.check()
        self.assertEqual(events, ['network_lost', 'network_ok'])  # A blip never reports.

    def test_offline_shows_confused_then_x_and_clears_without_a_reconnect(self):
        eyes = Eyes(clock=lambda: self.now, dead_after=120)
        eyes.event('listening')
        eyes.express('happy')
        eyes.event('network_lost')
        self.assertEqual(eyes.mode(), 'confused')
        eyes.event('network_ok')
        self.assertEqual(eyes.mode(), 'happy')  # Network events do not wipe the face.
        eyes.event('network_lost')
        self.now = 121
        self.assertEqual(eyes.mode(), 'dead')
        eyes.event('network_ok')
        eyes.event('reconnecting')
        self.assertEqual(eyes.mode(), 'confused')  # Gemini session still recovering.
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'normal')

    def test_session_runs_network_watch_unless_disabled(self):
        from features.eyes import controller, network
        for setting, expected in [('1', True), ('0', False)]:
            fake = Mock(exit_failed=False, connected_once=False, final_face=None)
            with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1', 'BAYMAX_EYES_NETWORK_WATCH': setting}), \
                    patch.object(controller, 'Eyes', return_value=fake), \
                    patch.object(network, 'NetworkWatch') as watch, patch.object(controller, 'failure_face'):
                with eyes_session():
                    if expected:
                        report = watch.call_args.args[0]
                        report('network_lost')
            self.assertEqual(watch.called, expected)
            if expected:
                watch.return_value.start.assert_called_once()
                watch.return_value.stop.assert_called_once()
                fake.event.assert_called_with('network_lost')

    def test_sleep_after_three_quiet_minutes_and_wake_on_conversation(self):
        eyes = Eyes(clock=lambda: self.now, booting=True, sleep_after=180)
        self.now = 500
        self.assertEqual(eyes.mode(), 'confused')  # Never sleeps through startup.
        eyes.event('listening')
        self.now = 679
        self.assertEqual(eyes.mode(), 'normal')
        self.now = 681
        self.assertEqual(eyes.mode(), 'sleep')
        eyes.event('user_speech')
        self.assertEqual(eyes.mode(), 'normal')
        for wake in ('thinking', 'speaking', 'calling_tool', 'reply_complete', 'reply_playback_done'):
            self.now += 200
            self.assertEqual(eyes.mode(), 'sleep')
            eyes.event(wake)
            self.assertNotEqual(eyes.mode(), 'sleep', wake)
        self.now += 200
        eyes.express('happy')
        self.assertEqual(eyes.mode(), 'happy')
        self.now += 200
        eyes.event('network_lost')
        self.assertEqual(eyes.mode(), 'confused')  # Being down beats sleeping.
        self.assertEqual(Eyes(clock=lambda: self.now).mode(), 'normal')  # sleep_after=0 never sleeps.

    def test_robot_eyes_sleep_60_seconds_after_ember_stops_talking(self):
        from features.eyes import controller, network
        fake = Mock(exit_failed=False, connected_once=False, final_face=None)
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1', 'BAYMAX_EYES_NETWORK_WATCH': '0'}), \
                patch.object(controller, 'Eyes', return_value=fake) as made, \
                patch.object(network, 'NetworkWatch'), patch.object(controller, 'failure_face'):
            os.environ.pop('BAYMAX_EYES_SLEEP_AFTER_S', None)
            with eyes_session():
                pass
        self.assertEqual(made.call_args.kwargs['sleep_after'], 60)
        eyes = Eyes(clock=lambda: self.now, sleep_after=60)
        eyes.event('listening')
        eyes.event('speaking')
        eyes.event('reply_complete')
        self.now += 15                  # Gemini is done; its last words are still playing
        eyes.event('reply_playback_done')
        self.now += 59
        self.assertEqual(eyes.mode(), 'normal')
        self.now += 2
        self.assertEqual(eyes.mode(), 'sleep')
        eyes.event('user_speech')       # spoken to: blinking again
        self.assertEqual(eyes.mode(), 'normal')

    def test_empty_playback_heartbeats_do_not_keep_eyes_awake(self):
        eyes = Eyes(clock=lambda: self.now, sleep_after=60)
        eyes.event('listening')
        eyes.event('speaking')
        eyes.event('reply_complete')
        self.now = 5
        with patch.object(monitor, 'event_observer', eyes.event):
            monitor.snapshot({'playback_bytes': 0})
            self.assertEqual(eyes.last_activity, 5)
            for now in range(6, 127):
                self.now = now
                monitor.snapshot({'playback_bytes': 0})
            self.assertEqual(eyes.last_activity, 5)
            self.assertEqual(eyes.mode(), 'sleep')
            eyes.event('user_speech')
            self.assertEqual(eyes.mode(), 'normal')

    def test_sleep_eyes_are_dimmed_and_brightness_returns_on_wake(self):
        eyes = Eyes(clock=lambda: self.now, sleep_after=180)
        eyes.event('listening')
        self.now = 200
        calls, frames = [], []
        leds = Mock()
        leds.set_intensity.side_effect = lambda device, value: calls.append((device, value))
        def draw(left, right=None):
            frames.append(left)
            if len(frames) == 2:
                eyes.event('user_speech')  # Someone talks to the sleeping robot.
            elif len(frames) == 3:
                eyes.stopping.set()  # Open eyes restored; the frozen clock never blinks again.
        leds.draw_pair.side_effect = draw
        with patch.object(eyes, 'hold', return_value=True):
            eyes.render(leds)
        self.assertEqual(calls, [(0, 1), (1, 1), (0, 3), (1, 3)])
        self.assertEqual(frames[1], EXPRESSIONS['sleep'][0][0][0])

    def test_lost_sensor_rules(self):
        from features.eyes.controller import lost_sensor, expected_sensors
        everything = ['microphone', 'speaker', 'camera']
        fresh = {'microphone': 99, 'speaker': 99, 'camera': 95}
        self.assertIsNone(lost_sensor(fresh, everything, 100, 0, 0))
        self.assertEqual(lost_sensor({**fresh, 'microphone': 94}, everything, 100, 0, 0), 'microphone')
        self.assertEqual(lost_sensor({**fresh, 'camera': 89}, everything, 100, 0, 0), 'camera')
        self.assertIsNone(lost_sensor({**fresh, 'camera': 91}, everything, 100, 0, 0))  # Camera allows 10s.
        # Never reported: only after the 30s grace from first connect.
        self.assertIsNone(lost_sensor({'microphone': 99, 'speaker': 99}, everything, 29, 0, 0))
        self.assertEqual(lost_sensor({'microphone': 29, 'speaker': 29}, everything, 31, 0, 0), 'camera')
        # Silence is counted from the latest (re)connect, so a reconnect's reopened streams are not "lost".
        self.assertIsNone(lost_sensor({'microphone': 10, 'speaker': 10, 'camera': 10}, everything, 103, 0, 100))
        self.assertEqual(expected_sensors({'BAYMAX_AUDIO_ENHANCEMENT': '1', 'BAYMAX_CAMERA_DEVICE': '2'}), everything)
        self.assertEqual(expected_sensors({'BAYMAX_AUDIO_ENHANCEMENT': '1', 'BAYMAX_CAMERA_DEVICE': 'none'}),
                         ['microphone', 'speaker'])
        self.assertEqual(expected_sensors({'BAYMAX_AUDIO_ENHANCEMENT': '0', 'BAYMAX_CAMERA_DEVICE': 'none'}), [])

    def test_lost_sensor_shows_x_only_while_connected_and_recovers(self):
        lost = [None]
        eyes = Eyes(clock=lambda: self.now, booting=True, sensors=lambda now, connected, ready: lost[0])
        lost[0] = 'microphone'
        self.assertEqual(eyes.mode(), 'confused')  # Not connected yet: startup, not a lost sensor.
        eyes.event('listening')
        self.assertEqual(eyes.mode(), 'dead')
        eyes.event('reconnecting')
        self.assertEqual(eyes.mode(), 'confused')  # Streams reopen during reconnects.
        eyes.event('listening')
        lost[0] = None
        self.assertEqual(eyes.mode(), 'normal')
        calls = []
        eyes = Eyes(clock=lambda: self.now, booting=True, sensors=lambda *args: calls.append(args))
        self.now = 50
        eyes.event('listening')
        self.now = 60
        eyes.event('listening')  # Ordinary post-reply 'listening' keeps the connect time.
        eyes.mode()
        self.assertEqual(calls[-1], (60, 50, 50))
        broken = Eyes(sensors=Mock(side_effect=RuntimeError('bad')))
        broken.event('listening')
        self.assertEqual(broken.mode(), 'normal')  # A failing check never blocks the eyes.

    def test_sensor_heartbeats_come_from_audio_and_camera_paths(self):
        from features.conversational_ability.processing import InputEnhancer
        from features.toolkits.fall_detection import worker
        import numpy as np
        monitor.sensors.clear()
        enhancer = InputEnhancer(processor=Mock(process=lambda near, far: near, reset=Mock(), speech_probability=0, gain_db=0))
        enhancer.process(np.zeros(480, dtype=np.int16))
        self.assertNotIn('microphone', monitor.sensors)  # Digital silence = no working device.
        enhancer.process(np.full(480, 5, dtype=np.int16))
        enhancer.rendered(bytes(960))
        worker.submit(np.zeros((2, 2, 3), dtype=np.uint8))
        self.assertEqual(set(monitor.sensors), {'microphone', 'speaker', 'camera'})
        self.assertIn('camera', monitor.snapshot()['sensor_age_s'])
        monitor.sensors.clear()

    def test_failed_starts_show_confused_then_x_after_three_and_connecting_resets(self):
        import tempfile
        from pathlib import Path
        from features.eyes.controller import failure_face, clear_failures
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".eyes-failures"
            self.assertEqual([failure_face(False, path) for _ in range(4)], ['confused', 'confused', 'dead', 'dead'])
            self.assertEqual(failure_face(True, path), 'confused')  # Failed after a real session: count restarts.
            clear_failures(path)
            self.assertFalse(path.exists())
            clear_failures(path)  # Missing file is fine.

    def test_final_face_is_left_on_matrices_instead_of_blanking(self):
        board = Board()
        eyes = Eyes(board_factory=lambda port: board)
        eyes.final_face = 'dead'
        eyes.stopping.set()
        eyes.run()
        left, right = EXPRESSIONS['dead'][0][0]
        self.assertEqual(tuple(board.registers[1, r + 1] for r in range(8)), left)
        self.assertEqual(tuple(board.registers[0, r + 1] for r in range(8)), right)
        self.assertNotEqual(board.registers[0, 12], 0)  # Not shut down.
        self.assertTrue(board.closed)

    def test_after_exit_draws_x_only_when_ember_was_killed_or_crashed(self):
        from features.eyes import show
        crashes = [{'SERVICE_RESULT': 'signal', 'EXIT_CODE': 'killed'}, {'SERVICE_RESULT': 'core-dump', 'EXIT_CODE': 'dumped'},
                   {'SERVICE_RESULT': 'timeout', 'EXIT_CODE': 'killed'}, {'SERVICE_RESULT': 'watchdog'}]
        handled = [{'SERVICE_RESULT': 'success', 'EXIT_CODE': 'exited'}, {'SERVICE_RESULT': 'exit-code', 'EXIT_CODE': 'exited'}, {}]
        for env in crashes:
            board = Board()
            self.assertEqual(show.main(['--after-exit'], {'BAYMAX_EYES_ENABLED': '1', **env}, lambda port: board), 0)
            left, right = EXPRESSIONS['dead'][0][0]
            self.assertEqual(tuple(board.registers[1, r + 1] for r in range(8)), left, env)
            self.assertEqual(tuple(board.registers[0, r + 1] for r in range(8)), right, env)
            self.assertEqual(board.registers[0, 12], 1)  # Lit, not shut down.
            self.assertEqual(board.registers[0, 10], 3)
            self.assertTrue(board.closed)
        for env in handled:  # Ember already drew its own exit face.
            factory = Mock()
            show.main(['--after-exit'], {'BAYMAX_EYES_ENABLED': '1', **env}, factory)
            factory.assert_not_called()

    def test_show_any_face_disabled_eyes_and_missing_arduino(self):
        from features.eyes import show
        board = Board()
        self.assertEqual(show.main(['sleep'], {'BAYMAX_EYES_ENABLED': '1'}, lambda port: board), 0)
        self.assertEqual(tuple(board.registers[1, r + 1] for r in range(8)), EXPRESSIONS['sleep'][0][0][0])
        factory = Mock()
        show.main(['--after-exit'], {'BAYMAX_EYES_ENABLED': '0', 'SERVICE_RESULT': 'signal'}, factory)
        factory.assert_not_called()
        ports = []
        show.main(['dead'], {'BAYMAX_EYES_ENABLED': '1', 'BAYMAX_EYES_PORT': '/dev/custom'},
                  lambda port: ports.append(port) or Board())
        self.assertEqual(ports, ['/dev/custom'])
        # A missing Arduino must never make the systemd hook fail.
        self.assertEqual(show.main(['dead'], {'BAYMAX_EYES_ENABLED': '1'}, Mock(side_effect=OSError('no port'))), 0)
        self.assertEqual(show.main(['bogus'], {'BAYMAX_EYES_ENABLED': '1'}, Mock()), 2)

    def test_session_sets_failure_face_only_for_errors(self):
        from features.eyes import controller
        cases = [(RuntimeError('speaker missing'), False, 'dead'), (KeyboardInterrupt(), False, None),
                 (SystemExit(0), False, None), (SystemExit(1), False, 'dead'), (None, True, 'dead'), (None, False, None)]
        for error, exit_failed, expected in cases:
            fake = Mock(exit_failed=False, connected_once=False, final_face=None)
            with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1'}), patch.object(controller, 'Eyes', return_value=fake), \
                    patch.object(controller, 'failure_face', return_value='dead') as face, \
                    patch.object(controller, 'clear_failures') as clear:
                try:
                    with eyes_session() as eyes:
                        eyes.exit_failed = exit_failed
                        if error is not None:
                            raise error
                except BaseException:
                    pass
            self.assertEqual(fake.final_face, expected, (error, exit_failed))
            fake.stop.assert_called_once()
            clear.assert_not_called()
        fake = Mock(exit_failed=False, connected_once=True, final_face=None)
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1'}), patch.object(controller, 'Eyes', return_value=fake), \
                patch.object(controller, 'clear_failures') as clear:
            with eyes_session():
                pass
        clear.assert_called_once()  # A healthy session resets the failure count.

    def test_mvp_session_connects_monitor_events_and_restores_observer(self):
        fake = Mock(exit_failed=False, connected_once=False)
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '1'}), patch(
                'features.eyes.controller.Eyes', return_value=fake), patch.object(monitor, 'event_observer', None):
            with eyes_session():
                monitor.emit('listening', 'Listening')
                monitor.transcribe('user', 'hello')
                monitor.emit('thinking', 'Waiting')
                monitor.emit('speaking', 'Audio')
            self.assertIsNone(monitor.event_observer)
        self.assertEqual([c.args[0] for c in fake.event.call_args_list],
                         ['listening', 'user_speech', 'thinking', 'speaking'])
        fake.start.assert_called_once()
        fake.stop.assert_called_once()

    def test_disabled_session_does_not_create_hardware(self):
        with patch.dict('os.environ', {'BAYMAX_EYES_ENABLED': '0'}), patch('features.eyes.controller.Eyes') as factory:
            with eyes_session():
                pass
        factory.assert_not_called()

    def test_observer_failure_cannot_break_monitor_or_transcription(self):
        with patch.object(monitor, 'event_observer', Mock(side_effect=ValueError('bad callback'))):
            monitor.emit('listening', 'Listening')
            self.assertIsNone(monitor.event_observer)


if __name__ == '__main__':
    unittest.main()
