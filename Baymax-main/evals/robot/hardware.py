"""The rig's hardware, behind small interfaces so the orchestration runs offline with fakes.

- Person speaker: the SECOND speaker, about 2 m from the robot, that plays the
  scripted person. Chosen by device name in the rig config. It must not be Ember's
  own speaker (Ember's echo cancellation would remove it).
- Display: a tablet or small screen in front of the camera showing a synthetic card.
- Runtime: the branch's real `run_mvp.py`, started as a child process with stdout
  sent to a log file, and always stopped at the end.
- Sampler: CPU and memory of the runtime process tree.

Nothing here runs at import time. Only the rig's real run (on a marked test robot)
constructs these classes; the tests use evals/robot/fakes.py.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np

from evals.robot import audio


class Clock:
    """Wall clock; the fakes replace it with one that advances on sleep()."""

    def time(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(max(0.0, seconds))


# ── Person speaker ───────────────────────────────────────────────────────────
class SoundDeviceSpeaker:
    """Plays int16 48 kHz mono on one output device matched by name (case-insensitive substring)."""

    def __init__(self, name, forbidden=(), sd=None):
        if sd is None:
            import sounddevice as sd
        self.sd = sd
        outputs = [(index, device) for index, device in enumerate(sd.query_devices())
                   if device.get("max_output_channels", 0) > 0]
        matches = [(i, d) for i, d in outputs if name.lower() in d["name"].lower()]
        if len(matches) != 1:
            names = ", ".join(d["name"] for _, d in outputs)
            raise RuntimeError(f"Person speaker {name!r} matched {len(matches)} output devices (need 1). "
                               f"Outputs: {names}")
        self.index, device = matches[0]
        for other in forbidden:
            if other and other.lower() in device["name"].lower():
                raise RuntimeError(f"Person speaker {device['name']!r} is Ember's own speaker ({other!r}); "
                                   "the rig needs a second speaker so Ember hears it like a person.")
        self.name = device["name"]
        self.until = 0.0

    def start(self, samples, label="", parts=None):  # parts: timeline cues inside samples (fakes use them).
        self.sd.play(samples, samplerate=audio.RATE, device=self.index, blocking=False)
        self.until = time.monotonic() + len(samples) / audio.RATE
        return len(samples) / audio.RATE

    def stop(self):
        self.sd.stop()

    def close(self):
        self.stop()


class CommandSpeaker:
    """Plays through a command, e.g. ["pw-play", "--target", "rig-speaker", "{wav}"] or
    ["aplay", "-D", "plughw:CARD=Rig", "{wav}"]. Useful when PipeWire hides USB devices from PortAudio."""

    def __init__(self, argv):
        if not argv or "{wav}" not in argv:
            raise ValueError("speaker.command needs a list containing '{wav}'")
        self.argv, self.process, self.folder = list(argv), None, Path(tempfile.mkdtemp(prefix="ember-rig-"))
        self.name = " ".join(argv)

    def start(self, samples, label="", parts=None):  # parts: timeline cues inside samples (fakes use them).
        self.stop()
        path = self.folder / "line.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(audio.RATE)
            out.writeframes(samples.tobytes())
        self.process = subprocess.Popen([str(path) if part == "{wav}" else part for part in self.argv],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return len(samples) / audio.RATE

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
        shutil.rmtree(self.folder, ignore_errors=True)


def make_speaker(config, ember_speaker_names=()):
    spec = config.get("person_speaker") or {}
    if spec.get("command"):
        return CommandSpeaker(spec["command"])
    if not spec.get("name"):
        raise RuntimeError("Rig config needs person_speaker.name (or person_speaker.command).")
    return SoundDeviceSpeaker(spec["name"], forbidden=ember_speaker_names)


# ── Display ──────────────────────────────────────────────────────────────────
# The page polls /state, shows a card or plays a clip fullscreen (muted: sound comes from the
# person speaker on the rig clock), and POSTs /ack with the server-clock time the card was painted
# or the clip's first frame was presented. Its clock offset comes from /time round trips (the
# best of several pings), so the rig knows when playback actually began, not when it asked.
PAGE = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Ember rig display</title>
<style>html,body{margin:0;height:100%;background:#fff;overflow:hidden}
img,video{position:fixed;inset:0;width:100%;height:100%;object-fit:contain;background:#fff}
video{background:#000}.off{display:none}</style>
<img id="card" alt=""><video id="clip" class="off" muted playsinline preload="auto"></video>
<script>
const card = document.getElementById('card'), clip = document.getElementById('clip');
let shown = null, offset = 0;
const pageNow = () => performance.timeOrigin + performance.now();   // ms since epoch, page clock
async function sync() {
  let best = Infinity;
  for (let i = 0; i < 5; i++) {
    const t0 = pageNow(); const server = parseFloat(await (await fetch('time', {cache: 'no-store'})).text());
    const t1 = pageNow();
    if (t1 - t0 < best) { best = t1 - t0; offset = server * 1000 - (t0 + t1) / 2; }
  }
}
function ack(version, event, pageMs) {
  fetch('ack', {method: 'POST', body: JSON.stringify({version, event, at: (pageMs + offset) / 1000})});
}
function paint(version, event) {
  requestAnimationFrame(() => requestAnimationFrame(() => ack(version, event, pageNow())));
}
async function tick() {
  try {
    const s = await (await fetch('state', {cache: 'no-store'})).json();
    if (s.version !== shown) {
      shown = s.version;
      if (s.kind === 'video') {
        card.classList.add('off'); clip.classList.remove('off');
        clip.src = 'clip?v=' + s.version;
        if (clip.requestVideoFrameCallback) {
          clip.requestVideoFrameCallback((now, meta) => ack(s.version, 'playing', performance.timeOrigin + meta.expectedDisplayTime));
        } else { clip.onplaying = () => ack(s.version, 'playing', pageNow()); }
        clip.onended = () => ack(s.version, 'ended', pageNow());
        clip.onerror = () => ack(s.version, 'error', pageNow());
        clip.play().catch(() => ack(s.version, 'error', pageNow()));
        if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
      } else {
        clip.pause(); clip.removeAttribute('src'); clip.classList.add('off'); card.classList.remove('off');
        card.onload = () => paint(s.version, 'shown');
        card.src = 'card.jpg?v=' + s.version;
      }
    }
  } catch (e) {}
  setTimeout(tick, 100);
}
sync().then(tick, tick); setInterval(sync, 60000);
</script>"""


