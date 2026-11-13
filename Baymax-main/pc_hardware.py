"""PC device selection only. Gemini session and processing stay in upstream core."""
import glob
import os
import platform
import re
import sys


def arm_linux():
    """Linux on 64-bit ARM (the Orange Pi 5's RK3588, the Orange Pi 4 Pro's A733). The
    1-frame camera buffer is limited to it (not measured yet on either board). Fast-core
    pinning needs it too, and by default also an RK3588 (use_performance_cpus)."""
    return sys.platform.startswith("linux") and platform.machine().lower() in ("aarch64", "arm64")


def performance_cpus(sysfs="/sys/devices/system/cpu"):
    """The fast cores of an ARM big.LITTLE CPU this process may use (on the Orange Pi 5's
    RK3588: the four Cortex-A76s, cpu4-7; on the Orange Pi 4 Pro's A733: its two A76s),
    or None when the cores are all alike, fewer than two fast cores are allowed, or it
    can't tell. Linux on 64-bit ARM only (x86 hybrid CPUs, e.g. Intel P/E cores, are left
    to the kernel's scheduler); reads each core's relative speed from sysfs. Whether Ember
    is pinned to them is use_performance_cpus's decision."""
    if not arm_linux() or not hasattr(os, "sched_getaffinity"):
        return None
    speed = {}
    for path in glob.glob(os.path.join(sysfs, "cpu[0-9]*")):
        cpu = int(os.path.basename(path)[3:])
        for name in ("cpu_capacity", "cpufreq/cpuinfo_max_freq"):
            try:
                with open(os.path.join(path, name)) as f:
                    speed[cpu] = int(f.read().strip())
                break
            except (OSError, ValueError):
                continue
    if len(speed) < 2 or len(set(speed.values())) < 2:
        return None
    fastest = max(speed.values())
    fast = {cpu for cpu, value in speed.items() if value >= 0.8 * fastest}
    fast &= os.sched_getaffinity(0)
    # One fast core alone (e.g. under taskset -c 0-4) would squeeze audio, pose and
    # Gemini onto it: leave such a limit as it is.
    return fast if len(fast) >= 2 and len(fast) < len(speed) else None


