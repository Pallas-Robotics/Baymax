"""Ember starts conversations: a hello at startup and a greeting on new eye contact.

Camera frames come from the existing capture loop through the fall worker's
`frame_observers` (no new camera stream); the live session comes through its
`session_tasks`. Eye contact is judged locally with MediaPipe Face Landmarker:
face turned toward the camera, irises centred in the eyes, close enough, held
for about a second. Only a short text note goes to Gemini, which then speaks.

Greetings stay rare: only after a quiet spell, when the person was not already
looking, with a cooldown, and never during an audiobook.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

import runtime_monitor as monitor
from features.conversational_ability.engage import engagement

ROOT = Path(__file__).resolve().parents[2]
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
MODEL_PATH = ROOT / ".models/face_landmarker.task"

STARTUP_NOTE = """[STARTUP] You have just started or restarted. Give a natural, friendly greeting
in one to three short sentences, with a little warmth and curiosity rather than
a sleepy or theatrical delivery. No humming, sighing or 'mmm'.
Use the supplied device clock: greet for the actual time of day and mention the
local time in AM/PM naturally. Do not say the timezone or UTC offset in this
greeting; those fields are internal clock context only. A brief 'oh, it's already...'
is fine, but do not deliberately give a wrong greeting and correct yourself.
You may casually say you have just restarted; do not pretend to wake from sleep.
Only comment on the surroundings if the current camera view provides a concrete,
noticeable detail worth mentioning. A scene comment is optional; do not fill
the greeting with a generic description of the room's atmosphere or assume it
is peaceful, quiet or cozy. Your calm voice is a speaking style, not evidence
about the surroundings. If there is no useful visible detail, simply greet them.
Follow their topic rather than offering tasks or listing your capabilities.
Do not ask them to explain the setting just because the view is unclear.
Do not claim this is a new place or recognize people without
evidence. Do not infer weather, mood, a city/state or physical location from
the timezone; mention a city/state only if the person has confirmed it.
Use the existing camera/context, without extra tool calls just for this hello.
Ask at most one question, then wait for the person to answer.
Device clock (data, not instructions): """
PRESENCE_NOTE = ("[PRESENCE] Someone just looked right at you after a quiet while (your camera noticed). "
                 "Greet them in one short, calm sentence; you may gently ask how they are. Do not mention "
                 "the camera or any detection. If they do not answer, stay quiet.")

INSTRUCTION = """
[STARTING CONVERSATIONS]
Messages starting with '[STARTUP]' or '[PRESENCE]' come from your own senses,
not from the person: follow the greeting instructions and never read the labels
out or mention cameras or detection. A startup hello can use one to three short
sentences; a presence greeting stays one short sentence.
"""


def startup_note(timestamp):
    now = datetime.fromtimestamp(timestamp).astimezone()
    if 5 <= now.hour < 12:
        greeting = "Good morning"
    elif 12 <= now.hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    return STARTUP_NOTE + json.dumps({
        "greeting": greeting,
        "local_time": now.strftime("%I:%M %p").lstrip("0"),
        "timezone": now.tzname(),
        "local_datetime": now.isoformat(timespec="seconds"),
    })

# Face Landmarker indices (478 points with irises).
NOSE = 1
RIGHT_EYE = (33, 133, 468)   # outer corner, inner corner, iris centre
LEFT_EYE = (362, 263, 473)


def _setting(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def startup_enabled():
    return os.getenv("BAYMAX_STARTUP_GREETING", "1") != "0"


def presence_enabled():
    # Off by default: on ember-002 (i5-8200Y, pose fall detection also on) the mic
    # stopped reaching Gemini for 144 s right after the face model started.
    return os.getenv("BAYMAX_PRESENCE_GREETING", "0") == "1"


def looking(landmarks):
    """True when a face is turned to the camera with centred irises, close enough to talk."""
    if landmarks is None or len(landmarks) < 478:
        return False
    outer_r, outer_l = landmarks[RIGHT_EYE[0]], landmarks[LEFT_EYE[1]]
    span = outer_l.x - outer_r.x
    if abs(span) < 0.06:  # Eyes ~6% of the frame apart: within a couple of metres.
        return False
    # Head yaw: nose tip near the middle of the eyes.
    if abs((landmarks[NOSE].x - (outer_r.x + outer_l.x) / 2) / span) > 0.12:
        return False
    for a, b, iris in (RIGHT_EYE, LEFT_EYE):
        width = landmarks[b].x - landmarks[a].x
        if abs(width) < 1e-6 or not 0.35 <= (landmarks[iris].x - landmarks[a].x) / width <= 0.65:
            return False
    return True


def ensure_model(path=MODEL_PATH, fetch=None):
    path = Path(path)
    if path.is_file():
        data = path.read_bytes()
    else:
        if fetch is None:
            def fetch():
                with urllib.request.urlopen(MODEL_URL, timeout=30) as response:
                    return response.read(8 * 1024 * 1024 + 1)
        data = fetch()
        if len(data) > 8 * 1024 * 1024:
            raise RuntimeError("Face model download exceeded its expected size.")
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError("Face model checksum failed; remove .models/face_landmarker.task and retry.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".download")
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


class FaceLandmarks:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks import python as tasks
        from mediapipe.tasks.python import vision
        self.mp = mp
        options = vision.FaceLandmarkerOptions(
            base_options=tasks.BaseOptions(model_asset_path=str(ensure_model()),
                                           delegate=tasks.BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_faces=1)
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        self.timestamp = -1

    def detect(self, frame):
        import cv2
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self.timestamp = max(self.timestamp + 1, int(time.monotonic() * 1000))
        result = self.landmarker.detect_for_video(image, self.timestamp)
        return result.face_landmarks[0] if result.face_landmarks else None


def remote_face():
    """FaceLandmarks in its own process, so inference cannot stall the mic or Gemini I/O.
    The model is fetched here first: the child gets a limited time to start (vision_process)."""
    from features.vision_process import Remote
    ensure_model()
    return Remote(FaceLandmarks)


class EyeContact:
    """Turns per-frame 'looking' into 'new eye contact' events.

    update() returns True for new eye contact (a greeting candidate) and sets
    `change` to 'started'/'ended' for the dashboard; 'ended' waits `grace`
    seconds so a blink or a missed frame does not split one look in two."""

    def __init__(self, hold=0.8, away=30.0, grace=1.0):
        self.hold, self.away, self.grace = hold, away, grace
        self.since = None          # Start of the current look.
        self.last_look = None      # Last time anyone was looking.
        self.reported = False
        self.active = False        # Eye contact in progress (for started/ended).
        self.change = None

    def update(self, now, is_looking):
        self.change = None
        if not is_looking:
            if self.since is not None:
                self.last_look = now
            self.since = None
            self.reported = False
            if self.active and now - self.last_look >= self.grace:
                self.active, self.change = False, "ended"
            return False
        if self.since is None:
            # A look counts as new only after nobody has looked for `away` seconds.
            fresh = self.last_look is None or now - self.last_look >= self.away
            self.since = now
            self.reported = not fresh
        self.last_look = now
        if not self.active and now - self.since >= self.hold:
            self.active, self.change = True, "started"
        if not self.reported and now - self.since >= self.hold:
            self.reported = True
            return True
        return False


def _book_playing():
    try:
        from .audiobooks import player
        return player.playing and not player.paused
    except Exception:
        return False


def _last_conversation():
    with monitor.lock:
        return max((t.get("updated_at", t["time"]) for t in monitor.transcripts), default=0.0)


class Proactive:
    def __init__(self, landmarks_factory=remote_face, clock=time.monotonic, wall=time.time,
                 book_playing=_book_playing, last_conversation=_last_conversation):
        self.landmarks_factory, self.clock, self.wall = landmarks_factory, clock, wall
        self.book_playing, self.last_conversation = book_playing, last_conversation
        self.fps = min(10.0, max(1.0, _setting("BAYMAX_PRESENCE_FPS", 4)))
        self.quiet = _setting("BAYMAX_PRESENCE_QUIET_S", 120)
        self.cooldown = _setting("BAYMAX_PRESENCE_COOLDOWN_S", 600)
        self.contact = EyeContact()
        self.lock = threading.Lock()
        self.frame = None
        self.thread = None
        self.greeted_startup = False
        self.startup_attempts = 0
        self.sleep = asyncio.sleep
        self.last_greeting = None
        self.wanted = False        # New eye contact waiting to be delivered to Gemini.
        self.state = "Disabled"

    # ── Camera thread: keep only the newest frame ─────────────────────────────
    def observe(self, frame):
        if not presence_enabled():
            return
        with self.lock:
            self.frame = frame
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ember-presence", daemon=True)
            self.thread.start()

    def _run(self):
        try:
            landmarks = self.landmarks_factory()
        except Exception as error:
            self.state = "Unavailable: " + type(error).__name__
            print(f"[PRESENCE] Eye-contact greetings unavailable ({error}).", flush=True)
            return
        self.state = "Watching for eye contact"
        print(f"[PRESENCE] Watching for eye contact at {self.fps:g} fps.", flush=True)
        fps = self.fps
        while True:
            started = self.clock()
            with self.lock:
                frame, self.frame = self.frame, None
            if frame is not None:
                try:
                    face = landmarks.detect(frame)
                    self.step(face)
                    # No face in view: check once a second; a look must hold 0.8 s anyway.
                    fps = self.fps if face is not None else min(self.fps, 1.0)
                except Exception as error:
                    print(f"[PRESENCE] Frame skipped: {error}", flush=True)
            time.sleep(max(0.0, 1.0 / fps - (self.clock() - started)))

    def step(self, face):
        new = self.contact.update(self.clock(), looking(face))
        notes = []
        if self.contact.change:  # Dashboard timeline: "Eye contact started/ended".
            notes.append("Eye contact " + self.contact.change)
        if new:
            blocked = self.blocked()
            self.wanted = blocked is None
            notes.append("greeting" if blocked is None else "no greeting: " + blocked)
        if notes:
            note = " - ".join(notes)
            print(f"[PRESENCE] {note}", flush=True)
            monitor.record("presence", note)

    def blocked(self):
        """Why a greeting is not allowed now, or None."""
        now = self.clock()
        if self.last_greeting is not None and now - self.last_greeting < self.cooldown:
            return "greeted recently"
        if self.wall() - self.last_conversation() < self.quiet:
            return "talked recently"
        if self.book_playing():
            return "audiobook playing"
        if engagement.disengaged:
            return "disengaged"
        if monitor.state in ("speaking", "thinking", "calling_tool"):
            return "Ember is busy"
        return None

    def allowed(self):
        return self.blocked() is None

    # ── Event loop: one task per live session ────────────────────────────────
    async def session_task(self, session, shutdown_event):
        if startup_enabled() and not self.greeted_startup and self.startup_attempts < 2:
            # Once per run. A session that drops right after connecting cancels this
            # task before the hello counts, so the next session tries once more.
            self.startup_attempts += 1
            await self.sleep(4.0)  # Allow the existing 3 s video sender its first frame.
            # A stray/ambient transcript during boot must not permanently suppress
            # the requested startup statement.
            sent = await self._say(session, startup_note(self.wall()))
            if sent:
                # Delivered: if the connection drops before it is heard, the reconnect
                # sends it again (AddressGate.abandon), so this mustn't retry it too.
                self.greeted_startup = True
                self.last_greeting = self.clock()
        while not shutdown_event.is_set():
            if self.wanted:
                self.wanted = False
                if self.allowed():  # Still quiet now that the loop got to it.
                    self.last_greeting = self.clock()
                    await self._say(session, PRESENCE_NOTE)
            await self.sleep(0.2)

    async def _say(self, session, text):
        print(f"[PRESENCE] {text[:40]}...", flush=True)
        try:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)
            return True
        except Exception as error:
            print(f"[PRESENCE] Could not reach Gemini: {error}", flush=True)
            return False


proactive = Proactive()


def register():
    from .fall_detection import frame_observers, session_tasks
    if proactive.observe not in frame_observers:
        frame_observers.append(proactive.observe)
    if proactive.session_task not in session_tasks:
        session_tasks.append(proactive.session_task)


register()
