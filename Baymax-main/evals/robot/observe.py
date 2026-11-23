"""What the robot did, read only from what the runtime already writes.

Sources:
- `.runtime-status.json` (runtime_monitor.py, rewritten every 0.2 s): the current
  state, recent events (states, toolkit activity, engagement, eyes, audio notes),
  live transcripts and metrics.
- The runtime's stdout log (the rig starts it with stdout redirected to a file):
  `[USER]`, `[GEMINI TXT]`, `[ENGAGE]`, `[WAKE]`, `[EYES]`, `[MIC LEVEL]`, ...
  Lines carry no timestamps, so each is stamped when the rig reads it (<= 0.2 s late).
- `conversation_timing` in the status file (v10, features/conversational_ability/
  timing.py, the rows devops/performance/check.py reads): per turn, the local speech
  end and when reply audio was scheduled to the speaker (`playback`), in wall-clock
  seconds stamped by the runtime itself.
- The agent harness event trace (Pallas-Robotics/Baymax#10, schema version 1), when the
  runtime writes one (`EMBER_TRACE=<file>`; see `TraceReader`). Every event carries the
  runtime's own `wall` time, so state changes are exact rather than seen on the next
  status poll, and `audio_received` gives when the first reply audio arrived from
  Gemini. Without a trace (an older runtime, or another schema version) the status/log
  parsing above is used unchanged.
- CPU/memory samples of the runtime process (hardware.ProcessSampler).

The observer merges these into one timeline; `Window` answers questions about one
step's slice of it. Times are wall-clock seconds (time.time()), the same clock the
runtime uses for its events, because the rig runs on the robot.
"""
import json
from pathlib import Path
import re

CONVERSATION_STATES = {"starting", "connecting", "listening", "thinking", "speaking", "reconnecting",
                       "stopping", "stopped"}
MIC_LEVEL = re.compile(r"\[MIC LEVEL\] rms=(\d+)")
ECHO_EVENT = "Possible speaker echo"


