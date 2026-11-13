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
import re
import threading
import time

import runtime_monitor as monitor
from features.person_recognition import faces, voices
from features.vision_process import Remote, Stopped

FACE_FPS = 2.0                 # PR #8's rate
FACE_LOG_S = 15.0              # a face track's identity is logged when it changes, else this often
ADOPT_SURE_EVERY_S = 60.0      # a voice already recognised "sure" is sampled at most this often
ADOPT_MIN_S = 2.0              # utterances at least this long teach Ember a voice (face in view)
FACE_RESTART_S = 60.0          # a face child that died is started again after this long
OPENER_GAP_S = 15.0            # PR #8's OPENER_SETTLE_SECONDS: one spoken prompt at a time
ARRIVAL_WAIT_S = 60.0          # an arrival not greeted within this long is dropped
# A known person who comes back (face or voice) after this long away is greeted by name.
GREET_AFTER_S = float(os.getenv("BAYMAX_PEOPLE_GREET_AFTER_S", "120"))
faces.AWAY_OTHERS = GREET_AFTER_S
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
identify_speaker. Before you ask anyone their name, or whether you may remember their face
or voice, call who_is_here: you may already know them. Names from who_is_here,
identify_speaker and [ARRIVED] come from your own recognition: use them naturally. Never
guess a name you aren't sure of; ask instead. A "might be" or "not sure" result means
check politely ("Is that you, Sarah?") before assuming; if they say yes, call
remember_person with that name so you recognize them better.
A message starting with [ARRIVED] means someone you know just came back (you saw their face
or heard their voice): greet them warmly by name, briefly. [NEW PERSON] means someone you
don't recognize has been in front of you for a while: greet them, say you're Ember, ask
their name, and ask whether it's okay to remember their face. Only if they clearly say
yes, and only once they have told you their name, call remember_person with that name. If
it says you already remember that face, tell them you know them already. If they say no,
call decline_to_be_remembered and don't ask again (if they later say yes themselves, call
remember_person with changed_mind true).
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


def voice_learning_enabled():
    """Learn a known person's voice from conversation (their face in view). Off with
    BAYMAX_VOICE_LEARNING=0; then only remember_voice ("learn my voice") does."""
    return os.getenv("BAYMAX_VOICE_LEARNING", "1") != "0"


def greetings_enabled():
    return os.getenv("BAYMAX_PEOPLE_GREETINGS", "1") != "0"


PLACEHOLDER_NAMES = {"", "user", "the user", "person", "the person", "someone", "somebody", "unknown",
                     "unknown person", "stranger", "guest", "friend", "visitor", "me", "you", "them",
                     "him", "her", "name", "their name", "your name", "ember", "human", "new person"}


def placeholder_name(name):
    """A name Gemini used without being told one ("User", "the person", ...)."""
    return " ".join(str(name or "").lower().strip(" .,!?'\"").split()) in PLACEHOLDER_NAMES


# Memory notes from earlier conversations assume one user. With several people,
# "The user's name is Nadi" (saved twice on the board, 2026-11-13) made Gemini call
# everyone Nadi, whoever its own recognition said was there.
IDENTITY_MEMORY = re.compile(
    r"\b(?:user|speaker)(?:'s|s)?\s+(?:full\s+|first\s+|real\s+|preferred\s+)?name\s+is\b"
    r"|\b(?:user|speaker)\s+(?:is\s+called|is\s+named|goes\s+by|wants\s+to\s+be\s+called"
    r"|introduced\s+(?:himself|herself|themselves|themself)\s+as)\b", re.I)
MEMORY_NOTE = ("Several people may talk to you, so these notes don't tell you who is talking now: "
               "go by your face and voice recognition (who_is_here, identify_speaker), not by these notes.")
SUMMARY_NOTE = ("Several people may talk to the assistant, and the transcript doesn't say which of them "
                "spoke. Don't write what the user's name is (no \"the user's name is ...\"): the robot "
                "recognizes people by face and voice itself. Facts about a person named in the "
                "conversation are fine.")


