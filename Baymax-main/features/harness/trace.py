"""Ember's event trace: every runtime event, timestamped, recorded in one place.

The runtime (realtime_gemini_10.py) calls `record(kind, **fields)` at each step (mic chunk sent,
model message received, tool start/end, reconnect, state change). Each event
gets a sequence number, a monotonic timestamp `t` (seconds, for durations and
ordering) and a wall-clock `wall` (Unix seconds, for lining up with logs),
then goes to every sink:

    memory   the last KEEP events, always (tests, in-process evals)
    jsonl    one JSON object per line, only when EMBER_TRACE=path is set
    monitor  counters into runtime_monitor's status, and tool problems into
             its event list, so devUI and the status file keep working

Privacy is enforced here, not by each caller:
    - audio and image bytes are never recorded (byte values are dropped);
    - transcript text and tool argument values (`TEXT_FIELDS`) are dropped
      unless EMBER_TRACE_TEXT=1 (or include_text=True);
    - the trace file is local only (nothing uploads it) and `*.trace.jsonl`
      and `traces/` are gitignored.
The schema is in features/harness/README.md. Bump SCHEMA_VERSION on an
incompatible change: the live eval, the robot rig and fusion tests read it.
"""
from collections import deque
import itertools
import json
import os
import threading
import time

SCHEMA_VERSION = 1
KEEP = 2000
TEXT_FIELDS = ("text", "arg_values")

# kind -> fields beyond the common ones (seq, t, wall, kind, session). Documented in README.md.
KINDS = {
    "trace_start": ("schema", "pid", "text_enabled"),
    "connect_start": ("attempt", "model"),
    "connected": ("attempt",),
    "connection_failed": ("attempt", "error", "message"),
    "session_closed": ("attempt",),
    "state": ("state",),
    "mic_chunk_sent": ("bytes", "queue_ms", "send_ms"),
    "mic_chunk_dropped": ("bytes", "queue_ms", "reason"),
    "mic_send_failed": ("bytes", "error"),
    "video_frame_sent": ("bytes", "encode_ms", "send_ms"),
    "video_frame_failed": ("error",),
    "input_transcription": ("chars", "text"),
    "output_transcription": ("chars", "text"),
    "model_text": ("chars", "text"),
    "audio_received": ("bytes", "duration_ms"),
    "turn_complete": (),
    "generation_complete": (),
    "interrupted": (),
    "tool_call": ("calls",),
    "tool_call_cancellation": ("ids", "pending"),
    "go_away": ("time_left",),
    "session_resumption": ("resumable", "has_handle"),
    "receive_error": ("error", "message"),
    "tool_start": ("id", "name", "arg_keys", "arg_values", "blocking", "limit_s"),
    "tool_end": ("id", "name", "outcome", "duration_ms", "error"),
    "tool_timeout": ("id", "name", "limit_s"),
    "tool_error": ("id", "name", "error"),
    "tool_invalid": ("id", "name", "problems"),
    "tool_cancelled": ("id", "name", "running"),
    "tool_late_result": ("id", "name", "duration_ms", "reason"),
    "tool_response_sent": ("ids", "count"),
}


