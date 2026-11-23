"""Synthetic sound for the robot rig: the scripted "person", background noise and a calibration noise.

Everything here is generated: text-to-speech (live_eval.synthesize), pink noise, a
synthetic TV babble of neutral scripted lines and a synthetic music bed. No
recordings of real people are used.

Levels are in dB relative to the calibrated reference: 0 dB is a person talking at
conversational level 1 m from the robot (about 60 dB(A) at the robot's microphone),
once the rig's calibration gain is set (README "Calibration").
"""
import math

import numpy as np

from evals import live_eval

RATE = live_eval.RATE            # 48 kHz int16 mono, as the robot's microphone
REFERENCE_DBFS = -26.0           # RMS of level-0 audio before the calibration gain (leaves headroom)
CALIBRATION_SECONDS = 6.0

# Neutral, invented TV lines. Several are near-misses for the wake words
# ("embers", "members engaged", "engage") to probe false wakes.
TV_LINES = (
    "Good evening, and here is the weather for the weekend.",
    "Embers glowed in the fireplace as the snow kept falling.",
    "The members engaged in a long debate about the new bridge.",
    "Stop by the farmers market this Saturday for fresh apples.",
    "Coming up after the break, a recipe for lemon bread.",
    "Remember to engage the parking brake on a steep hill.",
    "The home team scored twice in the final minute.",
    "Pause for a moment and listen to the waves.",
)


def distance_db(meters, reference=1.0):
    """Free-field level change of a talker at `meters` compared with `reference` metres."""
    if meters <= 0:
        raise ValueError("distance must be positive")
    return -20.0 * math.log10(meters / reference)


def rms_dbfs(samples):
    samples = np.asarray(samples, dtype=np.float64)
    if not len(samples):
        return -120.0
    rms = math.sqrt(float(np.mean(samples ** 2)))
    return 20.0 * math.log10(max(rms, 1e-3) / 32768.0)


def at_level(samples, level_db=0.0, gain_db=0.0):
    """int16 copy of `samples` scaled to REFERENCE_DBFS + level + calibration gain, and whether it clipped."""
    samples = np.asarray(samples, dtype=np.float64)
    if not len(samples):
        return np.zeros(0, dtype=np.int16), False
    target = REFERENCE_DBFS + level_db + gain_db
    scaled = samples * (10 ** ((target - rms_dbfs(samples)) / 20.0))
    clipped = bool(np.any(np.abs(scaled) > 32767))
    return np.clip(scaled, -32768, 32767).astype(np.int16), clipped


def pink_noise(seconds, seed=0):
    """Pink (1/f) noise, the usual calibration and room-noise signal."""
    count = int(RATE * seconds)
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(count))
    frequencies = np.fft.rfftfreq(count, 1.0 / RATE)
    frequencies[0] = frequencies[1] if len(frequencies) > 1 else 1.0
    pink = np.fft.irfft(spectrum / np.sqrt(frequencies), n=count)
    return pink / (np.max(np.abs(pink)) or 1.0) * 16000


def music(seconds, seed=0):
    """A synthetic chord progression with soft attacks: a stand-in for music in the room."""
    count = int(RATE * seconds)
    t = np.arange(count) / RATE
    chords = ((261.6, 329.6, 392.0), (220.0, 261.6, 329.6), (174.6, 220.0, 261.6), (196.0, 246.9, 293.7))
    beat = 2.0
    out = np.zeros(count)
    for index, start in enumerate(np.arange(0, seconds, beat)):
        chord = chords[index % len(chords)]
        mask = (t >= start) & (t < start + beat)
        local = t[mask] - start
        envelope = np.minimum(local / 0.05, 1.0) * np.exp(-local * 1.2)
        out[mask] = sum(np.sin(2 * np.pi * f * t[mask]) for f in chord) * envelope
    return out + pink_noise(seconds, seed) * 0.02


def tv(seconds, speak=None, seed=0):
    """Synthetic TV: alternating voices reading TV_LINES over a low pink-noise bed."""
    speak = speak or live_eval.synthesize
    count = int(RATE * seconds)
    bed = pink_noise(seconds, seed) * 0.08
    rng = np.random.default_rng(seed)
    position, index = int(RATE * 0.5), 0
    voices = (None,)
    while position < count:
        line = speak(TV_LINES[index % len(TV_LINES)], voices[index % len(voices)]).astype(np.float64)
        if not len(line):
            break
        line = line / (np.max(np.abs(line)) or 1.0) * 12000
        end = min(count, position + len(line))
        bed[position:end] += line[:end - position]
        position = end + int(RATE * rng.uniform(0.4, 1.5))
        index += 1
    return bed


NOISES = {"pink": lambda seconds, speak: pink_noise(seconds),
          "music": lambda seconds, speak: music(seconds),
          "tv": lambda seconds, speak: tv(seconds, speak)}


def noise(kind, seconds, speak=None):
    if kind not in NOISES:
        raise ValueError(f"Unknown noise {kind!r}; choose from {sorted(NOISES)}")
    return NOISES[kind](seconds, speak)


def pad(samples, seconds=0.3):
    """Silence either side (after leveling, so it does not change the speech level)."""
    silence = np.zeros(int(RATE * seconds), dtype=np.int16)
    return np.concatenate((silence, np.asarray(samples, dtype=np.int16), silence))
