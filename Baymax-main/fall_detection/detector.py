"""
detector.py — FallDetector class

Fall detection logic using trunk angle and angular velocity.

Detection strategy:
  1. Compute the midpoint of the shoulders and the midpoint of the hips.
  2. Calculate the trunk angle: the angle the shoulder-hip line makes with vertical.
       0°  → person is standing perfectly upright
       90° → person is fully horizontal (lying down or mid-fall)
  3. Track angular velocity (degrees/second) over a rolling time window.
  4. A fall is flagged when:
       - trunk_angle > ANGLE_THRESHOLD            (person is no longer upright)
       - AND angular_velocity > ANG_VEL_THRESHOLD  (rotation happened rapidly)
       - OR hip_descent_velocity > HIP_VEL_THRESHOLD (body dropped quickly)
     confirmed across CONFIRMATION_FRAMES consecutive frames.
     The person must also have been upright (trunk angle < UPRIGHT_ANGLE)
     within the last UPRIGHT_LOOKBACK_S seconds, so someone who is already
     sitting or leaning past the threshold does not trigger it by shifting.
  5. For WARMUP_S seconds after tracking (re)starts, no fall is declared:
     the pose estimate jumps around while it settles, which reads as motion.
     Tracking restarts after the torso has been out of view for LOSS_RESET_S.
  6. A detection only opens a *pending* fall. It is confirmed once the person
     has stayed down (not back under UPRIGHT_ANGLE) for STAY_DOWN_S seconds,
     and cancelled if they come back upright first. Bending over to pick
     something up looks exactly like the start of a fall; staying down is
     what tells them apart. Frames where the torso is hidden do not cancel
     it, since a real fall often ends partly out of view.
  7. After a fall is confirmed, a cooldown suppresses re-triggering.
"""

import time
import numpy as np
from collections import deque


# MediaPipe Pose landmark indices
_IDX = {
    "left_shoulder":  11,
    "right_shoulder": 12,
    "left_hip":       23,
    "right_hip":      24,
}

# Minimum landmark visibility score to trust a keypoint
_MIN_VISIBILITY = 0.5

# A torso gap longer than this restarts tracking (clears history, re-warms).
# Short gaps are tolerated because a falling body often occludes itself.
LOSS_RESET_S = 0.5


