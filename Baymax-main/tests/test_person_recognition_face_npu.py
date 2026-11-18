"""SFace + AuraFace on the NPU (face_npu.py): alignment, fusion, when it is used, the fused
thresholds and samples, and moving people saved with SFace alone over without forgetting
anyone. No NPU and no model files: the models are fakes, the thresholds are the real ones."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from features.person_recognition import face_npu, faces
from features.person_recognition.people import People

RNG = np.random.default_rng(11)


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def sample(base, similarity):
    noise = RNG.normal(size=len(base))
    noise -= (noise @ base) * base
    return unit(similarity * base + np.sqrt(1 - similarity ** 2) * unit(noise))


class Clock:
    def __init__(self):
        self.t = 5_000.0

    def __call__(self):
        return self.t


class Fused(unittest.TestCase):
    """Runs a test with the fused thresholds and key, restoring SFace's after."""

    def setUp(self):
        faces.use_recognizer(faces.FUSED)
        self.addCleanup(faces.use_recognizer, "sface")


class AlignmentTests(unittest.TestCase):
    def test_the_landmarks_land_on_arcfaces_template(self):
        import cv2
        frame = np.zeros((480, 640, 3), np.uint8)
        # The template scaled 2x and moved: a face at (200, 100)
        landmarks = face_npu.TEMPLATE * 2 + np.array([200, 100], np.float32)
        for (x, y) in landmarks:
            cv2.circle(frame, (int(x), int(y)), 3, (255, 255, 255), -1)
        crop = face_npu.align(frame, landmarks)
        self.assertEqual(crop.shape, (112, 112, 3))
        for (x, y) in face_npu.TEMPLATE:
            self.assertGreater(crop[int(round(y)), int(round(x))].max(), 100)

    def test_fused_scores_weigh_both_models(self):
        s1, s2 = unit(RNG.normal(size=128)), unit(RNG.normal(size=128))
        a1, a2 = unit(RNG.normal(size=512)), unit(RNG.normal(size=512))
        f1, f2 = face_npu.fuse(s1, a1), face_npu.fuse(s2, a2)
        self.assertEqual(f1.shape, (faces.FUSED_DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(f1)), 1.0, places=5)
        w = face_npu.SFACE_WEIGHT
        self.assertAlmostEqual(float(f1 @ f2), w * float(s1 @ s2) + (1 - w) * float(a1 @ a2), places=5)
        self.assertTrue(np.allclose(face_npu.sface_part(f1), s1, atol=1e-5))
        self.assertTrue(np.allclose(faces._sface_part(f1), s1, atol=1e-5))
        self.assertEqual(faces.FUSED_SFACE_WEIGHT, face_npu.SFACE_WEIGHT)

    def test_rgb_order_for_the_npu(self):
        crop = np.zeros((112, 112, 3), np.uint8)
        crop[..., 0] = 255                       # blue in OpenCV's BGR
        self.assertEqual(face_npu.to_rgb(crop)[0, 0].tolist(), [0, 0, 255])


class ReadyTests(unittest.TestCase):
    def manifest(self, folder, **fields):
        face_npu.write_manifest({"rknn": {"file": "m.rknn", "size": 4}, **fields}, folder)
        with open(os.path.join(folder, "m.rknn"), "wb") as f:
            f.write(b"1234")

    def test_used_only_on_the_orange_pi_after_a_passed_check(self):
        folder = tempfile.mkdtemp()
        self.manifest(folder, check="passed")
        linux = patch.multiple("platform", system=lambda: "Linux", machine=lambda: "aarch64")
        with linux:
            self.assertTrue(face_npu.ready(folder))
            with patch.dict(os.environ, {"BAYMAX_FACE_NPU": "0"}):
                self.assertFalse(face_npu.ready(folder))
        with patch.multiple("platform", system=lambda: "Darwin", machine=lambda: "arm64"):
            self.assertFalse(face_npu.ready(folder))                # not on a Mac
        self.manifest(folder, check="failed")
        with linux:
            self.assertFalse(face_npu.ready(folder))
        self.manifest(folder, check="passed")
        with open(os.path.join(folder, "m.rknn"), "wb") as f:
            f.write(b"12")                                   # a half-copied model
        with linux:
            self.assertFalse(face_npu.ready(folder))


