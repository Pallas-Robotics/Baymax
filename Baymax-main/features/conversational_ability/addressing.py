"""Optional strict local reply gate; disabled by default in the v10 runtime.

Set BAYMAX_ADDRESS_GATE=1 to opt in. By default the existing Gemini session's
proactive audio and instructions decide when to speak, without this transcript
name/timeout veto or an extra classifier request.

Proactive audio and the [WHEN TO SPEAK] prompt only ask Gemini to stay quiet;
it still answers noise and people talking among themselves. This gate decides
on the robot, from Gemini's own input transcription, whether a reply is played:

- the person said Ember's name (or a usual mishearing such as "Amber"; Hindi
  transcribed in Devanagari or Urdu script counts too: एम्बर, एंबर, अम्बर ...,
  ایمبر, امبر), or
- they spoke within BAYMAX_FOLLOW_UP_SECONDS (default 20) of Ember finishing
  speaking, so they are still in a conversation with it, or
- the reply answers one of Ember's own messages ([FALL ALERT], [REMINDER],
  [STARTUP], ...): any send_client_content(turn_complete=True) through the
  watched session, plus the reply after a tool call from an addressed turn.

Transcription can arrive after the first reply audio, so an undecided reply is
held (audio and transcript) and released if the name shows up before the turn
ends; otherwise it is dropped and logged. Gemini is not told a reply was
dropped. Noise tags such as "<noise>" are not speech.

The gate also remembers Ember's own recent messages that reached Gemini. If the
connection drops before the reply to one has finished playing (a fall check-in
still queued for the speaker, say), `abandon` hands it back so the next
connection sends it again. A message with no reply yet counts only within
PROMPT_TTL_S. A message that never reached Gemini is left to its sender's own
retry, so nothing is sent twice. This runs whether or not the gate is enabled.
Once the person speaks, the next reply is theirs and nothing is sent again.

People talking to each other (`side`, from person_recognition.conversation): while
several people take turns talking among themselves and nobody faces Ember, a reply is
held whether or not the gate is enabled, and played only if someone says Ember's
name (or it answers one of Ember's own messages). The 20 s follow-up rule doesn't
apply then. Otherwise this changes nothing: one person talking to Ember is never
held by it.

conversation.only_when_addressed = false in device.toml (or
BAYMAX_ADDRESS_GATE=0) plays every reply again. Nothing is sent anywhere and no
device is opened: the gate only reads the existing transcription.
"""
import os
import re
import threading
import time

PROMPT_TTL_S = 30  # A prompt Gemini never answered must not let a later overheard reply through.
RESEND_WINDOW_S = 120  # Ember's own message is sent again only if a reply to it could still be playing.
# Gemini sometimes ends a turn without replying, then answers in the next one
# (ember-002: "Amber, please response." -> empty turn -> "I'm here." dropped).
# Speech from a turn with no reply still counts for the next reply this long
# after the person's last words, so the name is not forgotten.
CARRY_S = 10
NAMES = r"(?:ember|embers|amber|ambers|emba|embar|emmer|emperor|ember's|amber's)"
NAME = re.compile(rf"\b{NAMES}\b")
WHOLE_NAME = re.compile(rf"{NAMES}")
# Hindi speech is often transcribed in Devanagari: "Ember" as एम्बर/एंबर, the
# "Amber" mishearing as अम्बर/अंबर (also the Hindi word for sky, so it can let an
# overheard reply through, like "amber" in English). \b is not used: Python
# treats the virama (्) and vowel signs as non-word characters, so \bबर\b would
# match inside एम्बर. Any Devanagari character next to the name blocks a match.
DEVANAGARI = "ऀ-ॿ"
HINDI_NAMES = r"(?:एम्बर|एंबर|एमबर|ऍम्बर|ऍंबर|ऐम्बर|ऐंबर|अम्बर|अंबर)"
HINDI_NAME = re.compile(rf"(?<![{DEVANAGARI}]){HINDI_NAMES}(?![{DEVANAGARI}])")
WHOLE_HINDI_NAME = re.compile(HINDI_NAMES)
# Hindi speech also comes back in Urdu script: in the live evals "एम्बर, मैं रसोई में चाय
# बना रही हूँ।" did in 9 of 14 runs, always with the name as ایمبر. Its Amber spelling is
# امبر (like अंबर, also a word for sky). Arabic letters are word characters to Python, so
# \b works here, also before the ، and ۔ that follow the name; a longer word that starts
# with the letters (امبرین, Ambreen) does not match. Arabic's own yeh (ي) is accepted too.
URDU_NAME = re.compile(r"\b(?:ا[یي]مبر|امبر)\b")
NOISE = re.compile(r"<[^>]*>")


