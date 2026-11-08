"""Opt-in desktop WebRTC audio cleanup, using Ember's existing playback reference."""
import copy
import os
import threading
import time

import numpy as np
import runtime_monitor as monitor
from .echo import ResidualEcho

_active_enhancer = None
_SILENCE_DBFS = round(float(20 * np.log10(1e-3 / 32768)), 1)  # levels() of digital silence


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


def comfort_noise():
    # While the mic is muted or withheld, send Gemini steady noise at the room's level
    # instead of digital zeros. Zeros between turns made Gemini 2.5 commit the next
    # question seconds late or not at all (Orange Pi 5 Max benchmark: median reply
    # 3.4 s, p90 about 9 s, 3 of 33 unanswered); see ComfortNoise. Off unless set:
    # run_mvp.py turns it on for the MVP.
    return os.getenv('BAYMAX_COMFORT_NOISE', '0') == '1'


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


class ComfortNoise:
    """What Gemini hears while the mic is muted or withheld, in place of digital zeros.

    Gemini's turn detection reads the mic stream as one continuous signal. Zeros while
    Ember speaks, then the room's floor again just before the person answers, made
    Gemini 2.5 commit that answer seconds late or not at all (Orange Pi 5 Max benchmark:
    p90 reply about 9 s, 3 of 33 questions unanswered). Noise
    at the level Gemini hears between turns keeps the stream steady, so the only onset
    left is the person's speech. None of it comes from the microphone.

    observe() learns the level from cleaned blocks that really go to Gemini while the
    speaker is silent and WebRTC hears no speech: it follows the floor down within about
    0.1 s and up over about 1 s, within -90..-40 dBFS (-70 until something is heard).
    fill() returns that level of 100-7000 Hz noise (white if scipy is missing), sliced
    from a table made once, so a block costs one slice and one multiply.
    """
    FLOOR_DBFS, CEILING_DBFS, START_DBFS = -90.0, -40.0, -70.0
    FALL, RISE = 0.2, 0.02  # per 20 ms block

    def __init__(self, rate=48000, seed=None, seconds=2.0):
        self.rng = np.random.default_rng(seed)
        noise = self.rng.standard_normal(int(rate * seconds))
        try:
            from scipy.signal import butter, sosfilt
            sos = butter(4, [100, min(7000, 0.45 * rate)], btype='bandpass', fs=rate, output='sos')
            noise = sosfilt(sos, noise)
        except Exception:
            pass  # white noise: still steady, only a few dB quieter after Gemini resamples
        self.table = (noise / (np.sqrt(np.mean(noise ** 2)) or 1.0)).astype(np.float32)
        self.level_dbfs = None
        self.pos = 0

    @property
    def level(self):
        return self.START_DBFS if self.level_dbfs is None else self.level_dbfs

    def observe(self, samples):
        values = samples.astype(np.float32)
        power = float(values @ values) / len(values) if len(values) else 0.0
        if power <= 0:
            return  # exact zeros: a dead device, not the room
        level = min(self.CEILING_DBFS, max(self.FLOOR_DBFS, 10 * np.log10(power) - 90.309))
        if self.level_dbfs is None:
            self.level_dbfs = level
        else:
            rate = self.FALL if level < self.level_dbfs else self.RISE
            self.level_dbfs += rate * (level - self.level_dbfs)

    def fill(self, count, restart=False):
        if restart:  # a new muted stretch starts somewhere else in the table
            self.pos = int(self.rng.integers(len(self.table)))
        out = np.empty(count, dtype=np.float32)
        done = 0
        while done < count:
            take = min(count - done, len(self.table) - self.pos)
            out[done:done + take] = self.table[self.pos:self.pos + take]
            done += take
            self.pos = (self.pos + take) % len(self.table)
        scale = 32768 * 10 ** (self.level / 20)
        return np.clip(np.round(out * scale), -32768, 32767).astype(np.int16)


