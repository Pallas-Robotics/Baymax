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

    def start(self, samples, label=""):
        duration = len(samples) / audio.RATE
        self.played.append({"label": label, "seconds": duration, "rms_dbfs": audio.rms_dbfs(samples)})
        if self.robot is not None:
            self.robot.hear(label, duration, samples)
        return duration

    def stop(self):
        pass

    def close(self):
        pass


class FakeDisplay:
    available = True

    def __init__(self):
        self.shown, self.cleared = [], 0

    def show(self, jpeg):
        self.shown.append(jpeg)

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


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
        clock.listeners.append(self.advance)

    # ── Runtime interface ────────────────────────────────────────────────────
    def start(self):
        self.pid, self.running, self.code = 4000 + self.starts, True, None
        self.starts += 1
        self.events, self.transcripts, self.sequence, self.active = [], [], 0, {}
        self.timing, self.last_user = [], None
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

    def hear(self, label, duration, samples):
        self.playing_until = self.clock.time() + duration
        self.playing_dbfs = audio.rms_dbfs(samples) + self.coupling_db
        self.heard_labels.append(label)
        for rule in self.rules:
            if re.search(rule["on"], label, re.IGNORECASE):
                delay = duration
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
            self.transcribe("ember", action["reply"])
            self.print(f"[GEMINI TXT] {action['reply']}")
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
