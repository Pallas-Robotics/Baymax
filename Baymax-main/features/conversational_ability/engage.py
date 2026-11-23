"""'Ember disengage' / 'Ember engage': put the conversation to sleep and wake it by voice.

While engaged nothing changes: Ember answers without needing its name. When the
user's Gemini transcript says "Ember disengage", the mic is withheld from Gemini
(it gets silence, exactly as during an audiobook) and the existing echo-cancelled
mic audio goes only to the offline Vosk listener, which wakes on a confident
"ember engage". Saying "Ember" alone, or anything else, reaches no one.

A reminder or a possible-fall check-in re-engages Ember so the person can answer.
Greetings stay quiet. The state lives in this process: reconnects keep it, a
restart starts engaged. BAYMAX_ENGAGE_WORDS=0 turns the feature off.
"""
import asyncio
import copy
import os
import re
import threading
import time

import runtime_monitor as monitor
from . import processing

MARKER = "[ENGAGE AND DISENGAGE]"
INSTRUCTION = """

[ENGAGE AND DISENGAGE]
If someone says "Ember disengage", you stop hearing them until they say "Ember engage".
Reply only with a very short, warm goodbye such as "Okay, I'll be quiet." Do not ask a
question and do not say the words "Ember engage" yourself. When an [ENGAGED] note
arrives they said "Ember engage": say a short hello such as "I'm here." and listen.
While engaged they do not need to say "Ember" to talk to you.
"""
ENGAGED_NOTE = ("[ENGAGED] The person said 'Ember engage', so you can hear them again. "
                "Say a short hello such as \"I'm here.\" and listen.")
UNAVAILABLE_NOTE = ("[ENGAGE AND DISENGAGE] Disengage is not available on this robot, so you are "
                    "still listening. Tell them briefly; they can turn you off at the power switch instead.")
# Gemini's transcript of "Ember disengage" ("Ember, disengage.", "amber dis-engage").
DISENGAGE = re.compile(r"\b(?:ember|amber)\b\W*\bdis\W?engage", re.IGNORECASE)
VOCABULARY = ["ember", "engage", "disengage"]  # "disengage" so it is never heard as "engage".
MIN_CONF = 0.9
WAKE_START_TIMEOUT_S = 5.0


def enabled():
    return os.getenv("BAYMAX_ENGAGE_WORDS", "1") != "0"


def apply(config):
    """Add the engage/disengage section once to the MVP persona; leave other prompts alone."""
    from features.persona import PERSONA
    instruction = config.get("system_instruction", "")
    if not enabled() or PERSONA not in instruction or MARKER in instruction:
        return config
    result = copy.deepcopy(config)
    result["system_instruction"] = instruction + INSTRUCTION
    return result


def heard(result, final, level=None, previous=None):
    """'ember engage' when a final Vosk result has the pair with confident words, else None."""
    if not final:
        return None
    words = [(w.get("word"), w.get("conf", 0)) for w in result.get("result", [])]
    for (first, c1), (second, c2) in zip(words, words[1:]):
        if (first, second) == ("ember", "engage") and min(c1, c2) >= MIN_CONF:
            return "ember engage"
    before = (previous or {}).get("result", [])
    after = result.get("result", [])
    if before and after:
        first, second = before[-1], after[0]
        if (first.get("word"), second.get("word")) == ("ember", "engage") \
                and min(first.get("conf", 0), second.get("conf", 0)) >= MIN_CONF \
                and "end" in first and "start" in second \
                and 0 <= second["start"] - first["end"] <= 3.0:
            return "ember engage"
    return None


def _listener(on_wake):
    from features.toolkits import wakeword
    return wakeword.WakeListener(on_wake, recognizer_factory=lambda: wakeword._vosk_recognizer(VOCABULARY),
                                 rules=heard, listening="Disengaged: listening only for 'Ember engage'.",
                                 unavailable="Ember engages again")


def audio_ready():
    processor = processing._active_enhancer
    return processing.enabled() and processor is not None and not processor.failed


