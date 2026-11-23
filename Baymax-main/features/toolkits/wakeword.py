"""Offline wake words while an audiobook plays (Vosk small English model).

During a book the mic is withheld from Gemini, which cannot tell the narrator
from the person. This listener hears only a few phrases in the echo-cancelled
mic and calls `on_wake(phrase)`.

Rules come from a test on 6 minutes of LibriVox narration: the tiny vocabulary
makes Vosk guess "ember" constantly in partial results and even return a final
"ember" at confidence 1.0, while real phrases gave every word 1.0 and narration
never produced a confident pair; its rare "stop"/"pause" hits sat mid-sentence
at narration loudness. So only final results count, "ember" alone never wakes,
and a wake is a confident pair ("hey ember", "okay ember", "ember stop", "ember
pause") or a confident "stop"/"pause" that either ends a short utterance
("stop", "please stop") or is clearly louder than the narration around it
(measured: a person's "stop" ~28 dB above the leaking narration).

On ember-002 (2026-10-22) that missed most real attempts: 6 wakes in 40
utterances, so people had to shout up close. Pairs now count in either order
("ember hey") and with "stop"/"pause" after "hey"/"okay", at confidence 0.8
("stop(0.88) hey(0.88)" was rejected at 0.9), and across two final results
up to PAIR_GAP_S apart, because Vosk often ended the utterance between "hey"
and "ember" (hey(1.00), then ember(0.85) two seconds later). Lone words keep
the original rules.

    python features/toolkits/wakeword.py --download   # fetch the model once
"""
import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import urllib.request
import zipfile

import numpy as np

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_DIR = Path(__file__).resolve().parents[2] / "models"
RATE = 16000
VOCABULARY = ["hey", "okay", "ember", "stop", "pause"]
PAIRS = {frozenset(pair) for pair in (("hey", "ember"), ("okay", "ember"), ("ember", "stop"),
                                      ("ember", "pause"), ("hey", "stop"), ("hey", "pause"),
                                      ("okay", "stop"), ("okay", "pause"))}  # Either order.
SINGLES = {"stop", "pause"}
MIN_CONF = 0.9         # A lone stop/pause.
HINT_CONF = 0.4        # Any wake word this sure lowers the book briefly (see on_hint).
PAIR_CONF = 0.8        # Each word of a pair.
PAIR_GAP_S = 3.0       # A pair may straddle two final results this close together.
LOUDER_DB = 12.0       # A lone stop/pause must beat the surrounding narration by this much.
HISTORY_SECONDS = 30   # Audio kept to measure word loudness.


def model_path():
    return MODEL_DIR / MODEL_NAME


def download_model():
    """Download and unpack the ~41 MB model once; returns its folder."""
    target = model_path()
    if target.is_dir():
        return target
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[WAKE] Downloading {MODEL_NAME} (~41 MB)...", flush=True)
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(MODEL_DIR)
    return target


_model = {"lock": threading.Lock(), "model": None}


def _vosk_model():
    """Load the model once; book and engage listeners share it."""
    import vosk
    with _model["lock"]:
        if _model["model"] is None:
            vosk.SetLogLevel(-1)
            _model["model"] = vosk.Model(str(download_model()))
        return _model["model"]


def _vosk_recognizer(vocabulary=VOCABULARY):
    import vosk
    recognizer = vosk.KaldiRecognizer(_vosk_model(), RATE, json.dumps(list(vocabulary) + ["[unk]"]))
    recognizer.SetWords(True)
    return recognizer


def _pair(first, second):
    words = (first.get("word"), second.get("word"))
    if frozenset(words) in PAIRS and len(set(words)) == 2 \
            and min(first.get("conf", 0), second.get("conf", 0)) >= PAIR_CONF:
        return " ".join(words)
    return None


def heard(result, final, level=None, previous=None):
    """Wake phrase in a final Vosk result dict (with word confidences), or None.

    level(start_s, end_s) -> dB of that stretch of audio, when available.
    previous: the last final result, so "hey" | "ember" split by Vosk still pairs."""
    if not final:
        return None
    entries = result.get("result", [])
    words = [(w.get("word"), w.get("conf", 0)) for w in entries]
    for first, second in zip(entries, entries[1:]):
        phrase = _pair(first, second)
        if phrase:
            return phrase
    # Across the split: the last word Vosk knew before, the first one after.
    before = [w for w in (previous or {}).get("result", []) if w.get("word") != "[unk]"]
    after = [w for w in entries if w.get("word") != "[unk]"]
    if before and after and "start" in after[0] and "end" in before[-1] \
            and 0 <= after[0]["start"] - before[-1]["end"] <= PAIR_GAP_S:
        phrase = _pair(before[-1], after[0])
        if phrase:
            return phrase
    # A real "stop"/"pause" ends a short utterance ("stop", "please stop"); narration
    # hits sat mid-sentence ("[unk] [unk] pause [unk]").
    if words and len(words) <= 2:
        word, conf = words[-1]
        if word in SINGLES and conf >= MIN_CONF:
            return word
    # Spoken over the book: accept it if it is much louder than the narration around it.
    if level is not None:
        narration = [level(w["start"], w["end"]) for w in entries if w.get("word") == "[unk]"]
        if narration:
            background = float(np.median(narration))
            for w in entries:
                if (w.get("word") in SINGLES and w.get("conf", 0) >= MIN_CONF
                        and level(w["start"], w["end"]) >= background + LOUDER_DB):
                    return w["word"]
    return None


