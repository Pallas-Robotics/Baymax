"""
Face recognition — Ember knows who is in front of it, and asks new people
who they are (with permission) before remembering them.

Models (OpenCV Zoo, downloaded once into .models/faces/ and hash-checked):
  YuNet  face detector    — MIT license
  SFace  face recognizer  — Apache-2.0 license, 128-d embeddings
Both run through OpenCV's built-in cv2.FaceDetectorYN / cv2.FaceRecognizerSF,
so no extra packages are needed.

Thresholds were calibrated on the LFW benchmark (2,771 images, 150 enrolled
people, 1,500 strangers, 5 samples per person): cosine >= 0.50 recognised
98.5% of probes with 0% misidentification and 0.07% of strangers wrongly
named. 0.40–0.50 is treated as "might be" — Ember asks before assuming.
Real home cameras (dim light, angles, distance) score lower than LFW, which is
why Ember keeps learning a known person's face from confident sightings.

Privacy: face embeddings (not photos) are stored only on the robot in
ember_people.json. Nobody is remembered without consent — either the family
adds photos in the app, or the person says yes when Ember asks. Strangers who
decline are remembered only in memory, for this visit, so Ember doesn't ask
again, and are never written to disk. When someone Ember knows arrives, a
head-and-shoulders crop is sent to Gemini (which already receives the camera
feed) to describe their appearance; only that text description is kept.
"""
import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field

import numpy as np

# Ported from PR #8 (face_id.py). On this build the models live with the other
# downloaded models in Baymax-main/.models/faces (ignored by Git) and the people file
# with the robot's other runtime data in BAYMAX_DATA_DIR.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.path.join(ROOT, ".models", "faces")
PEOPLE_PATH = os.path.join(os.getenv("BAYMAX_DATA_DIR") or ROOT, "ember_people.json")

MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}

# ─── Tuning (see calibration notes above) ───────────────────────────────────
MATCH_THRESHOLD = 0.50        # confident: "Margaret is here"
MAYBE_THRESHOLD = 0.40        # "might be Margaret" — ask before assuming
DETECTION_SCORE = 0.80
MIN_FACE_PX = 40              # smaller faces are too blurry to recognise
LEARN_FACE_PX = 64            # minimum face size to store as a sample
MAX_SAMPLES = 20              # per person; the most redundant sample is dropped
AUTO_LEARN_RANGE = (0.55, 0.85)   # confident but not a near-duplicate
AUTO_LEARN_INTERVAL = 15 * 60 # seconds between learned samples per person (each is a file write)
CONFIRM_VOTES = 2             # confident frames needed before naming someone
IN_VIEW_SECONDS = 2.0         # a face seen this recently is "in front of you"
TRACK_EXPIRY = 10.0           # forget a track this long after it leaves
INTRO_AFTER = 4.0             # an unknown face must stay this long before Ember asks
INTRO_MIN_SAMPLES = 3
INTRO_COOLDOWN = 90.0         # seconds between "who are you?" questions
VISIT_MEMORY = 30 * 60        # RAM-only memory of strangers asked this visit
SAME_VISITOR = 0.45           # similarity to treat a returning stranger as the same one
# A clear, front-on face only continues a track if it resembles that track's
# face — otherwise someone who steps into the spot another person just left
# would inherit their name. (LFW: 0.44% same-person splits, 0.88% merges.)
TRACK_CONTINUITY = 0.30
LAST_SEEN_SAVE_INTERVAL = 60.0   # Orange Pi port: Ember restarts often; was 600
# A new sample must resemble the person's stored face (best cosine >= this),
# so one wrong sample can't creep in; and claiming an existing name requires
# actually looking like that person.
SAMPLE_CONSISTENCY = 0.35
MAX_NAME = 60
# Keeping the load low (identifying a face is the expensive step):
DETECT_SCALE = 0.5            # find faces on a half-size copy (~3x cheaper); landmarks
                              # are then refined at full size before identifying
REVERIFY_SECONDS = 3.0        # someone already identified who stays put is re-checked
                              # this often instead of every frame
STEADY_IOU = 0.6              # "stays put": the face box overlaps last frame's this much
STEADY_GAP = 1.0              # …and was seen in the previous frame (seconds)
STEADY_LOOK = 0.85            # …and a 16x16 thumbnail of it still looks the same: a
                              # different person in the same spot scores < 0.79 (LFW),
                              # the same face frame to frame ~0.97
DOWNLOAD_TIMEOUT = 30         # seconds without data before a model download gives up
AWAY_PRIMARY = 3 * 3600       # greet the person Ember lives with after this long apart
AWAY_OTHERS = 2 * 60          # greet anyone else Ember knows after this long apart (PR #8: 20 min;
                              # people.GREET_AFTER_S sets it, BAYMAX_PEOPLE_GREET_AFTER_S)
GREET_COOLDOWN = 30.0         # seconds between greetings
ARRIVAL_MAX_AGE = 60.0        # a greeting not given within this long is dropped
VISIT_GAP = 10 * 60           # away this long = a new visit (for "here for ..." / "just arrived")
CROP_REFRESH = 5.0            # seconds between fresh head-and-shoulders crops per person


# ─── Gemini tools and instructions ──────────────────────────────────────────
PEOPLE_TOOLS = [
    {
        "name": "remember_person",
        "description": (
            "Remember the face of the person you just asked about, so you recognise "
            "them next time. ONLY call this after they clearly said yes to you "
            "remembering their face. Also call it when someone confirms they are a "
            "person you only thought they 'might be'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Their name, as they said it"},
                "relationship": {
                    "type": "STRING",
                    "description": "How they know the person you care for, if they said "
                                   "(e.g. 'neighbor', 'nurse')",
                },
                "changed_mind": {
                    "type": "BOOLEAN",
                    "description": "True only if this person said no earlier and has now "
                                   "clearly said yes themselves",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "decline_to_be_remembered",
        "description": "The person you asked does not want their face remembered.",
    },
    {
        "name": "forget_person",
        "description": (
            "Forget a person: their face and everything you remember about them. Only "
            "when that person asks you to forget them, or a family member asks."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"name": {"type": "STRING", "description": "Who to forget"}},
            "required": ["name"],
        },
    },
]

