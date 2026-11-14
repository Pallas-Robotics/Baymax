"""Who is talking in this conversation, known or not: session voices.

Runs in the voice child. Each utterance long enough to identify (voices.MIN_SECONDS) is
given a session voice: the voice it sounds most like among the ones heard recently, or a
new one ("voice 1", "voice 2", ...). Nothing is stored: session voices live in memory and
are forgotten after SPEAKER_IDLE_S without being heard. Stored voices (ember_people.json)
only ever change through voices.VoiceIdentifier's own rules.

What this gives:
- how many different people have been talking (conversation.ConversationTracker uses it to
  tell people talking to each other from people talking to Ember);
- better identification: one utterance on this robot's far-field mic is a weak sample
  (own voice 0.25-0.55 against a stored voice, others up to 0.28), but the average of
  several utterances of the same session voice is a much clearer one. When a session
  voice's average matches a stored voice "sure", its utterances count as that person's,
  even the short ones that alone only scored "might be".
"""
import numpy as np

SAME_SPEAKER = 0.32       # an utterance joins a session voice whose average it matches this well
# On the Orange Pi 5 Max's USB mic (2026-11-13) one speaker's clips resembled the average of
# the others by 0.43-0.46 and other people's clips scored 0.08-0.28 against it; single clips
# vary more (0.31-0.59 in a simulation matching those numbers). Too high splits one person
# into several voices, too low merges two: a split can't silence Ember (conversation.py
# needs two people evidently present), a merge only makes it more likely to answer.
SAME_SPEAKER_FIRST = 0.25 # ...or, heard only once so far, this well (one clip is a poor average)
MERGE_SPEAKERS = 0.50     # two session voices whose averages agree this well are one person
SPEAKER_IDLE_S = 10 * 60  # a session voice not heard for this long is forgotten
MAX_SPEAKERS = 12         # the oldest is dropped beyond this
GROUP_MIN = 2             # utterances in a session voice before its average identifies it


def _normalize(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


class SessionVoice:
    def __init__(self, number, embedding, now):
        self.number = number
        self.total = np.array(embedding, np.float32)
        self.count = 1
        self.last = now
        self.person_id = None      # set when it is (or its average is) recognised "sure"
        self.name = None

    @property
    def id(self):
        return f"v{self.number}"

    @property
    def label(self):
        return self.name or f"voice {self.number}"

    def centroid(self):
        return _normalize(self.total)

    def add(self, embedding, now):
        self.total = self.total + embedding
        self.count += 1
        self.last = now


class SessionSpeakers:
    def __init__(self):
        self.voices = []
        self.next_number = 1

    def assign(self, embedding, now, person_id=None, name=None, sure=False):
        """The session voice for this utterance's embedding (added to it). `person_id`
        with `sure`: the utterance was recognised as that stored person."""
        embedding = _normalize(embedding)
        self.voices = [v for v in self.voices if now - v.last <= SPEAKER_IDLE_S]
        if sure and person_id:
            # Their own session voice, if one is already theirs.
            voice = next((v for v in self.voices if v.person_id == person_id), None)
            if voice is None:
                voice = self._closest(embedding, unclaimed_or=person_id)
                if voice is not None:
                    voice.person_id, voice.name = person_id, name
        else:
            voice = self._closest(embedding)
        if voice is None:
            voice = SessionVoice(self.next_number, embedding, now)
            self.next_number += 1
            if sure and person_id:
                voice.person_id, voice.name = person_id, name
            self.voices.append(voice)
            if len(self.voices) > MAX_SPEAKERS:
                self.voices.remove(min(self.voices, key=lambda v: v.last))
        else:
            voice.add(embedding, now)
            voice = self._merge(voice)
        return voice

    def _merge(self, voice):
        """One person split over two session voices (their first utterances unlike each
        other): once the averages agree, they become one, the older number kept."""
        for other in list(self.voices):
            if other is voice or (other.person_id and voice.person_id and other.person_id != voice.person_id):
                continue
            if float(other.centroid() @ voice.centroid()) >= MERGE_SPEAKERS:
                keep, gone = (other, voice) if other.number < voice.number else (voice, other)
                keep.total, keep.count = keep.total + gone.total, keep.count + gone.count
                keep.last = max(keep.last, gone.last)
                keep.person_id, keep.name = keep.person_id or gone.person_id, keep.name or gone.name
                self.voices.remove(gone)
                return keep
        return voice

    def _closest(self, embedding, unclaimed_or=None):
        candidates = [v for v in self.voices
                      if unclaimed_or is None or v.person_id in (None, unclaimed_or)]
        if not candidates:
            return None
        scores = [float(v.centroid() @ embedding) - (SAME_SPEAKER if v.count >= 2 else SAME_SPEAKER_FIRST)
                  for v in candidates]
        best = int(np.argmax(scores))
        return candidates[best] if scores[best] >= 0 else None

    def recognise(self, voice, voiceprints, sure_threshold):
        """Match a session voice's average against stored voiceprints [(pid, name, vec)].
        Returns (person_id, name, score) when "sure", else None. A session voice already
        recognised keeps its person unless another matches clearly better."""
        if voice.count < GROUP_MIN or not voiceprints:
            return None
        centroid = voice.centroid()
        scores = [float(vec @ centroid) for _, _, vec in voiceprints]
        best = int(np.argmax(scores))
        if scores[best] < sure_threshold:
            return None
        pid, name, _ = voiceprints[best]
        taken = next((v for v in self.voices if v is not voice and v.person_id == pid), None)
        if taken is not None:
            return None            # another session voice is theirs: don't give them two
        voice.person_id, voice.name = pid, name
        return pid, name, scores[best]

    def active(self, now, within):
        return [v for v in self.voices if now - v.last <= within]