class ThresholdTests(unittest.TestCase):
    def test_each_model_has_its_own_thresholds_and_samples(self):
        sface = {name: getattr(faces, name) for name in faces.THRESHOLD_NAMES}
        faces.use_recognizer(faces.FUSED)
        try:
            self.assertEqual(faces.EMBEDDINGS_KEY, "embeddings_sface_auraface")
            for name in faces.THRESHOLD_NAMES:
                self.assertIsNotNone(getattr(faces, name), name)
            self.assertLess(faces.MAYBE_THRESHOLD, faces.MATCH_THRESHOLD)
            self.assertLess(faces.ANGLE_LEARN_RANGE[0], faces.ANGLE_LEARN_RANGE[1])
            self.assertEqual(faces.ANGLE_LEARN_RANGE[1], faces.AUTO_LEARN_RANGE[0])
        finally:
            faces.use_recognizer("sface")
        self.assertEqual({name: getattr(faces, name) for name in faces.THRESHOLD_NAMES}, sface)
        self.assertEqual(faces.EMBEDDINGS_KEY, "embeddings")

    def test_an_sface_score_means_the_same_on_the_fused_scale(self):
        faces.use_recognizer(faces.FUSED)
        try:
            sface = faces.RECOGNIZERS["sface"]
            for name in ("MATCH_THRESHOLD", "MAYBE_THRESHOLD", "INTRO_MAX_SCORE", "SAMPLE_CONSISTENCY"):
                self.assertAlmostEqual(faces._sface_to_fused(sface[name]), getattr(faces, name), places=3)
            # LFW (README): 0.50 on SFace's scale is 0.395 fused
            self.assertAlmostEqual(faces.MATCH_THRESHOLD, 0.395, places=3)
        finally:
            faces.use_recognizer("sface")


class FakeModels:
    """Faces are dicts with an SFace and an AuraFace embedding; embed fuses them."""

    def detect(self, frame, scale=1.0):
        out = []
        for f in frame:
            x, y, w, h = f["box"]
            lm = [(x + .3 * w, y + .4 * h), (x + .7 * w, y + .4 * h), (x + .5 * w, y + .6 * h),
                  (x + .35 * w, y + .8 * h), (x + .65 * w, y + .8 * h)]
            det = faces.Detection(box=f["box"], landmarks=np.array(lm, np.float32), score=0.95, row=None)
            det.face = f
            out.append(det)
        return out

    def refine(self, frame, det):
        return det

    def fingerprint(self, frame, det):
        return unit(RNG.normal(size=16))            # always re-identified

    def embed(self, frame, det):
        return face_npu.fuse(det.face["sface"], det.face["aura"])