PEOPLE_INSTRUCTIONS = (
    "# Recognizing people\n"
    "You recognize people by their face. [SELF] updates tell you who is in front "
    "of you — trust them. If one says someone \"might be\" a person, check "
    "politely (\"Is that you, Sarah?\") before assuming, and if they confirm, "
    "call remember_person with that name.\n"
    "A message starting with \"[NEW PERSON]\" means someone you don't recognize "
    "is in front of you. Greet them warmly, say you're Ember, and ask their name. "
    "Then ask whether it's okay for you to remember their face so you'll know "
    "them next time. Only if they clearly say yes, call remember_person. If they "
    "say no or seem unsure, call decline_to_be_remembered and don't ask again. If "
    "someone who said no later clearly says yes themselves, call remember_person "
    "with changed_mind true.\n"
    "Never remember a face without permission. Faces you remember stay on this "
    "robot only. If someone asks you to forget them, call forget_person — it "
    "removes their face and what you remember about them.\n"
    "A message starting with \"[ARRIVED]\" means someone you know just came into "
    "view after being away: greet them by name and, where it fits, pick up from "
    "what you talked about last time."
)


# ─── Models ──────────────────────────────────────────────────────────────────
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_models(model_dir=None):
    """Download the models if missing and verify their hashes.
    Returns {"detector": path, "recognizer": path}. Raises on failure."""
    model_dir = model_dir or MODEL_DIR
    os.makedirs(model_dir, exist_ok=True)
    paths = {}
    for key, (filename, url, sha) in MODELS.items():
        path = os.path.join(model_dir, filename)
        if not os.path.exists(path) or _sha256(path) != sha:
            print(f"[FACE] Downloading {filename}...")
            tmp = path + ".part"
            # A timeout so a stalled connection can't hang startup forever
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as res, open(tmp, "wb") as f:
                shutil.copyfileobj(res, f)
            if _sha256(tmp) != sha:
                os.remove(tmp)
                raise RuntimeError(f"{filename} failed its integrity check")
            os.replace(tmp, path)
        paths[key] = path
    return paths


@dataclass
class Detection:
    box: tuple          # x, y, w, h in pixels
    landmarks: np.ndarray   # 5x2: right eye, left eye, nose, mouth right, mouth left
    score: float
    row: np.ndarray     # raw YuNet row, needed for alignment

    @property
    def size(self):
        return min(self.box[2], self.box[3])

    def frontal(self):
        """Nose between the eyes and eyes roughly level — a usable, front-on face."""
        (rx, ry), (lx, ly), nx = self.landmarks[0], self.landmarks[1], self.landmarks[2][0]
        eye_dist = abs(lx - rx)
        if eye_dist < 1:
            return False
        return (min(rx, lx) < nx < max(rx, lx)) and abs(ly - ry) < 0.35 * eye_dist


class FaceModels:
    """Thin wrapper over OpenCV's YuNet detector and SFace recognizer."""

    def __init__(self, paths=None):
        import cv2
        paths = paths or ensure_models()
        self._cv2 = cv2
        self._detector = cv2.FaceDetectorYN.create(paths["detector"], "", (320, 320),
                                                   DETECTION_SCORE, 0.3, 50)
        self._recognizer = cv2.FaceRecognizerSF.create(paths["recognizer"], "")
        self._lock = threading.Lock()   # the OpenCV objects aren't thread-safe

    def detect(self, frame, scale=1.0):
        """Faces in `frame`. With scale < 1 the detector runs on a smaller copy
        (much cheaper); positions are still given in `frame`'s pixels."""
        if scale != 1.0:
            frame = self._cv2.resize(frame, None, fx=scale, fy=scale,
                                     interpolation=self._cv2.INTER_AREA)
        h, w = frame.shape[:2]
        with self._lock:
            self._detector.setInputSize((w, h))
            _, rows = self._detector.detect(frame)
        if rows is None:
            return []
        out = []
        for r in rows:
            r = r.copy()
            r[:14] /= scale                    # box + landmarks back to full-size pixels
            out.append(Detection(box=tuple(int(v) for v in r[:4]),
                                 landmarks=r[4:14].reshape(5, 2), score=float(r[14]), row=r))
        return out

    def refine(self, frame, det):
        """Exact landmarks for a face found on the half-size copy: run the
        detector on a full-size crop around it (a few ms). Identifying from
        refined landmarks is as accurate as detecting at full size."""
        x, y, w, h = det.box
        H, W = frame.shape[:2]
        x0, y0 = max(0, int(x - w)), max(0, int(y - h))
        x1, y1 = min(W, int(x + 2 * w)), min(H, int(y + 2 * h))
        if x1 - x0 < 16 or y1 - y0 < 16:
            return det
        found = self.detect(frame[y0:y1, x0:x1])
        best = max(found, key=lambda d: _iou((d.box[0] + x0, d.box[1] + y0, d.box[2], d.box[3]),
                                             det.box), default=None)
        if best is None or _iou((best.box[0] + x0, best.box[1] + y0, best.box[2], best.box[3]),
                                det.box) < 0.3:
            return det
        r = best.row.copy()                    # x, y, w, h, 5 landmarks (x, y), score
        r[[0, 4, 6, 8, 10, 12]] += x0          # positions move with the crop; sizes don't
        r[[1, 5, 7, 9, 11, 13]] += y0
        return Detection(box=tuple(int(v) for v in r[:4]), landmarks=r[4:14].reshape(5, 2),
                         score=float(r[14]), row=r)

    def fingerprint(self, frame, det):
        """A 16x16 grey thumbnail of the face (unit vector), to notice — almost
        for free — when the face in a spot is no longer the same one."""
        x, y, w, h = det.box
        crop = frame[max(0, y):max(0, y + h), max(0, x):max(0, x + w)]
        if crop.size == 0:
            return None
        g = self._cv2.resize(self._cv2.cvtColor(crop, self._cv2.COLOR_BGR2GRAY), (16, 16),
                             interpolation=self._cv2.INTER_AREA).astype(np.float32).ravel()
        g -= g.mean()
        n = np.linalg.norm(g)
        return g / n if n > 1e-6 else None

    def embed(self, frame, det):
        with self._lock:
            aligned = self._recognizer.alignCrop(frame, det.row)
            feat = self._recognizer.feature(aligned).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)