class FallDetector:
    """
    Stateful fall detector that consumes MediaPipe Pose landmarks frame-by-frame.

    Parameters
    ----------
    angle_threshold : float
        Trunk angle (degrees from vertical) above which the pose is considered
        non-upright. Default 60°.
    ang_vel_threshold : float
        Angular velocity (degrees/second) required to distinguish a fall from a
        slow, deliberate lie-down. Default 40 deg/s.
    hip_vel_threshold : float
        Downward velocity of the hip midpoint in normalised image coordinates
        per second. Used as a secondary signal. Default 0.20 (20% of frame
        height per second).
    history_window : int
        Number of past frames retained for velocity estimation. Default 8.
    confirmation_frames : int
        Consecutive suspicious frames required before a fall is declared.
        Default 3.
    cooldown_seconds : float
        Seconds to suppress re-triggering after a confirmed fall. Default 3.0.
    upright_angle : float
        Trunk angle below which the person counts as upright. A fall needs an
        upright frame within upright_lookback_s. Default 30°.
    upright_lookback_s : float
        How far back to look for that upright frame. Default 1.5 s.
    warmup_s : float
        Seconds after tracking (re)starts during which no fall is declared.
        Default 1.0 s.
    stay_down_s : float
        How long the person must stay down (trunk angle >= upright_angle, or
        out of view) before a pending fall is confirmed. Default 2.0 s.
    """

    def __init__(
        self,
        angle_threshold: float = 60.0,
        ang_vel_threshold: float = 40.0,
        hip_vel_threshold: float = 0.20,
        history_window: int = 8,
        confirmation_frames: int = 3,
        cooldown_seconds: float = 3.0,
        upright_angle: float = 30.0,
        upright_lookback_s: float = 1.5,
        warmup_s: float = 1.0,
        stay_down_s: float = 2.0,
    ):
        self.angle_threshold = angle_threshold
        self.ang_vel_threshold = ang_vel_threshold
        self.hip_vel_threshold = hip_vel_threshold
        self.confirmation_frames = confirmation_frames
        self.cooldown_seconds = cooldown_seconds
        self.upright_angle = upright_angle
        self.upright_lookback_s = upright_lookback_s
        self.warmup_s = warmup_s
        self.stay_down_s = stay_down_s

        # Rolling histories — keyed by timestamp (seconds)
        self._angles:    deque = deque(maxlen=history_window)
        self._hip_ys:    deque = deque(maxlen=history_window)
        self._times:     deque = deque(maxlen=history_window)

        # (time, angle) over the last upright_lookback_s, for the upright check
        self._recent: deque = deque()
        self._tracking_since: float | None = None
        self._last_valid_t:   float | None = None

        self._suspicious_streak: int = 0
        self._cooldown_until:    float = 0.0
        self.fall_active:        bool = False  # True while cooldown is running

        # A detected fall waits here until the person has stayed down.
        self._pending_since:  float | None = None
        self._pending_motion: dict = {}   # readings at the moment it triggered

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, landmarks, timestamp=None) -> dict:
        """
        Process one frame's pose landmarks.

        Parameters
        ----------
        landmarks : mediapipe.framework.formats.landmark_pb2.NormalizedLandmarkList
            The `pose_landmarks` field from a MediaPipe Pose result, or None.
        timestamp : float | None
            Monotonic capture time for asynchronous inference. Defaults to now.

        Returns
        -------
        dict with keys:
            fall_detected  : bool  — True on the frame a fall is confirmed
                             (stay_down_s after it was detected). On that
                             frame the three readings below describe the
                             motion that triggered it, not the current frame.
            fall_pending   : bool  — True while waiting to see if they stay down
            fall_active    : bool  — True during the cooldown window
            trunk_angle    : float | None  — current trunk angle in degrees
            angular_vel    : float | None  — angular velocity in deg/s
            hip_descent_vel: float | None  — hip downward velocity (norm/s)
        """
        now = time.monotonic() if timestamp is None else timestamp

        result = dict(
            fall_detected=False,
            fall_active=self.fall_active,
            fall_pending=False,
            trunk_angle=None,
            angular_vel=None,
            hip_descent_vel=None,
        )

        self._observe(landmarks, now, result)

        # ── Stay-down check ────────────────────────────────────────────
        # Runs on every frame, including ones where the torso is hidden.
        if self._pending_since is not None:
            angle = result["trunk_angle"]
            if angle is not None and angle < self.upright_angle:
                self._pending_since = None  # back upright: a bend, not a fall
            elif now - self._pending_since >= self.stay_down_s:
                self._pending_since = None
                self._cooldown_until = now + self.cooldown_seconds
                self.fall_active = True
                result["fall_detected"] = True
                result["fall_active"] = True
                # This frame may have no landmarks at all, so report the
                # readings from the moment the fall was detected.
                result.update(self._pending_motion)

        result["fall_pending"] = self._pending_since is not None
        return result

    def _observe(self, landmarks, now, result):
        """
        Fold one frame into the histories, fill result's readings, and open a
        pending fall if this frame completes a suspicious streak.
        """
        if landmarks is None:
            return

        # Tasks API returns landmarks as a plain list (not a proto).
        lm = landmarks

        # ── Extract keypoints ──────────────────────────────────────────
        ls = lm[_IDX["left_shoulder"]]
        rs = lm[_IDX["right_shoulder"]]
        lh = lm[_IDX["left_hip"]]
        rh = lm[_IDX["right_hip"]]

        # Skip frame if any key landmark is occluded
        if any(p.visibility < _MIN_VISIBILITY for p in (ls, rs, lh, rh)):
            return

        shoulder_mid = ((ls.x + rs.x) / 2.0, (ls.y + rs.y) / 2.0)
        hip_mid      = ((lh.x + rh.x) / 2.0, (lh.y + rh.y) / 2.0)

        # ── Trunk angle ────────────────────────────────────────────────
        # Vector from hip to shoulder.  In image coords y increases downward,
        # so dy < 0 when the person is upright (shoulder is above hip).
        dx = shoulder_mid[0] - hip_mid[0]
        dy = shoulder_mid[1] - hip_mid[1]

        # angle from vertical: 0° upright → 90° horizontal
        trunk_angle = float(np.degrees(np.arctan2(abs(dx), abs(dy))))

        # ── Tracking (re)start ─────────────────────────────────────────
        # After a long gap the old history describes a different moment, so
        # velocities across it would be bogus. Start over and re-warm.
        if self._last_valid_t is None or now - self._last_valid_t > LOSS_RESET_S:
            self._angles.clear()
            self._hip_ys.clear()
            self._times.clear()
            self._recent.clear()
            self._suspicious_streak = 0
            self._tracking_since = now
        self._last_valid_t = now

        self._recent.append((now, trunk_angle))
        while now - self._recent[0][0] > self.upright_lookback_s:
            self._recent.popleft()

        # ── Update histories ───────────────────────────────────────────
        self._angles.append(trunk_angle)
        self._hip_ys.append(hip_mid[1])
        self._times.append(now)

        # ── Derive velocities ──────────────────────────────────────────
        angular_vel    = None
        hip_descent_vel = None

        if len(self._times) >= 2:
            dt = self._times[-1] - self._times[0]
            if dt > 1e-6:
                angular_vel     = (self._angles[-1] - self._angles[0]) / dt
                # positive hip_descent_vel means hips are moving DOWN (y increasing)
                hip_descent_vel = (self._hip_ys[-1] - self._hip_ys[0]) / dt

        result["trunk_angle"]     = trunk_angle
        result["angular_vel"]     = angular_vel
        result["hip_descent_vel"] = hip_descent_vel

        # ── Cooldown check ─────────────────────────────────────────────
        if now < self._cooldown_until:
            self.fall_active = True
            result["fall_active"] = True
            return
        self.fall_active = False
        result["fall_active"] = False

        if self._pending_since is not None:
            return  # already waiting to see whether they stay down

        # ── Fall logic ─────────────────────────────────────────────────
        angle_high    = trunk_angle > self.angle_threshold
        fast_rotation = (angular_vel    is not None and angular_vel    > self.ang_vel_threshold)
        fast_descent  = (hip_descent_vel is not None and hip_descent_vel > self.hip_vel_threshold)

        was_upright   = any(a < self.upright_angle for _, a in self._recent)
        warming_up    = now - self._tracking_since < self.warmup_s

        suspicious = (angle_high and (fast_rotation or fast_descent)
                      and was_upright and not warming_up)

        if suspicious:
            self._suspicious_streak += 1
        else:
            # Decay the streak — allows one noisy frame without resetting
            self._suspicious_streak = max(0, self._suspicious_streak - 1)

        if self._suspicious_streak >= self.confirmation_frames:
            self._suspicious_streak = 0
            self._pending_since = now
            self._pending_motion = dict(
                trunk_angle=trunk_angle,
                angular_vel=angular_vel,
                hip_descent_vel=hip_descent_vel,
            )

    def reset(self):
        """Clear all internal state (e.g. between test subjects)."""
        self._angles.clear()
        self._hip_ys.clear()
        self._times.clear()
        self._recent.clear()
        self._tracking_since = None
        self._last_valid_t = None
        self._suspicious_streak = 0
        self._cooldown_until = 0.0
        self.fall_active = False
        self._pending_since = None
        self._pending_motion = {}