def follow_up_seconds():
    try:
        return max(0.0, float(os.getenv("BAYMAX_FOLLOW_UP_SECONDS", "20")))
    except ValueError:
        return 20.0


def names_ember(text):
    # Fragments arrive mid-word and sometimes with a space inside the word
    # ("A mber", "E mperor" on ember-002), so two neighbouring pieces that
    # together make the name count too; "a member" or "the member" do not.
    text = text.lower()
    if NAME.search(text) or HINDI_NAME.search(text) or URDU_NAME.search(text):
        return True
    words = re.findall(r"[a-z']+", text)
    if any(WHOLE_NAME.fullmatch(a + b) for a, b in zip(words, words[1:])):
        return True
    hindi = re.findall(rf"[{DEVANAGARI}]+", text)  # "एम् बर" split by transcription
    return any(WHOLE_HINDI_NAME.fullmatch(a + b) for a, b in zip(hindi, hindi[1:]))


def is_speech(text):
    return bool(re.search(r"\w", NOISE.sub(" ", text)))


class AddressGate:
    def __init__(self, enabled=True, follow_up=None, clock=time.monotonic, log=print, side=None):
        self.enabled = enabled
        self.side = side or (lambda: False)  # () -> people are talking to each other now
        self.follow_up = follow_up_seconds() if follow_up is None else follow_up
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._heard = []          # user transcription since the last reply
        self._heard_at = None     # when that speech started
        self._heard_last = None   # when the latest fragment arrived
        self._carried = False     # _heard is from an earlier turn that had no reply
        self._turn_from = 0       # where this turn's speech starts in _heard
        self._turn_at = None      # when this turn's speech started
        self._prompted_at = None  # when Ember's own message, still unanswered, was sent
        self._sent = []           # [time, args, kwargs, heard_until] of Ember's recent delivered messages
        self._answers_prompt = False  # this reply was let through for that message
        self._reply_started = None    # when the reply answering Ember's messages started
        self._spoke_until = None  # when Ember's last played reply ends
        self._decision = None     # None (undecided) / True (played) for this reply
        self._held = []           # actions held while undecided
        self._held_text = []

    # ── what happened ────────────────────────────────────────────────────────
    def heard(self, text):
        """User transcription fragment. Returns held actions to run now, if released."""
        with self._lock:
            self._expire_carried()
            if is_speech(text):
                self._sent = []  # The conversation moved on: the next reply answers the person.
                if self._heard_at is None:
                    self._heard_at = self.clock()
                if self._turn_at is None:
                    self._turn_at = self.clock()
                self._heard_last = self.clock()
            self._heard.append(text)
            return self._release()

    def prompted(self):
        """Ember's own systems are sending Gemini a message; the next reply answers it."""
        with self._lock:
            self._prompted_at = self.clock()

    def sent(self, args=(), kwargs=None):
        """That message reached Gemini: if the connection drops before its reply has
        played, `abandon` hands it back. Not called for a failed send, which the
        sender retries itself, so nothing is sent twice."""
        with self._lock:
            now = self.clock()
            self._sent = [m for m in self._sent if now - m[0] <= RESEND_WINDOW_S]
            self._sent.append([now, args, kwargs or {}, None])

    def abandon(self, audio_cut_off):
        """The connection dropped and took any reply with it. Returns the (args, kwargs)
        of Ember's own messages to send again: those whose reply was still playing or,
        within PROMPT_TTL_S, hadn't come yet. A fall check-in or family message must not
        be lost with the connection; one already heard isn't repeated."""
        with self._lock:
            now = self.clock()
            unanswered = self._prompt_waiting()
            sent, self._sent = self._sent, []
        self.end(interrupted=True)
        if not (audio_cut_off or unanswered):
            return []
        return [(args, kwargs) for at, args, kwargs, heard_until in sent
                if now - at <= RESEND_WINDOW_S and (heard_until is None or heard_until > now)]

    def tool_call(self):
        """A tool call from an addressed turn keeps the reply after its result."""
        with self._lock:
            self._expire_carried()
            if self._decision is True or self._addressed():
                self._prompted_at = self.clock()

    def spoke(self, until):
        """Played reply audio is buffered until `until` (clock time)."""
        with self._lock:
            self._spoke_until = max(self._spoke_until or until, until)

    # ── the decision ─────────────────────────────────────────────────────────
    def _clear_heard(self):
        self._heard, self._heard_at, self._heard_last, self._carried = [], None, None, False
        self._turn_from = 0

    def _expire_carried(self):
        # Caller holds the lock. Speech kept from a turn with no reply lapses after CARRY_S.
        if self._carried and (self._heard_last is None or self.clock() - self._heard_last > CARRY_S):
            self._clear_heard()

    def _release(self):
        # Caller holds the lock. Held actions to run if an undecided reply is now allowed.
        if self._decision is None and self._held and self._addressed(prompt=False):
            self._decision = True
            held, self._held, self._held_text = self._held, [], []
            return held
        return []

    def _prompt_waiting(self):
        return self._prompted_at is not None and self.clock() - self._prompted_at <= PROMPT_TTL_S

    def prompt_pending(self):
        """One of Ember's own messages (or a tool result from an addressed turn) is
        still waiting for its reply, within PROMPT_TTL_S: a note sent to Gemini now
        could cut that reply off."""
        with self._lock:
            return self._prompt_waiting()

    def _side(self):
        try:
            return bool(self.side())
        except Exception:
            return False             # never let this check stop Ember from answering

    def _addressed(self, prompt=True):
        # A prompt counts only when a reply starts: one sent mid-reply is
        # answered by the next reply, not the one being held.
        if prompt and self._prompt_waiting():
            return True
        side = self._side()
        if not self.enabled and not side:
            return True
        heard = "".join(self._heard)
        if names_ember(heard):
            return True
        if side:
            return False             # people talking to each other: only the name
        if not is_speech(heard) or self._spoke_until is None:
            return False
        started = self._heard_at if self._heard_at is not None else self.clock()
        return started - self._spoke_until <= self.follow_up

    def reply(self, action, text=None):
        """Run `action` now if this reply may be heard, else hold it. Returns True if run."""
        with self._lock:
            self._expire_carried()
            if self._decision is None and self._addressed(prompt=not self._held):
                self._decision = True
                self._answers_prompt = not self._held and self._prompt_waiting()
                if self._answers_prompt:
                    self._reply_started = self.clock()
            if self._decision is True:
                ready, self._held, self._held_text = self._held + [action], [], []
            else:
                self._held.append(action)
                if text:
                    self._held_text.append(text)
                ready = []
        for queued in ready:
            queued()
        return bool(ready)

    def end(self, interrupted=False):
        """The reply is over (turn_complete or interrupted)."""
        with self._lock:
            dropped = self._decision is not True and bool(self._held)
            replied = self._decision is True or bool(self._held)
            text = " ".join("".join(self._held_text).split())
            if self._answers_prompt and not interrupted:
                self._prompted_at = None
                # Messages sent before this reply started are answered once it has played.
                heard_at = self._spoke_until or self.clock()
                for message in self._sent:
                    if message[3] is None and message[0] <= self._reply_started:
                        message[3] = heard_at
            self._answers_prompt = False
            if replied:
                self._clear_heard()
            else:
                # No reply this turn: keep what was said for the next one (CARRY_S). When this
                # turn had speech, only its words: overheard talk must not pile up over many
                # quiet turns (a turn with no new speech keeps what was carried).
                if self._turn_from and self._turn_at is not None:
                    self._heard, self._heard_at = self._heard[self._turn_from:], self._turn_at
                self._carried = bool(self._heard)
                self._expire_carried()
            self._turn_from, self._turn_at = len(self._heard), None
            self._decision, self._held, self._held_text = None, [], []
        if dropped:
            why = " (people talking to each other)" if self._side() else ""
            self.log(f"[ADDRESS] Not spoken to{why}; dropped reply: {text or '(audio only)'}", flush=True)

    # ── session hook ─────────────────────────────────────────────────────────
    def watch(self, session):
        return _WatchedSession(session, self)


class _WatchedSession:
    """The live session, noting each complete message Ember's systems send."""

    def __init__(self, session, gate):
        self._session = session
        self._gate = gate

    async def send_client_content(self, *args, **kwargs):
        # Noted before sending: the reply must never arrive before the note.
        complete = kwargs.get("turn_complete", True)
        if complete:
            self._gate.prompted()
        result = await self._session.send_client_content(*args, **kwargs)
        if complete:
            self._gate.sent(args, kwargs)
        return result

    def __getattr__(self, name):
        return getattr(self._session, name)
