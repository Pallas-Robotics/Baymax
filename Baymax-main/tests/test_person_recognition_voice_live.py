"""Tests for voice_live.py, with synthetic audio and a fake identifier
(no model download, no microphone).

Run:  python -m pytest test_voice_live.py      (or: python test_voice_live.py)
"""
import numpy as np

from features.person_recognition import voices as voice_id
from features.person_recognition import voices as voice_live
from features.person_recognition.voices import VoiceListener

RATE = 48000
CHUNK = 1024


class LoudVAD:
    """Speech = loud frame (stands in for Silero in tests)."""
    def speech_prob(self, frame):
        return 1.0 if np.sqrt(np.mean(frame ** 2)) > 0.05 else 0.0

    def reset(self):
        pass


class FakeIdentifier:
    def __init__(self):
        self.calls = []
        self.enrolled = []

    def enroll(self, name, clips, sample_rate, consent=False, allow_new=False):
        self.enrolled.append((name, [len(c) / sample_rate for c in clips], consent, allow_new))
        return {"status": "saved", "person_id": "p1", "added": len(clips), "created": False}

    def identify(self, audio, sample_rate):
        self.calls.append(len(audio) / sample_rate)
        return voice_id.VoiceMatch("sure", "p1", "Stella", 0.6, seconds=len(audio) / sample_rate)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def stream(*parts):
    """parts: (seconds, loud?) -> list of int16 byte chunks like the mic callback gives."""
    audio = []
    for secs, loud in parts:
        n = int(secs * RATE)
        t = np.arange(n) / RATE
        audio.append((np.sin(2 * np.pi * 220 * t) * 0.3) if loud else np.random.randn(n) * 0.001)
    audio = (np.concatenate(audio) * 32767).astype(np.int16)
    return [audio[i:i + CHUNK].tobytes() for i in range(0, len(audio), CHUNK)]


def run(chunks, ember=lambda t: False):
    ids, clock, results = FakeIdentifier(), Clock(), []
    lis = VoiceListener(ids, RATE, vad=LoudVAD(), clock=clock,
                        on_result=lambda m, stage: results.append((stage, round(m.seconds, 1))))
    for c in chunks:
        lis.process(c, ember_speaking=ember(clock.t), captured=clock.t)
        clock.t += CHUNK / RATE
    return results, lis


def test_one_utterance_gives_early_and_final():
    results, lis = run(stream((1, False), (3, True), (1, False)))
    stages = [s for s, _ in results]
    assert stages == ["early", "final"], results
    early, final = results[0][1], results[1][1]
    assert 2.0 <= early < 2.6            # identified while still talking
    assert 3.0 <= final <= 4.0           # the whole utterance (+ pre-roll + tail)
    assert lis.latest()[1] == "final"


def test_two_utterances():
    results, _ = run(stream((0.5, False), (1.5, True), (1, False), (1.5, True), (1, False)))
    assert [s for s, _ in results] == ["final", "final"]


def test_too_short_is_ignored():
    results, _ = run(stream((1, False), (0.4, True), (1, False)))
    assert results == []


def test_skips_ember_speaking():
    # Loud audio while Ember talks (its own voice) must not be identified
    results, _ = run(stream((3, True), (1, False)), ember=lambda t: t < 3.0)
    assert results == []


def test_echo_tail_after_ember_stops():
    # Speech starting right after Ember stops is skipped for ECHO_TAIL_S
    results, _ = run(stream((1, False), (2, True), (1, False)), ember=lambda t: t < 1.0)
    assert [s for s, _ in results] == ["final"]
    assert results[0][1] < 2.0 + voice_live.PRE_ROLL_S


def test_long_speech_is_cut():
    results, _ = run(stream((20, True), (1, False)))
    finals = [secs for s, secs in results if s == "final"]
    assert len(finals) == 2 and finals[0] <= voice_live.MAX_UTTERANCE_S + 0.1


