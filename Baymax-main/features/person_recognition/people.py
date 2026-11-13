"""Who is in front of Ember (face) and who is talking (voice), for the live conversation.

PR #8 (people awareness, face recognition) and PR #36 (voice recognition), ported to the
Orange Pi build so that they cost the conversation nothing:

- Models run in their own processes on the slow cores (face_process, voice_process).
  Ember's process keeps face tracking (small numpy work, FACE_FPS) and copies mic
  blocks into a bounded queue from the mic callback (no waiting).
- Gemini is never sent notes mid-turn (PR #8's [SELF]/[MEMORY CONTEXT] and PR #36's
  [SPEAKER] went out while people talked; Gemini 2.5 ignores such notes, and on the
  board they delayed turns). It asks instead: who_is_here and identify_speaker answer
  from the latest results without waiting on a model. Spoken prompts ([ARRIVED],
  [NEW PERSON], [VOICE LEARNED]) wait until Ember is free to talk
  (everyday.available_to_speak: not speaking/thinking/calling a tool, nobody heard for
  3 s), at most one per OPENER_GAP_S.
- Camera frames come from the existing capture loop (fall_detection.frame_observers),
  mic audio from the existing callback: no extra device streams.

Not ported (yet): per-person conversation memory and forgetting it (person_memory.py),
appearance notes, the dashboard People page, ember_self/ember_identity.md (they replace
the base persona), cloud profile sync (the family app link already syncs the profile).

Enable: BAYMAX_PEOPLE_RECOGNITION=1 (run_mvp.py sets it); BAYMAX_VOICE_RECOGNITION=0 keeps
faces only; BAYMAX_PEOPLE_GREETINGS=0 keeps Ember from starting [ARRIVED]/[NEW PERSON]
conversations by itself.
"""
import asyncio
import functools
import os
import threading
import time

import runtime_monitor as monitor
from features.person_recognition import faces, voices
from features.vision_process import Remote, Stopped

FACE_FPS = 2.0                 # PR #8's rate
FACE_RESTART_S = 60.0          # a face child that died is started again after this long
OPENER_GAP_S = 15.0            # PR #8's OPENER_SETTLE_SECONDS: one spoken prompt at a time
ARRIVAL_WAIT_S = 60.0          # an arrival not greeted within this long is dropped
LEARNED_WAIT_S = 60.0          # same for a [VOICE LEARNED] result

DECLARATIONS = faces.PEOPLE_TOOLS + voices.VOICE_TOOLS + [{
    "name": "who_is_here",
    "description": ("Find out who is in front of your camera and who has been talking, from "
                    "faces and voices you know. Call it when you need to know who you're with."),
}]
NAMES = {d["name"] for d in DECLARATIONS}

INSTRUCTION = """
[RECOGNIZING PEOPLE]
You can recognize people you know by their face (your camera) and by their voice. When
you need to know who is in front of you or who has been talking, call who_is_here. If
someone asks who they are ("who am I?", "do you know who this is?"), call
identify_speaker. Never guess a name you aren't sure of; ask instead. A "might be" or
"not sure" result means check politely ("Is that you, Sarah?") before assuming; if they
say yes, call remember_person with that name so you recognize them better.
A message starting with [ARRIVED] means someone you know just came into view: greet them
warmly by name, briefly. [NEW PERSON] means someone you don't recognize has been in front
of you for a while: greet them, say you're Ember, ask their name, and ask whether it's
okay to remember their face. Only if they clearly say yes, and only once they have told
you their name, call remember_person with that name. If they
say no, call decline_to_be_remembered and don't ask again (if they later say yes
themselves, call remember_person with changed_mind true).
If someone asks you to learn their voice, first ask whether it's okay. Only if they
clearly say yes, call remember_voice with their name, then ask them to talk naturally for
about ten seconds. A message starting with [VOICE LEARNED] tells you how it went: tell
them briefly.
Faces and voices you remember stay on this robot as numbers, never photos or recordings.
If someone asks you to forget them, call forget_person. Don't mention these tools,
scores or the bracketed messages.
"""


def enabled():
    return os.getenv("BAYMAX_PEOPLE_RECOGNITION", "0") == "1"


def voice_enabled():
    return enabled() and os.getenv("BAYMAX_VOICE_RECOGNITION", "1") != "0" and voices.models_present()


def greetings_enabled():
    return os.getenv("BAYMAX_PEOPLE_GREETINGS", "1") != "0"


PLACEHOLDER_NAMES = {"", "user", "the user", "person", "the person", "someone", "somebody", "unknown",
                     "unknown person", "stranger", "guest", "friend", "visitor", "me", "you", "them",
                     "him", "her", "name", "their name", "your name", "ember", "human", "new person"}


def placeholder_name(name):
    """A name Gemini used without being told one ("User", "the person", ...)."""
    return " ".join(str(name or "").lower().strip(" .,!?'\"").split()) in PLACEHOLDER_NAMES


