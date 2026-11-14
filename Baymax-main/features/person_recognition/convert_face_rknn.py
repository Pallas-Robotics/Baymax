"""Convert AuraFace for the RK3588 NPU, on the Orange Pi (face_npu.py uses the result).

    <pose build>/venv-rknn/bin/python features/person_recognition/convert_face_rknn.py all \\
        [--images DIR]

Uses the environment pose's NPU conversion already made (features/pose/setup_converter.sh:
rknn-toolkit2 with rknn-toolkit-lite2, onnxruntime and OpenCV). Steps:

download  glintr100.onnx from fal/AuraFace-v1 at a fixed revision, SHA-256 checked.
build     FP16 RKNN for rk3588, (RGB - 127.5) / 127.5 baked in: the NPU takes the aligned
          112x112 face as uint8 RGB NHWC.
check     On the NPU: embeddings of real face crops (--images: photos with one face each,
          found with Ember's YuNet and aligned like the robot does) and of random crops,
          against ONNX Runtime on the same crops. Passes when every cosine is >= MIN_COSINE.
          Also times the NPU. face_npu.ready() is true only after a pass.

Everything goes to .models/faces_npu (BAYMAX_FACE_NPU_DIR). Nothing is uploaded.
"""
import argparse
import datetime
import glob
import hashlib
import os
import platform
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from features.person_recognition import face_npu  # noqa: E402

MIN_COSINE = 0.99          # NPU (FP16) vs ONNX Runtime (FP32) on the same crop
RANDOM_CROPS = 8
TIMING_RUNS = 30


def log(text):
    print(f"[FACE NPU] {text}", flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def onnx_path():
    return os.path.join(face_npu.MODEL_DIR, face_npu.ONNX_FILE)


def download(_args):
    path = onnx_path()
    if os.path.exists(path) and sha256(path) == face_npu.ONNX_SHA256:
        log(f"{path} already there.")
        return
    os.makedirs(face_npu.MODEL_DIR, exist_ok=True)
    tmp = path + ".part"
    log(f"Downloading {face_npu.ONNX_URL} (261 MB) ...")
    with urllib.request.urlopen(face_npu.ONNX_URL, timeout=60) as response, open(tmp, "wb") as f:
        shutil.copyfileobj(response, f, 1 << 20)
    digest = sha256(tmp)
    if digest != face_npu.ONNX_SHA256:
        os.remove(tmp)
        raise RuntimeError(f"SHA-256 mismatch: got {digest}")
    os.replace(tmp, path)
    manifest = face_npu.load_manifest()
    manifest["onnx"] = {"file": face_npu.ONNX_FILE, "sha256": digest, "url": face_npu.ONNX_URL,
                        "license": "Apache-2.0 (fal/AuraFace-v1)"}
    face_npu.write_manifest(manifest)
    log("Downloaded and checked.")


def build(args):
    from rknn.api import RKNN
    path = onnx_path()
    if not os.path.exists(path) or sha256(path) != face_npu.ONNX_SHA256:
        raise RuntimeError("Run the download step first.")
    output = os.path.join(face_npu.MODEL_DIR, face_npu.RKNN_FILE)
    rknn = RKNN(verbose=args.verbose)
    try:
        rknn.config(mean_values=[[face_npu.MEAN] * 3], std_values=[[face_npu.STD] * 3],
                    target_platform="rk3588")
        if rknn.load_onnx(model=path, inputs=["data"],
                          input_size_list=[[1, 3, face_npu.INPUT_SIZE, face_npu.INPUT_SIZE]]) != 0:
            raise RuntimeError("load_onnx failed")
        if rknn.build(do_quantization=False) != 0:
            raise RuntimeError("build failed")
        if rknn.export_rknn(output + ".tmp") != 0:
            raise RuntimeError("export_rknn failed")
    finally:
        rknn.release()
    os.replace(output + ".tmp", output)
    manifest = face_npu.load_manifest()
    manifest["rknn"] = {"file": face_npu.RKNN_FILE, "sha256": sha256(output), "size": os.path.getsize(output),
                        "dtype": "float16", "input": "uint8 NHWC RGB 112x112", "target": "rk3588",
                        "from_onnx_sha256": face_npu.ONNX_SHA256, "toolkit": _version("rknn-toolkit2")}
    manifest.pop("check", None)            # a new model needs a new check
    manifest["converted"] = _now()
    face_npu.write_manifest(manifest)
    log(f"{output} written.")


def _face_crops(directory):
    """Aligned crops of the one (largest) face in each photo, found like the robot does."""
    import cv2
    from features.person_recognition import faces
    paths = sorted(glob.glob(os.path.join(directory, "*.jpg")) + glob.glob(os.path.join(directory, "*.png")))
    detector = cv2.FaceDetectorYN.create(os.path.join(faces.MODEL_DIR, faces.MODELS["detector"][0]),
                                         "", (320, 320), 0.6, 0.3, 5000)
    crops = []
    for path in paths:
        image = cv2.imread(path)
        if image is None:
            continue
        detector.setInputSize((image.shape[1], image.shape[0]))
        _, found = detector.detect(image)
        if found is None or not len(found):
            continue
        row = max(found, key=lambda f: f[2] * f[3])
        crops.append(face_npu.align(image, row[4:14].reshape(5, 2)))
    return crops


def check(args):
    import numpy as np
    rng = np.random.default_rng(0)
    crops = _face_crops(args.images) if args.images else []
    real = len(crops)
    crops += [rng.integers(0, 256, (112, 112, 3), dtype=np.uint8) for _ in range(RANDOM_CROPS)]
    reference = face_npu.OnnxEmbedder(onnx_path())
    npu = face_npu.NpuEmbedder(os.path.join(face_npu.MODEL_DIR, face_npu.RKNN_FILE), core=args.core)
    try:
        cosines = [float(reference.embed_crop(c) @ npu.embed_crop(c)) for c in crops]
        t0 = time.perf_counter()
        for _ in range(TIMING_RUNS):
            npu.embed_crop(crops[0])
        ms = 1000 * (time.perf_counter() - t0) / TIMING_RUNS
    finally:
        npu.close()
    worst = min(cosines)
    passed = worst >= MIN_COSINE and real > 0
    log(f"{real} real faces, {RANDOM_CROPS} random crops: NPU vs ONNX cosine min {worst:.4f}, "
        f"mean {np.mean(cosines):.4f}; {ms:.1f} ms per face on NPU core {args.core}")
    if not real:
        log("No real faces checked: pass --images with photos of faces. Not marking the model ready.")
    manifest = face_npu.load_manifest()
    manifest["check"] = "passed" if passed else "failed"
    manifest["check_details"] = {"real_faces": real, "random_crops": RANDOM_CROPS, "min_cosine": worst,
                                 "mean_cosine": float(np.mean(cosines)), "ms_per_face": ms,
                                 "core": args.core, "when": _now(), "machine": platform.machine()}
    face_npu.write_manifest(manifest)
    log("Check passed: Ember will use AuraFace on the NPU." if passed else "Check FAILED: Ember keeps SFace.")
    if not passed:
        sys.exit(1)


def _version(package):
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return None


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("step", choices=["download", "build", "check", "all"])
    parser.add_argument("--images", help="folder of photos with one face each, for the check")
    parser.add_argument("--core", default=os.getenv("BAYMAX_FACE_NPU_CORE", "1"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    steps = {"download": [download], "build": [build], "check": [check],
             "all": [download, build, check]}[args.step]
    for step in steps:
        step(args)


if __name__ == "__main__":
    main()