class HttpDisplay:
    """A page for the screen in front of the camera: http://ROBOT:PORT/ shows the current card
    or plays the current clip, and confirms each with a timestamp (`ack`).

    Serves only what the rig is showing: the card, or the one clip file it was given (with
    HTTP Range support, which video elements use). It has no other routes."""

    available, plays_video = True, True

    def __init__(self, host="0.0.0.0", port=8790, clock=time.time):
        self.version, self.kind, self.jpeg, self.clip = 0, "card", b"", None
        self.acks, self.lock, self.clock = {}, threading.Lock(), clock
        display = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def send_body(self, body, kind, status=200, extra=()):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                with display.lock:
                    version, kind, jpeg, clip = display.version, display.kind, display.jpeg, display.clip
                if path == "/":
                    return self.send_body(PAGE, "text/html")
                if path == "/time":
                    return self.send_body(f"{display.clock():.6f}".encode(), "text/plain")
                if path == "/state":
                    return self.send_body(json.dumps({"version": version, "kind": kind}).encode(), "application/json")
                if path in ("/card.jpg", "/version") and kind == "card":
                    return self.send_body(jpeg if path == "/card.jpg" else str(version).encode(),
                                          "image/jpeg" if path == "/card.jpg" else "text/plain")
                if path == "/clip" and kind == "video" and clip is not None:
                    return self.send_clip(clip)
                self.send_error(404)

            def send_clip(self, clip):
                size = clip.stat().st_size
                mime = "video/webm" if clip.suffix.lower() == ".webm" else "video/mp4"
                start, end = 0, size - 1
                wanted = self.headers.get("Range", "")
                if wanted.startswith("bytes="):
                    first, _, last = wanted[6:].split(",")[0].partition("-")
                    start = int(first) if first else max(0, size - int(last or 0))
                    end = min(int(last), size - 1) if first and last else size - 1
                with clip.open("rb") as stream:
                    stream.seek(start)
                    body = stream.read(end - start + 1)
                extra = [("Accept-Ranges", "bytes")]
                if wanted.startswith("bytes="):
                    extra.append(("Content-Range", f"bytes {start}-{end}/{size}"))
                self.send_body(body, mime, 206 if wanted.startswith("bytes=") else 200, extra)

            def do_POST(self):
                if self.path.split("?")[0] != "/ack":
                    return self.send_error(404)
                try:
                    message = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
                    version, event, at = int(message["version"]), str(message["event"])[:16], float(message["at"])
                except (ValueError, KeyError, TypeError):
                    return self.send_error(400)
                received = display.clock()
                # The page's estimate, unless it is implausible (clock sync failed): then arrival time.
                when = at if abs(at - received) < 5.0 else received
                with display.lock:
                    display.acks.setdefault((version, event), when)
                self.send_body(b"ok", "text/plain")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer((host, port), Handler)
        self.url = f"http://{host}:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, name="rig-display", daemon=True).start()

    def _set(self, kind, jpeg=b"", clip=None):
        with self.lock:
            self.kind, self.jpeg, self.clip = kind, jpeg, clip
            self.version += 1
            return self.version

    def show(self, jpeg, label=""):
        return self._set("card", jpeg)

    def play_video(self, path, label=""):
        path = Path(path)
        if path.suffix.lower() not in (".mp4", ".webm") or not path.is_file():
            raise RuntimeError(f"display plays .mp4/.webm files only: {path.name}")
        return self._set("video", clip=path)

    def ack(self, version, event):
        """Rig-clock time the page confirmed `event` (shown, playing, ended, error) of `version`."""
        with self.lock:
            return self.acks.get((version, event))

    def clear(self):
        return self.show(b"")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class NoDisplay:
    available, plays_video = False, False

    def show(self, jpeg, label=""):
        raise RuntimeError("No rig display configured")

    def clear(self):
        pass

    def close(self):
        pass


