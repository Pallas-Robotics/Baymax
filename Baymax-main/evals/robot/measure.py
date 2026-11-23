"""Outside measurement: what a reference microphone near the robot actually heard.

The reference microphone records the room during a scenario: the rig's person
speaker and Ember's own speaker. Both are in the same recording, so response latency
needs no clock alignment:

1. Find the played person-utterance in the recording by normalized cross-correlation
   with the exact samples the rig played (searched around the rig clock's estimate).
2. Its end is the last audible sample of that clip (within 40 dB of its peak) at the
   located offset.
3. Ember's onset is the first 10 ms frame after that end where the level rises
   `rise_db` above the room's noise floor (measured just before the utterance) and
   stays there for `hold_ms`, after the utterance's reverberant tail has first fallen
   back. It is refined to 1 ms frames.

Ember must start after the utterance's tail has decayed (about 0.2-0.3 s in a
furnished room); real replies take longer than that, so this is not a limit in
practice, but an overlapping reply is reported as "no speech".

Resolution is about 10 ms (tests recover a known delay within ±20 ms with noise and
reverberation). It measures the first sound from Ember's speaker, which is what a
person waits for; the runtime's "speaking" state is 0.2-0.4 s coarser.

Only numbers leave this module. The recording itself is deleted by the rig after the
analysis (hardware.py recorders, rig.py), unless the operator explicitly keeps it
outside the repository.

`transcribe` is a hook for an optional local speech-to-text command (none is
configured by default and none is bundled).
"""
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np

ANALYSIS_RATE = 16000
FRAME_S = 0.01
RISE_DB = 12.0
HOLD_MS = 60.0
MIN_SCORE = 12.0         # GCC-PHAT peak prominence to trust "the utterance is here".
MIN_MATCH = 0.2          # Plain normalized correlation with the played samples at that lag.
MIN_SCORE_OVERLAP = 10.0  # A barge-in overlaps Ember's speech, so its match is weaker.


def to_rate(samples, rate, target=ANALYSIS_RATE):
    """float64 at `target` Hz (block average when the ratio is an integer, else interpolation)."""
    samples = np.asarray(samples, dtype=np.float64)
    if rate == target or not len(samples):
        return samples
    if rate % target == 0:
        step = rate // target
        count = len(samples) // step
        return samples[:count * step].reshape(count, step).mean(axis=1)
    count = int(round(len(samples) * target / rate))
    return np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples)


def envelope_db(samples, rate, frame_s=FRAME_S):
    """RMS level in dBFS of consecutive frames."""
    size = max(1, int(round(rate * frame_s)))
    count = len(samples) // size
    if count == 0:
        return np.zeros(0)
    frames = np.asarray(samples[:count * size], dtype=np.float64).reshape(count, size)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-3) / 32768.0)


def speech_bounds(samples, rate, below_peak_db=40.0):
    """(start, end) seconds of the audible part of a clip: first/last sample within
    `below_peak_db` of its peak. This defines "the end of the person's utterance"."""
    samples = np.abs(np.asarray(samples, dtype=np.float64))
    if not len(samples) or samples.max() <= 0:
        return 0.0, 0.0
    loud = np.nonzero(samples >= samples.max() * 10 ** (-below_peak_db / 20))[0]
    return loud[0] / rate, (loud[-1] + 1) / rate


def locate(reference, ref_rate, recording, rec_rate, hint_s=None, window_s=2.0):
    """(offset seconds of `reference` in `recording`, peak prominence score).

    The lag is whichever of the five strongest GCC-PHAT peaks best matches the played
    samples; the score is that peak divided by the standard
    deviation of the whole GCC-PHAT curve (0 when the plain correlation with the played
    samples at that lag is under MIN_MATCH), used to decide whether the clip is present."""
    ref = to_rate(reference, ref_rate)
    rec = to_rate(recording, rec_rate)
    base = 0
    if hint_s is not None:
        base = max(0, int((hint_s - window_s) * ANALYSIS_RATE))
        rec = rec[base:int((hint_s + window_s) * ANALYSIS_RATE) + len(ref)]
    if len(ref) == 0 or len(rec) < len(ref):
        return None, 0.0
    ref = ref - ref.mean()
    size = 1 << int(np.ceil(np.log2(len(rec) + len(ref))))
    cross = np.fft.rfft(rec - rec.mean(), size) * np.conj(np.fft.rfft(ref, size))
    # GCC-PHAT picks the lag: whitening keeps the direct sound's peak sharp in a reverberant room.
    phat = np.fft.irfft(cross / (np.abs(cross) + 1e-9), size)[:len(rec) - len(ref) + 1]
    spread = float(np.std(phat)) or 1e-9
    # Another voice can make a PHAT peak, but it does not also correlate with the exact played
    # samples: of the strongest few peaks (5 ms apart), take the one that matches best.
    candidates, order = [], np.argsort(phat)[::-1]
    for lag in order[:2000]:
        if all(abs(lag - other) > 0.005 * ANALYSIS_RATE for other in candidates):
            candidates.append(int(lag))
        if len(candidates) == 5:
            break

    def match(lag):
        segment = rec[lag:lag + len(ref)] - rec[lag:lag + len(ref)].mean()
        return float(np.dot(segment, ref) / (np.linalg.norm(segment) * np.linalg.norm(ref) + 1e-9))
    best = max(candidates, key=match)
    return (base + best) / ANALYSIS_RATE, float(phat[best] / spread) if match(best) >= MIN_MATCH else 0.0


def noise_floor(env, start_s, end_s, frame_s=FRAME_S):
    part = env[max(0, int(start_s / frame_s)):max(0, int(end_s / frame_s))]
    if len(part) < 5:
        part = env
    return float(np.percentile(part, 20)) if len(part) else -90.0