class WakeListener:
    def __init__(self, on_wake, recognizer_factory=_vosk_recognizer, input_rate=48000, on_hint=None,
                 rules=None, listening="Listening for 'Hey Ember', 'Ember stop/pause', 'stop' and 'pause' during audiobooks.",
                 unavailable="the mic stays open during books"):
        self.on_wake, self.recognizer_factory, self.input_rate = on_wake, recognizer_factory, input_rate
        # on_hint(): a wake word was heard without a full phrase, maybe the start of one;
        # the player lowers the book so the rest is heard over it.
        self.on_hint = on_hint
        self.rules, self.listening, self.unavailable = rules, listening, unavailable
        self.queue = queue.Queue(maxsize=100)
        self.ready = False
        self.failed = False
        self.thread = None
        self.fir = None          # Anti-alias filter, built on the listener thread.
        self.fir_state = None
        self.phase = 0           # Keeps every 3rd sample aligned across blocks.
        self.history = np.zeros(RATE * HISTORY_SECONDS, dtype=np.float32)
        self.fed16 = 0           # 16 kHz samples given to the recognizer (its time base).

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name="ember-wake-word", daemon=True)
            self.thread.start()

    def feed(self, samples):
        """Called from the capture thread with echo-cancelled int16 mic audio; never blocks."""
        if self.ready:
            try:
                self.queue.put_nowait(np.asarray(samples, dtype=np.int16).copy())
            except queue.Full:
                pass

    def clear(self):
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def _to_16k(self, samples):
        """48 kHz -> 16 kHz with filter state carried across blocks (no edge clicks)."""
        from scipy.signal import firwin, lfilter
        factor = self.input_rate // RATE
        if self.fir is None:
            self.fir = firwin(63, 0.45 * RATE, fs=self.input_rate)
            self.fir_state = np.zeros(len(self.fir) - 1)
        filtered, self.fir_state = lfilter(self.fir, 1.0, samples.astype(np.float64), zi=self.fir_state)
        out = filtered[(-self.phase) % factor::factor]
        self.phase = (self.phase + len(samples)) % factor
        out = np.clip(out, -32768, 32767)
        self.history = np.concatenate((self.history[len(out):], out.astype(np.float32)))[-len(self.history):]
        self.fed16 += len(out)
        return out.astype(np.int16).tobytes()

    def level(self, start, end):
        """dB of recognizer-time [start, end) seconds, from the recent history."""
        first = len(self.history) - (self.fed16 - int(start * RATE))
        last = len(self.history) - (self.fed16 - int(end * RATE))
        segment = self.history[max(0, first):max(0, last)]
        if not len(segment):
            return 0.0
        return 20 * float(np.log10(np.sqrt(np.mean(segment.astype(np.float64) ** 2)) + 1))

    def _log(self, result, phrase):
        """Robot tuning aid: print utterances containing a wake word (only those words, with
        confidence and loudness), so missed stops can be diagnosed. BAYMAX_WAKE_LOG=0 hides it."""
        entries = result.get("result", [])
        if os.getenv("BAYMAX_WAKE_LOG", "1") == "0" or not any(w.get("word") != "[unk]" for w in entries):
            return
        words = " ".join(f"{w['word']}({w.get('conf', 0):.2f},{self.level(w['start'], w['end']):.0f}dB)"
                         if w.get("word") != "[unk]" else "_" for w in entries)
        narration = [self.level(w["start"], w["end"]) for w in entries if w.get("word") == "[unk]"]
        background = f"{np.median(narration):.0f}dB" if narration else "none"
        print(f"[WAKE] heard {words} | narration {background} -> {phrase or 'no wake'}", flush=True)

    def run(self):
        try:
            self._run()
        except Exception as error:
            self.failed = True
            self.ready = False
            print(f"[WAKE] Wake listener failed ({error}); {self.unavailable}.", flush=True)

    def _run(self):
        try:
            recognizer = self.recognizer_factory()
        except Exception as error:
            self.failed = True
            print(f"[WAKE] Wake words unavailable ({error}); {self.unavailable}.", flush=True)
            return
        self.ready = True
        print(f"[WAKE] {self.listening}", flush=True)
        previous = None
        while True:
            pcm = self._to_16k(self.queue.get())
            if not pcm:
                continue
            if not recognizer.AcceptWaveform(pcm):
                continue  # Partial results are unreliable here; wait for the utterance to end.
            # Vosk word times count all audio fed so far, so never Reset (it would
            # break the mapping to `history`); the book pausing stops the feed anyway.
            result = json.loads(recognizer.Result())
            phrase = (heard(result, True, self.level, previous) if self.rules is None
                      else self.rules(result, True, self.level, previous))
            previous = None if phrase else result
            if not phrase and self.on_hint is not None and any(
                    w.get("word") != "[unk]" and w.get("conf", 0) >= HINT_CONF for w in result.get("result", [])):
                try:
                    self.on_hint()
                except Exception:
                    pass  # Ducking is a convenience; wake words must keep running.
            try:
                self._log(result, phrase)
            except Exception:
                pass  # Logging must never stop wake words.
            if phrase:
                self.clear()
                try:
                    self.on_wake(phrase)
                except Exception as error:
                    print(f"[WAKE] Wake handler failed: {error}", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--download"]:
        print(f"[WAKE] Model ready at {download_model()}")
