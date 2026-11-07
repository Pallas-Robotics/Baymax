import unittest
from types import SimpleNamespace

from features.conversational_ability.timing import ConversationTiming


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.timing = ConversationTiming(clock=lambda: self.now, wall=lambda: 1000 + self.now)

    def speech(self):
        for self.now in (1, 1.02, 1.04, 1.06, 1.08):
            self.timing.microphone(.9, -25, .02)
        self.now = 1.6
        self.timing.microphone(.1, -65, .02)

    def test_speech_transcript_reply_and_dac_are_separate_measurements(self):
        self.speech()
        self.now = 2
        self.timing.transcript('user', 1)
        self.now = 2.2
        self.timing.transcript('user', 1)
        self.now = 3
        turn_id = self.timing.received_audio()
        self.now = 3.1
        self.timing.received_text()
        self.timing.queued(turn_id, 960, 0)
        self.now = 3.2
        self.timing.played(0, 1920, .05, 48000)
        row = self.timing.snapshot()[0]
        self.assertAlmostEqual(row['speech_start'], 1001)
        self.assertAlmostEqual(row['speech_end'], 1001.1)
        self.assertEqual(row['transcript_first'], 1002)
        self.assertEqual(row['transcript_last'], 1002.2)
        self.assertEqual(row['reply_audio'], 1003)
        self.assertEqual(row['reply_text'], 1003.1)
        self.assertAlmostEqual(row['playback'], 1003.26)
        # To the transcript's last piece: its first piece streams in while the person speaks.
        self.assertEqual(row['delays']['recognition_ms'], 1100)
        self.assertEqual(row['delays']['audible_ms'], 2160)

    def test_high_probability_low_energy_noise_does_not_create_speech(self):
        for self.now in (1, 1.1, 1.2):
            self.timing.microphone(.95, -70, .02)
        self.timing.transcript('user', 1)
        self.assertNotIn('speech_start', self.timing.snapshot()[0])

    def test_local_indicator_precedes_transcript_and_then_waits_for_it(self):
        self.assertFalse(self.timing.speech_status()['available'])
        for self.now in (1, 1.02, 1.04, 1.06):
            self.timing.microphone(.9, -25, .02)
        self.assertTrue(self.timing.speech_status()['active'])
        self.assertEqual(self.timing.snapshot(), [])
        self.now = 1.6
        self.timing.microphone(.1, -65, .02)
        self.assertTrue(self.timing.speech_status()['awaiting_transcript'])
        self.timing.transcript('user', 1)
        self.assertFalse(self.timing.speech_status()['awaiting_transcript'])

    def test_stale_microphone_does_not_leave_indicator_lit(self):
        self.speech()
        self.now = 10
        self.assertFalse(self.timing.speech_status()['available'])
        self.assertFalse(self.timing.speech_status()['active'])

    def test_only_actual_output_underflows_increment_counter(self):
        self.timing.output_status(None)
        self.timing.output_status(SimpleNamespace(output_underflow=False))
        self.assertEqual(self.timing.speech_status()['output_underflows'], 0)
        self.timing.output_status(SimpleNamespace(output_underflow=True))
        self.assertEqual(self.timing.speech_status()['output_underflows'], 1)

    def test_ambiguous_segments_do_not_invent_recognition_latency(self):
        self.speech()
        for self.now in (2, 2.02, 2.04, 2.06):
            self.timing.microphone(.9, -20, .02)
        self.now = 3
        self.timing.microphone(.1, -70, .02)
        self.timing.transcript('user', 1)
        row = self.timing.snapshot()[0]
        self.assertIn('Ambiguous', row['association'])
        self.assertNotIn('speech_end', row)
        self.assertNotIn('recognition_ms', row['delays'])

    def test_two_buffered_replies_keep_their_own_playback_marks(self):
        self.timing.transcript('user', 1)
        first = self.timing.received_audio()
        self.timing.queued(first, 0, 0)
        self.timing.complete_reply()
        self.timing.transcript('user', 3)
        second = self.timing.received_audio()
        self.timing.queued(second, 200, 2)
        self.timing.played(0, 100, .05, 48000)
        rows = self.timing.snapshot()
        self.assertIn('playback', rows[0])
        self.assertNotIn('playback', rows[1])
        self.timing.played(100, 100, .05, 48000)
        self.now = 1
        self.timing.played(200, 100, .05, 48000)
        self.assertAlmostEqual(self.timing.snapshot()[1]['playback'], 1001.05 + 4 / 96000)

    def test_interruption_clears_unplayed_mark_and_ends_reply(self):
        self.timing.transcript('user', 1)
        turn_id = self.timing.received_audio()
        self.timing.queued(turn_id, 100, 0)
        self.timing.clear_playback()
        self.timing.complete_reply(interrupted=True)
        self.timing.played(0, 200, 0, 48000)
        row = self.timing.snapshot()[0]
        self.assertTrue(row['interrupted'])
        self.assertNotIn('playback', row)

    def test_unsolicited_reply_has_no_fabricated_user_latency(self):
        self.timing.received_audio()
        row = self.timing.snapshot()[0]
        self.assertNotIn('transcript_first', row)
        self.assertEqual(row['delays'], {})

    def test_history_is_bounded_and_partial_transcript_is_one_turn(self):
        for index in range(50):
            self.timing.transcript('user', index)
            self.timing.transcript('user', index)
            self.timing.received_audio()
            self.timing.complete_reply()
        self.assertEqual(len(self.timing.snapshot()), 20)

    def test_a_clock_corrected_after_start_moves_the_times_with_it(self):
        # NTP can step the clock after Ember starts (no clock battery); the heartbeat and
        # the performance sampler use the clock as it is now, so the times must too.
        step = [0]
        self.timing = ConversationTiming(clock=lambda: self.now, wall=lambda: 1000 + self.now + step[0])
        self.speech()
        self.now = 2
        self.timing.transcript('user', 1)
        step[0] = 100
        row = self.timing.snapshot()[0]
        self.assertAlmostEqual(row['speech_end'], 1101.1)
        self.assertAlmostEqual(row['transcript_first'], 1102)
        self.assertAlmostEqual(self.timing.speech_status()['speech_end'], 1101.1)
        self.assertEqual(row['delays']['recognition_ms'], 900)  # durations never move


