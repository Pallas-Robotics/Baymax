"""Virtual-device mode: the real runtime on the test robot, fed through virtual devices.

Without the person speaker and screen, the rig can run many more repeats (nights,
not hours) of the scenarios that do not depend on sound travelling through the room:

- Microphone: an ALSA loopback card (`snd-aloop`) "EmberRigMic". The rig plays the
  person into `plughw:CARD=EmberRigMic,DEV=0`; the runtime records from device 1 of
  the same card as if it were its USB microphone.
- Ember's speaker: a second loopback card "EmberRigOut". The runtime plays into
  device 0; the rig records device 1 as an exact digital "reference microphone".
- Camera: a `v4l2loopback` device (/dev/video42, "EmberRigCam"). `ffmpeg` writes the
  card, a test card or a clip into it; the runtime opens it as its camera.

The runtime, its device code, echo canceller, Gemini session and toolkits are the
real ones; only the transducers are replaced. What this cannot show: anything
acoustic (echo of Ember's own voice, distance, wake words over Ember's playback),
lighting, real optics. Scenarios list `"modes": ["physical"]` where that matters.

Gating: nothing here runs unless the rig config has a `virtual` section AND the rig
is started with `--virtual`. Kernel modules are loaded only with
`virtual.load_modules: true`, through `virtual.sudo` (default `["sudo", "-n"]`), and
only the modules this rig loaded are unloaded at the end. Nothing is written to
/etc/modules-load.d, systemd or the robot's startup.

Every command is built by a pure function so the tests check the argv without
running anything.
"""
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import wave

from evals.robot import audio

DEFAULTS = {
    "mic_card": {"index": 10, "id": "EmberRigMic"},
    "out_card": {"index": 11, "id": "EmberRigOut"},
    "camera": {"video_nr": 42, "label": "EmberRigCam", "width": 1280, "height": 720, "fps": 15},
    "sudo": ["sudo", "-n"],
    "load_modules": False,
}


class VirtualError(RuntimeError):
    """Virtual devices are missing or failed: a rig fault."""


def spec_of(config):
    spec = config.get("virtual")
    if not isinstance(spec, dict):
        raise VirtualError("--virtual needs a 'virtual' section in the rig config (README: Virtual-device mode)")
    merged = {key: (dict(value, **spec.get(key, {})) if isinstance(value, dict) else spec.get(key, value))
              for key, value in DEFAULTS.items()}
    return merged


# ── Command construction (pure) ──────────────────────────────────────────────
def load_commands(spec):
    mic, out, cam = spec["mic_card"], spec["out_card"], spec["camera"]
    sudo = list(spec["sudo"])
    return [
        sudo + ["modprobe", "snd-aloop", "enable=1,1", f"index={mic['index']},{out['index']}",
                f"id={mic['id']},{out['id']}", "pcm_substreams=1,1"],
        sudo + ["modprobe", "v4l2loopback", "devices=1", f"video_nr={cam['video_nr']}",
                f"card_label={cam['label']}", "exclusive_caps=1"],
    ]


def unload_commands(spec, loaded):
    names = {"snd-aloop": "snd_aloop", "v4l2loopback": "v4l2loopback"}
    return [list(spec["sudo"]) + ["modprobe", "-r", module] for name, module in names.items() if name in loaded]


def camera_device(spec):
    return f"/dev/video{spec['camera']['video_nr']}"


def person_feed_command(spec):
    """Plays a WAV into the virtual microphone (CommandSpeaker argv)."""
    return ["aplay", "-q", "-D", f"plughw:CARD={spec['mic_card']['id']},DEV=0", "{wav}"]


def ember_tap_command(spec):
    """Records Ember's speaker output (CommandRecorder argv)."""
    return ["arecord", "-q", "-D", f"plughw:CARD={spec['out_card']['id']},DEV=1", "-f", "S16_LE",
            "-r", str(audio.RATE), "-c", "1", "{wav}"]


def _scale(spec):
    cam = spec["camera"]
    w, h = cam["width"], cam["height"]
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:white,"
            "format=yuv420p")


def camera_command(spec, source, kind):
    """ffmpeg writing a still (`image`), a clip (`video`) or black (`blank`) into the camera."""
    cam = spec["camera"]
    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1"]
    if kind == "image":
        inputs = ["-re", "-loop", "1", "-framerate", str(cam["fps"]), "-i", str(source)]
    elif kind == "video":
        inputs = ["-re", "-i", str(source), "-an"]
    elif kind == "blank":
        inputs = ["-re", "-f", "lavfi", "-i", f"color=c=black:s={cam['width']}x{cam['height']}:r={cam['fps']}"]
    else:
        raise ValueError(f"unknown camera source kind {kind!r}")
    return head + inputs + ["-vf", _scale(spec), "-r", str(cam["fps"]), "-f", "v4l2", camera_device(spec)]