class Observer:
    def __init__(self, status_path, log_path, clock, trace_path=None):
        self.status_path, self.log_path, self.clock = Path(status_path), Path(log_path), clock
        self.trace = TraceReader(trace_path) if trace_path else None
        self.timing = {}               # (start, turn id) -> conversation_timing row
        self.reply_audio = []          # trace audio_received wall times
        self.events, self._event_keys = [], set()
        self.transcripts = {}          # id -> {"speaker", "text", "time"}
        self.log = []                  # (time read, line)
        self.polled = []               # (time, state) whenever the status file's state changes
        self.samples = []              # (time, {"cpu_percent", "rss_mb"})
        self.starts = []               # runtime start times
        self.heartbeat = None
        self.pid = None
        self.last_change = 0.0         # Last new event, transcript text or state (not log noise).
        self._offset = 0
        self._partial = ""

    def runtime_started(self, at):
        """A (re)start: the transcript ids restart, and engagement resets to engaged."""
        self.starts.append(at)
        self.last_change = self.clock.time()

    def poll(self):
        now = self.clock.time()
        self._read_status(now)
        if self.trace is not None:
            self._read_trace(now)
        self._read_log(now)

    def sample(self, values):
        if values:
            self.samples.append((self.clock.time(), values))

    def _read_status(self, now):
        try:
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(status, dict):
            return
        since = self.starts[-1] if self.starts else 0
        if status.get("heartbeat", 0) < since:
            return  # Still the previous session's file.
        self.heartbeat, self.pid = status.get("heartbeat"), status.get("pid")
        state = status.get("state")
        if state and (not self.polled or self.polled[-1][1] != state):
            self.polled.append((now, state))
            self.last_change = now
        for row in status.get("conversation_timing", []) or []:
            if isinstance(row, dict) and row.get("id") is not None:
                self.timing[(since, row["id"])] = {k: row.get(k) for k in ("speech_end", "reply_audio", "playback")}
        for event in status.get("events", []):
            if not isinstance(event, dict) or event.get("time", 0) < since:
                continue
            key = (event.get("id") or "", event.get("time"), event.get("state"), event.get("message"))
            if event.get("id"):
                key = (event["id"], since)  # Toolkit events are updated in place when they finish.
            if key not in self._event_keys:
                self._event_keys.add(key)
                self.events.append(dict(event))
                self.last_change = now
        for message in status.get("transcripts", []):
            if not isinstance(message, dict) or message.get("time", 0) < since:
                continue
            key = (since, message.get("id"))  # Ids restart with each runtime process.
            entry = self.transcripts.setdefault(key, {"speaker": message.get("speaker"), "time": message.get("time")})
            if entry.get("text") != message.get("text", ""):
                entry["text"] = message.get("text", "")
                self.last_change = now

    def _read_trace(self, now):
        since = self.starts[-1] if self.starts else 0
        for record in self.trace.read():
            when, kind = record["wall"], record["kind"]
            if when < since - 1:
                continue  # A previous runtime's events (the file is appended to).
            if kind == "state" and record.get("state"):
                self._add_event({"time": when, "state": record["state"], "message": "trace", "source": "trace"}, now)
            elif kind == "audio_received":
                self.reply_audio.append(when)
            elif kind in TRACE_EVENTS:
                detail = " ".join(str(record[k]) for k in ("name", "outcome", "error") if record.get(k))
                self._add_event({"time": when, "state": "trace", "message": f"{kind} {detail}".strip(),
                                 "source": "trace"}, now)

    def reply_audio_after(self, when):
        """Earliest trace `audio_received` (first reply audio from Gemini) at or after `when`."""
        times = [t for t in self.reply_audio if t >= when]
        return min(times) if times else None

    def _add_event(self, event, now):
        key = (event.get("id") or "", event.get("time"), event.get("state"), event.get("message"))
        if key not in self._event_keys:
            self._event_keys.add(key)
            self.events.append(event)
            self.last_change = now

    def playback_after(self, when):
        """Earliest runtime-reported reply playback (conversation_timing) at or after `when`."""
        times = [row["playback"] for row in self.timing.values()
                 if isinstance(row.get("playback"), (int, float)) and row["playback"] >= when]
        return min(times) if times else None

    def _read_log(self, now):
        try:
            with self.log_path.open("rb") as stream:
                stream.seek(self._offset)
                data = stream.read()
        except OSError:
            return
        if not data:
            return
        self._offset += len(data)
        text = self._partial + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop()
        self.log += [(now, line.rstrip("\r")) for line in lines if line.strip()]

    def tail(self, count=20):
        return [line for _, line in self.log[-count:]]

    def window(self, start, end=None):
        return Window(self, start, self.clock.time() if end is None else end)