def onset_after(recording, rate, start_s, end_s=None, floor_db=None, rise_db=RISE_DB, hold_ms=HOLD_MS,
                quiet_first=True):
    """Seconds of the first sustained rise above the floor after `start_s`, or None."""
    rec = to_rate(recording, rate)
    env = envelope_db(rec, ANALYSIS_RATE)
    if floor_db is None:
        floor_db = noise_floor(env, 0, len(env) * FRAME_S)
    threshold = floor_db + rise_db
    first = int(np.ceil(start_s / FRAME_S))
    last = len(env) if end_s is None else min(len(env), int(end_s / FRAME_S))
    hold = max(1, int(round(hold_ms / 1000 / FRAME_S)))
    i = first
    if quiet_first:  # Skip the utterance's own tail: wait until the level has fallen back first.
        while i < last and env[i] >= floor_db + rise_db / 2:
            i += 1
    while i + hold <= last:
        if np.all(env[i:i + hold] > threshold):
            return refine(rec, i * FRAME_S, threshold)
        i += 1
    return None


def refine(rec, coarse_s, threshold_db, frame_s=0.001):
    """Move a 10 ms onset to the first 1 ms frame above the threshold in the 10 ms before it."""
    start = max(0, int((coarse_s - FRAME_S) * ANALYSIS_RATE))
    env = envelope_db(rec[start:int((coarse_s + FRAME_S) * ANALYSIS_RATE)], ANALYSIS_RATE, frame_s)
    above = np.nonzero(env > threshold_db)[0]
    return start / ANALYSIS_RATE + (above[0] * frame_s if len(above) else FRAME_S)


def response_latency(recording, rec_rate, utterance, utt_rate, hint_s=None, search_s=30.0, floor_window_s=0.5,
                     min_score=MIN_SCORE, until_s=None):
    """End of the played utterance → first sound from Ember, measured in one recording.
    `until_s` (recording seconds) ends the search, e.g. where the rig's next line starts,
    so a silent Ember is never mistaken for the next utterance."""
    offset, score = locate(utterance, utt_rate, recording, rec_rate, hint_s)
    result = {"score": round(score, 3)}
    if offset is None or score < min_score:
        result["error"] = "the played utterance was not found in the reference recording"
        return result
    first, last = speech_bounds(utterance, utt_rate)
    start, end = offset + first, offset + last
    env = envelope_db(to_rate(recording, rec_rate), ANALYSIS_RATE)
    floor = noise_floor(env, start - floor_window_s, start)
    limit = end + search_s if until_s is None else min(end + search_s, until_s)
    onset = onset_after(recording, rec_rate, end, limit, floor)
    result.update({"utterance_start_s": round(start, 3), "utterance_end_s": round(end, 3),
                   "floor_dbfs": round(floor, 1)})
    if onset is None:
        result["error"] = "no speech from Ember after the utterance in the reference recording"
        return result
    result.update({"onset_s": round(onset, 3), "latency_s": round(onset - end, 3)})
    return result


def speech_after(recording, rec_rate, from_s, search_s=30.0, floor_window_s=1.0, until_s=None):
    """First sustained sound after `from_s` (e.g. a video's fall moment) and its delay."""
    env = envelope_db(to_rate(recording, rec_rate), ANALYSIS_RATE)
    floor = noise_floor(env, from_s - floor_window_s, from_s)
    limit = from_s + search_s if until_s is None else min(from_s + search_s, until_s)
    onset = onset_after(recording, rec_rate, from_s, limit, floor, quiet_first=False)
    if onset is None:
        return {"error": "no speech from Ember in the reference recording", "floor_dbfs": round(floor, 1)}
    return {"onset_s": round(onset, 3), "latency_s": round(onset - from_s, 3), "floor_dbfs": round(floor, 1)}


def barge_offset(recording, rec_rate, barge, barge_rate, hint_s, ember_from_s):
    """How long Ember had been speaking when the barge-in utterance started (seconds)."""
    offset, score = locate(barge, barge_rate, recording, rec_rate, hint_s, window_s=1.0)
    if offset is None or score < MIN_SCORE_OVERLAP:
        return {"error": "the barge-in utterance was not found in the reference recording", "score": round(score, 3)}
    barge_start = offset + speech_bounds(barge, barge_rate)[0]
    env = envelope_db(to_rate(recording, rec_rate), ANALYSIS_RATE)
    # The room's quietest moments: just before Ember's reply is usually the person's own line.
    floor = float(np.percentile(env, 10)) if len(env) else -90.0
    onset = onset_after(recording, rec_rate, ember_from_s, barge_start, floor)
    if onset is None:
        return {"error": "Ember was not speaking before the barge-in", "barge_start_s": round(barge_start, 3)}
    return {"ember_onset_s": round(onset, 3), "barge_start_s": round(barge_start, 3),
            "offset_s": round(barge_start - onset, 3), "score": round(score, 3)}


def summary(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, int(round(q * (len(values) - 1))))]  # noqa: E731
    return {"n": len(values), "median_s": round(pick(0.5), 3), "p90_s": round(pick(0.9), 3),
            "min_s": round(values[0], 3), "max_s": round(values[-1], 3)}


def transcribe(samples, rate, command, runner=subprocess.run, timeout=120):
    """Hook: run a configured local speech-to-text command on the recording (argv with
    "{wav}"; the text is read from stdout). Nothing is configured by default."""
    if not command:
        return None
    with tempfile.TemporaryDirectory(prefix="ember-rig-stt-") as folder:
        path = Path(folder) / "reference.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(np.asarray(samples, dtype=np.int16).tobytes())
        done = runner([str(path) if part == "{wav}" else part for part in command], capture_output=True,
                      text=True, timeout=timeout, check=True)
        return done.stdout.strip()
