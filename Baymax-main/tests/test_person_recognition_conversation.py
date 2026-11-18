"""Who is talking, and to whom: session voices (speakers.py), turn taking
(conversation.py), the reply gate's side-conversation hold, and the face changes that go
with them (facing the camera, learning new angles, not asking a half-recognised face its
name, backups of the people file). No models: embeddings are made-up directions."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from features.conversational_ability.addressing import AddressGate
from features.person_recognition import conversation, faces, speakers, voice_process, voices
from features.person_recognition.conversation import ConversationTracker
from features.person_recognition.people import People


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def direction(seed):
    return voices._normalize(np.random.default_rng(seed).normal(size=192))


def utterance(base, rng, noise=1.4):
    """One utterance of the speaker `base`: on the board a clip of one speaker matched the
    average of the others by ~0.45 and single clips their stored voice by 0.25-0.55, so the
    noise is large."""
    return voices._normalize(base + noise * voices._normalize(rng.normal(size=192)))


class SessionSpeakersTests(unittest.TestCase):
    def test_two_people_taking_turns_get_two_voices(self):
        rng = np.random.default_rng(0)
        joe, natey = direction(1), direction(2)
        session = speakers.SessionSpeakers()
        ids = [session.assign(utterance(who, rng, noise=1.2), 10.0 * i).id
               for i, who in enumerate([joe, natey] * 4)]
        self.assertEqual(ids[4:], ["v1", "v2", "v1", "v2"])
        self.assertEqual(len(session.active(80.0, 90.0)), 2)

    def test_one_person_split_over_two_voices_is_merged(self):
        rng = np.random.default_rng(7)
        joe = direction(1)
        session = speakers.SessionSpeakers()
        first, second = speakers.SessionVoice(1, utterance(joe, rng), 0.0), speakers.SessionVoice(2, utterance(joe, rng), 1.0)
        session.voices, session.next_number = [first, second], 3
        for i in range(6):
            session.assign(utterance(joe, rng, noise=1.0), 2.0 + i)
        self.assertEqual([v.id for v in session.voices], ["v1"])

    def test_a_voice_is_recognised_from_several_utterances(self):
        """Each utterance alone scores only "might be"; their average is "sure"."""
        rng = np.random.default_rng(3)
        joe = direction(1)
        stored = [("p_joe", "Joe", voices._normalize(np.mean([utterance(joe, rng) for _ in range(4)], axis=0)))]
        clips = [utterance(joe, rng) for _ in range(4)]
        self.assertLess(min(float(stored[0][2] @ c) for c in clips), 0.40)     # alone: "might be"
        session = speakers.SessionSpeakers()
        voice = None
        for i, clip in enumerate(clips):
            voice = session.assign(clip, float(i))
        self.assertEqual(session.recognise(voice, stored, 0.40)[:2], ("p_joe", "Joe"))
        self.assertEqual(voice.label, "Joe")

    def test_a_known_person_keeps_one_session_voice(self):
        rng = np.random.default_rng(4)
        joe = direction(1)
        session = speakers.SessionSpeakers()
        first = session.assign(utterance(joe, rng), 0.0, "p_joe", "Joe", sure=True)
        stranger = session.assign(direction(9), 1.0)
        again = session.assign(direction(10), 2.0, "p_joe", "Joe", sure=True)   # sure, though unlike
        self.assertIs(again, first)
        self.assertIsNot(stranger, first)
        self.assertIsNone(session.recognise(stranger, [("p_joe", "Joe", direction(9))], 0.40))

    def test_voices_are_forgotten_after_a_while(self):
        session = speakers.SessionSpeakers()
        session.assign(direction(1), 0.0)
        session.assign(direction(1), speakers.SPEAKER_IDLE_S + 1)
        self.assertEqual([v.count for v in session.voices], [1])


class VoiceChildTurnTests(unittest.TestCase):
    def child(self, prints=()):
        child = voice_process.VoiceChild.__new__(voice_process.VoiceChild)
        child.identifier = SimpleNamespace(voiceprints=lambda: list(prints))
        child.results, child.learned, child.adopted, child.turns = [], [], [], []
        child.recent, child.pending, child.next_id = __import__("collections").OrderedDict(), {}, 0
        child.speakers, child._final_voice = speakers.SessionSpeakers(), None
        return child

    def test_each_utterance_gets_a_session_voice_and_a_turn(self):
        child = self.child()
        for start, seed in ((0.0, 1), (5.0, 2)):
            match = voices.VoiceMatch("unknown", embedding=direction(seed), seconds=2.0)
            child._result(match, "final")
            child._turn(start, start + 2.0, 2.0, match)
        child._turn(9.0, 9.4, 0.4, None)                 # "yeah": too short to identify
        self.assertEqual([r["speaker"] for r in child.results], ["v1", "v2"])
        self.assertEqual([t["speaker"] for t in child.turns], ["v1", "v2", None])
        self.assertEqual(child.turns[0]["start"], 0.0)

    def test_maybe_results_become_sure_when_the_session_voice_is_recognised(self):
        rng = np.random.default_rng(5)
        joe = direction(1)
        prints = [("p_joe", "Joe", voices._normalize(np.mean([utterance(joe, rng) for _ in range(4)], axis=0)))]
        child = self.child(prints)
        for _ in range(3):
            child._result(voices.VoiceMatch("maybe", "p_joe", "Joe", 0.3, 2.0, utterance(joe, rng)), "final")
        self.assertEqual(child.results[0]["level"], "maybe")      # one utterance: not enough
        self.assertEqual(child.results[-1]["level"], "sure")
        self.assertEqual(child.results[-1]["by"], "several utterances")

    def test_listener_reports_every_utterance_with_its_times(self):
        rate, chunk = 16000, 320
        clock = Clock(0.0)

        class LoudVAD:
            def speech_prob(self, frame):
                return 1.0 if np.sqrt(np.mean(frame ** 2)) > 0.05 else 0.0

            def reset(self):
                pass
        identifier = SimpleNamespace(identify=lambda audio, rate: voices.VoiceMatch(
            "unknown", seconds=len(audio) / rate, embedding=direction(1)))
        turns = []
        listener = voices.VoiceListener(identifier, rate, vad=LoudVAD(), clock=clock,
                                        on_result=lambda m, stage: None,
                                        on_utterance=lambda *turn: turns.append(turn))
        audio = []
        for seconds, loud in ((1, False), (3, True), (1, False), (0.5, True), (1, False)):
            t = np.arange(int(seconds * rate)) / rate
            audio.append(np.sin(2 * np.pi * 220 * t) * 0.3 if loud else np.zeros(len(t)))
        audio = (np.concatenate(audio) * 32767).astype(np.int16)
        for i in range(0, len(audio), chunk):
            listener.process(audio[i:i + chunk].tobytes(), ember_speaking=False, captured=clock.now)
            clock.now += chunk / rate
        self.assertEqual([(round(s, 1), round(e, 1), m is not None) for s, e, _, m in turns],
                         [(1.0, 4.0, True), (5.0, 5.5, False)])


class ConversationTrackerTests(unittest.TestCase):
    def tracker(self):
        clock = Clock(0.0)
        return ConversationTracker(clock), clock

    def say(self, tracker, clock, speaker, seconds=3.0, person=None, name=None, facing=None):
        start = clock.now
        clock.now += seconds
        return tracker.add_turn({"start": start, "end": clock.now, "speech_s": seconds, "speaker": speaker,
                                 "person_id": person, "name": name}, facing)

    def test_two_people_talking_to_each_other(self):
        tracker, clock = self.tracker()
        tracker.faces(2, clock.now)
        for speaker in ("v1", "v2", "v1"):
            self.say(tracker, clock, speaker)
        self.assertFalse(tracker.side_conversation())          # v2 heard once: maybe a stray split
        changed = self.say(tracker, clock, "v2")
        self.assertTrue(changed["side"])
        self.assertIn("talking to each other", tracker.describe())
        tracker.ember_audible(clock.now + 0.5)                   # Ember was named and answered
        clock.now += 1
        self.assertFalse(tracker.side_conversation())
        self.say(tracker, clock, "v1")
        self.assertFalse(tracker.side_conversation())          # one voice since Ember spoke

    def test_turning_to_ember_ends_it(self):
        tracker, clock = self.tracker()
        tracker.faces(2, clock.now)
        for speaker in ("v1", "v2", "v1", "v2"):
            self.say(tracker, clock, speaker)
        self.assertTrue(tracker.side_conversation())
        self.say(tracker, clock, "v1", facing=True)
        self.assertFalse(tracker.side_conversation())

    def test_one_person_whose_voice_was_split_is_still_talking_to_ember(self):
        tracker, clock = self.tracker()
        tracker.faces(1, clock.now)
        for speaker in ("v1", "v2", "v1", "v2", "v1"):
            self.say(tracker, clock, speaker)
        self.assertFalse(tracker.side_conversation())

    def test_two_known_voices_are_two_people_even_off_camera(self):
        tracker, clock = self.tracker()
        self.say(tracker, clock, "v1", person="p_joe", name="Joe")
        self.say(tracker, clock, "v2", person="p_natey", name="Natey")
        self.assertTrue(tracker.side_conversation())
        state = tracker.state()
        self.assertEqual(state["known"], ["Joe", "Natey"])
        self.assertIn("2 people have been talking", tracker.describe())

    def test_short_turns_dont_count_as_another_voice(self):
        tracker, clock = self.tracker()
        tracker.faces(2, clock.now)
        for speaker in ("v1", None, "v1", None, "v1"):
            self.say(tracker, clock, speaker, seconds=0.5 if speaker is None else 3.0)
        self.assertFalse(tracker.side_conversation())
        self.assertEqual(tracker.state()["voices"], 1)

    def test_old_turns_drop_out(self):
        tracker, clock = self.tracker()
        tracker.faces(2, clock.now)
        for speaker in ("v1", "v2", "v1", "v2"):
            self.say(tracker, clock, speaker)
        clock.now += conversation.WINDOW_S + 1
        self.assertFalse(tracker.side_conversation())
        self.assertEqual(tracker.describe(), "Nobody has been talking in the last minute and a half.")


class PeopleConversationTests(unittest.TestCase):
    def people(self, faces_in_view):
        people = People(clock=Clock(100.0))
        people.identifier = SimpleNamespace(facing_people=lambda: faces_in_view)
        people.voice = SimpleNamespace()
        return people

    def test_whose_face_was_facing(self):
        turn = {"start": 99.0, "end": 100.0, "person_id": "p_joe"}
        cases = [([("p_joe", 99.5), ("p_natey", -1e9)], True),       # Joe faced Ember while talking
                 ([("p_joe", 50.0), ("p_natey", 99.5)], False),      # Joe looked away; Natey faced it
                 ([("p_natey", 99.5)], None)]                         # Joe off camera: can't tell
        for faces_in_view, expected in cases:
            with self.subTest(faces=faces_in_view):
                self.assertEqual(self.people(faces_in_view)._facing(turn), expected)
        unknown = {"start": 99.0, "end": 100.0, "person_id": None}
        self.assertTrue(self.people([(None, 99.5)])._facing(unknown))       # the only face there
        self.assertIsNone(self.people([(None, 99.5), (None, 99.5)])._facing(unknown))

    def test_side_conversation_is_off_without_recognition_or_when_switched_off(self):
        people = self.people([])
        people.conversation.side_conversation = lambda: True
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "1"}):
            self.assertTrue(people.side_conversation())
            with patch.dict(os.environ, {"BAYMAX_GROUP_AWARE": "0"}):
                self.assertFalse(people.side_conversation())
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "0"}):
            self.assertFalse(people.side_conversation())
        people.voice = None
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "1"}):
            self.assertFalse(people.side_conversation())

    def test_ember_does_not_start_conversations_while_people_talk_to_each_other(self):
        people = self.people([])
        people.conversation.side_conversation = lambda: True
        engagement = SimpleNamespace(disengaged=False)
        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "1"}), \
                patch("features.conversational_ability.engage.engagement", engagement), \
                patch("features.toolkits.everyday.tools.available_to_speak", lambda: True):
            self.assertFalse(people._can_speak())
            people.conversation.side_conversation = lambda: False
            self.assertTrue(people._can_speak())

    def test_ember_speaking_is_noted_from_the_mic_feed(self):
        people = self.people([])
        people.voice = SimpleNamespace(feed=Mock())
        people.feed_audio(b"\0\0", False)
        self.assertEqual(people.conversation.ember_last, -1e9)
        people.feed_audio(b"\0\0", True)
        self.assertEqual(people.conversation.ember_last, 100.0)


class SideConversationGateTests(unittest.TestCase):
    def gate(self, side, enabled=False):
        clock = Clock()
        log = Mock()
        return AddressGate(enabled=enabled, follow_up=20, clock=clock, log=log, side=lambda: side[0]), log

    def turn(self, gate, words):
        played = []
        for word in words:
            for held in gate.heard(word):
                held()
        gate.reply(lambda: played.append("reply"), "reply")
        gate.end()
        return played

    def test_one_person_is_answered_as_before(self):
        g, _ = self.gate([False])
        self.assertEqual(self.turn(g, ["what time is it?"]), ["reply"])

    def test_people_talking_to_each_other_get_no_reply_unless_ember_is_named(self):
        side = [True]
        g, log = self.gate(side)
        self.assertEqual(self.turn(g, ["did you finish the slides?"]), [])
        self.assertIn("people talking to each other", log.call_args[0][0])
        self.assertEqual(self.turn(g, ["Ember, what time is it?"]), ["reply"])
        side[0] = False
        self.assertEqual(self.turn(g, ["and the weather?"]), ["reply"])

    def test_a_held_reply_plays_once_the_speaker_turns_out_to_face_ember(self):
        side = [True]
        g, _ = self.gate(side)
        played = []
        g.heard("what's on my calendar?")
        g.reply(lambda: played.append(1), "You have")
        self.assertEqual(played, [])
        side[0] = False                                   # their turn came in: facing Ember
        g.reply(lambda: played.append(2), " a meeting")
        g.end()
        self.assertEqual(played, [1, 2])

    def test_embers_own_messages_are_always_answered(self):
        g, _ = self.gate([True])
        g.prompted()
        self.assertEqual(self.turn(g, []), ["reply"])

    def test_a_broken_check_never_silences_ember(self):
        g = AddressGate(enabled=False, clock=Clock(), log=Mock(), side=Mock(side_effect=RuntimeError))
        played = []
        g.reply(lambda: played.append(1))
        self.assertEqual(played, [1])


class FaceChangesTests(unittest.TestCase):
    def detection(self, nose_x):
        landmarks = np.array([[40, 50], [80, 50], [nose_x, 70], [45, 90], [75, 90]], np.float32)
        return faces.Detection(box=(20, 20, 80, 100), landmarks=landmarks, score=0.9, row=np.zeros(15))

    def test_facing_the_camera(self):
        self.assertTrue(self.detection(60).facing())
        self.assertTrue(self.detection(66).facing())        # turned a little
        self.assertFalse(self.detection(72).facing())       # turned away (~30 degrees and more)
        self.assertAlmostEqual(self.detection(70).yaw(), 0.25)

    def test_people_file_is_backed_up_before_changes(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "ember_people.json")
        store = faces.PeopleStore(path)
        store.add_samples("Joe", [direction(1)[:128]], source="conversation")
        store.add_samples("Natey", [direction(2)[:128]], source="conversation")
        backups = [f for f in os.listdir(folder) if ".backup-" in f]
        self.assertEqual(len(backups), 1)                   # once per BACKUP_INTERVAL
        with patch.object(faces, "BACKUP_INTERVAL", -1), patch("time.strftime", side_effect=[
                f"2026111{i}-000000" for i in range(8)]):
            for i in range(6):
                store.add_samples(f"P{i}", [direction(10 + i)[:128]], source="conversation")
        backups = sorted(f for f in os.listdir(folder) if ".backup-" in f)
        self.assertEqual(len(backups), faces.BACKUPS_KEPT)


if __name__ == "__main__":
    unittest.main()