def runtime_env(spec, devices, hostapis):
    """The runtime's device settings for the loopback cards and the virtual camera.

    `devices`/`hostapis` are sounddevice.query_devices()/query_hostapis() from the
    branch's interpreter. PortAudio names the cards "Loopback: PCM (hw:N,D)"; the
    card numbers come from the config's `index`."""
    def find(card, device, channels):
        suffix = f"(hw:{card},{device})"
        matches = [(i, d) for i, d in enumerate(devices)
                   if d.get("name", "").endswith(suffix) and d.get(channels, 0) > 0
                   and hostapis[d["hostapi"]]["name"] == "ALSA"]
        if len(matches) != 1:
            raise VirtualError(f"expected one ALSA device named …{suffix}, found {len(matches)}; is snd-aloop "
                               "loaded with the configured index (and not held by PipeWire)?")
        return matches[0]
    mic_index, mic = find(spec["mic_card"]["index"], 1, "max_input_channels")
    out_index, out = find(spec["out_card"]["index"], 0, "max_output_channels")
    return {"BAYMAX_MIC_DEVICE": str(mic_index), "BAYMAX_MIC_NAME": mic["name"], "BAYMAX_MIC_HOSTAPI": "ALSA",
            "BAYMAX_SPEAKER_DEVICE": str(out_index), "BAYMAX_SPEAKER_NAME": out["name"],
            "BAYMAX_SPEAKER_HOSTAPI": "ALSA", "BAYMAX_CAMERA_DEVICE": camera_device(spec)}


QUERY_DEVICES = ("import json, sounddevice as sd; "
                 "print(json.dumps({'devices': [dict(d) for d in sd.query_devices()], "
                 "'hostapis': [dict(h) for h in sd.query_hostapis()]}, default=str))")


# ── Setup / teardown ─────────────────────────────────────────────────────────
class Modules:
    """Loads snd-aloop and v4l2loopback when configured, and unloads only what it loaded."""

    def __init__(self, spec, run=subprocess.run, sys_module=Path("/sys/module"), dev=Path("/dev"),
                 asound=Path("/proc/asound")):
        self.spec, self.run, self.sys_module, self.dev, self.asound = spec, run, Path(sys_module), Path(dev), Path(asound)
        self.loaded = []

    def present(self):
        missing = []
        if not (self.asound / self.spec["mic_card"]["id"]).exists():
            missing.append(f"ALSA card {self.spec['mic_card']['id']}")
        if not (self.asound / self.spec["out_card"]["id"]).exists():
            missing.append(f"ALSA card {self.spec['out_card']['id']}")
        if not (self.dev / Path(camera_device(self.spec)).name).exists():
            missing.append(camera_device(self.spec))
        return missing

    def setup(self):
        if self.spec.get("load_modules"):
            for command, module in zip(load_commands(self.spec), ("snd-aloop", "v4l2loopback")):
                if (self.sys_module / module.replace("-", "_")).exists():
                    continue  # Already loaded by the operator: leave it alone.
                done = self.run(command, capture_output=True, text=True, timeout=30)
                if done.returncode != 0:
                    raise VirtualError(f"{' '.join(command)} failed: {(done.stderr or '').strip()[:200]}")
                self.loaded.append(module)
        missing = self.present()
        if missing:
            raise VirtualError("virtual devices missing: " + ", ".join(missing)
                               + (" (set virtual.load_modules or load them as in the README)"
                                  if not self.spec.get("load_modules") else ""))

    def teardown(self):
        for command in unload_commands(self.spec, self.loaded):
            self.run(command, capture_output=True, text=True, timeout=30)
        self.loaded = []


class VirtualCamera:
    """The rig's display in virtual mode: ffmpeg feeds the v4l2loopback camera.

    Same interface as hardware.HttpDisplay. `ack` reports when ffmpeg wrote its first
    frame (from its -progress output), which is when the runtime can see it."""

    available, plays_video = True, True

    def __init__(self, spec, popen=subprocess.Popen, clock=time.time):
        self.spec, self.popen, self.clock = spec, popen, clock
        self.folder = Path(tempfile.mkdtemp(prefix="ember-rig-cam-"))
        self.process, self.version, self.acks, self.lock = None, 0, {}, threading.Lock()
        self.show_blank()

    def _start(self, source, kind, event):
        self.stop()
        with self.lock:
            self.version += 1
            version = self.version
        self.process = self.popen(camera_command(self.spec, source, kind), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._watch, args=(self.process, version, event), daemon=True).start()
        return version

    def _watch(self, process, version, event):
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "frame" and value.strip().isdigit() and int(value) >= 1:
                with self.lock:
                    self.acks.setdefault((version, event), self.clock())
            if key == "progress" and value == "end":
                with self.lock:
                    self.acks.setdefault((version, "ended"), self.clock())

    def show(self, jpeg, label=""):
        path = self.folder / f"card-{self.version + 1}.jpg"
        path.write_bytes(jpeg)
        return self._start(path, "image", "shown")

    def play_video(self, path, label=""):
        return self._start(path, "video", "playing")

    def show_blank(self):
        return self._start(None, "blank", "shown")

    def clear(self):
        self.show_blank()

    def ack(self, version, event):
        with self.lock:
            return self.acks.get((version, event))

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def close(self):
        self.stop()
        for child in self.folder.glob("*"):
            child.unlink(missing_ok=True)
        self.folder.rmdir()


def write_wav(path, samples, rate=audio.RATE):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
