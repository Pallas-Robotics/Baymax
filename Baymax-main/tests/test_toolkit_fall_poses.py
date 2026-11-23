"""Made-up pose sequences through the fall decision logic. No camera, MediaPipe or images.

Each test replays an idealized stick-figure sequence (features/toolkits/fall_poses.py),
at v10's 10 FPS unless stated, with explicit timestamps and checks fall / no fall and timing. Cases marked
KNOWN LIMITATION are idealized inputs the current rules get wrong; they are
expectedFailure so the suite documents them without blocking, and an
"unexpected success" will flag when the rules change.
"""
from pathlib import Path
import re
import unittest

from fall_detection import detector as upstream
from features.toolkits import fall_logic, fall_poses

ROOT = Path(__file__).resolve().parents[1]
FPS = fall_logic.V10_FPS
SCENARIOS = fall_poses.scenarios(FPS)


def run(name, fps=FPS):
    scenario = fall_poses.scenario(name, fps) if fps != FPS else SCENARIOS[name]
    results = fall_logic.replay(scenario.frames)
    detected = [scenario.frames[i][0] for i, result in enumerate(results) if result["fall_detected"]]
    return scenario, results, detected


class SyntheticSequenceTests(unittest.TestCase):
    def test_frames_use_mediapipe_33_landmark_layout(self):
        for scenario in SCENARIOS.values():
            self.assertGreater(len(scenario.frames), 30, scenario.name)
            times = [moment for moment, _ in scenario.frames]
            self.assertEqual(times, sorted(times))
            for _, landmarks in scenario.frames:
                if landmarks is None:
                    continue
                self.assertEqual(len(landmarks), 33)
                for point in landmarks:
                    self.assertTrue(0 <= point.visibility <= 1)
                    if point.visibility > 0:  # anything visible is inside the normalized image
                        self.assertTrue(0 <= point.x <= 1 and 0 <= point.y <= 1, scenario.name)

    def test_standing_pose_geometry(self):
        points = fall_poses.body()
        shoulder_y = (points[11].y + points[12].y) / 2
        hip_y = (points[23].y + points[24].y) / 2
        self.assertLess(points[0].y, shoulder_y)   # head above shoulders (y grows downward)
        self.assertLess(shoulder_y, hip_y)
        self.assertLess(hip_y, points[27].y)       # ankles lowest
        self.assertEqual(fall_logic.posture(points, 0.0), "standing")

    def test_out_of_frame_and_hidden_landmarks(self):
        self.assertIsNone(fall_poses.body(hip_x=1.4))
        partial = fall_poses.body(hip_x=0.98)
        self.assertTrue(any(point.visibility == 0 for point in partial))
        hidden = fall_poses.body(hidden=("legs",))
        self.assertTrue(all(hidden[i].visibility < 0.5 for i in fall_poses.GROUPS["legs"]))
        self.assertTrue(all(hidden[i].visibility == 1 for i in fall_poses.TORSO))

    def test_demo_scenarios_exist(self):
        for name in fall_poses.DEMO_SCENARIOS:
            self.assertIn(name, SCENARIOS)


KEYS = ("fall_detected", "fall_pending", "fall_active", "trunk_angle", "angular_vel", "hip_descent_vel")
RUNTIME = (ROOT / "realtime_gemini_10.py").read_text(encoding="utf-8")


def runtime_constant(name):
    return float(re.search(rf"^{name}\s*=\s*([0-9.]+)", RUNTIME, re.M).group(1))


def runtime_detector():
    """The FallDetector exactly as realtime_gemini_10._fall_detection_thread builds it."""
    return upstream.FallDetector(
        angle_threshold=runtime_constant("FALL_ANGLE_THRESHOLD"),
        ang_vel_threshold=runtime_constant("FALL_ANG_VEL_THRESHOLD"),
        hip_vel_threshold=runtime_constant("FALL_HIP_VEL_THRESHOLD"),
        history_window=int(runtime_constant("FALL_HISTORY_FRAMES")),
        confirmation_frames=int(runtime_constant("FALL_CONFIRMATION_FRAMES")),
        cooldown_seconds=runtime_constant("FALL_COOLDOWN_SECONDS"),
        stay_down_s=runtime_constant("FALL_STAY_DOWN_SECONDS"))