# ─── People store (shared with the dashboard) ───────────────────────────────
class PeopleStore:
    """ember_people.json: remembered people and their face embeddings.
    Writers (AI core, dashboard) take an exclusive file lock for every
    read-modify-write; readers pick up changes by checking the file's mtime."""

    def __init__(self, path=None, clock=time.time):
        self.path = path or PEOPLE_PATH
        self._clock = clock
        self._lock = threading.RLock()
        self._data = {"version": 1, "people": []}
        self._mtime = None
        self._matrix = np.zeros((0, 128), np.float32)
        self._owners = []
        self.reload_if_changed(force=True)

    # ── File handling ────────────────────────────────────────────────────────
    def _read_file(self):
        """(contents, change marker of exactly the file that was read); no
        people if it doesn't exist. Raises ValueError if it exists but can't be
        read — callers must never treat that as "nobody remembered" and write
        it back."""
        try:
            with open(self.path, encoding="utf-8") as f:
                marker = self._marker(os.fstat(f.fileno()))
                data = json.load(f)
        except FileNotFoundError:
            return {"version": 1, "people": []}, None
        except (OSError, ValueError) as e:
            raise ValueError(f"{self.path} is unreadable: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("people"), list):
            raise ValueError(f"{self.path} has an unexpected format")
        return data, marker

    @staticmethod
    def _marker(st):
        # Every write replaces the file (new inode), so this changes even when
        # two writes land within the same clock tick.
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _set_aside(self, reason):
        """Keep an unreadable file for recovery instead of overwriting it."""
        backup = f"{self.path}.unreadable-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        try:
            os.replace(self.path, backup)
            print(f"[FACE] {reason} — kept it as {backup}", flush=True)
        except OSError:
            pass

    def _write_file(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())        # a power cut must not leave a half-written file
        os.replace(tmp, self.path)

    def _file_mtime(self):
        try:
            return self._marker(os.stat(self.path))
        except OSError:
            return None

    def reload_if_changed(self, force=False):
        mtime = self._file_mtime()
        with self._lock:
            if not force and mtime == self._mtime:
                return False
            try:
                data, marker = self._read_file()
            except ValueError as e:
                if self._data["people"]:
                    print(f"[FACE] {e}; keeping the people already loaded", flush=True)
                    self._mtime = mtime
                    return False
                self._set_aside(str(e))
                data, marker = {"version": 1, "people": []}, None
            # The marker of what was actually read: a write landing after the
            # read is still seen as a change next time.
            self._set(data, marker)
            return True

    def _set(self, data, mtime):
        self._data = data
        self._mtime = mtime
        rows, owners = [], []
        for person in data["people"]:
            for emb in person.get("embeddings", []):
                rows.append(emb)
                owners.append(person["id"])
        self._matrix = (np.array(rows, np.float32) if rows
                        else np.zeros((0, 128), np.float32))
        self._owners = owners

    def _update(self, change):
        """Locked read-modify-write; `change(data)` returns the call's result."""
        with self._lock:
            # Opened read-only: the dashboard runs as root and the AI core as
            # the robot's user, and either may have created the lock file.
            fd = os.open(self.path + ".lock", os.O_RDONLY | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    data, _ = self._read_file()
                except ValueError as e:
                    # Rebuild from the last good copy in memory rather than
                    # writing an empty list over everyone.
                    self._set_aside(str(e))
                    data = json.loads(json.dumps(self._data))
                result = change(data)
                self._write_file(data)
                self._set(data, self._file_mtime())   # nobody else can write while we hold the lock
            finally:
                os.close(fd)                          # also releases the lock
            return result

    # ── Queries ──────────────────────────────────────────────────────────────
    def people(self):
        with self._lock:
            return [dict(p) for p in self._data["people"]]

    def get(self, person_id):
        with self._lock:
            return next((dict(p) for p in self._data["people"] if p["id"] == person_id), None)

    def find_by_name(self, name):
        key = _name_key(clean_name(name))
        with self._lock:
            return next((dict(p) for p in self._data["people"]
                         if _name_key(p["name"]) == key), None)

    def match(self, embedding):
        """Best (person_id, score) for an embedding, or (None, 0.0)."""
        with self._lock:
            if not len(self._owners):
                return None, 0.0
            scores = self._matrix @ embedding
            i = int(np.argmax(scores))
            return self._owners[i], float(scores[i])

    def summary(self):
        """For the dashboard — no embeddings."""
        return [{k: p.get(k) for k in ("id", "name", "relationship", "source", "created",
                                       "last_seen")} | {"samples": len(p.get("embeddings", []))}
                for p in self.people()]

    # ── Changes ──────────────────────────────────────────────────────────────
    def add_samples(self, name, embeddings, source, relationship=None):
        """Add face samples to the person with this name (created if new).
        Returns (person_id, created)."""
        name = clean_name(name)
        if not name:
            raise ValueError("A name is needed.")
        embeddings = [np.asarray(e, np.float32) for e in embeddings]
        if not embeddings:
            raise ValueError("No usable face samples.")
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if _name_key(p["name"]) == _name_key(name)),
                          None)
            created = person is None
            if created:
                person = {"id": "p_" + uuid.uuid4().hex[:8], "name": name, "source": source,
                          "created": now, "last_seen": None, "embeddings": []}
                data["people"].append(person)
            if relationship:
                person["relationship"] = " ".join(str(relationship).split())[:60]
            samples = [np.asarray(e, np.float32) for e in person["embeddings"]] + embeddings
            person["embeddings"] = [[round(float(v), 5) for v in e]
                                    for e in _keep_diverse(samples, MAX_SAMPLES)]
            person["updated"] = now
            return person["id"], created

        return self._update(change)

    def add_samples_to(self, person_id, embeddings, check_consistency=True, relationship=None):
        """Add samples to an existing person (by id — never creates one, so a
        person deleted meanwhile stays deleted). With check_consistency, only
        samples that resemble their stored face are kept. Returns how many."""
        embeddings = [np.asarray(e, np.float32) for e in embeddings]

        def change(data):
            person = next((p for p in data["people"] if p["id"] == person_id), None)
            if person is None:
                return 0
            if relationship:
                person["relationship"] = " ".join(str(relationship).split())[:60]
            stored = [np.asarray(e, np.float32) for e in person["embeddings"]]
            keep = [e for e in embeddings if not check_consistency or not stored
                    or float(np.max(np.array(stored) @ e)) >= SAMPLE_CONSISTENCY]
            if keep:
                person["embeddings"] = [[round(float(v), 5) for v in e]
                                        for e in _keep_diverse(stored + keep, MAX_SAMPLES)]
                person["updated"] = self._clock()
            return len(keep)

        return self._update(change)

    def resemblance(self, person_id, embeddings):
        """Best cosine between any of `embeddings` and the person's stored face."""
        person = self.get(person_id)
        if not person or not person.get("embeddings") or not len(embeddings):
            return 0.0
        return float(np.max(np.array(person["embeddings"], np.float32)
                            @ np.array(embeddings, np.float32).T))

    def set_appearance(self, person_id, description, when=None, today=None):
        """Remember how someone looks (text only): `description` is their usual
        look (hair, glasses, ...), kept with the previous one for comparison;
        `today` is what they're wearing today, for compliments."""
        when = when or self._clock()

        def change(data):
            for p in data["people"]:
                if p["id"] == person_id:
                    if p.get("appearance"):
                        p["appearance_before"] = p["appearance"]
                    p["appearance"] = {"text": str(description)[:200], "t": when,
                                       "today": str(today or "")[:200]}
                    return True
            return False
        return self._update(change)

    def remove(self, person_id, forget_memories=False):
        """Delete a person. With forget_memories, also ask the AI core to remove
        them from its conversation memories (it has the memory database)."""
        def change(data):
            gone = [p for p in data["people"] if p["id"] == person_id]
            data["people"] = [p for p in data["people"] if p["id"] != person_id]
            if gone and forget_memories:
                data.setdefault("forget_requests", []).append(gone[0]["name"])
            return bool(gone)
        return self._update(change)

    def request_forget(self, name):
        """Queue a name to be forgotten from memories (kept on disk until done)."""
        def change(data):
            requests = data.setdefault("forget_requests", [])
            if name not in requests:
                requests.append(name)
        self._update(change)

    def forget_requests(self):
        """Names waiting to be forgotten from memories. They stay queued until
        forget_done(), so a restart part-way through doesn't lose them."""
        self.reload_if_changed()
        with self._lock:
            return list(self._data.get("forget_requests") or [])

    def forget_done(self, name):
        def change(data):
            requests = [n for n in data.get("forget_requests") or [] if n != name]
            if requests:
                data["forget_requests"] = requests
            else:
                data.pop("forget_requests", None)
        self._update(change)

    def touch(self, seen):
        """seen: {person_id: timestamp} — persist last-seen times."""
        def change(data):
            for p in data["people"]:
                if p["id"] in seen:
                    p["last_seen"] = seen[p["id"]]
        self._update(change)


