"""Offline stand-ins for the rig's hardware: a clock, speaker, display, sampler and a scripted robot.

`FakeRobot` plays the runtime: it writes `.runtime-status.json` and a stdout log in
the same formats as runtime_monitor.py and realtime_gemini_10.py, reacting to what
the fake speaker "plays" (by its text label) with scripted rules. Nothing here opens
a device, starts a process or contacts a service.
"""
import json
from pathlib import Path
import re

import numpy as np

from evals.robot import audio


class FakeClock:
    """Time that only moves when someone sleeps; listeners run on every advance."""

    def __init__(self, start=1_800_000_000.0):
        self.now, self.listeners = start, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)
        for listener in list(self.listeners):
            listener(self.now)


class FakeSpeaker:
    def __init__(self, robot=None):
        self.robot, self.played = robot, []

    def start(self, samples, label="", parts=None):
        """`parts` (timeline tracks): [(offset_s, label, seconds)] cues inside `samples`; the
        robot hears each one at its offset instead of the whole track."""
        duration = len(samples) / audio.RATE
        self.played.append({"label": label, "seconds": duration, "rms_dbfs": audio.rms_dbfs(samples),
                            "parts": list(parts or []), "at": self.robot.clock.time() if self.robot else None,
                            "samples": samples})
        if self.robot is not None:
            if parts is None:
                self.robot.hear(label, duration, samples)
            for offset, part, seconds in parts or []:
                self.robot.hear(part, seconds, samples, delay=offset)
        return duration

    def stop(self):
        if self.played and self.robot is not None and "stopped" not in self.played[-1]:
            self.played[-1]["stopped"] = self.robot.clock.time()  # A restart cuts the earlier buffer here.

    def close(self):
        pass


class FakeDisplay:
    """Records cards and clips; confirms each `ack_delay` seconds later (None: never)."""

    available, plays_video = True, True

    def __init__(self, clock=None, ack_delay=0.08):
        self.shown, self.cleared, self.videos, self.labels = [], 0, [], []
        self.clock, self.ack_delay, self.version, self.sent = clock, ack_delay, 0, {}

    def _next(self, event):
        self.version += 1
        if self.clock is not None:
            self.sent[self.version] = (event, self.clock.time())
        return self.version

    robot = None  # A FakeRobot "sees" what is shown: its rules also match card and clip labels.

    def show(self, jpeg, label=""):
        self.shown.append(jpeg)
        self.labels.append(label)
        version = self._next("shown")
        if self.robot is not None and label:
            self.robot.hear(label, 0.0, np.zeros(0, dtype=np.int16))
        return version

    def play_video(self, path, label=""):
        self.videos.append(str(path))
        self.labels.append(label)
        version = self._next("playing")
        if self.robot is not None:
            self.robot.hear(label or Path(path).name, 0.0, np.zeros(0, dtype=np.int16))
        return version

    def ack(self, version, event):
        sent = self.sent.get(version)
        if sent is None or sent[0] != event or self.ack_delay is None:
            return None
        return sent[1] + self.ack_delay if self.clock.time() >= sent[1] + self.ack_delay else None

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