def describe_away(seconds):
    if seconds is None:
        return None
    if seconds < 3600:
        return "a little while ago"
    if seconds < 2 * 3600:
        return "about an hour ago"
    if seconds < 20 * 3600:
        return f"about {round(seconds / 3600)} hours ago"
    if seconds < 36 * 3600:
        return "yesterday"
    return f"{round(seconds / 86400)} days ago"


def arrival_prompt(arrival):
    """PR #8's [ARRIVED] message, without the per-person memories (not ported yet)."""
    name = arrival.get("name") or "them"
    label = arrival.get("label") or name
    away = describe_away(arrival.get("away"))
    last = (f"You last saw {name} {away}." if away and arrival.get("last_seen")
            else f"This is the first time you've seen {name} since you learned their face.")
    welcome = "Welcome" if arrival.get("is_primary") else "Greet"
    return (f"[ARRIVED] {label} just came into view. {last} {welcome} {name} warmly by name; keep it "
            "short and natural. If they're busy or talking with someone else, a quick hello is enough.")


def learned_message(result):
    return voices.learned_message(result)


def speaker_answer(result):
    """identify_speaker's reply from the latest voice result (PR #36's speaker_report)."""
    if result is None:
        return {"speaker": None, "note": "You haven't heard enough of their voice yet. If you need "
                                         "to know who it is, ask them."}
    if result["level"] == "sure":
        return {"speaker": result["name"], "confidence": "sure"}
    if result["level"] == "maybe":
        return {"speaker": result["name"], "confidence": "not sure",
                "note": f"It might be {result['name']}. Check politely before assuming."}
    return {"speaker": None, "note": "You don't recognize this voice. If it matters, ask who they are."}


def log_voice_result(result):
    """One log line per identification, as PR #36 printed (who and timing, never words)."""
    who = result.get("name") if result["level"] == "sure" else (
        f"might be {result.get('name')}" if result["level"] == "maybe" else result["level"])
    print(f"[VOICE] {who} (score {result['score']:.2f}, {result['stage']}, {result['seconds']:.1f} s of speech, "
          f"{result['ms']} ms)", flush=True)


def _voice_remote(sample_rate):
    from features.person_recognition.voice_process import CALL_TIMEOUT_S, VoiceChild
    return Remote(functools.partial(VoiceChild, sample_rate, faces.PEOPLE_PATH), timeout=CALL_TIMEOUT_S)


