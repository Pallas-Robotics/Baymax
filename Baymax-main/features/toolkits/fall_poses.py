"""Synthetic ("made-up") pose sequences in MediaPipe's normalized 33-landmark layout.

No camera, MediaPipe or images: each frame is a list of 33 landmarks with x/y in
normalized image coordinates (0..1, y grows downward), z and visibility, or None
when no person is detected. The fall tests, the dev-UI "replay synthetic fall"
button and any later robot rig / evaluation harness share these sequences.

    from features.toolkits import fall_poses
    scenario = fall_poses.scenario("fast_fall")
    for moment, landmarks in scenario.frames: ...

Poses are idealized stick figures, not recordings of anyone. Passing them shows
the decision logic behaves as designed on clean input; it says nothing about
MediaPipe accuracy, camera placement or real falls.
"""
from dataclasses import dataclass, field
import math

# MediaPipe Pose landmark indices.
NOSE, LEFT_SHOULDER, RIGHT_SHOULDER = 0, 11, 12
LEFT_ELBOW, RIGHT_ELBOW, LEFT_WRIST, RIGHT_WRIST = 13, 14, 15, 16
LEFT_HIP, RIGHT_HIP, LEFT_KNEE, RIGHT_KNEE = 23, 24, 25, 26
LEFT_ANKLE, RIGHT_ANKLE = 27, 28
TORSO = (LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP)
GROUPS = {"head": tuple(range(0, 11)), "arms": tuple(range(13, 23)), "torso": TORSO,
          "legs": tuple(range(25, 33))}
# Same skeleton edges the runtime draws (MediaPipe POSE_CONNECTIONS).
CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8), (11, 12), (11, 13), (13, 15),
               (12, 14), (14, 16), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (27, 29), (27, 31), (29, 31),
               (24, 26), (26, 28), (28, 30), (28, 32), (30, 32)]


@dataclass
class Landmark:
    x: float
    y: float
    z: float = 0.0
    visibility: float = 1.0
    presence: float = 1.0


@dataclass
class Scenario:
    name: str
    description: str
    frames: list            # [(seconds from start, landmarks or None)]
    expect_fall: bool       # what an ideal detector should say
    fall_onset: float = None  # when the fall starts, for timing checks
    fps: float = 10.0       # realtime_gemini_10.FALL_DETECTION_FPS
    notes: str = ""
    tags: tuple = field(default_factory=tuple)

    @property
    def duration(self):
        return self.frames[-1][0] if self.frames else 0.0