class FakeHook:
    """A light, fault or plug: records calls with the clock time."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def set(self, percent):
        self.calls.append(("set", percent, self.clock.time()))

    def start(self):
        self.calls.append(("start", None, self.clock.time()))

    def stop(self):
        self.calls.append(("stop", None, self.clock.time()))


class FakeSampler:
    def __init__(self, cpu=20.0, rss=500.0):
        self.cpu, self.rss = cpu, rss

    def __call__(self, pid):
        return self

    def sample(self):
        return {"cpu_percent": self.cpu, "rss_mb": self.rss}


def fake_speak(text, voice=None):
    """A tone as long as speaking `text` takes (~0.3 s a word), instead of real TTS."""
    seconds = max(0.4, 0.3 * len(text.split()))
    t = np.arange(int(audio.RATE * seconds)) / audio.RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)


class FakeRobot:
    """A scripted runtime. `rules` are [{"on": regex over what was played, "do": [actions]}].

    Actions run in order, `after` seconds apart (default 0.5) from the end of playback:
      {"user": text}  {"reply": text, "seconds": 2}  {"tool": name}  {"log": line}
      {"event": [state, message]}  {"state": [state, message]}  {"exit": code}
    """

    def __init__(self, folder, clock, rules=(), ready_after=3.0, coupling_db=-6.0, startup_log=(), timing=True,
                 trace_path=None):
        folder = Path(folder)
        self.status_path, self.log_path = folder / ".runtime-status.json", folder / "runtime.log"
        self.stop_path = folder / ".runtime-stop.json"
        self.clock, self.rules, self.ready_after = clock, list(rules), ready_after
        self.coupling_db, self.startup_log = coupling_db, list(startup_log)
        self.pid, self.running, self.code, self.starts = 0, False, None, 0
        self.pending, self.playing_until, self.playing_dbfs = [], 0.0, -90.0
        self.next_mic, self.heard_labels = 0.0, []
        self.with_timing, self.trace_path = timing, trace_path  # v10 conversation_timing; EMBER_TRACE lines.
        self.display = None  # A FakeDisplay, for replies that read the screen.
        clock.listeners.append(self.advance)

    # ── Runtime interface ────────────────────────────────────────────────────
    def start(self):
        self.pid, self.running, self.code = 4000 + self.starts, True, None
        self.starts += 1
        self.events, self.transcripts, self.sequence, self.active = [], [], 0, {}
        self.timing, self.last_user, self.replies = [], None, []
        self.state, self.detail, self.pending = "starting", "Starting Ember", []
        self.trace("trace_start", schema=1, pid=self.pid, text_enabled=False)
        self.emit("connecting", "Connecting to Gemini Live")
        for line in self.startup_log:
            self.print(line)
        self.schedule(self.ready_after, {"state": ["listening", "Connected to Gemini; listening"]})
        self.write()
        return self.clock.time()

    def alive(self):
        return self.running

    def exit_code(self):
        return self.code

    def stop(self):
        if self.running:
            self.emit("stopped", "Ember stopped")
            self.running, self.code = False, 0
            self.write()
        return self.code

    # ── Monitor-shaped output ────────────────────────────────────────────────
    def print(self, line):
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    def trace(self, kind, **fields):
        """A harness trace event in schema version 1 (Pallas-Robotics/Baymax#10)."""
        if self.trace_path is not None:
            self.trace_seq = getattr(self, "trace_seq", 0) + 1
            event = {"seq": self.trace_seq, "t": self.clock.time() - 1_000_000.0, "wall": self.clock.time(),
                     "kind": kind, "session": self.starts}
            with Path(self.trace_path).open("a", encoding="utf-8") as out:
                out.write(json.dumps(dict(event, **fields)) + "\n")

    def emit(self, state, message):
        if (self.state, self.detail) != (state, message):
            self.events.append({"time": self.clock.time(), "state": state, "message": message})
            self.trace("state", state=state)
        self.state, self.detail = state, message

    def record(self, state, message):
        self.events.append({"time": self.clock.time(), "state": state, "message": message})

    def transcribe(self, speaker, text):
        self.sequence += 1
        self.transcripts.append({"id": self.sequence, "speaker": speaker, "text": text, "time": self.clock.time(),
                                 "live": False})

    def write(self):
        status = {"pid": self.pid, "heartbeat": self.clock.time(), "state": self.state, "detail": self.detail,
                  "events": self.events[-80:], "transcripts": self.transcripts[-60:], "activities": {},
                  "metrics": {}}
        if self.with_timing:
            status["conversation_timing"] = self.timing[-40:]
        self.status_path.write_text(json.dumps(status), encoding="utf-8")

    # ── Behaviour ────────────────────────────────────────────────────────────
    def schedule(self, delay, action):
        self.pending.append((self.clock.time() + delay, action))
        self.pending.sort(key=lambda item: item[0])

    def hear(self, label, duration, samples, delay=0.0):
        self.playing_until = max(self.playing_until, self.clock.time() + delay + duration)
        self.playing_dbfs = audio.rms_dbfs(samples) + self.coupling_db
        self.heard_labels.append(label)
        for rule in self.rules:
            if re.search(rule["on"], label, re.IGNORECASE):
                delay += duration
                for action in rule["do"]:
                    delay += action.get("after", 0.5)
                    self.schedule(delay, action)
                break

    def advance(self, now):
        if not self.running:
            return
        if self.stop_path.exists():
            self.stop_path.unlink()
            self.stop()
            return
        while self.pending and self.pending[0][0] <= now:
            self.act(self.pending.pop(0)[1])
            if not self.running:
                return
        if now >= self.next_mic:
            dbfs = self.playing_dbfs if now <= self.playing_until else -60.0
            self.print(f"[MIC LEVEL] rms={int(32768 * 10 ** (dbfs / 20))}  peak=0  (max=32767)")
            self.next_mic = now + 1.0
        self.write()

    def act(self, action):
        if "user" in action:
            self.transcribe("user", action["user"])
            self.last_user = self.clock.time()
            self.emit("thinking", "User speech received; waiting for Gemini")
            self.print(f"[USER] {action['user']}")
        elif "reply" in action:
            self.emit("speaking", "Gemini is producing reply audio")
            now = self.clock.time()
            # As timing.py: reply audio arrives, then the speaker starts it (playback) shortly after.
            self.trace("audio_received", bytes=9600, duration_ms=200.0)
            self.timing.append({"id": len(self.timing) + 1, "speech_end": self.last_user, "reply_audio": now,
                                "playback": now + action.get("playback_s", 0.04)})
            self.replies.append((now + action.get("playback_s", 0.04), action.get("seconds", 2.0)))
            text = action["reply"]
            if "{display}" in text:  # Read back what the fake display shows (the screen check's code).
                labels = getattr(self.display, "labels", None) or [""]
                text = text.replace("{display}", " ".join(re.findall(r"\d+", labels[-1])) or "nothing")
            self.transcribe("ember", text)
            self.print(f"[GEMINI TXT] {text}")
            self.schedule(action.get("seconds", 2.0), {"state": ["listening", "Reply complete; listening "
                                                                 "(buffered audio may still play)"]})
        elif "tool" in action:
            now = self.clock.time()
            self.events.append({"id": f"tool-{len(self.events)}", "time": now, "state": "toolkit",
                                "name": action["tool"], "started_at": now, "finished_at": now,
                                "message": "Finished " + action["tool"]})
        elif "log" in action:
            self.print(action["log"])
        elif "event" in action:
            self.record(*action["event"])
        elif "state" in action:
            self.emit(*action["state"])
        elif "exit" in action:
            self.running, self.code = False, action["exit"]


def ember_voice(seconds, rate, seed=3):
    """A speech-like harmonic signal for Ember's replies in a fake reference recording."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    syllables = 0.5 * (1 + np.sin(2 * np.pi * 4 * t)) ** 2
    x = sum(np.sin(2 * np.pi * 230 * k * t + rng.uniform(0, 6)) / k for k in range(1, 8)) * syllables
    return x / np.max(np.abs(x))


class FakeRecorder:
    """A reference microphone hearing the fake room: what the FakeSpeaker played (at `coupling`)
    plus Ember's replies from the FakeRobot's playback times, over a noise floor. 16 kHz."""

    def __init__(self, clock, speaker, robot, rate=16000, noise=30.0, coupling=0.4, ember=6000.0, broken=False):
        self.clock, self.speaker, self.robot, self.rate = clock, speaker, robot, rate
        self.noise, self.coupling, self.ember, self.broken = noise, coupling, ember, broken
        self.started, self.discarded, self.recording = None, 0, False

    def start(self):
        self.started, self.recording = self.clock.time(), True

    def stop(self):
        from evals.robot import measure
        if not self.recording:
            return None
        self.recording = False
        seconds = self.clock.time() - self.started
        out = np.random.default_rng(0).standard_normal(int(seconds * self.rate)) * self.noise

        def add(signal, at):
            start = int(round((at - self.started) * self.rate))
            if start >= len(out) or start + len(signal) <= 0:
                return
            if start < 0:
                signal, start = signal[-start:], 0
            part = out[start:start + len(signal)]
            part += signal[:len(part)]
        for played in self.speaker.played:
            if played.get("at") is not None and played["at"] + played["seconds"] >= self.started:
                heard = played["samples"]
                if "stopped" in played:
                    heard = heard[:max(0, int((played["stopped"] - played["at"]) * audio.RATE))]
                add(measure.to_rate(heard, audio.RATE, self.rate) * self.coupling, played["at"])
        if not self.broken:
            for when, length in self.robot.replies:
                add(ember_voice(min(length, 4.0), self.rate) * self.ember, when)
        return np.clip(out, -32768, 32767).astype(np.int16), self.rate, self.started

    def discard(self):
        self.discarded += 1

    def close(self):
        pass


class FakePower:
    def __init__(self, boot_s=42.0):
        self.boot_s, self.cycles = boot_s, 0

    def cycle(self, clock):
        self.cycles += 1
        clock.sleep(self.boot_s)
        return self.boot_s
