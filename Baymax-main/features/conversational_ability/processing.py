"""Opt-in desktop WebRTC audio cleanup, using Ember's existing playback reference."""
import copy
import os
import threading
import time

import numpy as np
import runtime_monitor as monitor
from .echo import ResidualEcho

_active_enhancer = None


def enabled():
    return os.getenv('BAYMAX_AUDIO_ENHANCEMENT', '0') == '1'


# Extra speaker sources: callable(n_bytes, voice_active) -> int16 PCM bytes or None.
output_sources = []
# Mic gates: callable(clean int16 samples) -> True to withhold this audio from Gemini
# (e.g. while an audiobook plays; the gate may still listen for wake words).
input_gates = []


def mute_during_reply():
    # Without interruptions, mic audio heard during a reply can only become a
    # later echo "user" turn, so default to silence while the speaker is audible.
    interruptions = os.getenv('BAYMAX_INTERRUPTION_ENABLED', '1') != '0'
    return os.getenv('BAYMAX_MUTE_MIC_DURING_REPLY', '0' if interruptions else '1') == '1'


def extend_config(config):
    interruptions = os.getenv('BAYMAX_INTERRUPTION_ENABLED', '1') != '0'
    silence = None
    if 'BAYMAX_TURN_END_SILENCE_MS' in os.environ:
        try:
            silence = int(os.environ['BAYMAX_TURN_END_SILENCE_MS'])
            if not 300 <= silence <= 2000:
                raise ValueError
        except ValueError:
            print('[AUDIO] Invalid turn-end silence; using 400 ms.', flush=True)
            silence = 400
    if not enabled():
        if interruptions and silence is None:
            return config
        result = copy.deepcopy(config)
        realtime = result.setdefault('realtime_input_config', {})
        if not interruptions:
            realtime['activity_handling'] = 'NO_INTERRUPTION'
        if silence is not None:
            realtime.setdefault('automatic_activity_detection', {})['silence_duration_ms'] = silence
        return result
    sensitivity = os.getenv('BAYMAX_SPEECH_START_SENSITIVITY', 'low').strip().lower()
    if sensitivity not in ('low', 'high'):
        print('[AUDIO] Invalid speech-start sensitivity; using low.', flush=True)
        sensitivity = 'low'
    end_sensitivity = os.getenv('BAYMAX_SPEECH_END_SENSITIVITY', 'low').strip().lower()
    if end_sensitivity not in ('low', 'high'):
        print('[AUDIO] Invalid speech-end sensitivity; using low.', flush=True)
        end_sensitivity = 'low'
    result = copy.deepcopy(config)
    realtime = result.setdefault('realtime_input_config', {})
    realtime['automatic_activity_detection'] = {
        'disabled': False, 'start_of_speech_sensitivity': 'START_SENSITIVITY_' + sensitivity.upper(),
        'end_of_speech_sensitivity': 'END_SENSITIVITY_' + end_sensitivity.upper(),
        'prefix_padding_ms': 0, 'silence_duration_ms': silence if silence is not None else 500}
    realtime['activity_handling'] = 'START_OF_ACTIVITY_INTERRUPTS' if interruptions else 'NO_INTERRUPTION'
    return result


class FanFilter:
    """4th-order high-pass below the voice band; filter state carries across callbacks.

    The LattePanda fan measured 29 Hz (52% of noise power) with 88/193 Hz tones;
    95% of its noise is under 211 Hz while speech intelligibility sits at 300-3400 Hz.
    """
    def __init__(self, rate, cutoff_hz):
        from scipy.signal import butter, sosfilt
        self.sosfilt = sosfilt
        self.cutoff = cutoff_hz
        self.sos = butter(4, cutoff_hz, btype='highpass', fs=rate, output='sos')
        self.reset()

    def reset(self):
        self.state = np.zeros((self.sos.shape[0], 2))

    def __call__(self, samples):
        out, self.state = self.sosfilt(self.sos, samples.astype(np.float64), zi=self.state)
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def fan_filter_hz():
    try:
        return float(os.getenv('BAYMAX_FAN_FILTER_HZ', '200'))
    except ValueError:
        print('[AUDIO] Invalid BAYMAX_FAN_FILTER_HZ; using 200.', flush=True)
        return 200.0