def device_tree_compatible(path="/proc/device-tree/compatible"):
    """The board's device-tree 'compatible' strings (Linux ARM boards); () elsewhere."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ()
    return tuple(part.decode("ascii", "replace") for part in data.split(b"\0") if part)


def is_rk3588(compatible=None):
    """Rockchip RK3588/RK3588S (Orange Pi 5 family), the SoC with the 6-TOPS NPU."""
    compatible = device_tree_compatible() if compatible is None else compatible
    return any(entry.startswith("rockchip,rk3588") for entry in compatible)


def npu_drivers(drivers="/sys/bus/platform/drivers"):
    """Loaded Rockchip NPU kernel drivers: 'RKNPU' (vendor kernel; what RKNN needs) and/or
    'rocket' (mainline accel driver, which librknnrt cannot use). Empty if neither."""
    return sorted(os.path.basename(path) for path in glob.glob(os.path.join(drivers, "*"))
                  if os.path.basename(path).lower() in ("rknpu", "rocket"))


def thread_ids(tasks="/proc/self/task"):
    """This process's Linux thread ids; [0] (the calling thread) if they can't be listed."""
    try:
        return sorted(int(name) for name in os.listdir(tasks) if name.isdigit()) or [0]
    except OSError:
        return [0]


def _each_thread(change, tasks="/proc/self/task"):
    """change(tid) for every existing thread of this process. A thread that exits meanwhile
    is skipped; a thread started meanwhile is caught by listing again."""
    done = set()
    for _ in range(3):
        pending = [tid for tid in thread_ids(tasks) if tid not in done]
        if not pending:
            return
        for tid in pending:
            try:
                change(tid)
            except OSError:          # exited meanwhile (ProcessLookupError) or not ours to change
                pass
            done.add(tid)


def pin_threads(cpus, tasks="/proc/self/task"):
    """Put every existing thread of this process on cpus. On Linux the affinity is per
    thread (os.sched_setaffinity(0, ...) moves only the caller); threads started later
    inherit it from the thread that starts them."""
    _each_thread(lambda tid: os.sched_setaffinity(tid, cpus), tasks)


def lower_priority(nice, tasks="/proc/self/task"):
    """Give every existing thread of this process at least this nice value (a lower
    priority). Linux keeps it per thread, like the affinity, and a new thread inherits its
    creator's. Raising a priority again needs root, so a thread already at or above `nice`
    is left as it is."""
    def lower(tid):
        if os.getpriority(os.PRIO_PROCESS, tid) < nice:
            os.setpriority(os.PRIO_PROCESS, tid, nice)
    _each_thread(lower, tasks)


# Recorded by use_performance_cpus for the vision children (features/vision_process.py),
# which inherit Ember's environment: the cores Ember runs on, and the other cores it may use
# (read before Ember narrowed itself, so an outer taskset still limits the children).
FAST_CPUS_ENV, SLOW_CPUS_ENV = "BAYMAX_FAST_CPUS", "BAYMAX_SLOW_CPUS"
# Library thread pools that only spun or idled next to Ember on the fast cores (Orange Pi 5
# Max, full-system test): OpenCV's three workers at 1.2-1.6% of a core each around the
# camera's 0.3 ms colour conversion, six idle OpenBLAS workers (numpy's and scipy's), and the
# pool tokenizers starts at the first memory lookup. Each library reads its setting when it
# loads, here and in child processes; a value already set (.env, the shell) wins. ONNX
# Runtime keeps its two threads (semantic_embedder.THREADS): a lookup that is still running
# when the reply starts holds back its first audio.
LIBRARY_THREADS = {"OPENBLAS_NUM_THREADS": "1", "OPENCV_FOR_THREADS_NUM": "1",
                   "TOKENIZERS_PARALLELISM": "false"}
# The speaker stream while Ember is on the fast cores (realtime_gemini_10.py reads both once,
# at start): 512-frame callbacks and PortAudio's low output latency instead of sounddevice's
# defaults (1024 frames, 'high'). On the Orange Pi 5 Max with its USB speaker on PipeWire's
# default device, the default stream reported 42.7 ms of PortAudio latency and the audio
# cleanup measured about 96 ms of output latency; 512/low reported 10.7 ms and about 42 ms,
# with no output underflows in three 3.5-minute runs (one with MediaPipe pose on the CPU) and
# the mic and speaker callbacks' timing unchanged. A value already set (.env, the shell) wins.
SPEAKER_STREAM = {"BAYMAX_OUTPUT_BLOCKSIZE": "512", "BAYMAX_OUTPUT_LATENCY": "low"}
# Where each vision child runs while Ember is on the fast cores: which cores, and its nice value.
# face: face landmarks and face recognition; voice: speaker recognition (person_recognition)
VISION_PLACES = {"pose": ("fast", 5), "face": ("slow", 10), "voice": ("slow", 10)}


def _cpu_text(cpus):
    return ",".join(str(cpu) for cpu in sorted(cpus))


def cpus_from_env(name):
    """A CPU set as use_performance_cpus records it ("4,5,6,7"), or None if unset or malformed."""
    try:
        cpus = {int(part) for part in os.environ.get(name, "").split(",")}
    except ValueError:
        return None
    return cpus if min(cpus) >= 0 else None


_warned = set()  # invalid values already reported: run_mvp.py and run_pc.py both apply the settings


def gil_switch_ms():
    """BAYMAX_GIL_SWITCH_MS: how many ms a thread may keep Python's GIL while another thread
    waits for it, with Ember on the fast cores. 1 (default); 0 leaves Python's own (5 ms);
    otherwise 0.1-100. An invalid value is reported once per process."""
    text = os.getenv("BAYMAX_GIL_SWITCH_MS", "").strip()
    if not text:
        return 1.0
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if value == 0 or 0.1 <= value <= 100:     # also rejects nan and inf
        return value
    if text not in _warned:
        _warned.add(text)
        print("[CPU] Invalid BAYMAX_GIL_SWITCH_MS; using 1 ms.", flush=True)
    return 1.0


def _fast_core_settings(fast, slow):
    """Only with Ember kept on the fast cores (the RK3588 by default): where the vision
    children may go, single-threaded library pools, the 512-frame low-latency speaker stream
    (SPEAKER_STREAM), and a 1 ms GIL switch interval instead of Python's 5 ms. The mic
    callback lets go of the GIL inside each WebRTC frame (2-3 times a block) and has to win
    it back each time, waiting up to a whole interval: on the Orange Pi 5 Max it took about
    2.5 ms at the median but 9.3-10.2 ms at p99, just as long with pose on the NPU as on the
    CPU, so the waits come from the GIL, not from busy cores. Returns the interval in ms (0:
    Python's own was kept)."""
    os.environ.setdefault(FAST_CPUS_ENV, _cpu_text(fast))
    if slow:
        os.environ.setdefault(SLOW_CPUS_ENV, _cpu_text(slow))
    for name, value in {**LIBRARY_THREADS, **SPEAKER_STREAM}.items():
        os.environ.setdefault(name, value)
    switch_ms = gil_switch_ms()
    if switch_ms:
        sys.setswitchinterval(switch_ms / 1000)
    return switch_ms


def place_vision_child(role):
    """Called in a vision child (features/vision_process.py) before its model starts any
    thread, so they all inherit its cores and nice value. Only where Ember runs on the fast
    cores (FAST_CPUS_ENV); anywhere else nothing changes and it returns None.

    "pose" (NPU or MediaPipe, also the fall toolkit's own) stays on the fast cores at nice 5,
    so Ember's audio wins a core it shares with pose: on an A55 the NPU path's own CPU work
    (about 11.5 ms a frame on an A76) would take 3.6-5x as long, estimated to push its p90
    past the 100 ms a frame has at 10 fps. "face" (eye-contact greetings at 4 fps, which wait
    0.8 s for a look anyway) goes to the slow cores at nice 10. BAYMAX_VISION_CPUS=fast or
    slow moves either, to measure. Returns the cores."""
    fast, slow = cpus_from_env(FAST_CPUS_ENV), cpus_from_env(SLOW_CPUS_ENV)
    if not fast or not hasattr(os, "sched_setaffinity"):
        return None
    role = role if role in VISION_PLACES else "pose"
    where, nice = VISION_PLACES[role]
    chosen = os.getenv("BAYMAX_VISION_CPUS", "").strip().lower()
    if chosen in ("fast", "slow"):
        where = chosen
    elif chosen not in ("", "auto"):
        print(f"[CPU] Warning: unknown BAYMAX_VISION_CPUS={chosen}; using the {where} cores.", flush=True)
    if where == "slow" and not slow:
        where = "fast"               # e.g. Ember started under taskset -c 4-7: stay inside it
    cpus = slow if where == "slow" else fast
    try:
        os.sched_setaffinity(0, cpus)
    except OSError:
        return None
    pin_threads(cpus)
    lower_priority(nice)
    print(f"[CPU] {role.capitalize()} process on the {where} cores {_cpu_text(cpus)} at nice {nice}.", flush=True)
    return cpus


def use_performance_cpus():
    """Keep Ember on the fast cores of an ARM big.LITTLE board: MediaPipe runs pose on one
    thread, about 5x slower on an A55 than an A76 (measured on the RK3588). Moves every
    thread that already exists and the caller; threads started later and child processes
    inherit it. Every thread of Ember's own process stays there: they share one GIL, and
    whatever one did on an A55 while holding it would keep the audio threads waiting 3.6-5x
    as long. Once pinned it applies _fast_core_settings; the vision children then place
    themselves (place_vision_child). run_mvp.py and run_pc.py call it first thing; a second
    call changes nothing and prints nothing.

    BAYMAX_CPU_PINNING: auto (default) pins only on an RK3588 (Orange Pi 5 / 5 Max, where
    it was measured: 4 fast cores); on pins on any ARM big.LITTLE board, e.g. to try the
    Orange Pi 4 Pro, whose A733 has only 2 fast cores for Ember and the pose process
    (unmeasured); off never pins."""
    mode = os.getenv("BAYMAX_CPU_PINNING", "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return None
    if mode not in ("on", "1", "true", "yes") and not is_rk3588():
        return None  # auto: the RK3588 only
    cpus = performance_cpus()
    if not cpus:
        return None
    allowed = os.sched_getaffinity(0)    # before narrowing: the other cores the children may use
    already = allowed == cpus
    try:
        os.sched_setaffinity(0, cpus)
    except OSError as error:
        print(f"[CPU] Could not keep Ember on the fast cores ({error}); using all.", flush=True)
        return None
    pin_threads(cpus)
    switch_ms = _fast_core_settings(cpus, allowed - cpus)
    if not already:
        print(f"[CPU] Running on the fast cores {_cpu_text(cpus)}, GIL switch interval "
              f"{switch_ms or sys.getswitchinterval() * 1000:g} ms (BAYMAX_CPU_PINNING=off to use all).",
              flush=True)
    return cpus


def camera_candidates():
    value = os.getenv("BAYMAX_CAMERA_DEVICE", "").strip()
    if value == "none":
        return []
    if sys.platform == "darwin":
        from macos_devices import camera_devices, resolve_camera
        if not value:
            return [device["index"] for device in camera_devices()]
        try:
            index = int(value)
            if index < 0:
                raise ValueError()
        except ValueError:
            raise RuntimeError("Choose a camera in devUI and save before starting Gemini.") from None
        return [resolve_camera(index, os.getenv("BAYMAX_CAMERA_UID", ""), os.getenv("BAYMAX_CAMERA_NAME", ""))]
    if sys.platform.startswith("linux") and value.startswith("/dev/"):
        # A udev link such as /dev/v4l/by-id/...-video-index0 survives reboots
        # that renumber /dev/videoN.
        return [value]
    if not value:
        return [0, 1, 2]
    try:
        index = int(value)
        if index < 0:
            raise ValueError()
        name = os.getenv("BAYMAX_CAMERA_NAME", "")
        if name and sys.platform == "win32":
            import comtypes
            from pygrabber.dshow_graph import FilterGraph
            comtypes.CoInitialize()
            try:
                names = FilterGraph().get_input_devices()
            finally:
                comtypes.CoUninitialize()
            if index >= len(names) or names[index] != name:
                matches = [i for i, candidate in enumerate(names) if candidate == name]
                if len(matches) != 1:
                    raise RuntimeError("Saved camera is unavailable or ambiguous. Reopen devUI and choose a camera.")
                index = matches[0]
        return [index]
    except ValueError:
        raise RuntimeError("Choose a camera in devUI and save before starting Gemini.") from None


def open_camera(cv2, index):
    if sys.platform == "darwin":
        from macos_devices import check_camera_permission
        check_camera_permission()
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
    if isinstance(index, str):
        backend = cv2.CAP_V4L2
    return cv2.VideoCapture(index, backend)


def latest_frames_only(cv2, capture):
    """OpenCV's V4L2 backend queues four frames, so reading at 15 fps from a 30 fps camera
    can return pictures up to ~0.2 s old (estimated from the queue, not measured). Keep
    one, so pose and fall detection see the present. Linux on 64-bit ARM only (the
    Orange Pi): the runtime calls it only for the pc profile, which the x86 LattePanda
    MVP runs too, and arm_linux() keeps that on OpenCV's default (unit 001's startup.sh
    path and the Docker image never call it). Not measured on the Orange Pi either:
    camera loop FPS and frame age with one buffer are still to be checked there."""
    if arm_linux():
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def configure_audio_backend(sd, microphone=None, speaker=None):
    if sys.platform != "win32":
        return
    hosts = sd.query_hostapis()
    settings = []
    for index, direction in ((microphone, "input"), (speaker, "output")):
        device = sd.query_devices(index, direction)
        settings.append(sd.WasapiSettings(auto_convert=True)
                        if hosts[device["hostapi"]]["name"] == "Windows WASAPI" else None)
    sd.default.extra_settings = tuple(settings)


def _without_alsa_card(name):
    return re.sub(r"\s*\(hw:\d+,\d+\)$", "", name)


def select_audio_devices(sd):
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    chosen = []
    for name, default, channels in (
        ("BAYMAX_MIC_DEVICE", default_in, "max_input_channels"),
        ("BAYMAX_SPEAKER_DEVICE", default_out, "max_output_channels"),
    ):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                index = int(raw)
            except ValueError:
                raise RuntimeError(f"{name} must be a device number from python check_pc.py.") from None
            prefix = "BAYMAX_MIC" if channels == "max_input_channels" else "BAYMAX_SPEAKER"
            saved_name = os.getenv(prefix + "_NAME", "")
            saved_host = os.getenv(prefix + "_HOSTAPI", "")
            if saved_name and saved_host:
                hosts = sd.query_hostapis()
                matches = [i for i, device in enumerate(devices) if device[channels] > 0
                           and device["name"] == saved_name and hosts[device["hostapi"]]["name"] == saved_host]
                if not matches:
                    # ALSA card numbers in "(hw:N,M)" can change between boots.
                    stable = _without_alsa_card(saved_name)
                    matches = [i for i, device in enumerate(devices) if device[channels] > 0
                               and _without_alsa_card(device["name"]) == stable
                               and hosts[device["hostapi"]]["name"] == saved_host]
                    if len(matches) > 1:
                        matches = []  # Identical renumbered devices: the saved index may be the other one.
                if index not in matches:
                    if len(matches) == 1:
                        index = matches[0]
                    else:
                        raise RuntimeError(f"Saved {prefix} device is unavailable or ambiguous. Reopen devUI and choose a device.")
        else:
            index = int(default)
            if index < 0:
                candidates = [i for i, device in enumerate(devices) if device[channels] > 0]
                preferred = "microphone" if channels == "max_input_channels" else "speakers"
                index = next((i for i in candidates if preferred in devices[i].get("name", "").lower()),
                             candidates[0] if candidates else -1)
        if index < 0 or index >= len(devices) or devices[index][channels] < 1:
            raise RuntimeError(f"No usable device for {name}. Run python check_pc.py and set {name} in .env.")
        chosen.append(index)
    if sys.platform == "win32" and hasattr(sd, "WasapiSettings"):
        configure_audio_backend(sd, *chosen)
    sd.check_input_settings(device=chosen[0], channels=min(2, devices[chosen[0]]["max_input_channels"]),
                            samplerate=48000, dtype="int16")
    sd.check_output_settings(device=chosen[1], channels=1, samplerate=48000, dtype="int16")
    return tuple(chosen)


# ─── The speaker's own volume (Linux/ALSA) ────────────────────────────────────
# Playback volume controls tried in this order; others on the card come after them.
SPEAKER_CONTROLS = ("PCM", "Master", "Speaker", "Headphone")


def _amixer(args, timeout=2.0):
    """amixer's output; RuntimeError when amixer is missing, fails or hangs."""
    import subprocess
    try:
        done = subprocess.run(["amixer", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"amixer: {error}") from None
    if done.returncode != 0:
        raise RuntimeError(f"amixer {' '.join(args)}: {(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


def _db_range(cget):
    """(lowest, loudest) dB of a volume control, from `amixer cget` of it, or None."""
    span = re.search(r"dB(?:minmax|linear)-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", cget)
    if span:
        return float(span.group(1)), float(span.group(2))
    scale = re.search(r"dBscale-min=(-?[\d.]+)dB,step=([\d.]+)dB", cget)
    steps = re.search(r"min=(-?\d+),max=(-?\d+)", cget)
    if scale and steps:
        low, step = float(scale.group(1)), float(scale.group(2))
        return low, low + step * (int(steps.group(2)) - int(steps.group(1)))
    return None


class SpeakerMixer:
    """The playback volume of the ALSA card Ember's speaker is on, set through amixer: no
    root, no extra audio stream. Made by speaker_mixer()."""

    def __init__(self, card, control, low_db, loud_db, switch=False, run=None):
        self.card, self.control = card, control
        self.low_db, self.loud_db = low_db, loud_db
        self.switch = switch  # the control has an on/off switch: set_db turns it on
        self._run = run or _amixer

    def get_db(self):
        values = re.findall(r"\[(-?\d+(?:\.\d+)?)dB\]", self._run(["-c", str(self.card), "sget", self.control]))
        if not values:
            raise RuntimeError(f"no dB reading for {self.control} on card {self.card}")
        return max(float(v) for v in values)  # the louder channel

    def set_db(self, db):
        """Set every channel to `db`, within the control's range; returns what was set."""
        db = min(self.loud_db, max(self.low_db, db))
        self._run(["-q", "-c", str(self.card), "sset", self.control, "--", f"{db:.2f}dB",
                   *(["unmute"] if self.switch else [])])
        return db


def speaker_mixer(device_name, run=None):
    """The hardware volume of the speaker PortAudio names `device_name`, e.g. "UACDemoV1.0:
    USB Audio (hw:4,0)", as a SpeakerMixer, or None: not Linux, not a card PortAudio opens
    directly (PipeWire/Pulse devices, macOS, Windows), BAYMAX_SPEAKER_MIXER=off, or no
    playback volume with a dB scale of at least 6 dB. BAYMAX_SPEAKER_MIXER=<name> picks the
    control by its simple name ("PCM", "Master", ...)."""
    setting = os.getenv("BAYMAX_SPEAKER_MIXER", "").strip()
    if not sys.platform.startswith("linux") or setting.lower() in ("0", "off", "no", "false"):
        return None
    card = re.search(r"\(hw:(\d+),\d+\)$", device_name or "")
    if not card:
        return None
    card = card.group(1)
    run = run or _amixer
    try:
        controls = run(["-c", card, "controls"])
        names = re.findall(r"name='(.+?) Playback Volume'", controls)
        wanted = [setting] if setting else (
            [c for c in SPEAKER_CONTROLS if c in names] + [n for n in names if n not in SPEAKER_CONTROLS])
        for control in wanted:
            if control not in names:
                continue
            span = _db_range(run(["-c", card, "cget", f"name={control} Playback Volume"]))
            if span is None or span[1] - span[0] < 6:
                continue
            return SpeakerMixer(int(card), control, *span, run=run,
                                switch=f"name='{control} Playback Switch'" in controls)
    except RuntimeError as error:
        print(f"[VOLUME] The speaker's own volume is unavailable ({error}).", flush=True)
    return None