def clean_name(name):
    """A name as it's stored: single spaces, at most MAX_NAME characters."""
    return " ".join(str(name or "").split())[:MAX_NAME]


def _name_key(name):
    return " ".join(str(name or "").lower().split())


def _keep_diverse(samples, limit):
    """Trim to `limit` samples: first drop outliers (a sample unlike all the
    others — likely a mistake), then the most redundant, so the stored set
    covers different looks of the same face."""
    samples = list(samples)
    while len(samples) > limit:
        m = np.array(samples) @ np.array(samples).T
        np.fill_diagonal(m, -1)
        best_match = m.max(axis=1)
        if best_match.min() < SAMPLE_CONSISTENCY:
            samples.pop(int(np.argmin(best_match)))
        else:
            samples.pop(int(np.argmax(best_match)))
    return samples


# ─── Tracking & introductions ────────────────────────────────────────────────
@dataclass
class Track:
    id: int
    first_seen: float
    last_seen: float
    box: tuple
    samples: deque = field(default_factory=lambda: deque(maxlen=10))
    votes: deque = field(default_factory=lambda: deque(maxlen=6))
    person_id: str = None
    maybe_id: str = None
    asked: bool = False
    declined: bool = False
    crop: bytes = None           # latest clear head-and-shoulders JPEG (RAM only)
    crop_t: float = 0.0
    visitor: dict = None         # RAM-only visit record of a stranger who was asked
    unsure_no: bool = False      # declined, but it wasn't clear whether it was this person
    last_embed: float = -1e9     # when this face was last identified (not just followed)
    look: np.ndarray = None      # thumbnail fingerprint from the last frame


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


