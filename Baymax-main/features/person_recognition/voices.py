"""Voice (speaker) recognition: who is speaking, from their voice.

Ported from PR #36 (voice_id.py + voice_live.py, sjylee) for the Orange Pi build. The
logic, thresholds, tools and messages are PR #36's; what changed:
- No PyTorch. The same SpeechBrain ECAPA-TDNN speaker model (speechbrain/
  spkrec-ecapa-voxceleb, 192-d embeddings) and Silero VAD run on ONNX Runtime, already
  installed for memory. convert_voice_model.py makes the ONNX files on a computer with
  PyTorch and checks them against SpeechBrain (cosine >= 0.9999), so PR #36's
  calibration (sure >= 0.40, maybe >= 0.25) still holds. Here only the STFT is numpy
  (power_spectrogram); the filterbank, log and mean normalisation are inside the model.
- It runs in its own process on the slow cores (voice_process.py), never in Ember's.

Models: Baymax-main/.models/voice/ (ECAPA_FILE, VAD_FILE), copied from the converter's
output; voice recognition stays off without them. Voiceprints are stored in the people
file (faces.PeopleStore) under VOICE_KEY, only with consent; forgetting a person removes
them.
"""
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.path.join(ROOT, ".models", "voice")
MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
ECAPA_FILE = "ecapa_voxceleb.onnx"
VAD_FILE = "silero_vad.onnx"
ONNX_THREADS = 2              # on the slow cores: leave the other two for the face child

SAMPLE_RATE = 16000           # what the model expects
EMBED_DIM = 192
VOICE_KEY = "voice_embeddings"
N_FFT, HOP = 400, 160         # SpeechBrain's Fbank: 25 ms window, 10 ms hop, centred, Hamming

# ─── Tuning (see calibration notes above) ───────────────────────────────────
SURE_THRESHOLD = 0.40         # confident: "Stella is speaking"
MAYBE_THRESHOLD = 0.25        # "might be Stella" — ask before assuming
MIN_SECONDS = 1.0             # shorter speech is too little to go on
MAX_ID_SECONDS = 5.0          # identify on at most this much of an utterance (speed)
ENROLL_MIN_SECONDS = 2.0      # enrollment clips must be at least this long
MAX_VOICE_SAMPLES = 20        # per person; the most redundant sample is dropped
# A new sample must resemble the person's existing voiceprint, so one wrong
# sample (someone else talking) can't creep in. Placeholder, not calibrated
# yet: set at the "maybe" threshold.
VOICE_CONSISTENCY = 0.25
MAX_NAME = 60


# ─── Audio ───────────────────────────────────────────────────────────────────
def to_model_audio(audio, sample_rate):
    """Any mic audio -> float32 mono at 16 kHz, values in [-1, 1].
    Accepts int16 or float arrays, mono (n,) or multi-channel (n, channels),
    e.g. Ember's 48 kHz int16 mic stream."""
    audio = np.asarray(audio)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    else:
        audio = audio.astype(np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)            # channels -> mono
    if sample_rate != SAMPLE_RATE:
        from scipy.signal import resample_poly
        g = int(np.gcd(int(sample_rate), SAMPLE_RATE))
        audio = resample_poly(audio, SAMPLE_RATE // g, int(sample_rate) // g).astype(np.float32)
    return audio

# ─── Model (ONNX Runtime) ────────────────────────────────────────────────────
_WINDOW = (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)


def power_spectrogram(audio):
    """16 kHz float audio -> [frames, 201] power spectrum, as SpeechBrain's STFT
    (torch.stft, center=True with zero padding, periodic Hamming window) and
    spectral_magnitude(power=1) compute it."""
    audio = np.asarray(audio, np.float32)
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2))
    frames = 1 + (len(padded) - N_FFT) // HOP
    index = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    spectrum = np.fft.rfft(padded[index] * _WINDOW, axis=1)
    return (spectrum.real ** 2 + spectrum.imag ** 2).astype(np.float32)


