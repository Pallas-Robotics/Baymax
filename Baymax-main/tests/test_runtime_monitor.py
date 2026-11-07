import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import runtime_monitor as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'status.json'
        self.transcripts = patch.object(monitor, 'transcripts', monitor.deque(maxlen=60))
        self.transcripts.start()
        self.addCleanup(self.transcripts.stop)
        self.active = patch.object(monitor, 'active_transcripts', {})
        self.active.start()
        self.addCleanup(self.active.stop)
        self.events = patch.object(monitor, 'events', monitor.deque(maxlen=80))
        self.events.start()
        self.addCleanup(self.events.stop)
        self.activities = patch.object(monitor, 'activities', {})
        self.activities.start()
        self.addCleanup(self.activities.stop)
        self.state = patch.object(monitor, 'state', 'starting')
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_missing_and_stale_heartbeat_report_offline(self):
        self.assertEqual(monitor.read_status(self.path)['state'], 'offline')
        self.path.write_text(json.dumps({'heartbeat': 10, 'state': 'speaking'}))
        self.assertEqual(monitor.read_status(self.path, now=16)['state'], 'offline')

    def test_tool_timeline_and_audio_buffer_are_reported(self):
        monitor.emit('thinking', 'Waiting for Gemini')
        monitor.activity('get_weather')
        self.assertIn('get_weather', monitor.snapshot()['activities'])
        monitor.activity('get_weather', running=False)
        monitor.emit('listening', 'Reply complete')
        snapshot = monitor.snapshot({'playback_bytes': 100})
        self.assertEqual(snapshot['state'], 'speaking')
        self.assertFalse(snapshot['activities'])
        self.assertEqual(snapshot['events'][-2]['message'], 'Finished get_weather')

    def test_atomic_status_file_and_stopped_state(self):
        with patch.object(monitor, 'PATH', self.path):
            monitor.emit('stopped', 'Ember stopped')
            monitor.write_status()
        self.assertEqual(monitor.read_status(self.path)['state'], 'stopped')
        self.assertFalse(self.path.with_suffix('.tmp').exists())

    def test_repeated_state_does_not_flood_timeline(self):
        for _ in range(100):
            monitor.emit('speaking', 'Reply audio')
        self.assertEqual(len(monitor.snapshot()['events']), 1)

    def test_unavailable_features_are_not_reported_as_running(self):
        capabilities = monitor.snapshot()['capabilities']
        self.assertIn('Disabled', capabilities['memory'])
        # v10 reports its real memory state (it was always shown as disabled).
        with patch.object(monitor, 'memory_status', 'On (3 memories at start)'):
            self.assertEqual(monitor.snapshot()['capabilities']['memory'], 'On (3 memories at start)')
        with patch.dict('os.environ', {'BAYMAX_GOOGLE_SEARCH': '0'}):
            self.assertEqual(monitor.snapshot()['capabilities']['google_search'], 'Disabled')
        with patch.dict('os.environ', {'BAYMAX_GOOGLE_SEARCH': '1', 'BAYMAX_TOOLKIT_ENABLED': '1'}):
            self.assertIn('grounding', monitor.snapshot()['capabilities']['google_search'])

    def test_stop_request_only_targets_current_runtime(self):
        request_path = Path(self.temp.name) / 'stop.json'
        with patch.object(monitor, 'STOP_PATH', request_path):
            request_path.write_text(json.dumps({'pid': monitor.os.getpid() + 1, 'requested_at': time.time()}))
            self.assertFalse(monitor.consume_stop_request())
            request_path.write_text(json.dumps({'pid': monitor.os.getpid(), 'requested_at': time.time() - 20}))
            self.assertFalse(monitor.consume_stop_request())
            request_path.write_text(json.dumps({'pid': monitor.os.getpid(), 'requested_at': time.time()}))
            self.assertTrue(monitor.consume_stop_request())
            self.assertFalse(request_path.exists())


    def test_fragments_append_into_one_live_message_without_losing_spaces(self):
        for fragment in ('Hello', ' . . . my', ' name', ' is Alex.'):
            monitor.transcribe('user', fragment)
        messages = monitor.snapshot()['transcripts']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['text'], 'Hello . . . my name is Alex.')
        self.assertTrue(messages[0]['live'])
        monitor.finish_transcripts()
        self.assertFalse(monitor.snapshot()['transcripts'][0]['live'])

    def test_split_word_and_speaker_order(self):
        monitor.transcribe('user', 'Hel')
        monitor.transcribe('user', 'lo')
        monitor.transcribe('ember', 'Hi!')
        monitor.finish_transcripts('ember')
        monitor.transcribe('user', 'Next question')
        messages = monitor.snapshot()['transcripts']
        self.assertEqual([m['speaker'] for m in messages], ['user', 'ember', 'user'])
        self.assertEqual(messages[0]['text'], 'Hello')
        self.assertFalse(messages[0]['live'])

    def test_offline_status_preserves_last_conversation(self):
        self.path.write_text(json.dumps({'heartbeat': 10, 'state': 'listening', 'transcripts': [{'text': 'Hello', 'live': True}]}))
        result=monitor.read_status(self.path, now=20)
        self.assertEqual(result['state'], 'offline')
        self.assertEqual(result['transcripts'][0]['text'], 'Hello')
        self.assertFalse(result['transcripts'][0]['live'])

    def test_snapshot_transcript_is_not_mutated_by_later_fragment(self):
        monitor.transcribe('user', 'Hello')
        before=monitor.snapshot()
        monitor.transcribe('user', ' there')
        self.assertEqual(before['transcripts'][0]['text'], 'Hello')


    def test_whitespace_only_delta_is_preserved_inside_message(self):
        monitor.transcribe('user', 'Hello')
        monitor.transcribe('user', ' ')
        monitor.transcribe('user', 'there')
        self.assertEqual(monitor.snapshot()['transcripts'][0]['text'], 'Hello there')


    def test_tool_start_and_end_update_one_stable_event(self):
        monitor.activity('get_weather')
        start = monitor.snapshot()['events'][-1]
        monitor.activity('get_weather', running=False)
        finish = monitor.snapshot()['events'][-1]
        self.assertEqual(len(monitor.snapshot()['events']), 1)
        self.assertEqual(start['id'], finish['id'])
        self.assertIsNone(start['finished_at'])
        self.assertIsNotNone(finish['finished_at'])

    def test_repeated_tool_calls_remain_separate_invocations(self):
        monitor.activity('get_weather')
        monitor.activity('get_weather', running=False)
        monitor.activity('get_weather')
        monitor.activity('get_weather', running=False)
        events=monitor.snapshot()['events']
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]['id'], events[1]['id'])
