"""Pure fall decision: landmarks + timestamps in, decision out. No camera or MediaPipe.

`FallDecision` wraps the vendored `fall_detection.detector.FallDetector` (unchanged;
`UPSTREAM.json` records its hash) with the thresholds v10 passes it in
`_fall_detection_thread` (`V10_SETTINGS`, the `FALL_*` constants of
realtime_gemini_10.py; a test keeps them in sync), and adds two display-only fields:
- `posture`: a coarse label for the demo screen;
- `countdown_s`: the time left until a pending fall is confirmed.
The decision itself is the detector's: tests replay every synthetic sequence through
both and require identical results frame by frame. `Labeler` adds the same two
fields to any FallDetector's results, e.g. the runtime's own detector on camera frames.
"""
import math
import time

from fall_detection.detector import FallDetector, _IDX, _MIN_VISIBILITY

# realtime_gemini_10.py: FALL_ANGLE_THRESHOLD, FALL_ANG_VEL_THRESHOLD, FALL_HIP_VEL_THRESHOLD,
# FALL_HISTORY_FRAMES, FALL_CONFIRMATION_FRAMES, FALL_COOLDOWN_SECONDS, FALL_STAY_DOWN_SECONDS.
V10_SETTINGS = dict(angle_threshold=60.0, ang_vel_threshold=25.0, hip_vel_threshold=0.12,
                    history_window=6, confirmation_frames=2, cooldown_seconds=3.0, stay_down_s=2.0)
V10_FPS = 10.0   # realtime_gemini_10.FALL_DETECTION_FPS

POSTURES = ("not visible", "standing", "upright", "sitting", "bending", "lying",
            "going down (possible fall)", "on floor (possible fall)")


def _visible(points, *names):
    return all(points[_IDX[name]].visibility >= _MIN_VISIBILITY for name in names)


def _mid(points, a, b):
    return ((points[a].x + points[b].x) / 2, (points[a].y + points[b].y) / 2)


def posture(landmarks, trunk_angle=None):
    """Coarse label from one frame: not visible / standing / upright / sitting / bending / lying."""
    if landmarks is None or trunk_angle is None or not _visible(
            landmarks, "left_shoulder", "right_shoulder", "left_hip", "right_hip"):
        return "not visible"
    if trunk_angle >= 60:
        return "lying"
    if trunk_angle >= 30:
        return "bending"
    knees = (25, 26)
    if all(landmarks[i].visibility >= _MIN_VISIBILITY for i in knees):
        hip, knee = _mid(landmarks, 23, 24), _mid(landmarks, *knees)
        thigh = math.degrees(math.atan2(abs(knee[0] - hip[0]), abs(knee[1] - hip[1])))
        return "sitting" if thigh > 50 else "standing"
    return "upright"


def countdown(detector, now):
    """Seconds until the detector confirms its pending fall, or None when nothing is pending."""
    since = detector._pending_since
    if since is None:
        return None
    return max(0.0, detector.stay_down_s - (now - since))


class Labeler:
    """Display-only posture/countdown for one detector's results. Never changes a decision."""

    def __init__(self):
        self._down_after_fall = False

    def label(self, detector, landmarks, result, now):
        angle = None if result["fall_detected"] else result["trunk_angle"]
        if result["fall_detected"]:  # the readings describe the trigger, not this frame
            angle = detector._angles[-1] if landmarks is not None and detector._angles else None
        label = posture(landmarks, angle)
        if result["fall_detected"]:
            self._down_after_fall = True
        elif angle is not None and angle < detector.upright_angle:
            self._down_after_fall = False
        if result["fall_pending"]:
            label = "going down (possible fall)"
        elif self._down_after_fall and label in ("lying", "bending", "not visible"):
            label = "on floor (possible fall)"
        return {"posture": label, "countdown_s": countdown(detector, now)}

    def reset(self):
        self._down_after_fall = False


class FallDecision:
    """One person's decision state. update(landmarks, now) -> the detector's result plus
    posture, countdown_s and time."""

    def __init__(self, **settings):
        self.settings = dict(V10_SETTINGS, **settings)
        self.detector = FallDetector(**self.settings)
        self.labeler = Labeler()

    @property
    def stay_down_s(self):
        return self.detector.stay_down_s

    def update(self, landmarks, now=None, timestamp=None):
        now = timestamp if now is None else now
        now = time.monotonic() if now is None else now
        result = self.detector.update(landmarks, timestamp=now)
        result.update(self.labeler.label(self.detector, landmarks, result, now), time=now)
        return result

    step = update

    def reset(self):
        self.detector.reset()
        self.labeler.reset()


def replay(frames, settings=None, start=0.0):
    """Run [(t, landmarks)] through a fresh FallDecision. Returns one result dict per frame."""
    decision = FallDecision(**(settings or {}))
    return [decision.update(landmarks, start + moment) for moment, landmarks in frames]


def detections(frames, settings=None):
    """Times (seconds from the sequence start) at which a fall was confirmed."""
    return [frames[i][0] for i, result in enumerate(replay(frames, settings)) if result["fall_detected"]]


def points(landmarks, digits=3):
    """Compact [[x, y, visibility], ...] for the local demo screen (None when no pose)."""
    if landmarks is None:
        return None
    return [[round(p.x, digits), round(p.y, digits), round(getattr(p, "visibility", 1.0), 2)] for p in landmarks]