class InputEnhancer:
    """DSP runs only on the capture thread; render callback only queues PCM.

    Process complete 10ms frames, retaining leftovers across 1024-sample callbacks.
    A fixed 10ms output delay avoids padding/discarding tails on every callback.
    """
    def __init__(self, rate=48000, processor=None, ns_level=None, clock=time.monotonic, fan_filter_hz=0):
        self.fan_filter = None
        if fan_filter_hz > 0:
            try:
                self.fan_filter = FanFilter(rate, fan_filter_hz)
            except Exception as error:
                print(f'[AUDIO] Fan filter unavailable ({error}); continuing without it.', flush=True)
        if processor is None:
            from pywebrtc_audio import AudioProcessor
            level = int(os.getenv('BAYMAX_NOISE_SUPPRESSION_LEVEL', '1')) if ns_level is None else ns_level
            if level not in range(4):
                raise ValueError('Noise suppression level must be between 0 and 3.')
            processor = AudioProcessor(sample_rate=rate, echo_cancellation=True,
                                       noise_suppression=True, high_pass_filter=True,
                                       auto_gain_control=True, ns_level=level,
                                       agc_max_gain_db=18, stream_delay_ms=50)
        self.processor = processor
        self.residual = ResidualEcho(rate, clock, enabled=os.getenv('BAYMAX_RESIDUAL_ECHO_ENABLED', '1') == '1')
        self.mute_during_reply = mute_during_reply()
        try:
            self.max_lag_ms = float(os.getenv('BAYMAX_MAX_MIC_LAG_MS', '1000'))
        except ValueError:
            self.max_lag_ms = 1000.0
        self.dropped_ms = 0.0
        self.live_reported = None
        self.clock = clock
        self.rate = rate
        self.frame = rate // 100
        self.lock = threading.Lock()
        self.render_buffer = bytearray()
        self.input_latency_ms = 0
        self.output_latency_ms = 0
        self.failed = False
        self.metrics = {}
        self.speaker_until = float('-inf')
        self.voice_until = float('-inf')  # Ember's own voice only (not audiobooks).
        self.voice_hint = None
        self.reset()

    @property
    def full_duplex(self):
        return not self.failed

    def reset(self):
        self.processor.reset()
        self.residual.reset()
        self.pending = np.empty(0, dtype=np.int16)
        self.far_pending = np.empty(0, dtype=np.int16)
        self.output = np.zeros(self.frame, dtype=np.int16)
        with self.lock:
            self.render_buffer.clear()
            self.speaker_until = float('-inf')
            self.voice_until = float('-inf')
        self.failed = False
        if self.fan_filter is not None:
            self.fan_filter.reset()

    def rendered(self, pcm, timing=None):
        # Feed exactly what reached the speaker, including silence. Never capture
        # a second microphone/loopback stream or pass unplayed queued TTS as AEC reference.
        current = getattr(timing, 'currentTime', 0)
        dac = getattr(timing, 'outputBufferDacTime', 0)
        if current > 0 and dac > 0:
            self.output_latency_ms = min(500, max(0, (dac - current) * 1000))
        active = bool(np.any(np.frombuffer(pcm, dtype=np.int16)))
        # mix_output() says whether Ember's voice was in this chunk; without it, all output is voice.
        voice = active if self.voice_hint is None else self.voice_hint
        self.voice_hint = None
        monitor.sensor_alive('speaker')  # Output callbacks run continuously, silence included.
        with self.lock:
            if active:
                # Include queued device audio and a bounded acoustic echo tail.
                end = self.clock() + self.output_latency_ms / 1000 + len(pcm) / (self.rate * 2)
                self.speaker_until = max(self.speaker_until, end + 0.25)
                if voice:
                    self.voice_until = max(self.voice_until, end + 0.25)
            self.render_buffer.extend(pcm)
            maximum = self.rate * 2 // 4  # 250ms bound: discard stale reference on a stall.
            if len(self.render_buffer) > maximum:
                del self.render_buffer[:-maximum]
        self.residual.rendered(pcm)

    def stale(self, queue_wait_ms, chunk_ms=1024 / 48):
        """True for mic audio that waited too long to send (e.g. queued during a
        network stall): sending it later replays old speech as a burst of turns."""
        if self.max_lag_ms <= 0 or queue_wait_ms <= self.max_lag_ms:
            now = self.clock()
            if self.live_reported is None or now - self.live_reported >= 1.0:  # Eyes: mic audio is really going to Gemini.
                self.live_reported = now
                monitor.notify('mic_live')
            if self.dropped_ms:
                message = f'Dropped {self.dropped_ms / 1000:.1f}s of stale microphone audio after a send stall'
                print(f'[AUDIO] {message}.', flush=True)
                monitor.record('audio', message)
                self.dropped_ms = 0.0
            return False
        if not self.dropped_ms:
            self.live_reported = None
            monitor.notify('mic_stalled')  # Eyes: Ember cannot hear right now.
        self.dropped_ms += chunk_ms
        return True

    def mix_output(self, chunk):
        """Mix extra speaker sources (audiobooks) under Ember's voice before playback.

        Runs in the speaker callback ahead of rendered(), so echo cancellation's
        reference is exactly what is played. Sources get voice_active and hold
        while Ember speaks."""
        voice = np.frombuffer(chunk, dtype=np.int16)
        self.voice_hint = bool(np.any(voice))
        mixed = None
        for source in list(output_sources):
            try:
                extra = source(len(chunk), self.voice_hint)
            except Exception:
                output_sources.remove(source)
                print('[AUDIO] Extra speaker source disabled after error.', flush=True)
                continue
            if extra:
                add = np.frombuffer(extra, dtype=np.int16).astype(np.int32)
                mixed = (voice.astype(np.int32) if mixed is None else mixed) + add
        if mixed is None:
            return chunk
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

    def speaker_active(self):
        with self.lock:
            return self.clock() < self.speaker_until

    def voice_active(self):
        with self.lock:
            return self.clock() < self.voice_until

    def _reference(self, samples):
        wanted = samples * 2
        with self.lock:
            pcm = bytes(self.render_buffer[:wanted])
            del self.render_buffer[:wanted]
        return np.frombuffer(pcm + b'\0' * (wanted - len(pcm)), dtype=np.int16)

    @staticmethod
    def levels(samples):
        values = samples.astype(np.float32)
        rms = float(np.sqrt(np.mean(values * values))) if values.size else 0
        peak = float(np.max(np.abs(values))) if values.size else 0
        return {'rms_dbfs': round(20 * np.log10(max(rms, 1e-3) / 32768), 1),
                'peak_dbfs': round(20 * np.log10(max(peak, 1e-3) / 32768), 1),
                'clipped_percent': round(float(np.mean(np.abs(values) >= 32760)) * 100, 2) if values.size else 0}

    def process(self, samples, timing=None):
        if np.any(samples):  # Exact digital silence means no working capture device.
            monitor.sensor_alive('microphone')
        if self.fan_filter is not None:
            try:
                samples = self.fan_filter(samples)
            except Exception:
                self.fan_filter = None
                print('[AUDIO] Fan filter disabled after error; cleanup continues.', flush=True)
        if self.failed:
            return self._fallback(samples)
        try:
            current = getattr(timing, 'currentTime', 0)
            adc = getattr(timing, 'inputBufferAdcTime', 0)
            if current > 0 and adc > 0:
                self.input_latency_ms = min(500, max(0, (current - adc) * 1000))
            echo_guard = self.speaker_active()
            voice_guard = self.voice_active()
            self.pending = np.concatenate((self.pending, samples))
            self.far_pending = np.concatenate((self.far_pending, self._reference(len(samples))))
            self.processor.stream_delay_ms = int(min(500, max(0, self.input_latency_ms + self.output_latency_ms)))
            chunks = [self.output]
            count = (len(self.pending) // self.frame) * self.frame
            for offset in range(0, count, self.frame):
                clean = self.processor.process(self.pending[offset:offset+self.frame],
                                               self.far_pending[offset:offset+self.frame])
                if echo_guard:
                    # AGC follows AEC: undo its positive gain while the speaker
                    # is audible, so residual robot speech is not boosted again.
                    gain = max(0.0, float(self.processor.gain_db))
                    clean = (clean.astype(np.float32) * 10 ** (-gain / 20)).astype(np.int16)
                chunks.append(clean)
            self.pending = self.pending[count:]
            self.far_pending = self.far_pending[count:]
            ready = np.concatenate(chunks)
            result, self.output = ready[:len(samples)].copy(), ready[len(samples):]
            try:
                result = self.residual.process(result, echo_guard)
            except Exception:
                self.residual.enabled = False
                self.residual.metrics = {'state': 'disabled after error; WebRTC retained', 'correlation': 0.0}
                print('[AUDIO] Residual detector disabled after error; WebRTC continues.', flush=True)
            # Mute only for Ember's own voice: an audiobook must not deafen Ember for a whole chapter.
            muted = self.mute_during_reply and voice_guard
            withheld = False
            if not muted:
                for gate in list(input_gates):
                    try:
                        withheld = bool(gate(result))
                    except Exception:
                        input_gates.remove(gate)
                        print('[AUDIO] Mic gate disabled after error.', flush=True)
                    if withheld:
                        break
            if muted or withheld:
                # AEC above still adapts on this audio; Gemini only gets silence.
                result = np.zeros_like(result)
            self.metrics = {'state': 'WebRTC echo cancellation + noise suppression + gain control',
                            'raw': self.levels(samples), 'clean': self.levels(result),
                            'speech_probability': round(float(self.processor.speech_probability), 3),
                            'gain_db': round(min(0.0, float(self.processor.gain_db)) if echo_guard else float(self.processor.gain_db), 1),
                            'agc_gain_db': round(float(self.processor.gain_db), 1),
                            'echo_guard': echo_guard,
                            'mic_muted_during_reply': muted,
                            'mic_withheld_for_audiobook': withheld,
                            'fan_filter_hz': self.fan_filter.cutoff if self.fan_filter is not None else 0,
                            'residual_echo': dict(self.residual.metrics),
                            'aec_delay_ms': self.processor.stream_delay_ms,
                            'input_latency_ms': round(self.input_latency_ms, 1),
                            'output_latency_ms': round(self.output_latency_ms, 1),
                            'full_duplex': True}
            monitor.timing.microphone(self.metrics['speech_probability'],
                                      self.metrics['clean']['rms_dbfs'],
                                      len(samples) / self.rate, self.input_latency_ms / 1000)
            return result
        except Exception:
            # Preserve microphone capture on DSP failure; restore half-duplex to
            # avoid feeding speaker echo back into Gemini. Expose the failure.
            self.failed = True
            return self._fallback(samples)

    def _fallback(self, samples):
        if _active_enhancer is self:
            from .engage import engagement, UNAVAILABLE_NOTE
            engagement.engage('audio cleanup unavailable', UNAVAILABLE_NOTE)
        active = self.speaker_active()
        self.metrics = {'state': 'Audio cleanup failed; raw input / half-duplex fallback',
                        'full_duplex': False, 'echo_guard': active}
        # Playback may already have left the Python queue but still be audible.
        # Only failed DSP uses this mute; healthy AEC retains near-end speech.
        return np.zeros_like(samples) if active else samples


def create(rate=48000):
    global _active_enhancer
    _active_enhancer = InputEnhancer(rate, fan_filter_hz=fan_filter_hz()) if enabled() else None
    return _active_enhancer