def _session(path, threads=ONNX_THREADS):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


def models_present(model_dir=None):
    model_dir = model_dir or MODEL_DIR
    return all(os.path.isfile(os.path.join(model_dir, name)) for name in (ECAPA_FILE, VAD_FILE))


class VoiceModel:
    """The ECAPA speaker encoder on ONNX Runtime (no PyTorch)."""

    def __init__(self, model_dir=None, num_threads=ONNX_THREADS):
        self._session = _session(os.path.join(model_dir or MODEL_DIR, ECAPA_FILE), num_threads)
        self._lock = threading.Lock()

    def embed(self, audio):
        """16 kHz float32 mono audio -> unit-length 192-d embedding."""
        power = power_spectrogram(audio)[None]
        with self._lock:
            emb = self._session.run(None, {"power": power})[0][0]
        return _normalize(emb)


# ─── Results ─────────────────────────────────────────────────────────────────
@dataclass
class VoiceMatch:
    level: str                 # "sure" | "maybe" | "unknown" | "too_short"
    person_id: str = None      # set for "sure" and "maybe"
    name: str = None
    score: float = 0.0         # best cosine similarity
    seconds: float = 0.0       # length of the audio judged
    embedding: np.ndarray = None   # kept so the caller can learn from it
    ms: float = 0.0            # time spent embedding

    def describe(self):
        """Short text for Gemini, e.g. '[SPEAKER] Stella (voice, sure)'."""
        if self.level == "sure":
            return f"[SPEAKER] {self.name} (voice, sure)"
        if self.level == "maybe":
            return f"[SPEAKER] might be {self.name} (voice, not sure)"
        return "[SPEAKER] unknown voice"


