"""AuraFace on the RK3588 NPU, combined with SFace for a stronger face recognizer.

AuraFace (fal, Apache-2.0, trained on data described as commercially usable) is an
ArcFace-style ResNet100: a 112x112 face aligned on 5 landmarks -> a 512-d embedding.
On its own it is no better than SFace (LFW, README "A stronger face model"), but the two
make different mistakes, so Ember uses both: each face's SFace and AuraFace embeddings
are joined into one 640-d unit vector (fuse), whose dot product with another is
SFACE_WEIGHT * SFace score + (1 - SFACE_WEIGHT) * AuraFace score. On LFW that misses
about half as many people as SFace at the same false-match rate. InsightFace's own
ArcFace models score higher but are licensed for non-commercial research only.

The model is converted on the Orange Pi by convert_face_rknn.py (FP16, the input
normalisation baked in, so the NPU takes uint8 RGB NHWC) and used only after that script's
check on the NPU passed (manifest "check": "passed"). It runs in the face child process
(face_process.FaceChild) on NPU core BAYMAX_FACE_NPU_CORE (default 1; pose uses core 0).
SFace alone is used wherever it isn't ready (Mac, LattePanda, an Orange Pi before
conversion); BAYMAX_FACE_NPU=0 keeps SFace alone.
"""
import json
import os
import platform

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.getenv("BAYMAX_FACE_NPU_DIR") or os.path.join(ROOT, ".models", "faces_npu")
MANIFEST = "manifest.json"
RKNN_FILE = "auraface_fp16.rknn"
ONNX_FILE = "glintr100.onnx"
# fal/AuraFace-v1 at a fixed revision, checked against this SHA-256.
ONNX_URL = ("https://huggingface.co/fal/AuraFace-v1/resolve/"
            "af6d057c9b0ec4071d4c49c80e3539258798b609/glintr100.onnx")
ONNX_SHA256 = "a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60"
INPUT_SIZE = 112
MEAN, STD = 127.5, 127.5     # InsightFace's ArcFace preprocessing: (RGB - 127.5) / 127.5
EMBED_DIM = 512
NAME = "auraface"
# Fusion, fitted on LFW (6000 pairs): AuraFace's different-person scores spread
# 1/1.564 as wide as SFace's, so it gets weight 1.564 / 2.564 and SFace 1 / 2.564.
SFACE_WEIGHT = 0.39
SFACE_DIM = 128
NPU_CORES = {"0": "NPU_CORE_0", "1": "NPU_CORE_1", "2": "NPU_CORE_2", "auto": "NPU_CORE_AUTO"}

# The 5-point template ArcFace models are trained on (eyes, nose, mouth corners in a
# 112x112 crop, image left first). YuNet's landmarks come in the same order.
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)


def load_manifest(directory=None):
    try:
        with open(os.path.join(directory or MODEL_DIR, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(manifest, directory=None):
    directory = directory or MODEL_DIR
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    os.replace(tmp, os.path.join(directory, MANIFEST))


def ready(directory=None):
    """The converted model is there and passed its check on this board's NPU."""
    if os.getenv("BAYMAX_FACE_NPU", "1") == "0":
        return False
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        return False
    directory = directory or MODEL_DIR
    manifest = load_manifest(directory)
    rknn = (manifest.get("rknn") or {})
    path = os.path.join(directory, rknn.get("file", RKNN_FILE))
    return (manifest.get("check") == "passed" and os.path.exists(path)
            and os.path.getsize(path) == rknn.get("size"))


def model_path(directory=None):
    directory = directory or MODEL_DIR
    return os.path.join(directory, (load_manifest(directory).get("rknn") or {}).get("file", RKNN_FILE))


def similarity_transform(src, dst):
    """Least-squares rotation + uniform scale + shift taking points `src` onto `dst`
    (Umeyama 1991), as InsightFace aligns faces: all 5 landmarks count, unlike OpenCV's
    robust estimators, which fit a subset."""
    src, dst = np.asarray(src, np.float64), np.asarray(dst, np.float64)
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    a, b = src - src_mean, dst - dst_mean
    cov = b.T @ a / len(src)
    u, d, vt = np.linalg.svd(cov)
    sign = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[1, 1] = -1
    rotation = u @ sign @ vt
    scale = np.trace(np.diag(d) @ sign) / a.var(axis=0).sum()
    matrix = np.zeros((2, 3))
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = dst_mean - matrix[:, :2] @ src_mean
    return matrix


def align(frame, landmarks):
    """The 112x112 BGR face crop ArcFace models expect, from 5 landmarks (5x2)."""
    import cv2
    matrix = similarity_transform(np.asarray(landmarks, np.float32).reshape(5, 2), TEMPLATE)
    return cv2.warpAffine(frame, matrix, (INPUT_SIZE, INPUT_SIZE), borderValue=0.0)


def fuse(sface, auraface):
    """One 640-d unit vector from a face's SFace (128-d) and AuraFace (512-d) unit
    embeddings; a.b of two of these = SFACE_WEIGHT * SFace a.b + (1-SFACE_WEIGHT) * AuraFace a.b."""
    return np.concatenate([np.sqrt(SFACE_WEIGHT) * normalize(sface),
                           np.sqrt(1 - SFACE_WEIGHT) * normalize(auraface)]).astype(np.float32)


def sface_part(fused):
    """The SFace embedding inside a fused one (for people saved with SFace alone)."""
    return normalize(np.asarray(fused, np.float32)[..., :SFACE_DIM])


def to_rgb(crop):
    return np.ascontiguousarray(crop[:, :, ::-1])


def normalize(v):
    v = np.asarray(v, np.float32).ravel()
    return v / (np.linalg.norm(v) + 1e-9)


class NpuEmbedder:
    """AuraFace on the NPU (rknn-toolkit-lite2), one face at a time."""
    name = NAME

    def __init__(self, path=None, core=None):
        from rknnlite.api import RKNNLite
        core = str(core or os.getenv("BAYMAX_FACE_NPU_CORE", "1")).strip().lower()
        if core not in NPU_CORES:
            raise ValueError(f"Unknown NPU core {core!r}; use one of {', '.join(NPU_CORES)}")
        self.runtime = RKNNLite(verbose=False)
        path = path or model_path()
        if self.runtime.load_rknn(path) != 0:
            raise RuntimeError(f"load_rknn({path}) failed")
        if self.runtime.init_runtime(core_mask=getattr(RKNNLite, NPU_CORES[core])) != 0:
            raise RuntimeError(f"init_runtime(core {core}) failed")

    def embed_crop(self, crop_bgr):
        outputs = self.runtime.inference(inputs=[to_rgb(crop_bgr)[None]], data_format="nhwc")
        if not outputs:
            raise RuntimeError("NPU inference returned nothing")
        return normalize(outputs[0])

    def embed(self, frame, landmarks):
        return self.embed_crop(align(frame, landmarks))

    def close(self):
        try:
            self.runtime.release()
        except Exception:
            pass


class OnnxEmbedder:
    """The same model on ONNX Runtime (CPU): the reference the NPU is checked against."""
    name = NAME

    def __init__(self, path, threads=4):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def embed_crop(self, crop_bgr):
        x = (to_rgb(crop_bgr).astype(np.float32) - MEAN) / STD
        return normalize(self.session.run(None, {self.input: x.transpose(2, 0, 1)[None]})[0])

    def embed(self, frame, landmarks):
        return self.embed_crop(align(frame, landmarks))