class Window:
    """One step's slice of the timeline: [start, end] in wall-clock seconds."""

    def __init__(self, observer, start, end):
        self.o, self.start, self.end = observer, start, end

    def _inside(self, when):
        return when is not None and self.start <= when <= self.end

    def events(self):
        return [e for e in self.o.events if self._inside(e.get("time"))]

    def event_lines(self):
        return [f"{e.get('state')}: {e.get('message', '')}" for e in self.events()]

    def log_lines(self):
        return [line for when, line in self.o.log if self._inside(when)]

    def transcripts(self, speaker):
        return [t for t in self.o.transcripts.values() if t["speaker"] == speaker and self._inside(t["time"])]

    def heard(self):
        return " ".join(t["text"].strip() for t in self.transcripts("user")).strip()

    def reply(self):
        return " ".join(t["text"].strip() for t in self.transcripts("ember")).strip()

    def tools(self):
        return [e.get("name") for e in self.events() if e.get("state") == "toolkit" and e.get("name")]

    def states(self):
        """Conversation states in order: runtime events, plus the polled state (which shows
        'speaking' while buffered reply audio is still playing)."""
        timeline = [(e["time"], e["state"]) for e in self.events() if e.get("state") in CONVERSATION_STATES]
        timeline += [(when, state) for when, state in self.o.polled if self._inside(when)]
        ordered = []
        for _, state in sorted(timeline, key=lambda item: item[0]):
            if not ordered or ordered[-1] != state:
                ordered.append(state)
        return ordered

    def first(self, state, after=None):
        """Earliest time `state` was reported at or after `after` (default: window start)."""
        after = self.start if after is None else after
        times = [e["time"] for e in self.events() if e.get("state") == state and e["time"] >= after]
        times += [when for when, s in self.o.polled if s == state and self._inside(when) and when >= after]
        return min(times) if times else None

    def replies(self):
        """Separate Ember replies that started in the window."""
        return len(self.transcripts("ember"))

    def mic_levels(self):
        return [int(m.group(1)) for line in self.log_lines() for m in [MIC_LEVEL.search(line)] if m]

    def cpu(self):
        return [values for when, values in self.o.samples if self._inside(when)]

    def echo_events(self):
        return [line for line in self.event_lines() if ECHO_EVENT in line]

    def engaged(self):
        """Engagement at the window's end: a restart starts engaged; '[ENGAGE]'/engagement events change it."""
        last_start = max([s for s in self.o.starts if s <= self.end], default=0)
        changes = [(e["time"], e.get("message", "")) for e in self.o.events
                   if e.get("state") == "engagement" and last_start <= e.get("time", 0) <= self.end]
        changes += [(when, line) for when, line in self.o.log
                    if line.startswith("[ENGAGE]") and last_start <= when <= self.end
                    and ("Disengaged" in line or "Engaged (" in line)]
        if not changes:
            return True
        return not re.search(r"\bDisengaged\b", sorted(changes)[-1][1])


TRACE_SCHEMA = 1
# Trace kinds kept as "trace: <kind> ..." events (matchable with `event` expectations).
TRACE_EVENTS = ("connected", "session_closed", "connection_failed", "interrupted", "turn_complete",
                "generation_complete", "tool_end", "tool_timeout", "tool_error", "tool_invalid", "tool_cancelled",
                "go_away", "receive_error")


class TraceReader:
    """Reads the agent harness event trace incrementally: JSON lines in schema version 1
    (Pallas-Robotics/Baymax#10, features/harness/README.md "Schema (version 1)").

    Every event has `seq`, `t` (monotonic), `wall` (Unix seconds), `kind` and `session`.
    The rig uses `wall`, the same clock as the status file. A `trace_start` with another
    `schema` turns the reader off, so the observer falls back to the status file and log.
    Transcript text is only present with EMBER_TRACE_TEXT=1, which the rig does not set:
    transcripts come from the status file. A partial last line is kept until it ends.
    """

    def __init__(self, path):
        self.path, self.offset, self.partial = Path(path), 0, ""
        self.active, self.schema, self.counts = False, None, {}

    def read(self):
        if self.schema is not None and self.schema != TRACE_SCHEMA:
            return []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
        except OSError:
            return []
        if not data:
            return []
        self.offset += len(data)
        lines = (self.partial + data.decode("utf-8", errors="replace")).split("\n")
        self.partial = lines.pop()
        records = []
        for line in lines:
            record = parse_trace(line)
            if record is None:
                continue
            if record["kind"] == "trace_start":
                self.schema = record.get("schema")
                if self.schema != TRACE_SCHEMA:
                    self.active = False
                    return []
                continue
            self.active = True
            self.counts[record["kind"]] = self.counts.get(record["kind"], 0) + 1
            records.append(record)
        return records


def parse_trace(line):
    """One schema-1 event as a dict, or None for a line that is not one."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) \
            or not isinstance(raw.get("wall"), (int, float)):
        return None
    return raw