# ─── Identification & enrollment ─────────────────────────────────────────────
class VoiceIdentifier:
    """Who is speaking, from enrolled voiceprints in the people store.

    `store` is face_id.PeopleStore (or anything with people(), get(),
    find_by_name(), reload_if_changed() and _update(change))."""

    def __init__(self, model, store, clock=time.time):
        self.model = model
        self.store = store
        self._clock = clock

    # ── Queries ──────────────────────────────────────────────────────────────
    def voiceprints(self, candidates=None):
        """[(person_id, name, voiceprint)] for everyone with voice samples.
        `candidates`: optional person ids to limit the search to."""
        self.store.reload_if_changed()
        out = []
        for p in self.store.people():
            samples = p.get(VOICE_KEY) or []
            if not samples or (candidates is not None and p["id"] not in candidates):
                continue
            out.append((p["id"], p["name"], _normalize(np.mean(np.array(samples, np.float32), axis=0))))
        return out

    def has_voice(self, person_id):
        person = self.store.get(person_id)
        return bool(person and person.get(VOICE_KEY))

    # ── Identify ─────────────────────────────────────────────────────────────
    def identify(self, audio, sample_rate=SAMPLE_RATE, candidates=None):
        """Who is speaking in `audio` (one utterance) -> VoiceMatch."""
        audio = to_model_audio(audio, sample_rate)
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_SECONDS:
            return VoiceMatch("too_short", seconds=seconds)
        # Bounded work on the slow cores: PR #36's calibration used 3-4 s clips, so the
        # first MAX_ID_SECONDS of a long utterance are enough.
        audio = audio[:int(MAX_ID_SECONDS * SAMPLE_RATE)]
        t0 = time.perf_counter()
        emb = self.model.embed(audio)
        ms = (time.perf_counter() - t0) * 1000
        prints = self.voiceprints(candidates)
        if not prints:
            return VoiceMatch("unknown", seconds=seconds, embedding=emb, ms=ms)
        scores = np.stack([v for _, _, v in prints]) @ emb
        i = int(np.argmax(scores))
        score = float(scores[i])
        level = ("sure" if score >= SURE_THRESHOLD
                 else "maybe" if score >= MAYBE_THRESHOLD else "unknown")
        pid, name = (prints[i][0], prints[i][1]) if level != "unknown" else (None, None)
        return VoiceMatch(level, pid, name, score, seconds, emb, ms)

    # ── Enroll ───────────────────────────────────────────────────────────────
    def embed_clips(self, clips, sample_rate=SAMPLE_RATE):
        """Clips long enough to enroll -> embeddings (too-short ones skipped)."""
        out = []
        for clip in clips:
            audio = to_model_audio(clip, sample_rate)
            if len(audio) / SAMPLE_RATE >= ENROLL_MIN_SECONDS:
                out.append(self.model.embed(audio))
        return out

    def enroll(self, name, clips, sample_rate=SAMPLE_RATE, consent=False, relationship=None,
               allow_new=False):
        """Explicit enrollment ("Ember, learn my voice"): add voice samples to
        the person with this name. Only with consent.
        allow_new=False (default): the person must already be known (by face),
        because face_id.remember() can't add a face to a person who has no
        face samples yet. allow_new=True creates a voice-only person (testing).
        Returns {"status", "person_id", "added", "created"}."""
        if not consent:
            raise ValueError("Ask before remembering someone's voice.")
        name = clean_name(name)
        if not name:
            raise ValueError("A name is needed.")
        embeddings = self.embed_clips(clips, sample_rate)
        if not embeddings:
            return {"status": "not_saved",
                    "reason": f"No clip was at least {ENROLL_MIN_SECONDS:.0f} seconds long."}
        existing = self.store.find_by_name(name)
        if existing and existing.get(VOICE_KEY):
            # Adding to a known voice: the new clips must sound like them
            added = self.add_samples(existing["id"], embeddings)
            if not added:
                return {"status": "not_saved", "person_id": existing["id"],
                        "reason": f"This voice doesn't match the {name} I already know."}
            return {"status": "saved", "person_id": existing["id"], "added": added, "created": False}
        if len(embeddings) > 1 and not _consistent_set(embeddings):
            return {"status": "not_saved",
                    "reason": "The clips don't sound like one person. Record them again, "
                              "one person at a time, somewhere quiet."}
        if existing:     # known by face, first voice samples
            added = self.add_samples(existing["id"], embeddings, check_consistency=False)
            return {"status": "saved", "person_id": existing["id"], "added": added, "created": False}
        if not allow_new:
            return {"status": "not_saved",
                    "reason": f"I don't know {name}'s face yet. Learn their face first, "
                              "then their voice."}
        person_id = self._create_person(name, embeddings, relationship)
        return {"status": "saved", "person_id": person_id, "added": len(embeddings), "created": True}

    def add_samples(self, person_id, embeddings, check_consistency=True):
        """Add voice samples to an existing person (by id; never creates one,
        so a person deleted meanwhile stays deleted). With check_consistency,
        only samples that sound like their stored voice are kept. Also used for
        passive learning, e.g. when the face confirms who is talking.
        Returns how many were kept."""
        embeddings = [_normalize(e) for e in embeddings]
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if p["id"] == person_id), None)
            if person is None:
                return 0
            stored = [np.asarray(e, np.float32) for e in person.get(VOICE_KEY) or []]
            voiceprint = _normalize(np.mean(stored, axis=0)) if stored else None
            keep = [e for e in embeddings if not check_consistency or voiceprint is None
                    or float(voiceprint @ e) >= VOICE_CONSISTENCY]
            if keep:
                person[VOICE_KEY] = [[round(float(v), 5) for v in e]
                                     for e in _keep_diverse(stored + keep, MAX_VOICE_SAMPLES)]
                person["voice_consent"] = True
                person["voice_updated"] = now
            return len(keep)

        return self.store._update(change)

    def _create_person(self, name, embeddings, relationship=None):
        """A new person known only by voice so far (no face samples)."""
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if _name_key(p["name"]) == _name_key(name)),
                          None)
            if person is None:
                person = {"id": "p_" + uuid.uuid4().hex[:8], "name": name, "source": "voice",
                          "created": now, "last_seen": None, "embeddings": []}
                data["people"].append(person)
            if relationship:
                person["relationship"] = " ".join(str(relationship).split())[:60]
            person[VOICE_KEY] = [[round(float(v), 5) for v in e]
                                 for e in _keep_diverse(embeddings, MAX_VOICE_SAMPLES)]
            person["voice_consent"] = True
            person["voice_updated"] = now
            return person["id"]

        return self.store._update(change)

    # ── Forget ───────────────────────────────────────────────────────────────
    def forget_voice(self, person_id):
        """Remove only someone's voice (their face and memories stay).
        To forget a person entirely, use PeopleStore.remove()."""
        def change(data):
            for p in data["people"]:
                if p["id"] == person_id and VOICE_KEY in p:
                    for key in (VOICE_KEY, "voice_consent", "voice_updated"):
                        p.pop(key, None)
                    return True
            return False
        return self.store._update(change)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def clean_name(name):
    """Same as face_id.clean_name (repeated so this file imports on any OS)."""
    return " ".join(str(name or "").split())[:MAX_NAME]