def test_pre_roll_is_bounded():
    _, lis = run(stream((5, False)))
    assert lis._pre_samples <= (voice_live.PRE_ROLL_S * RATE) + CHUNK


def test_feed_never_blocks():
    lis = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD())
    for _ in range(voice_live.QUEUE_CHUNKS + 10):
        lis.feed(b"\x00" * CHUNK * 2)
    assert lis.dropped == 10


def test_threaded_end_to_end():
    import time
    results = []
    lis = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD(),
                        on_result=lambda m, s: results.append(s)).start()
    for c in stream((0.5, False), (2.5, True), (1, False)):
        lis.feed(c)
    for _ in range(50):
        if "final" in results:
            break
        time.sleep(0.05)
    lis.stop()
    assert results == ["early", "final"]


def test_vad_rate_conversion():
    assert len(voice_live._to_vad_rate(np.zeros(4800, np.float32), 48000)) == 1600
    assert len(voice_live._to_vad_rate(np.zeros(1600, np.float32), 16000)) == 1600


def test_silero_loads_if_installed():
    # Ported: Silero runs on ONNX Runtime (voices.OnnxSileroVAD), models in .models/voice.
    if not voice_live.models_present():
        return                           # models not copied to this computer
    try:
        vad = voice_live.OnnxSileroVAD()
    except ImportError:
        return                           # onnxruntime not installed here
    p = vad.speech_prob(np.zeros(voice_live.VAD_FRAME, np.float32))
    assert 0.0 <= p < voice_live.SPEECH_PROB


# ─── Telling Gemini ──────────────────────────────────────────────────────────
def m(level, pid=None, name=None):
    return voice_id.VoiceMatch(level, pid, name, 0.5)


def test_notes_name_once_then_quiet():
    clock = Clock()
    notes = voice_live.SpeakerNotes(clock)
    assert notes.note_for(m("sure", "p1", "Stella")) == "[SPEAKER] Stella (voice, sure)"
    assert notes.note_for(m("sure", "p1", "Stella")) is None          # same speaker: no repeat
    clock.t += voice_live.REPEAT_NOTE_S + 1
    assert notes.note_for(m("sure", "p1", "Stella")) is not None      # reminder after a while


def test_notes_speaker_change():
    notes = voice_live.SpeakerNotes(Clock())
    notes.note_for(m("sure", "p1", "Stella"))
    assert "Yuetong" in notes.note_for(m("sure", "p2", "Yuetong"))
    assert "might be Stella" in notes.note_for(m("maybe", "p1", "Stella"))


def test_notes_unknown_only_after_someone_known():
    notes = voice_live.SpeakerNotes(Clock())
    assert notes.note_for(m("unknown")) is None                     # nothing known before
    notes.note_for(m("sure", "p1", "Stella"))
    assert notes.note_for(m("unknown")) == "[SPEAKER] unknown voice"  # someone else now
    assert notes.note_for(m("unknown")) is None
    assert notes.note_for(m("too_short")) is None


def test_report_with_nothing_heard():
    lis = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD())
    r = lis.speaker_report(wait=0.1)
    assert r["speaker"] is None


def test_report_after_utterance():
    _, lis = run(stream((1, False), (2.5, True), (1, False)))
    assert lis.speaker_report(wait=0.1) == {"speaker": "Stella", "confidence": "sure"}


