import asyncio
import os
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import runtime_monitor as monitor
import numpy as np
from features.conversational_ability.processing import InputEnhancer, extend_config
from test_public_core_integration import load_function


class FakeDSP:
    speech_probability = .5
    gain_db = 0
    def reset(self):
        self.frames = []
    def process(self, near, far):
        self.frames.append((near.copy(), far.copy()))
        return near.copy()


class AudioInputTests(unittest.TestCase):
    def test_turn_end_override_applies_with_or_without_cleanup_and_validates_input(self):
        for enhancement in ('0', '1'):
            for setting, expected in [('400', 400), ('600', 600), ('900', 900), ('1200', 1200), ('bad', 400), ('10', 400), ('9999', 400)]:
                with self.subTest(enhancement=enhancement, setting=setting), patch.dict(os.environ, {
                    'BAYMAX_AUDIO_ENHANCEMENT': enhancement, 'BAYMAX_TURN_END_SILENCE_MS': setting,
                    'BAYMAX_INTERRUPTION_ENABLED': '1'}):
                    base = {'realtime_input_config': {'turn_coverage': 'TURN_INCLUDES_ONLY_ACTIVITY'}}
                    result = extend_config(base)
                self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['silence_duration_ms'], expected)
                self.assertEqual(result['realtime_input_config']['turn_coverage'], 'TURN_INCLUDES_ONLY_ACTIVITY')
                self.assertNotIn('automatic_activity_detection', base['realtime_input_config'])

    def test_no_interruption_keeps_audio_configuration_and_original_untouched(self):
        base = {'speech_config': {'voice': 'same'}, 'input_audio_transcription': {},
                'realtime_input_config': {'turn_coverage': 'TURN_INCLUDES_ONLY_ACTIVITY'}}
        for enhancement in ('0', '1'):
            with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': enhancement,
                                        'BAYMAX_INTERRUPTION_ENABLED': '0'}):
                result = extend_config(base)
            self.assertEqual(result['realtime_input_config']['activity_handling'], 'NO_INTERRUPTION')
            self.assertEqual(result['realtime_input_config']['turn_coverage'], 'TURN_INCLUDES_ONLY_ACTIVITY')
            self.assertEqual(result['speech_config'], base['speech_config'])
            self.assertIn('input_audio_transcription', result)
            self.assertNotIn('activity_handling', base['realtime_input_config'])

    def test_disabled_configuration_is_unchanged_and_enabled_preserves_model_audio(self):
        base = {'speech_config': {'voice': 'same'}, 'realtime_input_config': {'automatic_activity_detection': {'silence_duration_ms': 100}}}
        with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '0'}):
            self.assertIs(extend_config(base), base)
        with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1'}):
            result = extend_config(base)
        self.assertEqual(result['speech_config'], base['speech_config'])
        self.assertEqual(base['realtime_input_config']['automatic_activity_detection']['silence_duration_ms'], 100)
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['silence_duration_ms'], 500)
        self.assertEqual(result['realtime_input_config']['activity_handling'], 'START_OF_ACTIVITY_INTERRUPTS')
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['prefix_padding_ms'], 0)
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['start_of_speech_sensitivity'], 'START_SENSITIVITY_LOW')

    def test_speech_start_can_be_explicitly_restored_to_high_without_changing_turn_end(self):
        base = {'speech_config': {'voice': 'same'}}
        for setting, expected in [('high', 'HIGH'), (' LOW ', 'LOW')]:
            with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1', 'BAYMAX_SPEECH_START_SENSITIVITY': setting}):
                result = extend_config(base)
            vad = result['realtime_input_config']['automatic_activity_detection']
            self.assertEqual(vad['start_of_speech_sensitivity'], 'START_SENSITIVITY_' + expected)
            self.assertEqual(vad['silence_duration_ms'], 500)
            self.assertEqual(vad['end_of_speech_sensitivity'], 'END_SENSITIVITY_LOW')
            self.assertEqual(result['speech_config'], base['speech_config'])

    def test_missing_or_invalid_sensitivity_defaults_to_low(self):
        for settings in ({}, {'BAYMAX_SPEECH_START_SENSITIVITY': 'invalid'}):
            with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1', **settings}, clear=True):
                result = extend_config({})
            self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['start_of_speech_sensitivity'], 'START_SENSITIVITY_LOW')

    def test_speech_end_sensitivity_can_be_set_for_device(self):
        with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1',
                                     'BAYMAX_SPEECH_END_SENSITIVITY': 'high'}):
            result = extend_config({})
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['end_of_speech_sensitivity'], 'END_SENSITIVITY_HIGH')

    def test_chunking_preserves_every_sample_across_1024_sample_callbacks(self):
        dsp = FakeDSP()
        enhancer = InputEnhancer(processor=dsp)
        enhancer.residual.enabled = False  # Isolate frame continuity from residual subtraction.
        source = np.arange(8192, dtype=np.int16)
        rendered = source * 2
        result = []
        for offset in range(0, len(source), 1024):
            enhancer.rendered(rendered[offset:offset+1024].tobytes())
            result.append(enhancer.process(source[offset:offset+1024]))
        result = np.concatenate(result)
        np.testing.assert_array_equal(result[:480], np.zeros(480))
        np.testing.assert_array_equal(result[480:], source[:-480])
        self.assertTrue(all(len(near) == len(far) == 480 for near, far in dsp.frames))
        np.testing.assert_array_equal(np.concatenate([far for _, far in dsp.frames]), rendered[:len(dsp.frames)*480])
        self.assertTrue(enhancer.full_duplex)

    def test_brief_80ms_input_is_not_discarded_by_chunking_or_a_local_gate(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        samples = np.zeros(16384, dtype=np.int16)
        samples[3500:7340] = 250
        out = np.concatenate([enhancer.process(samples[i:i+1024]) for i in range(0, len(samples), 1024)])
        np.testing.assert_array_equal(out[480:], samples[:-480])
        self.assertEqual(np.count_nonzero(out), 3840)

    def test_reference_is_bounded_and_missing_render_is_silence(self):
        dsp = FakeDSP()
        enhancer = InputEnhancer(processor=dsp)
        enhancer.process(np.ones(1024, dtype=np.int16))
        self.assertTrue(all(not np.any(far) for _, far in dsp.frames))
        enhancer.rendered(b'\1\0' * 100000)
        self.assertLessEqual(len(enhancer.render_buffer), 24000)

    def test_dsp_failure_keeps_raw_input_and_disables_full_duplex(self):
        dsp = FakeDSP()
        dsp.process = Mock(side_effect=RuntimeError('test DSP failure'))
        enhancer = InputEnhancer(processor=dsp)
        samples = np.full(1024, 1234, dtype=np.int16)
        np.testing.assert_array_equal(enhancer.process(samples), samples)
        self.assertFalse(enhancer.full_duplex)
        self.assertIn('failed', enhancer.metrics['state'])

    def test_negative_full_scale_is_detected_as_clipping(self):
        self.assertEqual(InputEnhancer.levels(np.array([-32768, 32767], dtype=np.int16))['clipped_percent'], 100)

    def test_residual_echo_is_not_re_amplified_but_near_end_speech_is_kept(self):
        class GainDSP(FakeDSP):
            gain_db = float(20 * np.log10(4))
            def process(self, near, far):
                return (near * 4).astype(np.int16)
        with patch.dict(os.environ, {'BAYMAX_MUTE_MIC_DURING_REPLY': '0'}):
            enhancer = InputEnhancer(processor=GainDSP())
        enhancer.rendered(np.full(960, 500, dtype=np.int16).tobytes())
        # Nonzero cleaned near-end speech must pass, at microphone-level gain.
        near = np.full(960, 1000, dtype=np.int16)
        result = enhancer.process(near)
        np.testing.assert_array_equal(result[480:], near[:-480])
        self.assertTrue(enhancer.metrics['echo_guard'])
        self.assertEqual(enhancer.metrics['gain_db'], 0)
        self.assertAlmostEqual(enhancer.metrics['agc_gain_db'], 12, places=0)
        enhancer.reset()
        quiet_result = enhancer.process(near)
        np.testing.assert_array_equal(quiet_result[480:], near[:-480] * 4)
        self.assertFalse(enhancer.metrics['echo_guard'])

    @staticmethod
    def tone(freq, seconds=1.0, amplitude=8000, rate=48000):
        t = np.arange(int(seconds * rate)) / rate
        return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)

    @staticmethod
    def db(samples):
        return 20 * np.log10(max(float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))), 1e-9))

    def test_fan_filter_removes_measured_fan_hum_and_keeps_voice_band(self):
        from features.conversational_ability.processing import FanFilter
        for freq, max_db in [(29, -55), (88, -24)]:
            fan = FanFilter(48000, 200)
            hum = self.tone(freq, 2.0)
            out = fan(hum)[48000:]  # Skip the start-up transient.
            self.assertLess(self.db(out) - self.db(hum[48000:]), max_db, freq)
        for freq in (300, 1000, 3000):
            fan = FanFilter(48000, 200)
            voice = self.tone(freq, 2.0)
            change = self.db(fan(voice)[48000:]) - self.db(voice[48000:])
            self.assertGreater(change, -1.0, freq)
            self.assertLess(change, 0.5, freq)

    def test_fan_filter_is_seamless_across_1024_sample_callbacks(self):
        from features.conversational_ability.processing import FanFilter
        signal = (self.tone(29) // 2 + self.tone(1000) // 2)
        whole = FanFilter(48000, 200)(signal)
        chunked_filter = FanFilter(48000, 200)
        chunked = np.concatenate([chunked_filter(signal[i:i + 1024]) for i in range(0, len(signal), 1024)])
        np.testing.assert_array_equal(whole, chunked)

    def test_runtime_enhancer_filters_before_dsp_and_setting_controls_it(self):
        from features.conversational_ability import processing
        for setting, expected in [(None, 200.0), ('0', 0), ('150', 150.0), ('loud', 200.0)]:
            with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1'}), \
                    patch.object(processing, 'InputEnhancer') as enhancer:
                os.environ.pop('BAYMAX_FAN_FILTER_HZ', None)
                if setting is not None:
                    os.environ['BAYMAX_FAN_FILTER_HZ'] = setting
                processing.create()
            self.assertEqual(enhancer.call_args.kwargs['fan_filter_hz'], expected, setting)
        dsp = FakeDSP()
        enhancer = InputEnhancer(processor=dsp, fan_filter_hz=200)
        enhancer.process(self.tone(29, 0.5))
        near = np.concatenate([frame for frame, _ in dsp.frames])
        self.assertLess(self.db(near[12000:]), self.db(self.tone(29, 0.25)) - 40)
        self.assertEqual(enhancer.metrics['fan_filter_hz'], 200)
        self.assertIsNone(InputEnhancer(processor=FakeDSP()).fan_filter)  # Direct use stays unfiltered.

    def test_missing_filter_library_keeps_audio_cleanup_running(self):
        import builtins
        real_import = builtins.__import__
        def no_scipy(name, *args, **kwargs):
            if name.startswith('scipy'):
                raise ImportError('no scipy')
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=no_scipy):
            enhancer = InputEnhancer(processor=FakeDSP(), fan_filter_hz=200)
        self.assertIsNone(enhancer.fan_filter)
        near = np.full(960, 1000, dtype=np.int16)
        self.assertEqual(len(enhancer.process(near)), 960)

    def test_mute_during_reply_defaults_on_only_without_interruptions(self):
        for env, expected in [({}, False), ({'BAYMAX_INTERRUPTION_ENABLED': '1'}, False),
                              ({'BAYMAX_INTERRUPTION_ENABLED': '0'}, True),
                              ({'BAYMAX_INTERRUPTION_ENABLED': '0', 'BAYMAX_MUTE_MIC_DURING_REPLY': '0'}, False),
                              ({'BAYMAX_INTERRUPTION_ENABLED': '1', 'BAYMAX_MUTE_MIC_DURING_REPLY': '1'}, True)]:
            clean = {k: v for k, v in os.environ.items()
                     if k not in ('BAYMAX_INTERRUPTION_ENABLED', 'BAYMAX_MUTE_MIC_DURING_REPLY')}
            with patch.dict(os.environ, {**clean, **env}, clear=True):
                self.assertEqual(InputEnhancer(processor=FakeDSP()).mute_during_reply, expected, env)

    def test_reply_mute_sends_silence_while_speaker_audible_but_keeps_aec_adapting(self):
        clock = [10.0]
        with patch.dict(os.environ, {'BAYMAX_MUTE_MIC_DURING_REPLY': '1'}):
            dsp = FakeDSP()
            enhancer = InputEnhancer(processor=dsp, clock=lambda: clock[0])
        enhancer.rendered(np.full(960, 500, dtype=np.int16).tobytes())
        near = np.full(960, 1000, dtype=np.int16)
        np.testing.assert_array_equal(enhancer.process(near), np.zeros_like(near))
        self.assertTrue(enhancer.metrics['mic_muted_during_reply'])
        self.assertTrue(enhancer.full_duplex)
        self.assertEqual(len(dsp.frames), 2)  # AEC still saw the echo frames.
        clock[0] += 1
        result = enhancer.process(near)
        self.assertTrue(np.any(result))
        self.assertFalse(enhancer.metrics['mic_muted_during_reply'])

    def test_speaker_guard_covers_hardware_buffer_and_echo_tail_then_expires(self):
        clock = [10.0]
        enhancer = InputEnhancer(processor=FakeDSP(), clock=lambda: clock[0])
        enhancer.output_latency_ms = 100
        enhancer.rendered(np.ones(480, dtype=np.int16).tobytes())
        self.assertTrue(enhancer.speaker_active())
        clock[0] = 10.359
        self.assertTrue(enhancer.speaker_active())
        clock[0] = 10.361
        self.assertFalse(enhancer.speaker_active())

    def test_rendered_silence_does_not_disable_normal_gain(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        enhancer.rendered(bytes(2048))
        self.assertFalse(enhancer.speaker_active())

    def test_callback_timestamps_supply_aec_delay_hint(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        enhancer.rendered(bytes(960), SimpleNamespace(currentTime=10, outputBufferDacTime=10.12))
        enhancer.process(np.zeros(480, dtype=np.int16), SimpleNamespace(currentTime=10.4, inputBufferAdcTime=10.3))
        self.assertAlmostEqual(enhancer.input_latency_ms, 100)
        self.assertAlmostEqual(enhancer.output_latency_ms, 120)
        self.assertTrue(219 <= enhancer.processor.stream_delay_ms <= 220)
        self.assertEqual(enhancer.metrics['input_latency_ms'], 100)
        self.assertEqual(enhancer.metrics['output_latency_ms'], 120)

    def test_dsp_failure_suppresses_audible_echo_even_after_playback_queue_drains(self):
        clock = [10.0]
        dsp = FakeDSP()
        dsp.process = Mock(side_effect=RuntimeError('DSP failed'))
        enhancer = InputEnhancer(processor=dsp, clock=lambda: clock[0])
        enhancer.rendered(np.ones(960, dtype=np.int16).tobytes())
        near = np.full(960, 1000, dtype=np.int16)
        np.testing.assert_array_equal(enhancer.process(near), np.zeros_like(near))
        self.assertFalse(enhancer.full_duplex)
        np.testing.assert_array_equal(enhancer.process(near), np.zeros_like(near))
        clock[0] += 1
        np.testing.assert_array_equal(enhancer.process(near), near)
        self.assertFalse(enhancer.metrics['echo_guard'])

    def test_microphone_is_sent_during_playback_with_healthy_echo_cancellation(self):
        async def exercise(active):
            mic_queue = asyncio.Queue()
            await mic_queue.put({'data': b'pcm-test', 'mime_type': 'audio/pcm;rate=48000'})
            sent = []
            async def send(**kwargs):
                sent.append(kwargs['audio'].data)
                raise asyncio.CancelledError()
            fn = load_function('send_audio_realtime', {'asyncio': asyncio, 'time': time,
                'MIC_SEND_TIMEOUT_S': 5, 'audio_queue_mic': mic_queue, 'tracker_mic_queue': SimpleNamespace(record=Mock()),
                'tracker_send_audio': SimpleNamespace(record=Mock()), '_playback_lock': threading.Lock(),
                '_playback_buffer': b'playing', '_audio_enhancer': active,
                'types': SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})
            try:
                await asyncio.wait_for(fn(SimpleNamespace(send_realtime_input=send)), .02)
            except (asyncio.CancelledError, TimeoutError):
                pass
            return sent
        fresh = lambda ms: False
        self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=True, stale=fresh))), [b'pcm-test'])
        self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=False, stale=fresh))), [])
        # MVP runs v8: audio queued during a network stall is dropped, not replayed as a burst of turns.
        from pathlib import Path
        with patch('test_public_core_integration.CORE', Path(__file__).resolve().parents[1] / "realtime_gemini_8.py"):
            self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=True, stale=fresh))), [b'pcm-test'])
            self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=True, stale=lambda ms: True))), [])

    def test_stale_mic_audio_is_dropped_and_reported_once_sending_recovers(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        with patch('features.conversational_ability.processing.monitor.record') as record:
            self.assertFalse(enhancer.stale(40))
            self.assertTrue(all(enhancer.stale(5000) for _ in range(94)))  # ~2 s of 1024-sample chunks.
            record.assert_not_called()
            self.assertFalse(enhancer.stale(30))
        self.assertIn('Dropped 2.0s of stale microphone audio', record.call_args.args[1])
        with patch.dict(os.environ, {'BAYMAX_MAX_MIC_LAG_MS': '0'}):
            self.assertFalse(InputEnhancer(processor=FakeDSP()).stale(60000))  # 0 disables dropping.

    def test_mic_live_and_stalled_reach_the_eyes_but_not_the_waiting_cue(self):
        now = [100.0]
        enhancer = InputEnhancer(processor=FakeDSP(), clock=lambda: now[0])
        events, cue = [], Mock()
        with patch('features.conversational_ability.processing.monitor.record'),                 patch.object(monitor, 'event_observer', events.append),                 patch.object(monitor, 'waiting_audio', cue):
            enhancer.stale(40)
            enhancer.stale(40)                      # Throttled: once a second.
            now[0] += 1.5
            enhancer.stale(40)
            enhancer.stale(5000)
            enhancer.stale(5000)                    # One stall report, not one per chunk.
            enhancer.stale(40)                      # Recovered: live again at once.
        self.assertEqual(events, ['mic_live', 'mic_live', 'mic_stalled', 'mic_live'])
        cue.event.assert_not_called()

    def test_desktop_v8_preserves_raw_recording_with_processed_gemini_input(self):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "realtime_gemini_8.py"
        with patch('test_public_core_integration.CORE', path):
            self.test_recording_stays_raw_while_only_gemini_input_is_processed()

    def test_recording_stays_raw_while_only_gemini_input_is_processed(self):
        raw_recording = queue.Queue()
        microphone = asyncio.Queue()
        samples = np.full((1024, 1), 1234, dtype=np.int16)
        enhancer = SimpleNamespace(reset=lambda: None, process=lambda values, timing=None: np.zeros_like(values))
        class Stream:
            latency = .02
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self):
                self.callback(samples, 1024, None, None)
            def __exit__(self, *args):
                pass
        listen = load_function('listen_audio', {'asyncio': asyncio, 'np': np, 'queue': queue,
            'time': time, '_audio_enhancer': enhancer, '_waiting_cue': None, 'INPUT_CHANNELS': 1,
            '_audio_record_queue': raw_recording, 'audio_queue_mic': microphone,
            'INTERRUPT_RMS_THRESHOLD': 1000, 'sd': SimpleNamespace(InputStream=Stream),
            'SEND_SAMPLE_RATE': 48000, 'CHUNK_SIZE': 1024})
        async def run():
            try:
                await asyncio.wait_for(listen(), .02)
            except TimeoutError:
                pass
        asyncio.run(run())
        self.assertEqual(raw_recording.get_nowait(), samples.tobytes())
        self.assertEqual(microphone.get_nowait()['data'], bytes(2048))