def _name_key(name):
    return " ".join(str(name or "").lower().split())


def _normalize(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def _consistent_set(embeddings):
    """Every clip resembles the average of the others (one speaker)."""
    m = np.array(embeddings)
    for i in range(len(m)):
        others = _normalize(np.delete(m, i, axis=0).mean(axis=0))
        if float(others @ m[i]) < VOICE_CONSISTENCY:
            return False
    return True


def _keep_diverse(samples, limit):
    """Trim to `limit` samples like face_id._keep_diverse: first drop outliers
    (unlike all the others — likely a mistake), then the most redundant."""
    samples = [np.asarray(s, np.float32) for s in samples]
    while len(samples) > limit:
        m = np.array(samples) @ np.array(samples).T
        np.fill_diagonal(m, -1)
        best_match = m.max(axis=1)
        if best_match.min() < VOICE_CONSISTENCY:
            samples.pop(int(np.argmin(best_match)))
        else:
            samples.pop(int(np.argmax(best_match)))
    return samples


# ─── Live listening (PR #36 voice_live.py) ─────────────────────────────────
VAD_RATE = 16000
VAD_FRAME = 512               # Silero VAD takes 512-sample frames at 16 kHz (32 ms)
SPEECH_PROB = 0.5             # VAD probability that counts as speech
END_SILENCE_S = 0.5           # this much silence ends an utterance
PRE_ROLL_S = 0.3              # keep a little audio from just before speech started
EARLY_ID_S = 2.0              # identify early once this much speech is heard
MAX_UTTERANCE_S = 15.0        # cut very long utterances (identify them in pieces)
ECHO_TAIL_S = 0.4             # keep skipping briefly after Ember stops talking
QUEUE_CHUNKS = 2000           # ~40 s of 1024-sample chunks at 48 kHz
REPEAT_NOTE_S = 60.0          # re-send the same speaker's note at most this often
TOOL_WAIT_S = 1.5             # identify_speaker waits this long for an utterance to finish
RESULT_MAX_AGE_S = 15.0       # older results don't say who is talking *now*
LEARN_SPEECH_S = 8.0          # remember_voice: collect this much speech...
LEARN_PIECE_S = 4.0           # ...cut into pieces this long (one voice sample each)
LEARN_TIMEOUT_S = 30.0        # ...or stop after this long with whatever was heard,
LEARN_MIN_SPEECH_S = 4.0      # ...if it's at least this much speech


# ─── Gemini tool and instructions ───────────────────────────────────────────
VOICE_TOOLS = [
    {
        "name": "identify_speaker",
        "description": (
            "Find out who is talking right now, from their voice. Call this when "
            "someone asks who they are (e.g. \"who am I?\", \"do you know who this "
            "is?\") or when you need to know who just spoke and no recent [SPEAKER] "
            "message tells you."
        ),
    },
    {
        "name": "remember_voice",
        "description": (
            "Learn the voice of the person talking to you, so you recognize them by voice "
            "next time, even off camera. ONLY call this after they clearly said yes to you "
            "remembering their voice. After calling it, ask them to talk naturally for "
            "about 10 seconds."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING",
                         "description": "Their name, exactly as you know them"},
            },
            "required": ["name"],
        },
    },
]

