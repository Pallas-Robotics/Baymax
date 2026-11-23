"""Make the voice-recognition models for the robot: ONNX files that run on ONNX Runtime,
so the robot needs no PyTorch.

Run on a development computer with PyTorch (not on the robot):

    pip install torch==2.6.* torchaudio==2.6.* speechbrain==1.0.* "huggingface_hub<0.26" \
        onnx onnxruntime==1.24.4 silero-vad requests
    python -m features.person_recognition.convert_voice_model [--out .models/voice]

It writes, into --out:
- ecapa_voxceleb.onnx: SpeechBrain's ECAPA-TDNN speaker encoder (speechbrain/
  spkrec-ecapa-voxceleb, Apache-2.0), the same model PR #36 used through PyTorch, with
  its mel filterbank, log/top-dB step and per-utterance mean normalisation inside. Its
  input is the power spectrogram, [1, frames, 201], from voices.power_spectrogram().
- silero_vad.onnx: Silero VAD (MIT), copied from the silero-vad package.

Before writing anything it checks the result against SpeechBrain's own encode_batch()
on synthetic speech-like audio of several lengths; the cosine between the two embeddings
must be at least 0.9999, so PR #36's thresholds (calibrated on SpeechBrain) still hold.
Then copy the folder to the robot's Baymax-main/.models/voice/.
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from features.person_recognition import voices  # noqa: E402  (numpy STFT used on the robot)


def speechlike(seconds, seed):
    """Deterministic test audio: voiced harmonics with a moving pitch, plus noise."""
    rng = np.random.default_rng(seed)
    n = int(seconds * voices.SAMPLE_RATE)
    t = np.arange(n) / voices.SAMPLE_RATE
    f0 = 110 + 40 * seed + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / voices.SAMPLE_RATE
    audio = sum(np.sin(k * phase) / k for k in range(1, 12))
    audio *= 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t) ** 2          # syllable-like envelope
    audio += 0.05 * rng.standard_normal(n)
    return (0.3 * audio / np.abs(audio).max()).astype(np.float32)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / ".models" / "voice"))
    args = parser.parse_args()
    import torch
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    work = Path(tempfile.mkdtemp(prefix="ecapa-"))
    encoder = EncoderClassifier.from_hparams(source=voices.MODEL_SOURCE, savedir=str(work / "src"),
                                             run_opts={"device": "cpu"}, local_strategy=LocalStrategy.COPY)
    encoder.eval()
    fbanks = encoder.mods.compute_features.compute_fbanks
    model = encoder.mods.embedding_model

    class FromPowerSpectrogram(torch.nn.Module):
        """compute_features after the STFT, mean_var_norm (sentence mean only) and the encoder."""

        def __init__(self):
            super().__init__()
            self.fbanks, self.model = fbanks, model

        def forward(self, power):                                  # [1, frames, 201]
            feats = self.fbanks(power)
            feats = feats - feats.mean(dim=1, keepdim=True)
            return self.model(feats).squeeze(1)                    # [1, 192]

    wrapper = FromPowerSpectrogram().eval()
    onnx_path = work / voices.ECAPA_FILE
    example = torch.from_numpy(voices.power_spectrogram(speechlike(3.0, 0)))[None]
    torch.onnx.export(wrapper, (example,), str(onnx_path), input_names=["power"], output_names=["embedding"],
                      dynamic_axes={"power": {1: "frames"}}, opset_version=17, dynamo=False)

    import onnxruntime as ort
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    worst = 1.0
    for seconds in (1.0, 2.0, 3.5, 8.0, 15.0):
        for seed in (1, 2):
            audio = speechlike(seconds, seed)
            with torch.no_grad():
                reference = encoder.encode_batch(torch.from_numpy(audio)[None]).squeeze().numpy()
            power = voices.power_spectrogram(audio)[None]
            ours = session.run(None, {"power": power})[0][0]
            cosine = float(np.dot(reference, ours) / (np.linalg.norm(reference) * np.linalg.norm(ours)))
            worst = min(worst, cosine)
            print(f"{seconds:5.1f} s seed {seed}: cosine {cosine:.6f}")
    if worst < 0.9999:
        sys.exit(f"ONNX model differs from SpeechBrain (worst cosine {worst:.6f}); nothing written")

    import silero_vad
    vad_source = Path(silero_vad.__file__).parent / "data" / voices.VAD_FILE
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(onnx_path, out / voices.ECAPA_FILE)
    shutil.copyfile(vad_source, out / voices.VAD_FILE)
    for name in (voices.ECAPA_FILE, voices.VAD_FILE):
        digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
        print(f"{out / name}: {os.path.getsize(out / name):,} bytes, sha256 {digest}")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