class People:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.frame = None
        self.frame_id = 0
        self.thread = None
        self.identifier = None         # faces.FaceIdentifier once models are ready
        self.voice = None              # voice_process.VoiceFeed once started
        self.learned = []              # [(result, when)] waiting to be spoken
        self.last_opener = -1e9
        self.sample_rate = 48000

    # ── Camera (the capture loop's thread): store the reference only ───────────
    def observe(self, frame):
        if not enabled():
            return
        self.frame = frame
        self.frame_id += 1
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="people-faces", daemon=True)
            self.thread.start()

    # ── Mic callback (PortAudio's thread): one deque append ───────────────────
    def feed_audio(self, chunk, ember_speaking):
        voice = self.voice
        if voice is not None:
            voice.feed(chunk, ember_speaking)

    # ── Face thread ────────────────────────────────────────────────────────────
    def _run(self):
        try:
            paths = faces.ensure_models()        # first start: ~39 MB from OpenCV Zoo, checked
        except Exception as error:
            print(f"[PEOPLE] Face recognition unavailable (model download: {error})", flush=True)
            return
        store = faces.PeopleStore()
        if voice_enabled():
            from features.person_recognition.voice_process import VoiceFeed
            self.voice = VoiceFeed(functools.partial(_voice_remote, self.sample_rate),
                                   on_result=log_voice_result, on_learned=self._voice_learned).start()
            print("[PEOPLE] Voice recognition started (own process, slow cores)", flush=True)
        elif enabled() and os.getenv("BAYMAX_VOICE_RECOGNITION", "1") != "0":
            print(f"[PEOPLE] Voice recognition off: no models in {voices.MODEL_DIR}", flush=True)
        seen = 0
        while True:
            try:
                from features.person_recognition.face_process import RemoteFaceModels
                models = RemoteFaceModels(paths)
            except Exception as error:
                print(f"[PEOPLE] Face process could not start: {error}", flush=True)
                time.sleep(FACE_RESTART_S)
                continue
            with self.lock:
                if self.identifier is None:
                    self.identifier = faces.FaceIdentifier(models, store)
                    print(f"[PEOPLE] Face recognition ready ({len(store.people())} people known)", flush=True)
                else:
                    self.identifier.models = models
            try:
                while True:
                    started = self.clock()
                    frame, frame_id = self.frame, self.frame_id
                    if frame is not None and frame_id != seen:
                        seen = frame_id
                        self.identifier.process(frame)
                    else:
                        self.identifier.no_new_frame()
                    time.sleep(max(0.0, 1.0 / FACE_FPS - (self.clock() - started)))
            except Stopped as error:
                print(f"[PEOPLE] Face process stopped ({error}); restarting in {FACE_RESTART_S:g} s",
                      flush=True)
            except Exception as error:
                print(f"[PEOPLE] Face recognition error: {type(error).__name__}: {error}", flush=True)
            models.close()
            time.sleep(FACE_RESTART_S)

    def _voice_learned(self, result):
        with self.lock:
            self.learned.append((result, self.clock()))

    # ── Tools (asyncio.to_thread from gemini_tools) ─────────────────────────────
    def execute(self, name, args):
        identifier = self.identifier
        if identifier is None:
            return {"error": "Recognizing people isn't ready yet. Ask who they are."}
        if name in ("remember_person", "remember_voice") and placeholder_name((args or {}).get("name")):
            # Seen on the board: "Can you remember my face?" -> remember_person(name="User")
            # in the same breath as "What is your name?".
            return {"status": "not_saved",
                    "reason": "You don't know their name yet. Ask their name, then call this again "
                              "with the name they tell you."}
        if name == "who_is_here":
            speaker = self.voice.speaker(wait=0.0) if self.voice else None
            return {"in_front_of_you": identifier.describe_in_view(),
                    "visits": identifier.describe_visits(),
                    "talking": speaker_answer(speaker) if self.voice else "voice recognition is off"}
        if name == "identify_speaker":
            if self.voice is None:
                return {"speaker": None, "in_front_of_you": identifier.describe_in_view(),
                        "note": "Voice recognition is off; only faces are known."}
            return speaker_answer(self.voice.speaker())
        if name == "remember_voice":
            if self.voice is None:
                return {"error": "Voice recognition isn't available right now."}
            person_name = str((args or {}).get("name") or "")
            person = identifier.find_person(person_name)
            if person is None:
                return {"status": "not_saved",
                        "reason": f"You don't know {person_name}'s face yet. Learn their face first "
                                  "(with their permission), then their voice."}
            has_voice = bool(person.get(voices.VOICE_KEY))
            in_view = {n.lower() for n in identifier.present_names()}
            if not has_voice and person["name"].lower() not in in_view:
                return {"status": "not_saved",
                        "reason": f"To check it's really {person['name']}, ask them to look at your "
                                  "camera for a few seconds, then try again."}
            self.voice.learn(person["name"])
            return {"status": "listening",
                    "note": "Now ask them to talk naturally for about 10 seconds, e.g. about their day. "
                            "Their voice is saved automatically; a [VOICE LEARNED] message will tell you "
                            "when it's done. Don't call remember_voice again."}
        return identifier.handle_tool_call(name, args or {})

    # ── Spoken prompts, one live session at a time ─────────────────────────────
    def _can_speak(self):
        from features.conversational_ability.engage import engagement
        from features.toolkits.everyday.tools import available_to_speak
        return (greetings_enabled() and not engagement.disengaged
                and self.clock() - self.last_opener >= OPENER_GAP_S and available_to_speak())

    async def session_task(self, session, shutdown_event):
        arrival = None
        while not shutdown_event.is_set():
            await asyncio.sleep(0.5)
            identifier = self.identifier
            if identifier is None:
                continue
            now = self.clock()
            message = None
            with self.lock:
                self.learned = [(r, t) for r, t in self.learned if now - t < LEARNED_WAIT_S]
                if self.learned and self._can_speak():
                    result, _ = self.learned.pop(0)
                    message = learned_message(result)
            if message is None:
                if arrival is None:
                    arrival = await asyncio.to_thread(identifier.pending_arrival)
                    if arrival is not None:
                        arrival["waiting_since"] = now
                if arrival is not None and now - arrival["waiting_since"] > ARRIVAL_WAIT_S:
                    arrival = None
                if arrival is not None and self._can_speak():
                    message, arrival = arrival_prompt(arrival), None
            if message is None and self._can_speak():
                message = await asyncio.to_thread(identifier.pending_introduction)
            if message is None:
                continue
            self.last_opener = self.clock()
            print(f"[PEOPLE] {message[:60]}...", flush=True)
            monitor.record("people", message.split("]")[0] + "]")
            try:
                await session.send_client_content(turns={"role": "user", "parts": [{"text": message}]},
                                                  turn_complete=True)
            except Exception as error:
                print(f"[PEOPLE] Could not reach Gemini: {error}", flush=True)
                return


people = People()


def register():
    from features.toolkits.fall_detection import frame_observers, session_tasks
    if people.observe not in frame_observers:
        frame_observers.append(people.observe)
    if people.session_task not in session_tasks:
        session_tasks.append(people.session_task)


register()