def make_display(config):
    spec = config.get("display") or {}
    if spec.get("kind", "none") == "none":
        return NoDisplay()
    if spec["kind"] == "http":
        return HttpDisplay(spec.get("host", "0.0.0.0"), int(spec.get("port", 8790)))
    raise RuntimeError(f"Unknown display kind {spec['kind']!r} (use 'http' or 'none')")


# ── Runtime ──────────────────────────────────────────────────────────────────
RUNTIME_PATTERN = ("run_mvp.py", "run_pc.py", "realtime_gemini_")


def other_runtimes(own_pids=(), ps=None):
    """PIDs and commands of Ember runtimes already running on this machine (any checkout)."""
    try:
        listing = ps() if ps else subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True,
                                                 timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) not in own_pids and int(pid) != os.getpid() and "python" in command \
                and any(p in command for p in RUNTIME_PATTERN) and "evals/robot" not in command:
            found.append((int(pid), command.strip()))
    return found


def runtime_environment(environ):
    """The runtime's environment: no GitHub/Actions tokens, and no test key (it reads the rig's key file)."""
    env = {k: v for k, v in environ.items()
           if "TOKEN" not in k.upper() and k not in ("GEMINI_TEST_KEY", "GOOGLE_API_KEY", "ACTIONS_ID_TOKEN_REQUEST_URL")}
    env["PYTHONUNBUFFERED"] = "1"
    return env