class DecisionMatchesRuntimeTests(unittest.TestCase):
    """FallDecision must make exactly the decisions of v10's own detector (the vendored
    FallDetector with realtime_gemini_10's FALL_* thresholds, fed capture timestamps)."""

    def test_every_scenario_frame_by_frame(self):
        for fps in (FPS, 15.0, 5.0):
            for scenario in fall_poses.scenarios(fps).values():
                reference = runtime_detector()
                expected = [reference.update(landmarks, timestamp=100.0 + moment)
                            for moment, landmarks in scenario.frames]
                actual = fall_logic.replay(scenario.frames, start=100.0)
                for index, (want, got) in enumerate(zip(expected, actual)):
                    self.assertEqual({k: want[k] for k in KEYS}, {k: got[k] for k in KEYS},
                                     (fps, scenario.name, index))

    def test_labeler_on_the_runtime_detector_matches_fall_decision(self):
        # The demo screen labels the runtime's own camera results with Labeler.
        for scenario in SCENARIOS.values():
            detector, labeler = runtime_detector(), fall_logic.Labeler()
            decision = fall_logic.FallDecision()
            for moment, landmarks in scenario.frames:
                result = detector.update(landmarks, timestamp=moment)
                labels = labeler.label(detector, landmarks, result, moment)
                self.assertEqual(labels, {k: v for k, v in decision.update(landmarks, moment).items()
                                          if k in ("posture", "countdown_s")}, (scenario.name, moment))

    def test_settings_match_v10_runtime_thresholds(self):
        settings = fall_logic.V10_SETTINGS
        self.assertEqual(settings["angle_threshold"], runtime_constant("FALL_ANGLE_THRESHOLD"))
        self.assertEqual(settings["ang_vel_threshold"], runtime_constant("FALL_ANG_VEL_THRESHOLD"))
        self.assertEqual(settings["hip_vel_threshold"], runtime_constant("FALL_HIP_VEL_THRESHOLD"))
        self.assertEqual(settings["history_window"], runtime_constant("FALL_HISTORY_FRAMES"))
        self.assertEqual(settings["confirmation_frames"], runtime_constant("FALL_CONFIRMATION_FRAMES"))
        self.assertEqual(settings["cooldown_seconds"], runtime_constant("FALL_COOLDOWN_SECONDS"))
        self.assertEqual(settings["stay_down_s"], runtime_constant("FALL_STAY_DOWN_SECONDS"))
        self.assertEqual(fall_logic.V10_FPS, runtime_constant("FALL_DETECTION_FPS"))
        # The runtime still builds its detector from exactly these constants.
        for argument in ("angle_threshold=FALL_ANGLE_THRESHOLD", "ang_vel_threshold=FALL_ANG_VEL_THRESHOLD",
                         "hip_vel_threshold=FALL_HIP_VEL_THRESHOLD", "history_window=FALL_HISTORY_FRAMES",
                         "confirmation_frames=FALL_CONFIRMATION_FRAMES", "cooldown_seconds=FALL_COOLDOWN_SECONDS",
                         "stay_down_s=FALL_STAY_DOWN_SECONDS", "fall_detector.update(landmarks, timestamp=captured_at)"):
            self.assertIn(argument, RUNTIME)


class NoFallTests(unittest.TestCase):
    def assertNoFall(self, name):
        scenario, results, detected = run(name)
        self.assertFalse(scenario.expect_fall)
        self.assertEqual(detected, [], name)
        return results

    def test_standing_still(self):
        results = self.assertNoFall("standing_still")
        self.assertTrue(all(result["posture"] == "standing" for result in results))
        self.assertFalse(any(result["fall_pending"] for result in results))

    def test_walking(self):
        results = self.assertNoFall("walking")
        self.assertFalse(any(result["fall_pending"] for result in results))

    def test_sitting_down_normally(self):
        results = self.assertNoFall("sitting_down")
        self.assertFalse(any(result["fall_pending"] for result in results))
        self.assertEqual(results[-1]["posture"], "sitting")

    def test_lying_down_slowly_in_bed(self):
        results = self.assertNoFall("lie_in_bed")
        self.assertFalse(any(result["fall_pending"] for result in results))
        self.assertEqual(results[-1]["posture"], "lying")  # horizontal, but not "on floor (possible fall)"

    def test_already_lying_in_bed(self):
        results = self.assertNoFall("already_in_bed")
        self.assertTrue(all(result["posture"] == "lying" for result in results))

    def test_bending_to_pick_something_up_opens_then_cancels(self):
        results = self.assertNoFall("bend_pick_up")
        self.assertTrue(any(result["fall_pending"] for result in results))  # looks like a fall at first
        self.assertFalse(results[-1]["fall_pending"])
        self.assertEqual(results[-1]["posture"], "standing")

    def test_flickering_torso_landmarks(self):
        self.assertNoFall("standing_flicker")

    def test_hips_hidden_behind_desk(self):
        results = self.assertNoFall("hips_hidden_desk")
        self.assertTrue(all(result["posture"] == "not visible" for result in results))

    def test_leaving_the_frame(self):
        results = self.assertNoFall("leaves_frame")
        self.assertEqual(results[-1]["posture"], "not visible")
        self.assertFalse(results[-1]["fall_pending"])