def identity_memory(text):
    """A memory line saying who "the user" is: left out while people are recognized."""
    return enabled() and bool(IDENTITY_MEMORY.search(text or ""))


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
    if arrival.get("by") == "voice":
        return (f"[ARRIVED] {label} is here: you recognized their voice. Greet {name} warmly by name; "
                "keep it short and natural, and answer what they said if they asked something.")
    away = describe_away(arrival.get("away"))
    last = (f"You last saw {name} {away}." if away and arrival.get("last_seen")
            else f"This is the first time you've seen {name} since you learned their face.")
    welcome = "Welcome" if arrival.get("is_primary") else "Greet"
    return (f"[ARRIVED] {label} just came into view. {last} {welcome} {name} warmly by name; keep it "
            "short and natural. If they're busy or talking with someone else, a quick hello is enough.")


def learned_message(result):
    return voices.learned_message(result)


def speaker_answer(result, face=None):
    """identify_speaker's reply from the latest voice result (PR #36's speaker_report).
    `face`: the person id of the only face in view, if recognised. A voice that "might be"
    that same person is answered as sure: face and voice agree."""
    if result is not None and result["level"] == "maybe" and face and result.get("person_id") == face:
        return {"speaker": result["name"], "confidence": "sure", "by": "voice and face"}
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
        self._face_log = {}
        self.greeted = {}              # person id -> when Ember last greeted them (face or voice)
        self.heard = {}                # person id -> when their voice was last recognised
        self.adopted_at = {}           # person id -> when a clip of theirs was last offered for learning
        self.voice_arrivals = []

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
                                   on_result=self._voice_result, on_learned=self._voice_learned,
                                   on_adopted=self._voice_adopted).start()
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
                        self._log_faces()
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

    def _log_faces(self):
        """One line per face track when who it is changes, else every FACE_LOG_S: the
        track, who it is (or might be), and its last match score."""
        now = self.clock()
        names = {}
        for track in list(getattr(self.identifier, "_tracks", [])):
            last = track.votes[-1] if track.votes else (None, 0.0)
            pid = track.person_id or track.maybe_id or last[0]
            if pid not in names:
                person = self.identifier.store.get(pid) if pid else None
                names[pid] = person["name"] if person else None
            state = ("known" if track.person_id else "maybe" if track.maybe_id else "unknown",
                     track.person_id or track.maybe_id)
            previous = self._face_log.get(track.id)
            if previous and previous[0] == state and now - previous[1] < FACE_LOG_S:
                continue
            self._face_log[track.id] = (state, now)
            best = f"{names[pid]} {last[1]:.2f}" if names.get(pid) else f"no match {last[1]:.2f}"
            print(f"[FACE] track {track.id}: {state[0]} ({best}, {len(track.votes)} checks)", flush=True)
        live = {t.id for t in getattr(self.identifier, "_tracks", [])}
        self._face_log = {k: v for k, v in self._face_log.items() if k in live}

    def last_present(self, person_id):
        """When this person was last seen (face) or heard (voice), monotonic, or None."""
        seen = getattr(self.identifier, "_last_present", {}).get(person_id)
        heard = self.heard.get(person_id)
        return max((t for t in (seen, heard) if t is not None), default=None)

    def speaker_by_face(self):
        """The person whose voice this must be: the only face in view, recognised, or None."""
        identifier = self.identifier
        if identifier is None:
            return None
        tracks = identifier.in_view()
        if len(tracks) != 1 or not tracks[0].person_id:
            return None
        return tracks[0].person_id

    def _voice_result(self, result):
        log_voice_result(result)
        if (result.get("stage") == "final" and result.get("seconds", 0) >= ADOPT_MIN_S and self.voice is not None
                and voice_learning_enabled()):
            # Learn their voice from conversation: one known face in view, and a voice that
            # is theirs or nobody's Ember knows. The child keeps a clip only if it sounds
            # like their stored voice (or, for a first voice, like their other clips):
            # someone off camera may be the one talking.
            face = self.speaker_by_face()
            now = self.clock()
            if face and (result["level"] == "unknown" or result.get("person_id") == face) and (
                    result["level"] != "sure" or now - self.adopted_at.get(face, -1e9) >= ADOPT_SURE_EVERY_S):
                self.adopted_at[face] = now
                self.voice.adopt(face, result["id"])
        pid = result.get("person_id")
        if result["level"] != "sure" or not pid:
            return
        now = self.clock()
        last = self.last_present(pid)
        self.heard[pid] = now
        if (last is None or now - last >= GREET_AFTER_S) and now - self.greeted.get(pid, -1e9) >= GREET_AFTER_S:
            with self.lock:
                if not any(a["person_id"] == pid for a in self.voice_arrivals):
                    self.voice_arrivals.append({"person_id": pid, "name": result.get("name"), "by": "voice",
                                                "away": None if last is None else now - last,
                                                "last_seen": None, "t": now})

    def _rename_placeholder(self, identifier, new_name):
        """remember_person(name) for a face in view that was saved under a placeholder
        ("User", from before the placeholder check): rename that person."""
        new_name = faces.clean_name(new_name)
        if placeholder_name(new_name) or identifier.find_person(new_name):
            return None
        for track in identifier.in_view():
            person = identifier.store.get(track.person_id) if track.person_id else None
            if person and placeholder_name(person["name"]):
                def change(data):
                    for p in data["people"]:
                        if p["id"] == person["id"]:
                            p["name"] = new_name
                            return True
                    return False
                if identifier.store._update(change):
                    print(f"[FACE] Renamed {person['name']} to {new_name}", flush=True)
                    return {"status": "remembered", "name": new_name,
                            "note": "You already knew this face under a placeholder; it now has their name."}
        return None

    def _voice_adopted(self, result):
        person = self.identifier.store.get(result["person_id"]) if self.identifier else None
        name = person["name"] if person else result["person_id"]
        if result.get("added"):
            what = "first voice saved" if result.get("first") else "voice sample added"
            print(f"[VOICE] Learning {name}'s voice from conversation: {what}", flush=True)
        elif result.get("first"):
            print(f"[VOICE] Learning {name}'s voice from conversation: {result.get('collected', 0)} clips "
                  f"so far{'; ' + result['note'] if result.get('note') else ''}", flush=True)

    def _voice_learned(self, result):
        with self.lock:
            self.learned.append((result, self.clock()))

    # ── Tools (asyncio.to_thread from gemini_tools) ─────────────────────────────
    def execute(self, name, args):
        result = self._execute(name, args)
        shown = {k: v for k, v in result.items() if k in ("status", "name", "speaker", "confidence",
                                                           "in_front_of_you", "error", "reason")}
        print(f"[PEOPLE] tool {name}({(args or {}).get('name', '')}) -> {shown}", flush=True)
        return result

    def _execute(self, name, args):
        identifier = self.identifier
        if identifier is None:
            return {"error": "Recognizing people isn't ready yet. Ask who they are."}
        if name == "remember_person" and not placeholder_name((args or {}).get("name")):
            renamed = self._rename_placeholder(identifier, (args or {}).get("name"))
            if renamed:
                return renamed
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
                    "talking": speaker_answer(speaker, self.speaker_by_face()) if self.voice
                    else "voice recognition is off"}
        if name == "identify_speaker":
            if self.voice is None:
                return {"speaker": None, "in_front_of_you": identifier.describe_in_view(),
                        "note": "Voice recognition is off; only faces are known."}
            return speaker_answer(self.voice.speaker(), self.speaker_by_face())
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
                    with self.lock:
                        arrival = self.voice_arrivals.pop(0) if self.voice_arrivals else None
                if arrival is None:
                    arrival = await asyncio.to_thread(identifier.pending_arrival)
                if arrival is not None and "waiting_since" not in arrival:
                    arrival["waiting_since"] = now
                    if now - self.greeted.get(arrival["person_id"], -1e9) < GREET_AFTER_S:
                        arrival = None          # just greeted (by voice or face): once is enough
                if arrival is not None and now - arrival["waiting_since"] > ARRIVAL_WAIT_S:
                    arrival = None
                if arrival is not None and self._can_speak():
                    self.greeted[arrival["person_id"]] = now
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