class Engagement:
    def __init__(self, listener_factory=_listener, audio_ready=audio_ready):
        self.listener_factory, self.audio_ready = listener_factory, audio_ready
        self.clock = time.monotonic
        self.listener_started_at = None
        self.lock = threading.Lock()
        self.disengaged = False
        self.listener = None
        self.handled = None   # Transcript id that already disengaged (its later fragments repeat it).
        self.session = None
        self.loop = None

    # ── Gemini receive loop: the user's transcript ────────────────────────────
    def observe(self, speaker, message):
        if speaker != "user" or not enabled() or self.disengaged or message["id"] == self.handled:
            return
        if DISENGAGE.search(message["text"]):
            self.handled = message["id"]
            self.disengage()

    def disengage(self):
        if not self.audio_ready():
            # Without audio cleanup there is no mic gate, so Ember would keep hearing everything.
            self.tell(UNAVAILABLE_NOTE)
            return False
        with self.lock:
            if self.disengaged:
                return False
            self.disengaged = True
            if self.listener is None:
                self.listener = self.listener_factory(self.wake)
            listener = self.listener
        listener.clear()
        self.listener_started_at = self.clock()
        listener.start()  # Loads the wake-word model on first use; the mic stays withheld meanwhile.
        print("[ENGAGE] Disengaged; only 'Ember engage' is heard.", flush=True)
        monitor.record("engagement", "Disengaged by voice; only 'Ember engage' is heard")
        monitor.notify("disengaged")
        return True

    def engage(self, reason, note=None):
        """Hear the person again. Returns True if Ember was disengaged."""
        with self.lock:
            if not self.disengaged:
                return False
            self.disengaged = False
            listener = self.listener
        if listener is not None:
            listener.clear()
        print(f"[ENGAGE] Engaged ({reason}).", flush=True)
        monitor.record("engagement", f"Engaged ({reason})")
        monitor.notify("engaged")
        if note:
            self.tell(note)
        return True

    def wake(self, phrase):
        self.engage(f"heard '{phrase}'", ENGAGED_NOTE)

    # ── Capture thread ────────────────────────────────────────────────────────
    def gate(self, samples):
        """True while disengaged: Gemini gets silence, the engage listener gets the audio."""
        if not self.disengaged:
            return False
        listener = self.listener
        if listener is not None and listener.failed:
            # No Vosk model or library: never leave the person unable to wake Ember.
            self.engage("wake words unavailable", UNAVAILABLE_NOTE)
            return False
        if listener is not None and not listener.ready and self.listener_started_at is not None \
                and self.clock() - self.listener_started_at >= WAKE_START_TIMEOUT_S:
            self.engage("wake listener startup timed out", UNAVAILABLE_NOTE)
            return False
        if listener is not None:
            listener.feed(samples)  # Dropped until the model has loaded.
        return True

    # ── Live session, for the [ENGAGED] note ──────────────────────────────────
    async def session_task(self, session, shutdown_event):
        self.session, self.loop = session, asyncio.get_running_loop()

    def tell(self, text):
        session, loop = self.session, self.loop
        if session is None or loop is None or loop.is_closed():
            return

        async def send():
            try:
                await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]},
                                                  turn_complete=True)
            except Exception as error:
                print(f"[ENGAGE] Could not tell Gemini: {error}", flush=True)

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(send()))


engagement = Engagement()


def register(gates=None, listeners=None, tasks=None):
    """Gate the mic (ahead of the audiobook gate), read user transcripts, keep the live session."""
    if gates is None:
        gates = processing.input_gates
    if listeners is None:
        listeners = monitor.transcript_listeners
    if tasks is None:
        from features.toolkits.fall_detection import session_tasks as tasks
    if engagement.gate not in gates:
        gates.insert(0, engagement.gate)
    if engagement.observe not in listeners:
        listeners.append(engagement.observe)
    if engagement.session_task not in tasks:
        tasks.append(engagement.session_task)


register()