VOICE_INSTRUCTIONS = (
    "# Recognizing voices\n"
    "You also recognize people you know by their voice, even when they're not in "
    "front of your camera. A message starting with \"[SPEAKER]\" tells you who is "
    "talking right now: \"[SPEAKER] Sarah (voice, sure)\" means it's Sarah; "
    "\"might be Sarah (voice, not sure)\" means check politely (\"Is that you, "
    "Sarah?\") before assuming; \"unknown voice\" means someone whose voice you "
    "don't know is talking. [SELF] updates say who is in view, which can be "
    "someone else: the [SPEAKER] message is about who is talking.\n"
    "If someone asks who they are, or you need to know who is talking and no "
    "recent [SPEAKER] message says, call identify_speaker. Never guess a name you "
    "aren't sure of — ask instead. Don't mention [SPEAKER] messages or scores.\n"
    "If someone asks you to learn or remember their voice, first ask whether it's "
    "okay to remember their voice. Only if they clearly say yes, call remember_voice "
    "with their name, then ask them to talk naturally for about 10 seconds, e.g. "
    "about their day. A message starting with \"[VOICE LEARNED]\" tells you whether "
    "it worked: tell them briefly. Voices you remember stay on this robot, and "
    "forget_person removes their voice too."
)


def remember_voice(listener, name, person, in_view, on_done):
    """The remember_voice tool. `person`: the stored person with exactly this
    name (or None); `in_view`: names of known people in front of the camera.
    A first voice sample is only taken when that person's face is in view, so
    nobody can save their voice under someone else's name; adding to an
    existing voice is protected by voice_id's "sounds like them" check."""
    if person is None:
        return {"status": "not_saved",
                "reason": f"You don't know {name}'s face yet. Learn their face first "
                          "(with their permission), then their voice."}
    has_voice = bool(person.get(VOICE_KEY))
    if not has_voice and person["name"].lower() not in {n.lower() for n in in_view}:
        return {"status": "not_saved",
                "reason": f"To check it's really {person['name']}, ask them to look at your "
                          "camera for a few seconds, then try again."}
    listener.start_learning(person["name"], on_done)
    return {"status": "listening",
            "note": "Now ask them to talk naturally for about 10 seconds, e.g. about their "
                    "day. Their voice is saved automatically; a [VOICE LEARNED] message "
                    "will tell you when it's done. Don't call remember_voice again."}


def learned_message(result):
    """The [VOICE LEARNED] message for Gemini once learning has finished."""
    name = result.get("name", "them")
    if result.get("status") == "saved":
        return (f"[VOICE LEARNED] You've learned {name}'s voice. Tell them briefly that "
                "you'll recognize their voice now.")
    return (f"[VOICE LEARNED] You couldn't learn {name}'s voice: {result.get('reason', '')} "
            "Tell them briefly, and offer to try again another time.")