def test_report_waits_for_sentence_in_progress():
    # "Ember, who am I?": the tool is called while the sentence is still being
    # processed — the report should cover that sentence, not come back empty
    import threading
    import time
    lis = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD()).start()
    out = {}
    chunks = stream((0.3, False), (1.4, True), (1, False))
    for c in chunks[:len(chunks) // 2]:          # mid-sentence
        lis.feed(c)
    time.sleep(0.2)
    t = threading.Thread(target=lambda: out.update(lis.speaker_report(wait=2.0)))
    t.start()
    for c in chunks[len(chunks) // 2:]:          # the rest arrives after the tool call
        lis.feed(c)
    t.join(3)
    lis.stop()
    assert out.get("speaker") == "Stella", out


def test_tool_declaration_shape():
    tool = voice_live.VOICE_TOOLS[0]
    assert tool["name"] == "identify_speaker" and "who am I" in tool["description"]
    assert "[SPEAKER]" in voice_live.VOICE_INSTRUCTIONS


# ─── Learning a voice (remember_voice) ───────────────────────────────────────
def learn(chunks, ember=lambda t: False, timeout=voice_live.LEARN_TIMEOUT_S):
    ids, clock, done = FakeIdentifier(), Clock(), []
    lis = VoiceListener(ids, RATE, vad=LoudVAD(), clock=clock, on_result=lambda m, s: None)
    lis.start_learning("Stella", done.append, timeout=timeout)
    for c in chunks:
        lis.process(c, ember_speaking=ember(clock.t), captured=clock.t)
        clock.t += CHUNK / RATE
    return done, ids, lis


def test_learning_from_a_few_sentences():
    done, ids, lis = learn(stream((0.5, False), (3, True), (1, False), (3, True), (1, False),
                                  (3, True), (1, False), (3, True), (1, False)))
    assert len(done) == 1 and done[0]["status"] == "saved" and done[0]["name"] == "Stella"
    name, pieces, consent, allow_new = ids.enrolled[0]
    assert name == "Stella" and consent and not allow_new
    assert len(pieces) >= 2 and all(p >= 2.0 for p in pieces)
    assert not lis.learning()


def test_learning_splits_one_long_answer():
    done, ids, _ = learn(stream((0.5, False), (10, True), (1, False)))
    assert done and done[0]["status"] == "saved"
    pieces = ids.enrolled[0][1]
    assert len(pieces) == 3 and max(pieces) <= voice_live.LEARN_PIECE_S + 0.01


def test_learning_times_out_with_too_little():
    done, ids, lis = learn(stream((0.5, False), (1.2, True), (1, False), (3, False)), timeout=4.0)
    assert done and done[0]["status"] == "not_saved" and not ids.enrolled
    assert not lis.learning()


def test_learning_ignores_ember():
    # Ember talks for 9 s while "learning": none of it may become a voice sample
    done, ids, lis = learn(stream((9, True), (1, False)), ember=lambda t: t < 9.0)
    assert not done and not ids.enrolled and lis.learning()


def test_remember_voice_rules():
    lis = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD())
    stella = {"id": "p1", "name": "Stella"}
    r = voice_live.remember_voice(lis, "Minji", None, [], print)
    assert r["status"] == "not_saved" and "face" in r["reason"]          # unknown person
    r = voice_live.remember_voice(lis, "Stella", stella, ["Joe"], print)
    assert r["status"] == "not_saved" and not lis.learning()             # not in view
    r = voice_live.remember_voice(lis, "stella", stella, ["Stella"], print)
    assert r["status"] == "listening" and lis.learning()                 # in view: OK
    lis2 = VoiceListener(FakeIdentifier(), RATE, vad=LoudVAD())
    with_voice = {**stella, voice_id.VOICE_KEY: [[0.1] * 192]}
    r = voice_live.remember_voice(lis2, "Stella", with_voice, [], print)
    assert r["status"] == "listening"                                    # adding to a known voice


def test_learned_message():
    assert "learned Stella's voice" in voice_live.learned_message(
        {"status": "saved", "name": "Stella"})
    msg = voice_live.learned_message({"status": "not_saved", "name": "Stella", "reason": "Too quiet."})
    assert msg.startswith("[VOICE LEARNED]") and "Too quiet." in msg


def test_remember_voice_declared():
    names = [t["name"] for t in voice_live.VOICE_TOOLS]
    assert names == ["identify_speaker", "remember_voice"]
    assert "remember_voice" in voice_live.VOICE_INSTRUCTIONS


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
