"""Turn taking: is someone talking to Ember, or are people talking to each other?

Gemini hears everything in the room and, told to stay quiet when people talk among
themselves ([WHEN TO SPEAK]), still often answers them. This keeps track, on the robot, of
who has been talking and in what order, from the voice child's turns (speakers.py: a
session voice for every utterance, known or not), Ember's own speech (the mic callback's
"Ember is speaking" flag) and the faces in view (who is facing the camera).

People are talking to each other when, since Ember last spoke, at least two different
voices have taken turns, at least two people are evidently here (two faces in view, or two
known people heard), and the latest speaker isn't facing Ember. Then Ember's reply is
held back unless someone says its name (addressing.AddressGate's `side` check) and Ember
doesn't start conversations of its own ([ARRIVED], [NEW PERSON]). It ends as soon as Ember
speaks again (named, or one of its own messages), or someone turns to it to talk.

One person alone never counts as a side conversation, even if their voice is split into
two session voices: that needs two people evidently present. A turn too short to identify
(under 1 s) counts as a turn but not as a different voice. Nothing is stored or sent:
turns live in memory for WINDOW_S.
"""
import collections
import threading
import time

WINDOW_S = 90.0           # voices heard this recently are "in the conversation"
ESTABLISHED_TURNS = 2     # a session voice heard this often is a real voice, not a stray split
FACING_LEAD_S = 1.0       # facing the camera this long before an utterance started still counts
PRESENT_S = 30.0          # two faces seen together this recently: two people are here
MAX_TURNS = 60


class ConversationTracker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.turns = collections.deque(maxlen=MAX_TURNS)
        self.ember_last = -1e9         # when Ember was last audible (mic callback)
        self.two_faces_at = -1e9       # when two or more faces were last in view together
        self.side_since = None         # when the current side conversation was noticed

    # ── What happened ────────────────────────────────────────────────────────
    def ember_audible(self, now):
        """Ember is speaking (called from the mic callback: one assignment)."""
        self.ember_last = now

    def faces(self, count, now):
        if count >= 2:
            self.two_faces_at = now

    def add_turn(self, turn, facing=None):
        """A finished human utterance from the voice child: {"start", "end", "speech_s",
        "speaker" (session voice id or None), "person_id", "name"}. `facing`: whether the
        speaker was facing the camera (None when it can't be told whose face it is).
        Returns the state if it changed between "with Ember" and "to each other"."""
        turn = dict(turn, facing=facing)
        with self.lock:
            self.turns.append(turn)
            before = self.side_since is not None
            state = self._state(self.clock())
            self.side_since = (self.side_since or self.clock()) if state["side"] else None
        return state if state["side"] != before else None

    # ── What it means ────────────────────────────────────────────────────────
    def state(self, now=None):
        with self.lock:
            return self._state(self.clock() if now is None else now)

    def side_conversation(self):
        return self.state()["side"]

    def _state(self, now):
        recent = [t for t in self.turns if now - t["end"] <= WINDOW_S]
        counts = collections.Counter(t["speaker"] for t in recent if t["speaker"])
        known = {t["person_id"]: t["name"] for t in recent if t.get("person_id")}
        established = {s for s, n in counts.items() if n >= ESTABLISHED_TURNS} | {
            t["speaker"] for t in recent if t.get("person_id") and t["speaker"]}
        since = [t for t in recent if t["start"] > self.ember_last]
        voices_since = {t["speaker"] for t in since if t["speaker"] in established}
        present = now - self.two_faces_at <= PRESENT_S or len(known) >= 2
        latest = since[-1] if since else None
        facing = bool(latest and latest.get("facing"))
        side = len(voices_since) >= 2 and present and not facing
        labels = {}
        for t in recent:
            if t["speaker"]:
                labels[t["speaker"]] = t.get("name") or labels.get(t["speaker"])
        return {"side": side, "voices": len(established) or len(counts), "known": sorted(known.values()),
                "unknown_voices": sum(1 for s in (established or counts) if not labels.get(s)),
                "turns_since_ember": len(since), "voices_since_ember": len(voices_since),
                "facing": facing, "present": present}

    def describe(self, now=None):
        """For who_is_here: how many people have been talking, and whether to each other."""
        state = self.state(now)
        if not state["voices"]:
            return "Nobody has been talking in the last minute and a half."
        names = list(state["known"])
        if state["unknown_voices"]:
            names.append(f"{state['unknown_voices']} voice{'s' if state['unknown_voices'] > 1 else ''} "
                         "you don't know")
        who = ", ".join(names) if names else "someone"
        text = (f"{state['voices']} {'person has' if state['voices'] == 1 else 'people have'} "
                f"been talking in the last minute and a half ({who}).")
        if state["side"]:
            text += (" Right now they are talking to each other, not to you: stay out of it "
                     "unless someone says your name or turns to you.")
        return text

    def log_line(self, state):
        if state["side"]:
            who = ", ".join(state["known"]) or "people"
            return (f"[GROUP] {state['voices_since_ember']} voices taking turns ({who}), nobody facing Ember: "
                    "people talking to each other; Ember waits for its name")
        return "[GROUP] Someone is talking to Ember again"
