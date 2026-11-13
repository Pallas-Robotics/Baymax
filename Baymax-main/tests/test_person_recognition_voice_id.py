"""Tests for voice_id.py. No model download needed: a fake model turns each
"speaker" into a fixed direction plus a little noise.

Run:  python -m pytest test_voice_id.py      (or: python test_voice_id.py)
"""
import json
import os
import tempfile

import numpy as np

from features.person_recognition import voices as voice_id
from features.person_recognition.voices import VoiceIdentifier, VoiceMatch, SAMPLE_RATE, VOICE_KEY

DIM = voice_id.EMBED_DIM


class FakeModel:
    """The first sample of the audio is the speaker number; the embedding is
    that speaker's fixed direction plus a little noise."""

    def __init__(self, noise=0.3):
        self.noise = noise
        self.rng = np.random.default_rng(0)
        self.dirs = np.random.default_rng(42).normal(size=(10, DIM)).astype(np.float32)

    def embed(self, audio):
        speaker = int(round(audio[0] * 10))
        v = self.dirs[speaker] / np.linalg.norm(self.dirs[speaker])
        v = v + self.rng.normal(size=DIM).astype(np.float32) * self.noise / np.sqrt(DIM)
        return v / np.linalg.norm(v)


class MemoryStore:
    """In-memory stand-in for face_id.PeopleStore (same methods voice_id uses)."""

    def __init__(self, people=None):
        self.data = {"version": 1, "people": people or []}

    def reload_if_changed(self, force=False):
        return False

    def people(self):
        return [dict(p) for p in self.data["people"]]

    def get(self, pid):
        return next((dict(p) for p in self.data["people"] if p["id"] == pid), None)

    def find_by_name(self, name):
        return next((dict(p) for p in self.data["people"]
                     if p["name"].lower() == name.lower()), None)

    def _update(self, change):
        data = json.loads(json.dumps(self.data))   # like re-reading the file
        result = change(data)
        self.data = data
        return result


def voice(speaker, seconds=3.0):
    """Fake audio for speaker n (16 kHz float32)."""
    a = np.zeros(int(seconds * SAMPLE_RATE), np.float32)
    a[0] = speaker / 10
    return a


def make(people=None):
    return VoiceIdentifier(FakeModel(), MemoryStore(people))


def test_enroll_and_identify():
    ids = make()
    ids.enroll("Stella", [voice(1), voice(1), voice(1)], consent=True, allow_new=True)
    ids.enroll("Yuetong", [voice(2), voice(2), voice(2)], consent=True, allow_new=True)
    m = ids.identify(voice(1))
    assert m.level == "sure" and m.name == "Stella"
    m = ids.identify(voice(2))
    assert m.level == "sure" and m.name == "Yuetong"


def test_unknown_voice_gets_no_name():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    m = ids.identify(voice(5))
    assert m.level == "unknown" and m.person_id is None and m.name is None


def test_nobody_enrolled():
    assert make().identify(voice(1)).level == "unknown"


def test_too_short():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    assert ids.identify(voice(1, seconds=0.5)).level == "too_short"


def test_levels_follow_thresholds():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    vp = ids.voiceprints()[0][2]
    for score, level in ((0.6, "sure"), (0.30, "maybe"), (0.1, "unknown")):
        # an embedding with exactly this cosine to the voiceprint
        other = np.random.default_rng(1).normal(size=DIM).astype(np.float32)
        other -= (other @ vp) * vp
        other /= np.linalg.norm(other)
        emb = score * vp + np.sqrt(1 - score ** 2) * other
        ids.model = type("M", (), {"embed": lambda self, a, e=emb: e})()
        assert ids.identify(voice(1)).level == level


def test_new_person_needs_face_first():
    r = make().enroll("Stella", [voice(1)] * 3, consent=True)
    assert r["status"] == "not_saved"


def test_consent_required():
    try:
        make().enroll("Stella", [voice(1)], consent=False)
    except ValueError:
        return
    raise AssertionError("enrolled without consent")


def test_short_enroll_clips_skipped():
    r = make().enroll("Stella", [voice(1, seconds=1.0)], consent=True, allow_new=True)
    assert r["status"] == "not_saved"


def test_voice_added_to_person_known_by_face():
    face = {"id": "p_face", "name": "Stella", "embeddings": [[0.1] * 128]}
    ids = make([face])
    r = ids.enroll("stella", [voice(1)] * 3, consent=True, allow_new=True)
    assert r["person_id"] == "p_face" and not r["created"]
    p = ids.store.get("p_face")
    assert len(p["embeddings"]) == 1 and len(p[VOICE_KEY]) == 3   # face untouched
    assert ids.identify(voice(1)).person_id == "p_face"


def test_wrong_voice_for_known_name_rejected():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    r = ids.enroll("Stella", [voice(3)] * 3, consent=True, allow_new=True)
    assert r["status"] == "not_saved"
    assert len(ids.store.find_by_name("Stella")[VOICE_KEY]) == 3


def test_mixed_speakers_rejected():
    r = make().enroll("Stella", [voice(1), voice(1), voice(4)], consent=True, allow_new=True)
    assert r["status"] == "not_saved"


def test_samples_capped():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    pid = ids.store.find_by_name("Stella")["id"]
    for _ in range(30):
        ids.add_samples(pid, [ids.model.embed(voice(1))])
    assert len(ids.store.get(pid)[VOICE_KEY]) == voice_id.MAX_VOICE_SAMPLES


def test_add_samples_never_recreates_deleted_person():
    ids = make()
    assert ids.add_samples("p_gone", [ids.model.embed(voice(1))]) == 0
    assert ids.store.people() == []


def test_forget_voice_keeps_face():
    face = {"id": "p_face", "name": "Stella", "embeddings": [[0.1] * 128]}
    ids = make([face])
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    assert ids.forget_voice("p_face")
    p = ids.store.get("p_face")
    assert VOICE_KEY not in p and p["embeddings"]
    assert ids.identify(voice(1)).level == "unknown"


def test_candidates_limit_search():
    ids = make()
    ids.enroll("Stella", [voice(1)] * 3, consent=True, allow_new=True)
    ids.enroll("Yuetong", [voice(2)] * 3, consent=True, allow_new=True)
    yid = ids.store.find_by_name("Yuetong")["id"]
    m = ids.identify(voice(1), candidates={yid})
    assert m.name != "Stella"


def test_48k_stereo_int16_input():
    a = np.zeros((48000 * 2, 2), np.int16)
    out = voice_id.to_model_audio(a, 48000)
    assert out.dtype == np.float32 and out.ndim == 1 and len(out) == 32000


def test_describe():
    assert VoiceMatch("sure", "p", "Stella").describe() == "[SPEAKER] Stella (voice, sure)"
    assert "might be Stella" in VoiceMatch("maybe", "p", "Stella").describe()
    assert VoiceMatch("unknown").describe() == "[SPEAKER] unknown voice"


# This repository runs unittest (tests/run.py, check_release.py): PR #36's plain test
# functions become methods of one TestCase.
import unittest  # noqa: E402


class PR36Tests(unittest.TestCase):
    pass


def _as_method(test):
    def method(self):
        test()
    method.__name__ = method.__qualname__ = test.__name__
    return method


for _name, _test in list(globals().items()):
    if _name.startswith("test_") and callable(_test):
        setattr(PR36Tests, _name, _as_method(_test))
del _name, _test

if __name__ == "__main__":
    unittest.main()
