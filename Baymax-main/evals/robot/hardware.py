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

    def start(self, samples, label=""):
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

    def start(self, samples, label=""):
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
PAGE = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Ember rig card</title>
<style>html,body{margin:0;height:100%;background:#fff}img{width:100%;height:100%;object-fit:contain}</style>
<img id="card" alt="">
<script>
let shown = -1;
async function tick(){
  try {
    const v = await (await fetch('version', {cache: 'no-store'})).text();
    if (v !== String(shown)) { document.getElementById('card').src = 'card.jpg?v=' + v; shown = v; }
  } catch (e) {}
  setTimeout(tick, 300);
}
tick();
</script>"""


class HttpDisplay:
    """A page for the tablet in front of the camera: http://ROBOT:PORT/ shows the current card.

    Serves only the synthetic card the rig is showing (or a blank page). Bind it to
    the rig network address in the config; it has no other routes."""

    def __init__(self, host="0.0.0.0", port=8790):
        self.version, self.jpeg, self.lock = 0, b"", threading.Lock()
        display = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                with display.lock:
                    body, kind = {"/": (PAGE, "text/html"), "/version": (str(display.version).encode(), "text/plain"),
                                  "/card.jpg": (display.jpeg, "image/jpeg")}.get(path, (None, None))
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer((host, port), Handler)
        self.url = f"http://{host}:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, name="rig-display", daemon=True).start()

    def show(self, jpeg):
        with self.lock:
            self.jpeg, self.version = jpeg, self.version + 1

    def clear(self):
        self.show(b"")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class NoDisplay:
    available = False

    def show(self, jpeg):
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