BLOCK = 1024 / 48000  # one v10 microphone callback


def replay(voice, events, end, latency):
    """Feed the detector the runtime's mic blocks on a fake clock and run each
    (time, event) in time order. voice: (start, stop) seconds the person talks."""
    now = [0.0]
    timing = ConversationTiming(clock=lambda: now[0], wall=lambda: 1000 + now[0])
    blocks = [(index * BLOCK, None) for index in range(int(end / BLOCK))]
    for at, event in sorted(blocks + events, key=lambda item: item[0]):
        now[0] = at
        if event is None:
            talking = any(start <= at < stop for start, stop in voice)
            timing.microphone(.95 if talking else .05, -25 if talking else -70, BLOCK, latency)
        else:
            event(timing)
    return timing.snapshot()


def said(message_id):
    return lambda timing: timing.transcript('user', message_id)


def reply(timing):
    return timing.received_audio()


def done(timing):
    timing.complete_reply()


class ReplyAttributionTests(unittest.TestCase):
    """Live API orderings: the transcript streams while the person speaks, and every
    reply's audio starts 0.6 s after their true last word. Mic input latency 0 and 30 ms."""

    def assertAnswered(self, row):
        # The local estimate ends with the last mic block (21 ms) that had voice.
        self.assertAlmostEqual(row['delays']['response_ms'], 600, delta=50)

    def test_each_reply_is_charged_to_the_turn_it_answers(self):
        for latency in (0, .03):
            with self.subTest(latency=latency):
                rows = replay([(0, 2), (10, 12)], [(.8, said(1)), (1.7, said(1)), (2.6, reply), (5, done),
                              (10.5, said(2)), (11.5, said(2)), (12.6, reply), (15, done)], 16, latency)
                self.assertEqual([row['user_id'] for row in rows], [1, 2])
                for row in rows:
                    self.assertAnswered(row)

    def test_a_turn_gemini_leaves_unanswered_does_not_take_the_next_reply(self):
        # Proactive audio (conversation.only_when_addressed, on by default): Gemini ends a
        # hesitation's turn without a reply, then answers the next turn.
        for latency in (0, .03):
            with self.subTest(latency=latency):
                hesitation, question = replay([(0, .8), (4, 6)], [(.5, said(1)), (1.4, done), (4.5, said(2)),
                                              (5.5, said(2)), (6.6, reply), (9, done)], 10, latency)
                self.assertNotIn('reply_audio', hesitation)
                self.assertIsNotNone(hesitation['reply_complete'])  # no longer "Waiting for reply"
                self.assertAnswered(question)

    def test_a_transcript_arriving_after_the_reply_audio_keeps_its_reply(self):
        # Transcription can trail the reply's first audio (features/conversational_ability/addressing.py).
        for latency in (0, .03):
            with self.subTest(latency=latency):
                rows = replay([(0, 2), (10, 12)], [(2.6, reply), (2.7, said(1)), (5, done),
                              (10.5, said(2)), (11.5, said(2)), (12.6, reply), (15, done)], 16, latency)
                self.assertEqual([row.get('user_id') for row in rows], [1, 2])
                for row in rows:
                    self.assertAnswered(row)

    def test_a_pause_inside_one_turn_ends_the_turn_with_its_last_words(self):
        # 450 ms of quiet ends a local segment (400 ms); Gemini's turn goes on (500 ms or more).
        for latency in (0, .03):
            with self.subTest(latency=latency):
                first, second = replay([(0, 1.5), (1.95, 3.5), (10, 12)], [
                    (.8, said(1)), (2.5, said(1)), (3.3, said(1)), (4.1, reply), (6, done),
                    (10.5, said(2)), (11.5, said(2)), (12.6, reply), (14, done)], 15, latency)
                self.assertAnswered(first)
                self.assertEqual(second['association'], 'Local speech estimate')  # not ambiguous
                self.assertAnswered(second)

    def test_speech_over_embers_own_message_is_not_charged_to_it(self):
        # Ember speaks first (a check-in); the person talks over it and is answered afterwards.
        rows = replay([(1.5, 3)], [(1, reply), (2, said(1)), (2.5, done), (3.6, reply), (5, done)], 6, .03)
        self.assertEqual([row.get('user_id') for row in rows], [None, 1])
        self.assertEqual(rows[0]['association'], 'Reply without matched user turn')
        self.assertAnswered(rows[1])


if __name__ == '__main__':
    unittest.main()
