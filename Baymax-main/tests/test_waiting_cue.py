"""Local thinking cue: fake clock/PCM and real output callback, no audio devices."""
import asyncio
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

from features.conversational_ability.waiting import WaitingCue, create
import runtime_monitor as monitor
from test_public_core_integration import load_function

CORE = Path(__file__).resolve().parents[1] / 'realtime_gemini_8.py'


class WaitingCueTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.pcm = np.array([100, 200, -100, -200], dtype=np.int16).tobytes()
        self.cue = WaitingCue(self.pcm, clock=lambda: self.now)

    def wait(self):
        self.cue.event('user_speech')
        self.cue.event('thinking')
        self.now += 2.001

    def test_no_sound_at_or_before_two_seconds_then_only_once(self):
        self.cue.event('thinking')
        for self.now in (0, 1.999, 2):
            self.assertEqual(self.cue.render(b'', 4), b'')
        self.now = 2.001
        self.assertEqual(self.cue.render(b'', 4), self.pcm[:4])
        self.assertEqual(self.cue.render(b'', 4), self.pcm[4:])
        self.now = 30
        self.assertEqual(self.cue.render(b'', 4), b'')

    def test_transcription_fragments_reset_delay_without_repeating_cue(self):
        self.cue.event('user_speech')
        self.now = 1.5
        self.cue.event('user_speech')
        self.now = 3
        self.assertEqual(self.cue.render(b'', 8), b'')
        self.now = 3.501
        self.assertEqual(self.cue.render(b'', 8), self.pcm)
        self.cue.event('user_speech')
        self.now += 3
        self.assertEqual(self.cue.render(b'', 8), b'')

    def test_real_reply_cancels_cue_with_bytes_unchanged(self):
        self.wait()
        self.assertEqual(self.cue.render(b'', 2), self.pcm[:2])
        self.assertEqual(self.cue.render(b'\x11\x22', 8), b'\x11\x22')
        self.cue.event('user_speech')
        self.cue.event('thinking')
        self.now += 5
        self.assertEqual(self.cue.render(b'', 8), b'')

    def test_speaking_end_interruption_and_reconnect_cancel_remaining_audio(self):
        for event in ('speaking', 'listening', 'connecting', 'reconnecting', 'stopping', 'stopped'):
            self.cue.event('listening')
            self.wait()
            self.cue.render(b'', 2)
            self.cue.event(event)
            self.assertEqual(self.cue.render(b'', 8), b'')

    def test_next_completed_turn_can_have_one_new_cue(self):
        self.wait()
        self.assertEqual(self.cue.render(b'', 8), self.pcm)
        self.cue.event('speaking')
        self.cue.event('listening')
        self.wait()
        self.assertEqual(self.cue.render(b'', 8), self.pcm)

    def test_cleaned_user_speech_cancels_cue_but_low_noise_does_not(self):
        self.wait()
        self.cue.microphone(np.full(48, 20, dtype=np.int16).tobytes(), 1000)
        self.assertEqual(self.cue.render(b'', 2), self.pcm[:2])
        self.cue.microphone(np.full(48, 2000, dtype=np.int16).tobytes(), 1000)
        self.assertEqual(self.cue.render(b'', 8), b'')

    def callback(self, enhancer):
        namespace = {'_playback_buffer': b'', '_playback_lock': threading.Lock(),
                     '_waiting_cue': self.cue, '_audio_enhancer': enhancer,
                     'np': np, 'OUTPUT_CHANNELS': 1, 'monitor': monitor, 'RECEIVE_SAMPLE_RATE': 48000,
                     'voice_volume': SimpleNamespace(apply=lambda chunk: chunk)}  # v10's speaker volume
        with patch('test_public_core_integration.CORE', CORE):
            fn = load_function('_output_callback', namespace)
        return fn, namespace

    def test_cue_uses_real_speaker_callback_and_echo_reference(self):
        enhancer = SimpleNamespace(full_duplex=True, rendered=Mock(), mix_output=lambda chunk: chunk)
        callback, namespace = self.callback(enhancer)
        self.wait()
        output = np.empty((8, 1), dtype=np.int16)
        callback(output, 8, None, None)
        expected = self.pcm + bytes(8)
        self.assertEqual(output.tobytes(), expected)
        enhancer.rendered.assert_called_once_with(expected, None)
        namespace['_playback_buffer'] = b'\x01\x00' * 8
        callback(output, 8, None, None)
        self.assertEqual(output.tobytes(), b'\x01\x00' * 8)

    def test_echo_canceller_failure_silences_cue_without_touching_reply(self):
        callback, namespace = self.callback(SimpleNamespace(full_duplex=False, rendered=Mock(), mix_output=lambda chunk: chunk))
        self.wait()
        output = np.empty((4, 1), dtype=np.int16)
        callback(output, 4, None, None)
        self.assertEqual(output.tobytes(), bytes(8))
        namespace['_playback_buffer'] = self.pcm
        callback(output, 4, None, None)
        self.assertEqual(output.tobytes(), self.pcm)

    def test_monitor_drives_cue_even_without_eye_hardware(self):
        with patch.object(monitor, 'waiting_audio', self.cue), patch.object(monitor, 'event_observer', None):
            monitor.transcribe('user', 'hello')
            monitor.emit('thinking', 'Waiting')
            self.now = 2.001
            self.assertEqual(self.cue.render(b'', 8), self.pcm)
            monitor.emit('speaking', 'Audio')
            self.assertEqual(self.cue.render(b'', 8), b'')

    def test_asset_is_bounded_non_silent_pcm_and_disabled_mode_reads_nothing(self):
        with patch.dict(os.environ, {'BAYMAX_WAIT_CUE_ENABLED': '0'}), patch('wave.open') as read:
            self.assertIsNone(create(True))
            read.assert_not_called()
        with patch.dict(os.environ, {'BAYMAX_WAIT_CUE_ENABLED': '1'}):
            cue = create(True)
            self.assertIsNotNone(cue)
            self.assertTrue(0 < len(cue.pcm) <= 192000)
            self.assertGreater(np.max(np.abs(np.frombuffer(cue.pcm, dtype=np.int16))), 100)
            self.assertIsNone(create(False))

    def test_missing_or_wrong_format_asset_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cue.wav'
            with patch.dict(os.environ, {'BAYMAX_WAIT_CUE_ENABLED': '1'}), patch(
                    'features.conversational_ability.waiting.ASSET', path):
                self.assertIsNone(create(True))
                with wave.open(str(path), 'wb') as out:
                    out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    out.writeframes(self.pcm)
                self.assertIsNone(create(True))

    def test_microphone_hook_uses_cleaned_pcm_and_preserves_raw_recording(self):
        recordings, microphone = queue.Queue(), asyncio.Queue()
        samples = np.full((1024, 1), 1500, dtype=np.int16)
        cue = Mock()
        enhancer = SimpleNamespace(reset=Mock(), process=lambda values, timing=None: np.zeros_like(values))
        class Stream:
            latency = .02
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self):
                self.callback(samples, 1024, None, None)
            def __exit__(self, *args):
                pass
        namespace = {'asyncio': asyncio, 'np': np, 'queue': queue, 'time': time,
                     '_audio_enhancer': enhancer, '_waiting_cue': cue, 'INPUT_CHANNELS': 1,
                     '_audio_record_queue': recordings, 'audio_queue_mic': microphone,
                     'INTERRUPT_RMS_THRESHOLD': 1000, 'sd': SimpleNamespace(InputStream=Stream),
                     'SEND_SAMPLE_RATE': 48000, 'CHUNK_SIZE': 1024}
        with patch('test_public_core_integration.CORE', CORE):
            listen = load_function('listen_audio', namespace)
        async def exercise():
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(listen(), .02)
        asyncio.run(exercise())
        cue.microphone.assert_called_once_with(bytes(2048), 1000)
        self.assertEqual(recordings.get_nowait(), samples.tobytes())
        self.assertEqual(microphone.get_nowait()['data'], bytes(2048))


if __name__ == '__main__':
    unittest.main()
