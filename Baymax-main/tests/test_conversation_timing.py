import unittest
from types import SimpleNamespace

from features.conversational_ability.timing import ConversationTiming


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.timing = ConversationTiming(clock=lambda: self.now, wall=lambda: 1000)

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
        self.timing.played(1920, .05, 48000)
        row = self.timing.snapshot()[0]
        self.assertAlmostEqual(row['speech_start'], 1001)
        self.assertAlmostEqual(row['speech_end'], 1001.1)
        self.assertEqual(row['transcript_first'], 1002)
        self.assertEqual(row['transcript_last'], 1002.2)
        self.assertEqual(row['reply_audio'], 1003)
        self.assertEqual(row['reply_text'], 1003.1)
        self.assertAlmostEqual(row['playback'], 1003.26)
        self.assertEqual(row['delays']['recognition_ms'], 900)
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
        self.timing.played(100, .05, 48000)
        rows = self.timing.snapshot()
        self.assertIn('playback', rows[0])
        self.assertNotIn('playback', rows[1])
        self.timing.played(100, .05, 48000)
        self.now = 1
        self.timing.played(100, .05, 48000)
        self.assertAlmostEqual(self.timing.snapshot()[1]['playback'], 1001.05 + 4 / 96000)

    def test_interruption_clears_unplayed_mark_and_ends_reply(self):
        self.timing.transcript('user', 1)
        turn_id = self.timing.received_audio()
        self.timing.queued(turn_id, 100, 0)
        self.timing.clear_playback()
        self.timing.complete_reply(interrupted=True)
        self.timing.played(200, 0, 48000)
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


if __name__ == '__main__':
    unittest.main()