class MigrationTests(Fused):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "people.json")
        self.clock = Clock()
        self.joe_sface, self.joe_aura = unit(RNG.normal(size=128)), unit(RNG.normal(size=512))
        old = faces.PeopleStore(self.path, key="embeddings")      # saved before AuraFace
        old.add_samples("Joe", [sample(self.joe_sface, .8) for _ in range(5)], source="conversation")
        self.store = faces.PeopleStore(self.path, clock=self.clock)
        self.models = FakeModels()
        self.ident = faces.FaceIdentifier(self.models, self.store, clock=self.clock)

    def frames(self, make, seconds, fps=2):
        for _ in range(int(seconds * fps)):
            self.ident.process(make())
            self.clock.t += 1 / fps

    def joe(self):
        return {"box": (200, 100, 120, 120), "sface": sample(self.joe_sface, .9),
                "aura": sample(self.joe_aura, .9)}

    def test_someone_saved_with_sface_is_still_recognised_and_moves_over(self):
        self.assertTrue(self.store.unmigrated())
        # Known before a single fused sample: not a stranger, and "I'm Joe" fits
        track = SimpleNamespace(samples=[face_npu.fuse(sample(self.joe_sface, .9), unit(RNG.normal(size=512)))],
                                maybe_id=None)
        self.assertEqual(self.ident._looks_known(track), self.store.find_by_name("Joe")["id"])
        self.assertGreaterEqual(self.store.resemblance(self.store.find_by_name("Joe")["id"], track.samples),
                                faces.SAMPLE_CONSISTENCY)
        with patch("builtins.print"):
            self.frames(lambda: [self.joe()], 6)
        self.assertEqual(self.ident.describe_in_view(), "Joe is in front of you.")
        joe = self.store.find_by_name("Joe")
        self.assertGreaterEqual(len(joe["embeddings_sface_auraface"]), faces.MIGRATE_SAMPLES)
        self.assertEqual(len(joe["embeddings"]), 5)                 # SFace's kept (for going back)
        self.assertFalse(self.store.unmigrated())
        # Next visit: recognised from the fused samples
        self.clock.t += 3600
        self.frames(lambda: [self.joe()], 3)
        self.assertEqual(self.ident.describe_in_view(), "Joe is in front of you.")
        pid, score, legacy = self.store.match_all(face_npu.fuse(*(lambda j: (j["sface"], j["aura"]))(self.joe())))
        self.assertEqual((pid, legacy), (joe["id"], False))

    def test_back_to_sface_alone_keeps_people_met_with_the_npu(self):
        """The NPU model stops working: people met meanwhile are still known by SFace."""
        ann_s, ann_a = unit(RNG.normal(size=128)), unit(RNG.normal(size=512))
        self.store.add_samples("Ann", [face_npu.fuse(sample(ann_s, .85), sample(ann_a, .85))
                                       for _ in range(3)], source="conversation")
        faces.use_recognizer("sface")
        sface_store = faces.PeopleStore(self.path)
        pid, score = sface_store.match(sample(ann_s, .9))
        self.assertEqual(sface_store.get(pid)["name"], "Ann")
        self.assertGreaterEqual(score, faces.MATCH_THRESHOLD)

    def test_a_stranger_is_not_given_someones_name(self):
        stranger = {"box": (200, 100, 120, 120), "sface": unit(RNG.normal(size=128)),
                    "aura": unit(RNG.normal(size=512))}
        self.frames(lambda: [dict(stranger, sface=sample(stranger["sface"], .9),
                                  aura=sample(stranger["aura"], .9))], 6)
        self.assertNotIn("Joe", self.ident.describe_in_view())
        self.assertNotIn("embeddings_sface_auraface", self.store.find_by_name("Joe"))

    def test_new_people_are_saved_with_the_new_model(self):
        bob = unit(RNG.normal(size=512))
        self.frames(lambda: [{"box": (200, 100, 120, 120), "sface": unit(RNG.normal(size=128)),
                              "aura": sample(bob, .9)}], 6)
        self.ident.pending_introduction()
        with patch("builtins.print"):
            self.assertEqual(self.ident.handle_tool_call("remember_person", {"name": "Bob"})["status"],
                             "remembered")
        self.assertEqual(len(self.store.find_by_name("Bob")["embeddings_sface_auraface"][0]), faces.FUSED_DIM)


class NpuFallbackTests(unittest.TestCase):
    def test_sface_when_the_npu_model_wont_start(self):
        """people.People: the child with AuraFace fails to start -> SFace, without waiting."""
        people = People()
        started = []

        class Remote:
            def __init__(self, paths):
                started.append(dict(paths))
                if paths.get("npu"):
                    raise RuntimeError("init_runtime(core 1) failed")
                raise SystemExit                          # stop the loop here

        with patch.dict(os.environ, {"BAYMAX_PEOPLE_RECOGNITION": "1", "BAYMAX_VOICE_RECOGNITION": "0"}), \
                patch.object(faces, "ensure_models", lambda: {"detector": "d", "recognizer": "r"}), \
                patch.object(face_npu, "ready", lambda: True), \
                patch.object(face_npu, "model_path", lambda: "/m.rknn"), \
                patch("features.person_recognition.face_process.RemoteFaceModels", Remote), \
                patch.object(faces, "PeopleStore", lambda: SimpleNamespace(key=faces.EMBEDDINGS_KEY)), \
                patch("builtins.print"):
            try:
                with self.assertRaises(SystemExit):
                    people._run()
            finally:
                recognizer = faces.RECOGNIZER
                faces.use_recognizer("sface")
        self.assertEqual([p.get("npu") for p in started], ["/m.rknn", None])
        self.assertEqual(recognizer, "sface")


if __name__ == "__main__":
    unittest.main()