def body(hip_x=0.5, hip_y=0.62, trunk=0.0, thigh=0.0, shin=0.0, height=0.6, depth=1.0,
         hidden=(), visibility=1.0):
    """One pose. Angles are signed degrees in the image plane.

    trunk: hip->shoulder direction from straight up (+ leans toward +x; 90 = horizontal).
    thigh/shin: hip->knee and knee->ankle from straight down (+ toward +x; 90 = horizontal).
    height: standing head-to-ankle size in normalized image units.
    depth: vertical foreshortening of the torso (1 = side-on; <0 = shoulders below hips,
           e.g. lying with the head toward a downward-looking camera).
    hidden: landmark groups ("head", "arms", "torso", "legs") or indices with low visibility.
    Landmarks outside the frame get visibility 0; with the whole torso out, returns None.
    """
    torso, neck, upper_arm, forearm = 0.30 * height, 0.12 * height, 0.15 * height, 0.13 * height
    thigh_len = shin_len = 0.25 * height
    t, a, b = math.radians(trunk), math.radians(thigh), math.radians(shin)
    up = (math.sin(t), -math.cos(t) * depth)         # hip -> shoulder unit-ish vector
    across = (math.cos(t), math.sin(t))              # body's left-right axis in the image
    hip = (hip_x, hip_y)
    shoulder = (hip[0] + up[0] * torso, hip[1] + up[1] * torso)
    head = (shoulder[0] + up[0] * neck, shoulder[1] + up[1] * neck)
    knee = (hip[0] + math.sin(a) * thigh_len, hip[1] + math.cos(a) * thigh_len)
    ankle = (knee[0] + math.sin(b) * shin_len, knee[1] + math.cos(b) * shin_len)
    points = [None] * 33

    def put(index, centre, side, width):
        points[index] = Landmark(centre[0] + across[0] * side * width, centre[1] + across[1] * side * width)

    for index in range(11):  # face points clustered around the nose
        put(index, head, (-1, 1)[index % 2] * (index > 0), 0.012 * height * (1 + index // 4))
    for side, (s, e, w) in ((1, (11, 13, 15)), (-1, (12, 14, 16))):  # MediaPipe left = image right (facing camera)
        put(s, shoulder, side, 0.06 * height)
        elbow = (shoulder[0] - up[0] * upper_arm, shoulder[1] - up[1] * upper_arm)
        put(e, elbow, side, 0.07 * height)
        wrist = (elbow[0] - up[0] * forearm, elbow[1] - up[1] * forearm)
        put(w, wrist, side, 0.07 * height)
        for finger in ((17, 19, 21) if side == 1 else (18, 20, 22)):
            put(finger, (wrist[0] - up[0] * 0.02, wrist[1] - up[1] * 0.02), side, 0.07 * height)
    for side, (h, k, an, heel, toe) in ((1, (23, 25, 27, 29, 31)), (-1, (24, 26, 28, 30, 32))):
        put(h, hip, side, 0.045 * height)
        put(k, knee, side, 0.045 * height)
        put(an, ankle, side, 0.045 * height)
        put(heel, (ankle[0] - 0.01, ankle[1] + 0.01), side, 0.045 * height)
        put(toe, (ankle[0] + 0.03, ankle[1] + 0.01), side, 0.045 * height)
    hide = set()
    for item in hidden:
        hide.update(GROUPS[item] if isinstance(item, str) else (item,))
    for index, point in enumerate(points):
        point.visibility = 0.05 if index in hide else visibility
        if not (0 <= point.x <= 1 and 0 <= point.y <= 1):
            point.visibility = 0.0
    if all(not (0 <= points[i].x <= 1 and 0 <= points[i].y <= 1) for i in TORSO):
        return None  # nobody in view: MediaPipe reports no pose
    return points


def _ease(kind, s):
    if kind == "in":        # accelerating, like falling under gravity
        return s * s
    if kind == "smooth":    # natural start/stop
        return s * s * (3 - 2 * s)
    return s


def keyframes(*keys):
    """keys: (time, pose dict[, easing into this key]). Returns pose_at(t) interpolating them."""
    keys = [k if len(k) == 3 else (k[0], k[1], "smooth") for k in keys]

    def pose_at(moment):
        if moment <= keys[0][0]:
            return dict(keys[0][1])
        for (t0, p0, _), (t1, p1, ease) in zip(keys, keys[1:]):
            if moment <= t1:
                s = _ease(ease, (moment - t0) / (t1 - t0)) if t1 > t0 else 1.0
                return {name: p0[name] + (p1.get(name, p0[name]) - p0[name]) * s for name in p0}
        return dict(keys[-1][1])
    return pose_at


def generate(duration, pose_at, fps=10.0, modify=None):
    """Sample pose_at(t) -> body() keyword arguments at fps. modify(t, kwargs) may adjust them."""
    frames = []
    for index in range(int(round(duration * fps)) + 1):
        moment = round(index / fps, 6)
        kwargs = pose_at(moment)
        if modify:
            kwargs = modify(moment, dict(kwargs)) or kwargs
        frames.append((moment, None if kwargs is None else body(**kwargs)))
    return frames


STAND = dict(hip_x=0.5, hip_y=0.62, trunk=0.0, thigh=0.0, shin=0.0, depth=1.0)
SIT = dict(STAND, hip_y=0.77, trunk=8.0, thigh=90.0, shin=0.0)
FLOOR = dict(STAND, hip_x=0.45, hip_y=0.88, trunk=90.0, thigh=-90.0, shin=-90.0)


def _hide(groups, start, end):
    def modify(moment, kwargs):
        if start <= moment < end:
            kwargs["hidden"] = groups
        return kwargs
    return modify


def _fast_fall(onset=2.0, fall_s=0.7, lie_s=5.0, target=FLOOR):
    return keyframes((0, STAND), (onset, STAND), (onset + fall_s, target, "in"), (onset + fall_s + lie_s, target))


def _scenarios(fps):
    walk_speed = 0.15

    def walking(moment):
        phase = 2 * math.pi * moment  # one stride per second
        return dict(STAND, hip_x=0.2 + walk_speed * moment, hip_y=0.62 + 0.008 * math.sin(2 * phase),
                    trunk=3 * math.sin(phase), thigh=20 * math.sin(phase), shin=10 * math.sin(phase - 0.6))

    def leaving(moment):
        return dict(walking(moment), hip_x=0.55 + 0.35 * moment)  # brisk exit, gone after ~2 s

    flicker = lambda moment, kwargs: dict(kwargs, hidden=("torso",)) if int(moment * fps) % 3 == 0 else kwargs
    bed_edge = dict(STAND, hip_y=0.72, trunk=5.0, thigh=90.0, shin=0.0)
    in_bed = dict(bed_edge, trunk=-90.0, thigh=90.0, shin=90.0)
    toward_camera = dict(STAND, hip_y=0.86, depth=-0.25, thigh=0.0)
    yield Scenario("standing_still", "Standing still for 8 s.",
                   generate(8, keyframes((0, STAND), (8, STAND)), fps), False, fps=fps)
    yield Scenario("walking", "Walking across the room with stride, sway and bob.",
                   generate(4, walking, fps), False, fps=fps)
    yield Scenario("sitting_down", "Normal sit into a chair (1.6 s), leaning forward ~30 degrees, then seated.",
                   generate(7, keyframes((0, STAND), (2, STAND), (2.8, dict(SIT, trunk=30.0)), (3.6, SIT), (7, SIT)), fps),
                   False, fps=fps)
    yield Scenario("fast_fall", "Standing, a 0.7 s fall to the floor, then lying still for 5 s.",
                   generate(7.7, _fast_fall(), fps), True, fall_onset=2.0, fps=fps, tags=("demo",))
    yield Scenario("lie_in_bed", "Sits on a bed edge, then lies back over 6 s (deliberate, slow).",
                   generate(13, keyframes((0, STAND), (2, STAND), (3.5, bed_edge), (4.5, bed_edge), (10.5, in_bed),
                                          (13, in_bed)), fps), False, fps=fps)
    yield Scenario("lie_in_bed_brisk", "Sits on a bed edge, then lies back over 3 s.",
                   generate(10, keyframes((0, STAND), (2, STAND), (3.5, bed_edge), (4.5, bed_edge), (7.5, in_bed),
                                          (10, in_bed)), fps), False, fps=fps,
                   notes="Known limitation: reads as a fall (see tests).")
    yield Scenario("already_in_bed", "Already lying in bed when Ember starts watching.",
                   generate(8, keyframes((0, in_bed), (8, in_bed)), fps), False, fps=fps)
    yield Scenario("bend_pick_up", "Bends to the floor (0.8 s), 0.5 s reach, straightens (0.8 s).",
                   generate(7, keyframes((0, STAND), (2, STAND), (2.8, dict(STAND, trunk=85.0)),
                                         (3.3, dict(STAND, trunk=85.0)), (4.1, STAND), (7, STAND)), fps),
                   False, fps=fps)
    yield Scenario("bend_long", "Bends and stays bent 2.5 s (e.g. tying a shoe), then straightens.",
                   generate(8, keyframes((0, STAND), (2, STAND), (2.8, dict(STAND, trunk=85.0)),
                                         (5.3, dict(STAND, trunk=85.0)), (6.1, STAND), (8, STAND)), fps),
                   False, fps=fps, notes="Known limitation: reads as a fall (see tests).")
    yield Scenario("fall_legs_hidden", "Fast fall with legs hidden behind furniture throughout.",
                   generate(7.7, _fast_fall(), fps, _hide(("legs",), 0, 99)), True, fall_onset=2.0, fps=fps)
    yield Scenario("fall_torso_dropout", "Fast fall with the torso hidden for 0.3 s mid-fall.",
                   generate(7.7, _fast_fall(), fps, _hide(("torso",), 2.25, 2.55)), True, fall_onset=2.0, fps=fps)
    yield Scenario("standing_flicker", "Standing; torso landmarks drop out every third frame.",
                   generate(8, keyframes((0, STAND), (8, STAND)), fps, flicker), False, fps=fps)
    yield Scenario("hips_hidden_desk", "Upper body only (hips behind a desk), leaning about.",
                   generate(8, keyframes((0, STAND), (3, dict(STAND, trunk=40.0)), (5, STAND), (8, STAND)), fps,
                            _hide(("torso",), 0, 99)), False, fps=fps)
    yield Scenario("leaves_frame", "Walks briskly out of frame and stays out.",
                   generate(6, leaving, fps), False, fps=fps)
    yield Scenario("fall_out_of_view", "Fast fall whose last moments and resting place are below the frame.",
                   generate(7.7, _fast_fall(target=dict(FLOOR, hip_y=1.05)), fps), True, fall_onset=2.0, fps=fps,
                   notes="Known limitation at v10's 60-degree angle threshold: one frame past 60 degrees is "
                         "visible before the torso leaves view (two are required). v8's 45 degrees caught it.")
    yield Scenario("fall_out_of_view_early", "Fast fall that leaves the bottom of the frame almost at once.",
                   generate(7.7, _fast_fall(target=dict(FLOOR, hip_y=1.15)), fps), True, fall_onset=2.0, fps=fps,
                   notes="Known limitation: the torso leaves view after one suspicious frame (two are required).")
    yield Scenario("fall_toward_camera", "Falls forward toward the camera: torso foreshortens, stays near-vertical in the image.",
                   generate(7.7, _fast_fall(target=toward_camera), fps), True, fall_onset=2.0, fps=fps,
                   notes="Known limitation: 2D trunk angle cannot see falls along the camera axis.")


def scenarios(fps=10.0):
    """All scenarios by name, in a stable order."""
    return {item.name: item for item in _scenarios(fps)}


def scenario(name, fps=10.0):
    return scenarios(fps)[name]


DEMO_SCENARIOS = ("fast_fall", "bend_pick_up", "sitting_down", "lie_in_bed")