class FaceIdentifier:
    """Turns camera frames into "who is in front of me", decides when to ask a
    new person who they are, and handles the remember/decline/forget tools."""

    def __init__(self, models, store, clock=time.monotonic, profile=None, wall=None):
        self.models = models
        self.store = store
        self._clock = clock                       # durations (immune to clock changes)
        self._wall = wall or (time.time if clock is time.monotonic else clock)  # stored times
        self._lock = threading.RLock()
        self._tracks = []
        self._next_track = 1
        self._visitors = []          # RAM only: [{"mean", "asked_at", "declined", "last_seen"}]
        self._last_intro = -1e9
        self._last_asked_track = None
        self._last_learned = {}
        self._seen = {}
        self._last_seen_saved = clock()
        self._last_present = {}      # person_id -> last time seen (for arrivals)
        self._here_since = {}        # person_id -> when this visit started
        self._arrivals = []
        self._last_greet = -1e9
        self.profile = profile or {}

    # ── Frame processing ─────────────────────────────────────────────────────
    def no_new_frame(self):
        """The camera has stopped delivering frames: let people age out instead
        of re-reading a frozen picture."""
        with self._lock:
            self._update_tracks([], self._clock())

    def process(self, frame):
        now = self._clock()
        self.store.reload_if_changed()
        dets = [d for d in self.models.detect(frame, scale=DETECT_SCALE) if d.size >= MIN_FACE_PX]
        looks = [self.models.fingerprint(frame, d) for d in dets]
        with self._lock:
            steady = self._steady(dets, looks, now)
        observations = []
        for i, det in enumerate(dets):
            if i in steady:                  # same face as a moment ago: just follow it
                observations.append((det, None, steady[i], looks[i]))
                continue
            det = self.models.refine(frame, det)
            observations.append((det, self.models.embed(frame, det), None, looks[i]))
        with self._lock:
            self._update_tracks(observations, now, frame)
        self._maybe_save_last_seen(now)

    def recently_seen(self, seconds):
        """Whether any face has been in view within `seconds`."""
        now = self._clock()
        with self._lock:
            return any(now - t.last_seen <= seconds for t in self._tracks)

    def _steady(self, dets, looks, now):
        """Detections that are clearly the same face as a track identified a
        moment ago — same place, seen last frame, looking the same, nobody
        overlapping — and don't need identifying again until REVERIFY_SECONDS.
        Anyone new, unconfirmed, moving or close to someone else is always
        identified."""
        steady = {}
        live = [t for t in self._tracks if now - t.last_seen <= STEADY_GAP]
        for i, det in enumerate(dets):
            track = max(live, key=lambda t: _iou(t.box, det.box), default=None)
            if (track is None or _iou(track.box, det.box) < STEADY_IOU
                    or now - track.last_embed >= REVERIFY_SECONDS
                    or track in steady.values()
                    or track.look is None or looks[i] is None
                    or float(track.look @ looks[i]) < STEADY_LOOK):
                continue
            if track.person_id:
                last = track.votes[-1] if track.votes else (None, 0.0)
                if last[0] != track.person_id or last[1] < MATCH_THRESHOLD:
                    continue                 # the last check wasn't a clear match: keep checking
            elif not (track.asked and len(track.samples) == track.samples.maxlen) or \
                    (track.votes and track.votes[-1][0] is not None):
                continue                     # a stranger Ember is still getting to know,
                                             # or who might be someone it knows
            if any(j != i and _iou(d.box, track.box) > 0 for j, d in enumerate(dets)) or \
                    any(o is not track and _iou(o.box, det.box) > 0 for o in live):
                continue                     # someone else close by: tell them apart properly
            steady[i] = track
        return steady

    def _update_tracks(self, observations, now, frame=None):
        self._tracks = [t for t in self._tracks if now - t.last_seen <= TRACK_EXPIRY]
        used = set()
        for det, emb, steady, look in observations:
            good = det.size >= LEARN_FACE_PX and det.frontal()
            if emb is None:                  # followed, not identified (see _steady)
                if steady not in self._tracks or steady.id in used:
                    continue                 # changed meanwhile: identify it next frame
                track = steady
            else:
                track = self._assign(det, emb, now, used, good)
                track.last_embed = now
            used.add(track.id)
            track.last_seen = now
            track.box = det.box
            track.look = look
            # Photos (for how someone looks) only from frames where the face
            # was actually identified — never from one that was just followed
            if emb is not None and good and now - track.crop_t >= CROP_REFRESH:
                crop = _crop_jpeg(frame, det)
                if crop:
                    track.crop, track.crop_t = crop, now
            if emb is None:
                if track.person_id:
                    self._seen[track.person_id] = self._wall()
                    self._note_presence(track, det, frame, False, now)   # (no photo)
                elif track.visitor is not None:
                    track.visitor["last_seen"] = now
                continue
            if good:
                track.samples.append(emb)
            pid, score = self.store.match(emb)
            track.votes.append((pid if score >= MAYBE_THRESHOLD else None, score))
            self._resolve(track)
            if track.person_id:
                self._seen[track.person_id] = self._wall()
                self._note_presence(track, det, frame, good, now)
                if (good and pid == track.person_id
                        and AUTO_LEARN_RANGE[0] <= score <= AUTO_LEARN_RANGE[1]
                        and now - self._last_learned.get(pid, -1e9) >= AUTO_LEARN_INTERVAL):
                    self._last_learned[pid] = now
                    self.store.add_samples_to(pid, [emb])
            elif track.visitor is not None:
                track.visitor["last_seen"] = now        # still here: the visit continues
            elif track.samples and not (track.asked or track.declined):
                self._inherit_visit(track, now)

    def _assign(self, det, emb, now, used, good=False):
        live = [t for t in self._tracks if t.id not in used and now - t.last_seen <= 1.5]
        best = max(live, key=lambda t: _iou(t.box, det.box), default=None)
        if best is not None and _iou(best.box, det.box) >= 0.3:
            if not good:
                same_face = True             # too unclear to tell: keep following
            elif best.samples:
                same_face = float(_normalize(np.mean(best.samples, axis=0)) @ emb) >= TRACK_CONTINUITY
            elif best.person_id:             # no samples of its own yet: compare with who it is
                same_face = self.store.resemblance(best.person_id, [emb]) >= TRACK_CONTINUITY
            else:
                same_face = True
            if same_face:
                return best
        for t in self._tracks:
            if t.id not in used and t.samples and float(np.mean(t.samples, axis=0) @ emb) >= 0.5:
                return t
        track = Track(id=self._next_track, first_seen=now, last_seen=now, box=det.box)
        self._next_track += 1
        self._tracks.append(track)
        return track

    def _resolve(self, track):
        before = track.person_id
        self._vote(track)
        if track.person_id != before and track.person_id is not None:
            # Now someone (else): the crop and samples may show the face this
            # track followed before — never use them as this person's.
            track.crop, track.crop_t = None, 0.0
            track.samples.clear()

    def _vote(self, track):
        confident = [pid for pid, s in track.votes if pid and s >= MATCH_THRESHOLD]
        if confident:
            top = max(set(confident), key=confident.count)
            if confident.count(top) >= CONFIRM_VOTES:
                if track.person_id is None or (track.person_id != top
                                               and confident.count(top) >= 3):
                    track.person_id = top
        if self.store.get(track.person_id) is None:
            track.person_id = None          # forgotten / deleted in the app
        maybes = [pid for pid, s in track.votes if pid and MAYBE_THRESHOLD <= s < MATCH_THRESHOLD]
        top_maybe = max(set(maybes), key=maybes.count) if maybes else None
        track.maybe_id = (top_maybe if not track.person_id and top_maybe
                          and maybes.count(top_maybe) >= CONFIRM_VOTES else None)

    def _inherit_visit(self, track, now):
        """A stranger who stepped away and came back is the same visitor."""
        self._visitors = [v for v in self._visitors if now - v["last_seen"] <= VISIT_MEMORY]
        mean = _normalize(np.mean(track.samples, axis=0))
        for v in self._visitors:
            if float(v["mean"] @ mean) >= SAME_VISITOR:
                track.asked, track.declined = True, v["declined"]
                track.unsure_no = v.get("unsure", False)
                track.visitor = v
                v["last_seen"] = now
                return

    def _remember_visit(self, track, now, declined=False):
        if not track.samples:
            return
        mean = _normalize(np.mean(track.samples, axis=0))
        for v in self._visitors:
            if float(v["mean"] @ mean) >= SAME_VISITOR:
                v.update(last_seen=now, declined=v["declined"] or declined,
                         unsure=v.get("unsure", False) or track.unsure_no)
                track.visitor = v
                return
        track.visitor = {"mean": mean, "asked_at": now, "last_seen": now, "declined": declined,
                         "unsure": track.unsure_no}
        self._visitors.append(track.visitor)

    # ── Arrivals (someone Ember knows comes back) ────────────────────────────
    def _is_primary(self, person_id):
        person = self.store.get(person_id)
        user = self.profile.get("user_name") or ""
        return bool(person and user and _name_key(person["name"]) == _name_key(user))

    def _note_presence(self, track, det, frame, good, now):
        pid = track.person_id
        prev = self._last_present.get(pid)
        if prev is not None:
            away = now - prev
            last_seen_wall = self._wall() - away
        else:                               # first sighting since start: use the saved time
            last_seen_wall = (self.store.get(pid) or {}).get("last_seen")
            away = None if last_seen_wall is None else max(0.0, self._wall() - last_seen_wall)
        if away is None or away >= VISIT_GAP or pid not in self._here_since:
            self._here_since[pid] = now
        threshold = AWAY_PRIMARY if self._is_primary(pid) else AWAY_OTHERS
        queued = next((a for a in self._arrivals if a["person_id"] == pid), None)
        if queued is None and (away is None or away >= threshold):
            self._arrivals.append({"person_id": pid, "away": away, "last_seen": last_seen_wall,
                                   "t": now, "crop": None})
            queued = self._arrivals[-1]
        if queued is not None and queued["crop"] is None and good:
            queued["crop"] = _crop_jpeg(frame, det)
        self._last_present[pid] = now

    def pending_arrival(self):
        """Someone Ember knows has come back into view after being away: return
        {"person_id", "name", "label", "is_primary", "away", "last_seen",
        "crop"} once (crop = JPEG of head and shoulders, RAM only), else None."""
        now = self._clock()
        with self._lock:
            self._arrivals = [a for a in self._arrivals
                              if now - a["t"] <= ARRIVAL_MAX_AGE and self.store.get(a["person_id"])]
            if now - self._last_greet < GREET_COOLDOWN:
                return None
            here = {t.person_id for t in self._tracks
                    if t.person_id and now - t.last_seen <= IN_VIEW_SECONDS}
            for a in list(self._arrivals):
                if a["person_id"] in here:
                    self._arrivals.remove(a)
                    person = self.store.get(a["person_id"])
                    if person is None:                 # removed in the dashboard just now
                        continue
                    self._last_greet = now
                    return {**a, "name": person["name"],
                            "label": self._label(a["person_id"]) or person["name"],
                            "is_primary": self._is_primary(a["person_id"]),
                            "appearance": person.get("appearance")}
        return None

    def people_here(self):
        """Known people in view → [{"person_id", "name", "label", "is_primary",
        "here_for" (s), "crop" (JPEG or None), "appearance" (stored dict or None)}],
        plus the number of unrecognized faces."""
        now = self._clock()
        out, unknown = [], 0
        for t in self.in_view():
            person = self.store.get(t.person_id) if t.person_id else None
            if not person:
                unknown += 1
                continue
            if any(p["person_id"] == person["id"] for p in out):
                continue
            out.append({"person_id": person["id"], "name": person["name"],
                        "label": self._label(person["id"]) or person["name"],
                        "is_primary": self._is_primary(person["id"]),
                        "here_for": now - self._here_since.get(person["id"], t.first_seen),
                        "crop": t.crop, "appearance": person.get("appearance")})
        return out, unknown

    def describe_visits(self):
        """'Sarah just arrived; Margaret has been here for about 2 hours.' or None."""
        people, _ = self.people_here()
        parts = []
        for p in people:
            mins = p["here_for"] / 60
            if mins < 10:
                parts.append(f"{p['name']} just arrived")
            elif mins < 60:
                parts.append(f"{p['name']} has been here for a while")
            else:
                hours = int(mins // 60)
                parts.append(f"{p['name']} has been here for about {hours} hour{'s' if hours != 1 else ''}")
        return ("; ".join(parts) + ".") if parts else None

    def present_names(self):
        """Names of who's in view right now, for the conversation log."""
        names, unknown = [], 0
        for t in self.in_view():
            person = self.store.get(t.person_id) if t.person_id else None
            if person:
                if person["name"] not in names:
                    names.append(person["name"])
            else:
                unknown += 1
        return names + ["someone unrecognized"] * unknown

    def _maybe_save_last_seen(self, now):
        if self._seen and now - self._last_seen_saved >= LAST_SEEN_SAVE_INTERVAL:
            seen, self._seen = self._seen, {}
            self._last_seen_saved = now
            self.store.touch(seen)

    # ── What's in view ───────────────────────────────────────────────────────
    def in_view(self):
        now = self._clock()
        with self._lock:
            return [t for t in self._tracks if now - t.last_seen <= IN_VIEW_SECONDS]

    def _label(self, person_id):
        person = self.store.get(person_id)
        if not person:
            return None
        name = person["name"]
        user = self.profile.get("user_name") or ""
        if user and _name_key(name) == _name_key(user):
            return name
        relationship = person.get("relationship") or next(
            (h.get("relationship") for h in self.profile.get("household", [])
             if _name_key(h.get("name")) == _name_key(name)), None)
        if relationship:
            whose = f"{user}'s" if user else "their"
            return f"{name} ({whose} {relationship})"
        return name

    def describe_in_view(self):
        tracks = self.in_view()
        if not tracks:
            return "Nobody is in front of your camera right now."
        known, maybe, unknown = [], [], 0
        for t in tracks:
            if t.person_id and (label := self._label(t.person_id)):
                if label not in known:
                    known.append(label)
            elif t.maybe_id and (label := self._label(t.maybe_id)):
                maybe.append(f"someone who might be {label} (not sure)")
            else:
                unknown += 1
        parts = known + maybe
        if unknown:
            parts.append("someone you don't recognize" if unknown == 1
                         else f"{unknown} people you don't recognize")
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        people = len(known) + len(maybe) + unknown
        return f"{text[0].upper()}{text[1:]} {'is' if people == 1 else 'are'} in front of you."

    # ── Introductions ────────────────────────────────────────────────────────
    def pending_introduction(self):
        """If someone unfamiliar has been in view long enough and hasn't been
        asked, mark them asked and return the prompt for Gemini; else None."""
        now = self._clock()
        with self._lock:
            if now - self._last_intro < INTRO_COOLDOWN:
                return None
            for t in self._tracks:
                if (now - t.last_seen <= 1.0 and not t.person_id and not t.asked
                        and not t.declined and now - t.first_seen >= INTRO_AFTER
                        and len(t.samples) >= INTRO_MIN_SAMPLES):
                    t.asked = True
                    self._last_intro = now
                    self._last_asked_track = t.id
                    self._remember_visit(t, now)
                    return self._intro_prompt(t)
        return None

    def _intro_prompt(self, track):
        others = [self._label(t.person_id) for t in self.in_view() if t.person_id]
        with_text = f" They're with {', '.join(others)}." if others else ""
        maybe = self.store.get(track.maybe_id) if track.maybe_id else None
        if maybe and (label := self._label(track.maybe_id)):
            name = maybe["name"]
            return (f"[NEW PERSON] Someone who might be {label} is in front of you, but "
                    f"you're not sure.{with_text} Ask kindly, e.g. \"Is that you, {name}?\" "
                    f"If they say yes, call remember_person with the name \"{name}\" so you "
                    "recognize them better. If not, ask their name and whether you may "
                    "remember their face.")
        user = self.profile.get("user_name")
        hint = ""
        if user and not self.store.find_by_name(user) and not others:
            hint = (f" You haven't learned the face of {user}, the person you care for, "
                    f"yet — this could be them, so you might ask \"Are you {user}?\"")
        return (f"[NEW PERSON] Someone you don't recognize has been in front of you for a "
                f"few seconds.{with_text}{hint} Greet them, tell them you're Ember, and ask "
                "their name. Then ask if it's okay to remember their face so you'll know "
                "them next time. Only if they clearly say yes, call remember_person.")

    # ── Tools ────────────────────────────────────────────────────────────────
    def handle_tool_call(self, name, args):
        args = dict(args or {})
        handlers = {
            "remember_person": lambda: self.remember(args.get("name"), args.get("relationship"),
                                                     bool(args.get("changed_mind"))),
            "decline_to_be_remembered": self.decline,
            "forget_person": lambda: self.forget(args.get("name")),
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler()
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            print(f"[FACE] {name} failed: {e}")
            return {"error": str(e)}

    def _looks_known(self, track):
        """A track not yet named whose face clearly matches someone stored
        (e.g. their first second in view)."""
        pid, score = self.store.match(_normalize(np.mean(track.samples, axis=0)))
        return pid if score >= MATCH_THRESHOLD else None

    def _unfamiliar_in_view(self, now):
        return [t for t in self._tracks if now - t.last_seen <= IN_VIEW_SECONDS and t.samples
                and not t.person_id and not t.declined and not self._looks_known(t)]

    def _asked_track(self):
        """The stranger Ember last asked, if still tracked (may have just stepped away)."""
        return next((t for t in self._tracks if t.id == self._last_asked_track
                     and not t.person_id and not t.declined and t.samples), None)

    def _target_for_remember(self, name, now, changed_mind=False):
        """The face to save for `name` — only when it's unambiguous who that is."""
        declined = [t for t in self._tracks if now - t.last_seen <= IN_VIEW_SECONDS and t.samples
                    and not t.person_id and t.declined and not self._looks_known(t)]
        if changed_mind:                   # someone who said no earlier now says yes
            if len(declined) == 1 and not declined[0].unsure_no:
                return declined[0], None
            if len(declined) == 1:
                return None, ("Earlier it wasn't clear who said no, so to be safe I can't "
                              "remember this face now. Their family can add them in the app.")
            return None, ("I can't tell which person changed their mind. Ask them to come to "
                          "your camera on their own, then try again." if declined else
                          "I can't see the person who said no earlier. Ask them to look at "
                          "your camera, then try again.")
        existing = self.store.find_by_name(name)
        in_view = [t for t in self._tracks if now - t.last_seen <= IN_VIEW_SECONDS and t.samples]
        if existing:                       # confirming someone Ember wasn't sure about
            maybes = [t for t in in_view if t.maybe_id == existing["id"] and not t.person_id
                      and not t.declined]
            if len(maybes) == 1:
                return maybes[0], None
        unknown = self._unfamiliar_in_view(now)
        if len(unknown) == 1:
            return unknown[0], None
        if len(unknown) > 1:
            return None, ("Several unfamiliar faces are in view, so I can't tell whose face to "
                          "remember. Ask them to come to your camera one at a time, then try again.")
        asked = self._asked_track()
        if asked and not self._looks_known(asked):     # just stepped out of view
            return asked, None
        if declined:
            return None, ("The person in front of you said no to being remembered earlier. "
                          "Only if they themselves now clearly say yes, call remember_person "
                          "again with changed_mind true.")
        known = {t.person_id for t in in_view if t.person_id}
        if len(known) == 1 and (person := self.store.get(known.pop())) \
                and _name_key(person["name"]) != _name_key(name):
            return None, f"That face is already remembered as {person['name']}."
        return None, ("I can't see an unfamiliar face clearly right now. Ask them to look "
                      "at your camera for a few seconds, then try again.")

    def _canonical_name(self, name):
        """Use the family's spelling when the name matches someone in the profile."""
        known = [self.profile.get("user_name") or ""] + [
            h.get("name", "") for h in self.profile.get("household", [])]
        return next((k for k in known if k and _name_key(k) == _name_key(name)),
                    " ".join(str(name).split()))

    def remember(self, name, relationship=None, changed_mind=False):
        if not name or not str(name).strip():
            raise ValueError("Ask for their name first.")
        name = clean_name(self._canonical_name(name))
        now = self._clock()
        with self._lock:
            # Already known and in view (Gemini sometimes asks again, even with
            # changed_mind): say so instead of a refusal about someone saying no.
            existing = self.store.find_by_name(name)
            if existing and any(t.person_id == existing["id"] and now - t.last_seen <= IN_VIEW_SECONDS
                                for t in self._tracks):
                return {"status": "already_known", "name": existing["name"],
                        "note": f"You already know {existing['name']}: their face is remembered "
                                "and they are in front of you."}
            track, problem = self._target_for_remember(name, now, changed_mind)
            if problem:
                return {"status": "not_saved", "reason": problem}
            if track.person_id:
                current = self.store.get(track.person_id)
                if current and _name_key(current["name"]) != _name_key(name):
                    return {"status": "not_saved",
                            "reason": f"That face is already remembered as {current['name']}."}
            samples = list(track.samples)
            existing = self.store.find_by_name(name)
            other = self._looks_known(track)
            if other and (not existing or other != existing["id"]):
                return {"status": "not_saved", "reason": "That face is already remembered as "
                        f"{(self.store.get(other) or {}).get('name', 'someone else')}."}
            if existing and self.store.resemblance(existing["id"], samples) < SAMPLE_CONSISTENCY:
                return {"status": "not_saved",
                        "reason": f"This face doesn't match the {existing['name']} I already know. "
                                  f"If it's a different person with the same name, ask for a "
                                  f"last name or nickname and use that."}
            if existing:
                self.store.add_samples_to(existing["id"], samples, relationship=relationship)
                person_id, created = existing["id"], False
            else:
                person_id, created = self.store.add_samples(name, samples, source="conversation",
                                                            relationship=relationship)
            self._last_asked_track = None
            track.person_id, track.maybe_id, track.declined = person_id, None, False
            track.unsure_no = False
            track.votes.clear()
            self._last_present[person_id] = now
            self._here_since.setdefault(person_id, track.first_seen)
            self._arrivals = [a for a in self._arrivals if a["person_id"] != person_id]
            self._visitors = [v for v in self._visitors
                              if float(v["mean"] @ _normalize(np.mean(samples, axis=0)))
                              < SAME_VISITOR]
        label = self._label(person_id) or name
        print(f"[FACE] Remembered {label} ({len(samples)} samples)", flush=True)
        return {"status": "remembered", "name": self.store.get(person_id)["name"],
                "who": label, "new_person": created and existing is None}

    def decline(self):
        """Someone said no: the person Ember asked if they're in view, else the
        one unfamiliar face in view. When it's unclear (the person asked
        stepped away and someone else is there), both count as having said no
        — and neither can be remembered by just saying yes later."""
        now = self._clock()
        with self._lock:
            unknown = self._unfamiliar_in_view(now)
            asked = self._asked_track()
            if asked and (asked in unknown or not unknown):
                tracks, unsure = [asked], False
            elif not asked and len(unknown) == 1:
                tracks, unsure = unknown, False
            else:
                tracks, unsure = unknown + ([asked] if asked else []), True
            if not tracks:
                return {"status": "ok", "note": "Nobody to forget — nothing was saved."}
            for track in tracks:
                track.declined = track.asked = True
                track.unsure_no = unsure
                self._remember_visit(track, now, declined=True)
            self._last_asked_track = None
        return {"status": "ok", "note": "Their face was not saved. You won't ask again "
                                        "during this visit."}

    def find_person(self, name):
        """The stored person with exactly this name (any case). Never guessed
        from a first name: a visiting "Margaret" must not erase "Margaret
        Jones" — if it's not exact, the caller asks which person they mean."""
        return self.store.find_by_name(name)

    def forget(self, name):
        person = self.find_person(name)
        if not person:
            names = [p["name"] for p in self.store.people()]
            return {"status": "not_found", "remembered_people": names}
        self.store.remove(person["id"])
        now = self._clock()
        with self._lock:
            for t in self._tracks:
                if t.person_id == person["id"] or t.maybe_id == person["id"]:
                    t.person_id = t.maybe_id = None
                    t.votes.clear()
                    t.asked = t.declined = True     # they just asked to be forgotten
                    self._remember_visit(t, now, declined=True)
        print(f"[FACE] Forgot {person['name']}", flush=True)
        return {"status": "forgotten", "name": person["name"]}


def _crop_jpeg(frame, det):
    """Head-and-shoulders crop (hair included) as JPEG bytes, or None."""
    if not isinstance(frame, np.ndarray):
        return None
    import cv2
    x, y, w, h = det.box
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(x - 0.6 * w)), max(0, int(y - 0.9 * h))
    x1, y1 = min(W, int(x + 1.6 * w)), min(H, int(y + 1.5 * h))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    ok, buf = cv2.imencode(".jpg", frame[y0:y1, x0:x1], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return buf.tobytes() if ok else None


def _normalize(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# ─── Enrollment from photos (used by the dashboard) ─────────────────────────
def embeddings_from_photo(models, image_bytes):
    """Decode an uploaded photo and return (embedding, None) or (None, reason).
    Needs one clear, front-facing main face; small faces in the background
    (at most 40% of the main face's area) are ignored."""
    import cv2
    data = np.frombuffer(image_bytes, np.uint8)
    try:
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    except cv2.error:                  # e.g. over OPENCV_IO_MAX_IMAGE_PIXELS
        return None, "This photo is too large. Use a smaller photo."
    if img is None:
        return None, "Not an image we can read."
    # YuNet misses faces larger than ~500 px, so try a 640 px copy first (close-ups,
    # selfies), then 1280 px if no face — or only a too-small one — was found
    # (small faces in a big group photo).
    faces = []
    original = img
    for target in (640, 1280):
        longest = max(original.shape[:2])
        scale = min(1.0, target / longest)
        img = (original if scale == 1.0 else
               cv2.resize(original, (int(original.shape[1] * scale), int(original.shape[0] * scale)),
                          interpolation=cv2.INTER_AREA))
        faces = [d for d in models.detect(img) if d.size >= MIN_FACE_PX]
        if scale == 1.0 or (faces and max(d.size for d in faces) >= LEARN_FACE_PX):
            break
    if not faces:
        return None, "No face found. Use a clear, well-lit photo of their face."
    faces.sort(key=lambda d: d.box[2] * d.box[3], reverse=True)
    if len(faces) > 1 and faces[1].box[2] * faces[1].box[3] > 0.4 * faces[0].box[2] * faces[0].box[3]:
        return None, "More than one face in this photo. Use a photo of just them."
    main = faces[0]
    if main.size < LEARN_FACE_PX or not main.frontal():
        return None, "The face is too small or turned away. Use a closer, front-facing photo."
    return models.embed(img, main), None