class ProcessRuntime:
    """The branch's real runtime: `python run_mvp.py` in its Baymax-main, stdout to a log file.

    run_mvp.py is what the Orange Pi robots autostart (devops/mvp, devops/clickables/launch.py);
    it starts run_pc.py, which runs the core it names (realtime_gemini_10.py on main)."""

    def __init__(self, root, python, log_path, command=("run_mvp.py",), trace_path=None):
        self.root, self.python, self.log_path = Path(root), str(python), Path(log_path)
        self.trace_path = trace_path
        self.command, self.process = list(command), None
        self.status_path = self.root / ".runtime-status.json"
        self.stop_path = self.root / ".runtime-stop.json"

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def start(self):
        if self.alive():
            raise RuntimeError("Runtime already started by the rig")
        others = other_runtimes()
        if others:
            raise RuntimeError("Another Ember runtime is running (the rig never stops production services; "
                               "stop it or set ~/.config/ember/no-autostart): "
                               + "; ".join(f"{pid} {cmd[:80]}" for pid, cmd in others))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        env = runtime_environment(os.environ)
        self.process = subprocess.Popen([self.python] + self.command, cwd=self.root, stdout=log,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                        start_new_session=True)
        log.close()
        return time.time()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def exit_code(self):
        return None if self.process is None else self.process.poll()

    def stop(self, grace_s=25.0, term_s=15.0):
        """Clean stop through the monitor's stop file (as the launcher's Stop does), then SIGTERM, then SIGKILL."""
        if not self.alive():
            return self.exit_code()
        try:
            self.stop_path.write_text(json.dumps({"pid": self.process.pid, "requested_at": time.time()}),
                                      encoding="utf-8")
        except OSError:
            pass
        for sig, wait in ((None, grace_s), (signal.SIGTERM, term_s), (signal.SIGKILL, 10.0)):
            if sig is not None:
                try:
                    os.killpg(self.process.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                return self.process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        return self.exit_code()


# ── CPU / memory ─────────────────────────────────────────────────────────────
class ProcessSampler:
    """CPU (% of all cores) and resident memory of a process and its children.

    On Linux it reuses devops/performance/check.py's read-only sampler (the same numbers
    the feature performance protocol reports, plus system CPU, temperature and NPU load
    where exposed). Otherwise psutil when installed, else `ps` (macOS)."""

    def __init__(self, pid, performance=None):
        self.pid, self.last = pid, None
        self.performance = performance if performance is not None else load_performance()
        self.previous = None
        try:
            import psutil
            self.psutil = psutil
            self.process = psutil.Process(pid)
            self.process.cpu_percent(None)
        except Exception:
            self.psutil = None
        self.cores = os.cpu_count() or 1

    def sample(self):
        try:
            if self.performance is not None and Path("/proc").is_dir():
                return self._performance()
            if self.psutil is not None:
                return self._psutil()
            if Path("/proc").is_dir():
                return self._proc()
            return self._ps()
        except Exception:
            return None

    def _performance(self):
        values, self.previous = self.performance.sample(self.pid, self.previous)
        cpu = values.get("process_cpu_percent")
        result = {"cpu_percent": None if cpu is None else round(cpu / self.cores, 1),
                  "rss_mb": round(values["rss_mib"], 1)}
        for key in ("cpu_percent", "system_used_mib", "temperature_c", "npu_percent"):
            value = values.get(key)
            if value is not None:
                result["system_cpu_percent" if key == "cpu_percent" else key] = round(value, 1)
        return result

    def _psutil(self):
        processes = [self.process] + self.process.children(recursive=True)
        cpu = sum(p.cpu_percent(None) for p in processes)
        rss = sum(p.memory_info().rss for p in processes)
        return {"cpu_percent": round(cpu / self.cores, 1), "rss_mb": round(rss / 2 ** 20, 1)}

    def _tree(self, parents):
        tree, frontier = {self.pid}, [self.pid]
        while frontier:
            current = frontier.pop()
            for child, parent in parents.items():
                if parent == current and child not in tree:
                    tree.add(child)
                    frontier.append(child)
        return tree

    def _proc(self):
        stats, parents = {}, {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            parents[int(entry.name)] = int(fields[1])
            stats[int(entry.name)] = (int(fields[11]) + int(fields[12]), int(fields[21]))
        tree = self._tree(parents)
        ticks = sum(stats[p][0] for p in tree if p in stats)
        pages = sum(stats[p][1] for p in tree if p in stats)
        now, hz = time.monotonic(), os.sysconf("SC_CLK_TCK")
        cpu = None
        if self.last is not None and now > self.last[0]:
            cpu = (ticks - self.last[1]) / hz / (now - self.last[0]) * 100 / self.cores
        self.last = (now, ticks)
        return {"cpu_percent": None if cpu is None else round(max(cpu, 0.0), 1),
                "rss_mb": round(pages * os.sysconf("SC_PAGE_SIZE") / 2 ** 20, 1)}

    def _ps(self):
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,%cpu=,rss="], capture_output=True, text=True,
                                 timeout=5).stdout
        rows = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 4:
                rows[int(parts[0])] = (int(parts[1]), float(parts[2]), int(parts[3]))
        tree = self._tree({pid: row[0] for pid, row in rows.items()})
        cpu = sum(rows[p][1] for p in tree if p in rows) / self.cores
        rss = sum(rows[p][2] for p in tree if p in rows) / 1024
        return {"cpu_percent": round(cpu, 1), "rss_mb": round(rss, 1)}


def load_performance(root=None):
    """devops/performance/check.py of this rig's checkout (standard library only), or None."""
    path = Path(root or Path(__file__).resolve().parents[3]) / "devops" / "performance" / "check.py"
    if not path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("rig_performance_check", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module if hasattr(module, "sample") else None


def python_for(root):
    """The branch's own interpreter: Baymax-main/.venv, else the rig's."""
    for candidate in (Path(root) / ".venv" / "bin" / "python", Path(root) / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


# ── Optional rig hardware: light, fault hooks, smart plug, reference microphone ─
def _argv(template, **values):
    return [values.get(part[1:-1], part) if part.startswith("{") and part.endswith("}") else part
            for part in template]


class CommandLight:
    """A dimmable bulb through a command, e.g. ["kasa", "--host", "192.168.50.20", "--type", "bulb",
    "brightness", "{percent}"]. Restored to `restore_percent` when the rig closes."""

    def __init__(self, command, restore_percent=100, run=subprocess.run):
        if "{percent}" not in command:
            raise ValueError("light.command needs '{percent}'")
        self.command, self.restore, self.run, self.changed = list(command), restore_percent, run, False

    def set(self, percent):
        done = self.run(_argv(self.command, percent=str(int(round(percent)))), capture_output=True, text=True,
                        timeout=15)
        if done.returncode != 0:
            raise RuntimeError(f"light command failed: {(done.stderr or '').strip()[:160]}")
        self.changed = True

    def close(self):
        if self.changed and self.restore is not None:
            self.set(self.restore)


class CommandHook:
    """A fault the timeline can inject and always restores: `start`/`stop` argv lists, e.g. the
    network-drop helper in the README (which also restores itself after its own timeout)."""

    def __init__(self, name, start, stop, run=subprocess.run):
        if not start or not stop:
            raise ValueError(f"fault {name} needs start and stop commands")
        self.name, self.start_argv, self.stop_argv, self.run, self.active = name, list(start), list(stop), run, False

    def _call(self, argv, what):
        done = self.run(argv, capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(f"fault {self.name} {what} failed: {(done.stderr or '').strip()[:160]}")

    def start(self):
        self._call(self.start_argv, "start")
        self.active = True

    def stop(self):
        self._call(self.stop_argv, "stop")
        self.active = False

    def close(self):
        if self.active:
            self.stop()


class CommandPower:
    """A smart plug on the robot, driven from an EXTERNAL controller (the rig cannot power-cycle the
    machine it runs on). `off`/`on` argv lists and a `ready` argv that exits 0 once the robot is
    listening again (e.g. ssh to it and read .runtime-status.json)."""

    def __init__(self, off, on, ready, off_s=10.0, timeout_s=300.0, run=subprocess.run):
        self.off, self.on, self.ready, self.run = list(off), list(on), list(ready), run
        self.off_s, self.timeout_s = float(off_s), float(timeout_s)

    def cycle(self, clock):
        for argv in (self.off,):
            self.run(argv, capture_output=True, timeout=30, check=True)
        clock.sleep(self.off_s)
        self.run(self.on, capture_output=True, timeout=30, check=True)
        powered = clock.time()
        while clock.time() - powered < self.timeout_s:
            if self.run(self.ready, capture_output=True, timeout=30).returncode == 0:
                return round(clock.time() - powered, 1)
            clock.sleep(1.0)
        return None


class CommandRecorder:
    """The reference microphone through a command writing {wav}, e.g. ["arecord", "-q", "-D",
    "plughw:CARD=RefMic,DEV=0", "-f", "S16_LE", "-r", "48000", "-c", "1", "{wav}"]. The file is in a
    private 0700 folder and `discard` deletes it; only numbers leave the rig."""

    def __init__(self, command, popen=subprocess.Popen, clock=time.time):
        if "{wav}" not in command:
            raise ValueError("reference_mic.command needs '{wav}'")
        self.command, self.popen, self.clock = list(command), popen, clock
        self.folder, self.process, self.started, self.discarded = None, None, None, 0

    def start(self):
        self.discard()
        self.folder = Path(tempfile.mkdtemp(prefix="ember-rig-ref-"))
        os.chmod(self.folder, 0o700)
        self.process = self.popen(_argv(self.command, wav=str(self.folder / "reference.wav")),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.started = self.clock()

    def stop(self):
        """(int16 samples, rate, rig time of the first sample) or None."""
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)  # arecord finalizes the WAV header on SIGINT.
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        path = self.folder / "reference.wav"
        if not path.is_file():
            return None
        with wave.open(str(path), "rb") as stream:
            rate, channels = stream.getframerate(), stream.getnchannels()
            data = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16)
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1).astype(np.int16)
        return data, rate, self.started

    def discard(self):
        if self.folder is not None:
            self.discarded += sum(1 for _ in self.folder.glob("*.wav"))
            shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = None

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        self.process = None
        self.discard()


def make_rig_hardware(config, ember_mic_names=()):
    """Light, fault hooks, smart plug and reference microphone from the rig config (all optional)."""
    light = (config.get("light") or {})
    faults = config.get("faults") or {}
    power = config.get("power") or {}
    mic = config.get("reference_mic") or {}
    for other in ember_mic_names:
        if other and mic.get("command") and any(other in part for part in mic["command"]):
            raise RuntimeError(f"reference_mic names Ember's own microphone ({other!r}); use a separate USB mic")
    return {
        "light": CommandLight(light["command"], light.get("restore_percent", 100)) if light.get("command") else None,
        "faults": {name: CommandHook(name, spec.get("start"), spec.get("stop")) for name, spec in faults.items()},
        "power": CommandPower(power["off"], power["on"], power["ready"], power.get("off_s", 10),
                              power.get("timeout_s", 300))
        if power.get("external_controller") and power.get("off") else None,
        "recorder": CommandRecorder(mic["command"]) if mic.get("command") else None,
    }