def _clean(value):
    """JSON-safe copy with every byte payload removed (audio/images never reach a sink)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()
                if not isinstance(v, (bytes, bytearray, memoryview))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value if not isinstance(v, (bytes, bytearray, memoryview))]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "item") and getattr(value, "ndim", 1) == 0:  # numpy scalar
        return value.item()
    if hasattr(value, "tobytes"):  # numpy arrays: audio
        return None
    return str(value)


def _env_on(name, environ):
    return environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


class Trace:
    def __init__(self, include_text=False, sinks=(), clock=time.monotonic, wall=time.time, keep=KEEP):
        self.include_text = include_text
        self.clock, self.wall = clock, wall
        self.lock = threading.Lock()
        self.events = deque(maxlen=keep)
        self.sinks = list(sinks)
        self.session = 0
        self._seq = itertools.count(1)
        self._state = None

    @classmethod
    def from_env(cls, environ=os.environ, monitor=True):
        """The runtime's trace: memory always, monitor status, and a JSONL file only if EMBER_TRACE is set."""
        trace = cls(include_text=_env_on("EMBER_TRACE_TEXT", environ))
        if monitor:
            trace.add_sink(MonitorSink())
        path = environ.get("EMBER_TRACE", "").strip()
        if path:
            trace.add_sink(JsonlSink(path))
        trace.record("trace_start", schema=SCHEMA_VERSION, pid=os.getpid(), text_enabled=trace.include_text)
        return trace

    def add_sink(self, sink):
        with self.lock:
            self.sinks.append(sink)

        def remove():
            with self.lock:
                if sink in self.sinks:
                    self.sinks.remove(sink)
        return remove

    def record(self, kind, **fields):
        """Record one event; returns it. Never raises into the runtime."""
        if not self.include_text:
            for name in TEXT_FIELDS:
                fields.pop(name, None)
        event = {"seq": 0, "t": 0.0, "wall": 0.0, "kind": kind, "session": self.session}
        for name, value in fields.items():
            if isinstance(value, (bytes, bytearray, memoryview)) or getattr(value, "ndim", 0):
                continue  # Audio/image payloads are never recorded.
            event[name] = _clean(value)
        with self.lock:
            event["seq"] = next(self._seq)
            event["t"] = round(self.clock(), 6)
            event["wall"] = round(self.wall(), 6)
            self.events.append(event)
            sinks = list(self.sinks)
        for sink in sinks:
            try:
                sink(event)
            except Exception as error:
                with self.lock:
                    if sink in self.sinks:
                        self.sinks.remove(sink)
                print(f"[TRACE] Sink {type(sink).__name__} disabled after an error: {error}", flush=True)
        return event

    def state(self, state):
        """A state change; repeats of the current state are not recorded (the monitor re-sends some)."""
        if state == self._state:
            return None
        self._state = state
        return self.record("state", state=state)

    def kinds(self):
        return [event["kind"] for event in self.events]

    def close(self):
        for sink in list(self.sinks):
            close = getattr(sink, "close", None)
            if close is not None:
                close()


class JsonlSink:
    """Append each event as one JSON line. Local only; a write error disables the sink, never the runtime."""

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.lock = threading.Lock()
        self.file = open(self.path, "a", encoding="utf-8", buffering=1)
        print(f"[TRACE] Writing the event trace to {self.path} (local only).", flush=True)

    def __call__(self, event):
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            if self.file is not None:
                self.file.write(line + "\n")

    def close(self):
        with self.lock:
            if self.file is not None:
                self.file.close()
                self.file = None


class MonitorSink:
    """Keep runtime_monitor's status in step with the trace.

    Counts per kind go into the status file's `trace` section; tool problems
    (timeouts, rejected arguments, errors, cancellations, dropped late results)
    are added to its event list so devUI shows them next to the existing
    toolkit start/finish events. State and tool start/finish are still emitted
    by the runtime exactly as in v8, so nothing already shown changes.
    """
    PROBLEMS = {
        "tool_timeout": lambda e: f"{e.get('name')} timed out after {e.get('limit_s')} s",
        "tool_invalid": lambda e: f"{e.get('name')} rejected: invalid arguments ({'; '.join(e.get('problems') or [])})",
        "tool_error": lambda e: f"{e.get('name')} failed: {e.get('error')}",
        "tool_cancelled": lambda e: f"{e.get('name')} cancelled by Gemini",
        "tool_late_result": lambda e: f"{e.get('name')} result arrived late and was dropped",
    }

    def __init__(self, monitor=None):
        if monitor is None:
            import runtime_monitor as monitor
        self.monitor = monitor
        self.counts = {}

    def __call__(self, event):
        kind = event["kind"]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.monitor.trace_status = {"schema": SCHEMA_VERSION, "events": event["seq"], "session": event["session"],
                                     "last": kind, "counts": dict(self.counts)}
        message = self.PROBLEMS.get(kind)
        if message is not None:
            self.monitor.record("toolkit_issue", message(event))


class MemorySink(list):
    """A list sink for tests: `trace.add_sink(sink := MemorySink())`."""

    def __call__(self, event):
        self.append(event)


# ── The process-wide trace the runtime records into ────────────────────────
_default = None
_default_lock = threading.Lock()


def get():
    """The runtime's trace, built from the environment on first use."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Trace.from_env()
    return _default


def record(kind, **fields):
    return get().record(kind, **fields)


def state(value):
    return get().state(value)


def read(path):
    """The events in a JSONL trace file (for evals and tests)."""
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
