"""Local timing estimates; bounded metadata only, never audio or audio gating."""
from collections import deque
import threading
import time

# Times are kept on the monotonic clock and become wall-clock times only in snapshot()
# and speech_status(), with the clocks' offset at that moment. A clock corrected after
# Ember started (NTP on a board without a clock battery) then moves them along with the
# monitor's heartbeat and the performance sampler's window instead of leaving them behind.
TIMES = ('speech_start', 'speech_end', 'transcript_first', 'transcript_last',
         'reply_text', 'reply_audio', 'reply_complete', 'playback')


class ConversationTiming:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock = clock
        self.wall = wall
        self.lock = threading.RLock()
        self.segments = deque(maxlen=40)
        self.turns = deque(maxlen=20)
        self.candidate = self.speech = self.reply = None
        self.marks = deque(maxlen=40)
        self.sequence = 0
        self.mic_updated = None
        self.output_underflows = 0

    def now(self):
        return self.clock()

    def microphone(self, probability, rms_dbfs, duration, latency=0):
        """Diagnostic detector: 60 ms onset, 400 ms silence, cleaned RMS >= -45 dBFS."""
        now = self.now() - max(0, min(.5, latency))
        with self.lock:
            self.mic_updated = self.now()
            if probability >= .7 and rms_dbfs >= -45:
                if self.candidate is None:
                    self.candidate = now
                if self.speech is None and now + duration - self.candidate >= .06:
                    self.speech = dict(speech_start=self.candidate, speech_end=None,
                                       last_voice=now + duration, claimed=False)
                    self.segments.append(self.speech)
                if self.speech is not None:
                    self.speech['last_voice'] = now + duration
            else:
                self.candidate = None
                if self.speech is not None and now - self.speech['last_voice'] >= .4:
                    self.speech['speech_end'] = self.speech['last_voice']
                    self.speech = None

    def speech_status(self):
        with self.lock:
            now = self.now()
            offset = self.wall() - now
            latest = self.segments[-1] if self.segments else None
            fresh = self.mic_updated is not None and 0 <= now - self.mic_updated < 2
            pending = bool(latest and not latest['claimed']
                           and now - latest['last_voice'] < 30)
            start, end = (latest['speech_start'], latest['speech_end']) if latest else (None, None)
            return {'available': fresh, 'active': fresh and self.speech is not None,
                    'awaiting_transcript': fresh and pending and self.speech is None,
                    'speech_start': None if start is None else start + offset,
                    'speech_end': None if end is None else end + offset,
                    'output_underflows': self.output_underflows}

    def output_status(self, status):
        if getattr(status, 'output_underflow', False):
            with self.lock:
                self.output_underflows += 1

    def transcript(self, speaker, message_id):
        with self.lock:
            now = self.now()
            turn = next((t for t in self.turns if t.get('user_id') == message_id), None)
            if turn is None:
                # A segment still being spoken ends with the mic block in progress,
                # which can end a moment after now.
                candidates = [s for s in self.segments if not s['claimed'] and -.5 <= now - (
                    s['last_voice'] if s['speech_end'] is None else s['speech_end']) <= 30]
                # Gemini answers a user turn before the next one starts: an older turn
                # still waiting for reply audio has none coming (Gemini stayed silent).
                for older in self.turns:
                    if (older.get('user_id') is not None and older is not self.reply
                            and older.get('reply_audio') is None and not older.get('reply_complete')):
                        older['reply_complete'] = now
                for segment in candidates:
                    segment['claimed'] = True
                segment = candidates[0] if len(candidates) == 1 else None
                turn = self._answering(segment)
                if turn is None:
                    self.sequence += 1
                    turn = dict(id=self.sequence)
                    self.turns.append(turn)
                turn.update(user_id=message_id, transcript_first=now,
                            association='Local speech estimate' if segment else
                            'Ambiguous local speech; times omitted' if candidates else 'No local speech match')
                if segment:
                    turn['_segment'] = segment
            elif turn.get('_segment') and turn.get('reply_audio') is None and not turn.get('reply_complete'):
                # The person went on in the same Gemini turn after a pause that ended a
                # local segment: the turn's speech ends with its last segment.
                for segment in self.segments:
                    if not segment['claimed'] and segment['speech_start'] > turn['_segment']['speech_start']:
                        segment['claimed'] = True
                        turn['_segment_end'] = segment
            turn['transcript_last'] = now

    def _answering(self, segment):
        """The transcript can arrive after the reply's first audio. A reply in progress
        with no user turn yet answers this one when the person began speaking before
        it started (not an unsolicited message of Ember's that they talked over)."""
        reply = self.reply
        if reply is None or 'user_id' in reply or segment is None:
            return None
        first = reply.get('reply_audio', reply.get('reply_text'))
        return reply if first is not None and segment['speech_start'] < first else None

    def _reply(self):
        if self.reply is None:
            now = self.now()
            # The newest turn still waiting: one Gemini left unanswered must not take it.
            self.reply = next((t for t in reversed(self.turns) if not t.get('reply_complete')
                               and t.get('transcript_first') is not None
                               and 0 <= now - t['transcript_first'] <= 60), None)
            if self.reply is None:
                self.sequence += 1
                self.reply = dict(id=self.sequence, association='Reply without matched user turn')
                self.turns.append(self.reply)
        return self.reply

    def received_text(self, at=None):
        with self.lock:
            self._reply().setdefault('reply_text', self.now() if at is None else at)

    def received_audio(self, at=None):
        with self.lock:
            turn = self._reply()
            turn.setdefault('reply_audio', self.now() if at is None else at)
            return turn['id']

    def complete_reply(self, interrupted=False):
        with self.lock:
            if self.reply is not None:
                self.reply.update(reply_complete=self.now(), interrupted=interrupted)
                self.reply = None

    def queued(self, turn_id, ahead_bytes, first_sample):
        with self.lock:
            self.marks.append([ahead_bytes + first_sample * 2, turn_id])

    def played(self, byte_count, dac_delay, rate):
        """First nonzero reply sample's scheduled DAC time, not acoustic measurement."""
        with self.lock:
            now = self.now() + max(0, min(.5, dac_delay))
            while self.marks and self.marks[0][0] < byte_count:
                offset, turn_id = self.marks.popleft()
                turn = next((t for t in self.turns if t['id'] == turn_id), None)
                if turn is not None:
                    turn.setdefault('playback', now + offset / (rate * 2))
            for mark in self.marks:
                mark[0] -= byte_count

    def clear_playback(self):
        with self.lock:
            self.marks.clear()

    def snapshot(self):
        with self.lock:
            offset = self.wall() - self.now()
            rows = []
            for turn in self.turns:
                row = {k: v for k, v in turn.items() if not k.startswith('_')}
                if turn.get('_segment'):
                    row['speech_start'] = turn['_segment']['speech_start']
                    row['speech_end'] = turn.get('_segment_end', turn['_segment'])['speech_end']
                # recognition_ms ends at the transcript's last piece: Gemini streams it
                # while the person speaks, so its first piece comes before speech_end.
                pairs = {'recognition_ms': ('speech_end', 'transcript_last'),
                         'response_ms': ('speech_end', 'reply_audio'),
                         'audible_ms': ('speech_end', 'playback'),
                         'transcript_to_reply_ms': ('transcript_first', 'reply_audio'),
                         'playback_wait_ms': ('reply_audio', 'playback')}
                row['delays'] = {name: round((row[end] - row[start]) * 1000, 1)
                                 for name, (start, end) in pairs.items()
                                 if row.get(start) is not None and row.get(end) is not None
                                 and row[end] >= row[start]}
                for key in TIMES:
                    if row.get(key) is not None:
                        row[key] += offset
                rows.append(row)
            return rows