class InputEnhancer:
    """DSP runs only on the capture thread; render callback only queues PCM.

    Process complete 10ms frames. v10's 960-sample (20 ms) blocks are two whole
    frames, cleaned with no added delay. A block that isn't whole frames (v8's 1024)
    starts a 10ms output carry once, retaining leftovers across callbacks, so no
    tail is padded or discarded on every callback.
    """
    def __init__(self, rate=48000, processor=None, ns_level=None, clock=time.monotonic, fan_filter_hz=0,
                 comfort=None):
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
        # Muted or withheld mic audio: zeros, or with BAYMAX_COMFORT_NOISE=1 room-level noise.
        self.comfort = comfort if comfort is not None else (ComfortNoise(rate) if comfort_noise() else None)
        self.standing_in = False  # the latest block Gemini got was the stand-in, not the mic
        try:
            self.max_lag_ms = float(os.getenv('BAYMAX_MAX_MIC_LAG_MS', '1000'))
        except ValueError:
            self.max_lag_ms = 1000.0
        self.dropped_ms = 0.0
        self.live_reported = None
        self.clock = clock
        self.rate = rate
        self.frame = rate // 100
        self.block_ms = 960 * 1000 / rate  # the latest mic block's length (see stale())
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
        # Cleaned audio carried to the next callback: none while blocks are whole frames.
        self.output = np.empty(0, dtype=np.int16)
        with self.lock:
            self.render_buffer.clear()
            self.speaker_until = float('-inf')
            self.voice_until = float('-inf')
        self.failed = False
        self.standing_in = False  # the comfort level is kept: the room is the same after a reconnect
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

    def stale(self, queue_wait_ms, chunk_ms=None):
        """True for mic audio that waited too long to send (e.g. queued during a
        network stall): sending it later replays old speech as a burst of turns.
        chunk_ms: its length; by default that of the blocks process() cleans."""
        chunk_ms = self.block_ms if chunk_ms is None else chunk_ms
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
        self.block_ms = len(samples) * 1000 / self.rate
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
            if len(samples) % self.frame and not len(self.output) and not len(self.pending):
                # Not whole 10 ms frames: from now on 10 ms of cleaned audio is carried
                # to the next callback, so every callback still returns a full block.
                self.output = np.zeros(self.frame, dtype=np.int16)
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
                # AEC above still adapts on this audio; Gemini only gets the stand-in
                # (zeros, or room-level comfort noise), never this audio.
                result = self._stand_in(len(result))
            else:
                self.standing_in = False
                if (self.comfort is not None and not echo_guard
                        and float(self.processor.speech_probability) < 0.5):
                    self.comfort.observe(result)  # the room as Gemini hears it between turns
            self.metrics = {'state': 'WebRTC echo cancellation + noise suppression + gain control',
                            'raw': self.levels(samples), 'clean': self.levels(result),
                            'speech_probability': round(float(self.processor.speech_probability), 3),
                            'gain_db': round(min(0.0, float(self.processor.gain_db)) if echo_guard else float(self.processor.gain_db), 1),
                            'agc_gain_db': round(float(self.processor.gain_db), 1),
                            'echo_guard': echo_guard,
                            'mic_muted_during_reply': muted,
                            'mic_withheld_for_audiobook': withheld,
                            'comfort_noise_dbfs': round(self.comfort.level, 1) if self.comfort is not None else None,
                            'fan_filter_hz': self.fan_filter.cutoff if self.fan_filter is not None else 0,
                            'residual_echo': dict(self.residual.metrics),
                            'aec_delay_ms': self.processor.stream_delay_ms,
                            'input_latency_ms': round(self.input_latency_ms, 1),
                            'output_latency_ms': round(self.output_latency_ms, 1),
                            'full_duplex': True}
        except Exception:
            # Preserve microphone capture on DSP failure; restore half-duplex to
            # avoid feeding speaker echo back into Gemini. Expose the failure.
            self.failed = True
            return self._fallback(samples)
        # Diagnostics, outside the cleanup's try: an error here turns conversation
        # timing off and never switches Ember to the half-duplex fallback above.
        try:
            if not monitor.timing.failed:
                # The stand-in is not the person: timing sees it as the silence it replaces.
                monitor.timing.microphone(self.metrics['speech_probability'],
                                          _SILENCE_DBFS if self.standing_in else self.metrics['clean']['rms_dbfs'],
                                          len(samples) / self.rate, self.input_latency_ms / 1000)
        except Exception:
            monitor.timing.failed = True
            print('[AUDIO] Conversation timing turned off after an error; audio continues.', flush=True)
        return result

    def _fallback(self, samples):
        if _active_enhancer is self:
            from .engage import engagement, UNAVAILABLE_NOTE
            engagement.engage('audio cleanup unavailable', UNAVAILABLE_NOTE)
        active = self.speaker_active()
        self.metrics = {'state': 'Audio cleanup failed; raw input / half-duplex fallback',
                        'full_duplex': False, 'echo_guard': active}
        # Playback may already have left the Python queue but still be audible.
        # Only failed DSP uses this mute; healthy AEC retains near-end speech.
        if active:
            return self._stand_in(len(samples))  # comfort noise keeps the last cleaned level
        self.standing_in = False
        return samples

    def _stand_in(self, count):
        """Gemini's audio while the mic is muted or withheld: zeros, or the comfort noise."""
        restart = not self.standing_in
        self.standing_in = True
        if self.comfort is None:
            return np.zeros(count, dtype=np.int16)
        return self.comfort.fill(count, restart)


def create(rate=48000):
    global _active_enhancer
    _active_enhancer = InputEnhancer(rate, fan_filter_hz=fan_filter_hz()) if enabled() else None
    return _active_enhancer