class FallTests(unittest.TestCase):
    def assertFallOnce(self, name, fps=FPS):
        scenario, results, detected = run(name, fps)
        self.assertTrue(scenario.expect_fall)
        self.assertEqual(len(detected), 1, (name, detected))
        delay = detected[0] - scenario.fall_onset
        # Two-second stay-down plus the time for the trunk to pass 60 degrees and two confirming frames.
        self.assertGreaterEqual(delay, 2.0, name)
        self.assertLessEqual(delay, 3.0, name)
        return results, delay

    def test_fast_fall_then_lying_still(self):
        results, delay = self.assertFallOnce("fast_fall")
        self.assertAlmostEqual(delay, 2.7, delta=0.1)
        # While lying still, detection does not re-trigger after the 3 s cooldown.
        self.assertEqual(results[-1]["posture"], "on floor (possible fall)")
        self.assertFalse(results[-1]["fall_active"])

    def test_countdown_runs_from_stay_down_to_zero(self):
        _, results, _ = run("fast_fall")
        countdowns = [result["countdown_s"] for result in results if result["countdown_s"] is not None]
        self.assertAlmostEqual(countdowns[0], 2.0, places=6)
        self.assertEqual(countdowns, sorted(countdowns, reverse=True))
        self.assertLessEqual(countdowns[-1], 1 / FPS + 1e-6)  # the last frame before confirmation
        labels = [result["posture"] for result in results]
        self.assertIn("going down (possible fall)", labels)
        self.assertLess(labels.index("going down (possible fall)"), labels.index("on floor (possible fall)"))

    def test_fall_detected_at_lower_frame_rates(self):
        for fps in (15.0, 8.0, 5.0):
            with self.subTest(fps=fps):
                self.assertFallOnce("fast_fall", fps)

    def test_fall_with_legs_hidden(self):
        self.assertFallOnce("fall_legs_hidden")

    def test_fall_with_torso_hidden_mid_fall(self):
        self.assertFallOnce("fall_torso_dropout")

    def test_getting_up_after_a_fall_clears_on_floor(self):
        frames = fall_poses.generate(10, fall_poses.keyframes(
            (0, fall_poses.STAND), (2, fall_poses.STAND), (2.7, fall_poses.FLOOR, "in"), (6, fall_poses.FLOOR),
            (8, fall_poses.STAND), (10, fall_poses.STAND)))
        results = fall_logic.replay(frames)
        self.assertEqual(sum(result["fall_detected"] for result in results), 1)
        self.assertEqual(results[-1]["posture"], "standing")


class KnownLimitationTests(unittest.TestCase):
    """KNOWN LIMITATION: idealized inputs the current rules get wrong. Not fixed here
    because each needs a design/threshold decision and real-camera evidence, not a small fix."""

    @unittest.expectedFailure
    def test_brisk_lie_down_in_bed_is_not_a_fall(self):
        # Lying back onto a bed over 3 s peaks above the 25 deg/s rotation threshold past 60 degrees,
        # starts from upright within 1.5 s and then stays down, so it is confirmed as a fall.
        self.assertEqual(run("lie_in_bed_brisk")[2], [])

    @unittest.expectedFailure
    def test_long_bend_is_not_a_fall(self):
        # Staying bent over for more than the 2 s stay-down (tying a shoe) is confirmed as a fall:
        # "stay-down" only checks the trunk is not back under 30 degrees, not that hips dropped.
        self.assertEqual(run("bend_long")[2], [])

    @unittest.expectedFailure
    def test_fall_ending_out_of_view_is_detected(self):
        # New with v10's 60-degree threshold (v8's 45 caught it): only one frame past 60 degrees
        # is visible before the torso leaves the bottom of the frame; two are required.
        self.assertEqual(len(run("fall_out_of_view")[2]), 1)

    @unittest.expectedFailure
    def test_fall_that_leaves_view_immediately_is_detected(self):
        # The torso drops below the frame after one suspicious frame; two consecutive are required,
        # so no pending fall opens and the later "hidden frames do not cancel" rule never applies.
        self.assertEqual(len(run("fall_out_of_view_early")[2]), 1)

    @unittest.expectedFailure
    def test_fall_toward_the_camera_is_detected(self):
        # A fall along the camera axis keeps the 2D shoulder-hip line near vertical (angle ~0),
        # so the 60-degree trunk-angle condition never holds even though the hips drop fast.
        self.assertEqual(len(run("fall_toward_camera")[2]), 1)

    def test_limitations_are_labelled_in_the_generator(self):
        for name in ("lie_in_bed_brisk", "bend_long", "fall_out_of_view", "fall_out_of_view_early",
                     "fall_toward_camera"):
            self.assertIn("Known limitation", SCENARIOS[name].notes)


class PostureTests(unittest.TestCase):
    def test_labels(self):
        cases = [(fall_poses.body(), 0, "standing"), (fall_poses.body(**dict(fall_poses.SIT)), 8, "sitting"),
                 (fall_poses.body(trunk=40), 40, "bending"), (fall_poses.body(**dict(fall_poses.FLOOR)), 90, "lying"),
                 (fall_poses.body(hidden=("legs",)), 0, "upright"), (fall_poses.body(hidden=("torso",)), 0, "not visible"),
                 (None, None, "not visible")]
        for landmarks, angle, label in cases:
            self.assertEqual(fall_logic.posture(landmarks, angle), label)

    def test_points_are_compact(self):
        points = fall_logic.points(fall_poses.body())
        self.assertEqual(len(points), 33)
        self.assertEqual(len(points[0]), 3)
        self.assertIsNone(fall_logic.points(None))


if __name__ == "__main__":
    unittest.main()