class SpeakerNotes:
    """Decides which results are worth telling Gemini: a name or a 'might be'
    when the speaker changes (or every REPEAT_NOTE_S for the same speaker), and
    'unknown voice' only right after someone known was talking."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = None            # (level, person_id)
        self._last_t = -1e9

    def note_for(self, match, stage=None):
        if match.level not in ("sure", "maybe", "unknown"):
            return None                          # too short: nothing to say
        key = (match.level, match.person_id)
        if match.level == "unknown" and (self._last is None or self._last[0] == "unknown"):
            return None                          # nothing new to tell
        now = self._clock()
        if key == self._last and now - self._last_t < REPEAT_NOTE_S:
            return None
        self._last, self._last_t = key, now
        return match.describe()


# ─── Voice activity detection ───────────────────────────────────────────────
class OnnxSileroVAD:
    """Silero VAD (MIT) on ONNX Runtime, as silero_vad's own OnnxWrapper runs it:
    512-sample frames at 16 kHz with the previous 64 samples in front, plus its state."""
    CONTEXT = 64

    def __init__(self, model_dir=None):
        self._session = _session(os.path.join(model_dir or MODEL_DIR, VAD_FILE), threads=1)
        self.reset()

    def speech_prob(self, frame):
        frame = np.asarray(frame, np.float32).reshape(1, -1)
        x = np.concatenate([self._context, frame], axis=1)
        out, self._state = self._session.run(None, {"input": x, "state": self._state,
                                                    "sr": np.array(VAD_RATE, dtype=np.int64)})
        self._context = x[:, -self.CONTEXT:]
        return float(out.reshape(-1)[0])

    def reset(self):
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros((1, self.CONTEXT), np.float32)


class EnergyVAD:
    """Fallback: speech = clearly louder than the recent background noise."""

    def __init__(self, ratio=3.0, floor=0.003):
        self._ratio = ratio
        self._floor = floor
        self._noise = None

    def speech_prob(self, frame):
        rms = float(np.sqrt(np.mean(frame ** 2)))
        if self._noise is None:
            self._noise = rms
        loud = rms > max(self._floor, self._noise * self._ratio)
        if not loud:      # track the background level only during quiet frames
            self._noise = 0.95 * self._noise + 0.05 * rms
        return 1.0 if loud else 0.0

    def reset(self):
        pass


def make_vad(model_dir=None):
    try:
        return OnnxSileroVAD(model_dir)
    except Exception as e:
        print(f"[VOICE] Silero VAD unavailable ({e}); using a simple loudness detector.")
        return EnergyVAD()


def _to_vad_rate(audio, sample_rate):
    """Cheap per-chunk conversion to 16 kHz for the VAD only (identification
    uses the original audio, resampled properly)."""
    if sample_rate == VAD_RATE:
        return audio
    if sample_rate % VAD_RATE == 0:              # e.g. 48 kHz: average each 3 samples
        k = sample_rate // VAD_RATE
        n = len(audio) // k * k
        return audio[:n].reshape(-1, k).mean(axis=1)
    from scipy.signal import resample_poly
    g = int(np.gcd(int(sample_rate), VAD_RATE))
    return resample_poly(audio, VAD_RATE // g, int(sample_rate) // g).astype(np.float32)


# ─── Listener ────────────────────────────────────────────────────────────────
def print_result(match, stage):
    if match.level in ("sure", "maybe"):
        who = match.name if match.level == "sure" else f"might be {match.name}"
    else:
        who = match.level
    print(f"[VOICE] {who:<22} score={match.score:.2f}  {stage:<5} "
          f"{match.seconds:4.1f}s audio  {match.ms:4.0f} ms", flush=True)


class VoiceListener:
    """Who is speaking, live. Give it mono audio chunks with feed()."""

    def __init__(self, identifier, sample_rate, is_ember_speaking=None, on_result=None,
                 vad=None, clock=time.monotonic):
        self.identifier = identifier
        self.sample_rate = int(sample_rate)
        self._is_ember_speaking = is_ember_speaking or (lambda: False)
        self._on_result = on_result or print_result
        self._vad = vad
        self._clock = clock
        self._queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._latest = None          # (VoiceMatch, stage, when)
        self._results = 0
        self.dropped = 0
        self._reset_utterance()
        self._pre = deque()              # recent quiet audio, up to PRE_ROLL_S
        self._pre_samples = 0
        self._vad_buf = np.zeros(0, np.float32)
        self._ember_last = -1e9
        self._learning = None            # remember_voice in progress (see start_learning)

    # ── Called from the mic callback: must never block ───────────────────────
    def feed(self, chunk):
        """chunk: mono int16 bytes, or a mono numpy array (int16 or float).
        Whether Ember is talking is checked now, when the audio was captured,
        not later when the thread gets to it."""
        try:
            self._queue.put_nowait((chunk, bool(self._is_ember_speaking()), self._clock()))
        except queue.Full:
            self.dropped += 1            # identification falls behind; audio to Gemini is unaffected

    # ── Results ──────────────────────────────────────────────────────────────
    def latest(self, max_age=None):
        """The most recent (VoiceMatch, stage, when), or None (or too old)."""
        with self._lock:
            latest = self._latest
        if latest and max_age is not None and self._clock() - latest[2] > max_age:
            return None
        return latest

    def speaker_report(self, wait=TOOL_WAIT_S, max_age=RESULT_MAX_AGE_S):
        """For the identify_speaker tool: who is talking, as a dict for Gemini.
        If someone is mid-sentence (e.g. "...who am I?"), waits up to `wait`
        seconds for that sentence's result."""
        with self._cond:
            start = self._results
            if self._in_speech or self._queue.qsize() > 5:
                self._cond.wait_for(lambda: self._results > start and not self._in_speech,
                                    timeout=wait)
            latest = self._latest
        if not latest or self._clock() - latest[2] > max_age:
            return {"speaker": None,
                    "note": "You haven't heard enough of their voice yet. If you need to "
                            "know who it is, ask them."}
        m = latest[0]
        if m.level == "sure":
            return {"speaker": m.name, "confidence": "sure"}
        if m.level == "maybe":
            return {"speaker": m.name, "confidence": "not sure",
                    "note": f"It might be {m.name}. Check politely before assuming."}
        return {"speaker": None,
                "note": "You don't recognize this voice. If it matters, ask who they are."}

    # ── Learning a voice from live speech (remember_voice) ───────────────────
    def start_learning(self, name, on_done, allow_new=False, timeout=LEARN_TIMEOUT_S):
        """Collect the next ~LEARN_SPEECH_S of speech and enroll it as `name`'s
        voice. on_done(result) is called (on the voice thread) when finished:
        result is enroll()'s dict plus "name"."""
        with self._lock:
            self._learning = {"name": name, "on_done": on_done, "allow_new": allow_new,
                              "pieces": [], "speech_s": 0.0,
                              "deadline": self._clock() + timeout}

    def learning(self):
        with self._lock:
            return self._learning is not None

    def _collect_for_learning(self, audio):
        """Called with each finished utterance while learning."""
        piece = int(LEARN_PIECE_S * self.sample_rate)
        shortest = ENROLL_MIN_SECONDS * self.sample_rate
        for start in range(0, len(audio), piece):
            part = audio[start:start + piece]
            if len(part) >= shortest:
                self._learning["pieces"].append(part)
        self._learning["speech_s"] += self._speech_s
        if self._learning["speech_s"] >= LEARN_SPEECH_S and len(self._learning["pieces"]) >= 2:
            self._finish_learning()

    def _finish_learning(self):
        with self._lock:
            job, self._learning = self._learning, None
        if not job["pieces"] or job["speech_s"] < LEARN_MIN_SPEECH_S:
            result = {"status": "not_saved",
                      "reason": "I didn't hear enough of their voice. Each answer needs to "
                                "be a few seconds long."}
        else:
            try:
                result = self.identifier.enroll(job["name"], job["pieces"], self.sample_rate,
                                                consent=True, allow_new=job["allow_new"])
            except Exception as e:
                result = {"status": "not_saved", "reason": str(e)}
        result["name"] = job["name"]
        print(f"[VOICE] Learning {job['name']}'s voice: {result['status']} "
              f"({len(job['pieces'])} pieces)", flush=True)
        try:
            job["on_done"](result)
        except Exception as e:
            print(f"[VOICE] Error reporting learned voice: {e}", flush=True)

    # ── Thread ───────────────────────────────────────────────────────────────
    def start(self):
        if self._vad is None:
            self._vad = make_vad()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice-id", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process(*chunk)
            except Exception as e:          # one bad chunk mustn't stop recognition
                print(f"[VOICE] Error: {e}", flush=True)
                self._reset_utterance()

    # ── Core logic (also called directly by tests) ───────────────────────────
    def _reset_utterance(self):
        self._utt = []                   # audio chunks at the source rate
        self._utt_samples = 0
        self._speech_s = 0.0             # seconds of frames judged as speech
        self._silence_s = 0.0            # silence since the last speech frame
        self._in_speech = False
        self._early_done = False

    def process(self, chunk, ember_speaking=None, captured=None):
        audio = (np.frombuffer(chunk, np.int16) if isinstance(chunk, (bytes, bytearray))
                 else np.asarray(chunk))
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        audio = audio.astype(np.float32).ravel()

        if self._learning is not None and self._clock() > self._learning["deadline"]:
            self._finish_learning()              # time's up: use whatever was heard
        now = self._clock() if captured is None else captured
        if ember_speaking is None:
            ember_speaking = self._is_ember_speaking()
        if ember_speaking:
            self._ember_last = now
        if now - self._ember_last < ECHO_TAIL_S:
            # Ember is talking (or just stopped): ignore, and drop any half utterance
            if self._in_speech:
                self._finish(identify=False)
            self._clear_pre()
            self._vad_buf = np.zeros(0, np.float32)
            return

        # VAD on 512-sample frames at 16 kHz
        self._vad_buf = np.concatenate([self._vad_buf, _to_vad_rate(audio, self.sample_rate)])
        frames = len(self._vad_buf) // VAD_FRAME
        speech_frames = 0
        for i in range(frames):
            frame = self._vad_buf[i * VAD_FRAME:(i + 1) * VAD_FRAME]
            speech_frames += self._vad.speech_prob(frame) >= SPEECH_PROB
        self._vad_buf = self._vad_buf[frames * VAD_FRAME:]
        frame_s = VAD_FRAME / VAD_RATE
        chunk_has_speech = speech_frames > 0

        if not self._in_speech:
            if chunk_has_speech:
                self._in_speech = True
                for pre in self._pre:            # include the start of the first word
                    self._add(pre)
                self._clear_pre()
            else:
                self._pre.append(audio)
                self._pre_samples += len(audio)
                while self._pre and self._pre_samples - len(self._pre[0]) >= PRE_ROLL_S * self.sample_rate:
                    self._pre_samples -= len(self._pre.popleft())
                return
        self._add(audio)

        if chunk_has_speech:
            self._speech_s += speech_frames * frame_s
            self._silence_s = 0.0
        else:
            self._silence_s += len(audio) / self.sample_rate

        if not self._early_done and self._speech_s >= EARLY_ID_S:
            self._early_done = True
            self._identify("early")
        if self._silence_s >= END_SILENCE_S:
            self._finish()
        elif self._utt_samples / self.sample_rate >= MAX_UTTERANCE_S:
            self._finish()

    def _clear_pre(self):
        self._pre.clear()
        self._pre_samples = 0

    def _add(self, audio):
        self._utt.append(audio)
        self._utt_samples += len(audio)

    def _finish(self, identify=True):
        if identify and self._speech_s >= MIN_SECONDS:
            self._identify("final")
            if self._learning is not None:
                self._collect_for_learning(np.concatenate(self._utt))
        with self._cond:
            self._reset_utterance()
            self._cond.notify_all()
        self._vad.reset()

    def _identify(self, stage):
        audio = np.concatenate(self._utt) if self._utt else np.zeros(0, np.float32)
        match = self.identifier.identify(audio, self.sample_rate)
        with self._cond:
            self._latest = (match, stage, self._clock())
            self._results += 1
            self._cond.notify_all()
        self._on_result(match, stage)
