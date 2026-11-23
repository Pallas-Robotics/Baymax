"""The harness event trace (features/harness/trace.py): schema, sinks, privacy defaults, the monitor
feed, and the trace v10's real receive and connection loops produce for a scripted fake Gemini session."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from google.genai import errors

import runtime_monitor as monitor
from features.conversational_ability import live_models
from features.conversational_ability.addressing import AddressGate
from features.harness import trace as harness_trace
from features.harness.trace import KINDS, SCHEMA_VERSION, JsonlSink, MemorySink, MonitorSink, Trace
from features.toolkits import gemini_tools
from test_public_core_integration import load_function
import test_v10_runtime

ROOT = Path(__file__).resolve().parents[1]
COMMON = ("seq", "t", "wall", "kind", "session")
NOISE = ("mic_chunk_sent", "mic_chunk_dropped", "mic_send_failed", "state", "video_frame_sent", "trace_start")


class SchemaTests(unittest.TestCase):
    def test_every_event_has_the_common_fields_in_order(self):
        clock = iter([5.0, 5.5, 7.25])
        trace = Trace(clock=lambda: next(clock), wall=lambda: 1700000000.0)
        trace.session = 2
        trace.record("connect_start", attempt=1, model="m")
        trace.record("audio_received", bytes=960, duration_ms=20.0)
        trace.record("turn_complete")
        events = list(trace.events)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual([e["t"] for e in events], [5.0, 5.5, 7.25])
        for event in events:
            self.assertEqual(tuple(event)[:5], COMMON)
            self.assertEqual(event["wall"], 1700000000.0)
            self.assertEqual(event["session"], 2)
            self.assertIn(event["kind"], KINDS)
            self.assertTrue(set(event) - set(COMMON) <= set(KINDS[event["kind"]]), event)
        self.assertEqual(trace.kinds(), ["connect_start", "audio_received", "turn_complete"])

    def test_readme_documents_every_kind_and_the_version(self):
        readme = (ROOT / "features" / "harness" / "README.md").read_text(encoding="utf-8")
        for kind, fields in KINDS.items():
            self.assertIn(f"`{kind}`", readme)
            for field in fields:
                self.assertIn(field, readme, (kind, field))
        self.assertIn(f'"schema":{SCHEMA_VERSION}', readme.replace(" ", ""))

    def test_state_repeats_are_collapsed(self):
        trace = Trace()
        for state in ("listening", "reply_playback_done", "reply_playback_done", "user_speech", "user_speech",
                      "thinking"):
            trace.state(state)
        self.assertEqual([e["state"] for e in trace.events], ["listening", "reply_playback_done", "user_speech",
                                                              "thinking"])


class PrivacyTests(unittest.TestCase):
    def test_no_text_or_argument_values_by_default(self):
        trace = Trace()
        trace.record("input_transcription", chars=8, text="my secret")
        trace.record("tool_start", id="1", name="set_location", arg_keys=["city"], arg_values={"city": "Hometown"})
        self.assertNotIn("text", trace.events[0])
        self.assertEqual(trace.events[0]["chars"], 8)
        self.assertNotIn("arg_values", trace.events[1])
        self.assertNotIn("secret", json.dumps(list(trace.events)))
        self.assertNotIn("Hometown", json.dumps(list(trace.events)))

    def test_text_only_when_enabled(self):
        for value, included in (("", False), ("0", False), ("1", True), ("true", True)):
            env = {"EMBER_TRACE_TEXT": value} if value else {}
            trace = Trace.from_env(environ=env, monitor=False)
            trace.record("output_transcription", chars=5, text="Hello")
            self.assertEqual(trace.events[-1].get("text") == "Hello", included, value)
            self.assertEqual(trace.events[0]["text_enabled"], included)  # trace_start says whether text is on.

    def test_audio_and_image_bytes_are_never_recorded(self):
        trace = Trace(include_text=True)
        event = trace.record("audio_received", bytes=4, duration_ms=1.0, data=b"\x01\x02",
                             samples=np.arange(4, dtype=np.int16), nested={"audio": b"\x00", "n": 1},
                             parts=[b"\x00", "ok"])
        self.assertNotIn("data", event)
        self.assertNotIn("samples", event)
        self.assertEqual(event["nested"], {"n": 1})
        self.assertEqual(event["parts"], ["ok"])
        json.dumps(event)

    def test_trace_files_are_gitignored(self):
        if not (ROOT.parent / ".git").exists():
            self.skipTest("not a git checkout")
        for path in ("Baymax-main/session.trace.jsonl", "Baymax-main/traces/today.jsonl"):
            result = subprocess.run(["git", "-C", str(ROOT.parent), "check-ignore", "-q", path])
            self.assertEqual(result.returncode, 0, path)


class SinkTests(unittest.TestCase):
    def test_jsonl_sink_only_when_ember_trace_is_set(self):
        trace = Trace.from_env(environ={}, monitor=False)
        self.assertFalse(any(isinstance(s, JsonlSink) for s in trace.sinks))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "run.trace.jsonl")
            trace = Trace.from_env(environ={"EMBER_TRACE": path}, monitor=False)
            trace.record("connect_start", attempt=1, model="m")
            trace.close()
            events = harness_trace.read(path)
        self.assertEqual([e["kind"] for e in events], ["trace_start", "connect_start"])
        self.assertEqual(events[0]["schema"], SCHEMA_VERSION)
        self.assertEqual(events[0]["pid"], os.getpid())

    def test_a_failing_sink_is_dropped_and_never_raises(self):
        trace = Trace()
        good = MemorySink()

        def bad(event):
            raise OSError("disk full")
        trace.add_sink(bad)
        trace.add_sink(good)
        trace.record("turn_complete")
        trace.record("interrupted")
        self.assertEqual([e["kind"] for e in good], ["turn_complete", "interrupted"])
        self.assertNotIn(bad, trace.sinks)

    def test_remove_sink(self):
        trace, sink = Trace(), MemorySink()
        remove = trace.add_sink(sink)
        trace.record("turn_complete")
        remove()
        trace.record("turn_complete")
        self.assertEqual(len(sink), 1)

    def test_monitor_sink_feeds_status_and_tool_problems(self):
        records = []
        fake = SimpleNamespace(trace_status=None, record=lambda state, message: records.append((state, message)))
        trace = Trace(sinks=[MonitorSink(fake)])
        trace.record("tool_start", id="1", name="get_weather", arg_keys=[], blocking=True, limit_s=8)
        trace.record("tool_timeout", id="1", name="get_weather", limit_s=8)
        trace.record("tool_invalid", id="2", name="reminder", problems=["missing required 'action'"])
        self.assertEqual(fake.trace_status["events"], 3)
        self.assertEqual(fake.trace_status["counts"], {"tool_start": 1, "tool_timeout": 1, "tool_invalid": 1})
        self.assertEqual(records, [("toolkit_issue", "get_weather timed out after 8 s"),
                                   ("toolkit_issue", "reminder rejected: invalid arguments (missing required 'action')")])

    def test_monitor_status_file_carries_the_trace_section(self):
        with patch.object(monitor, "trace_status", None):
            self.assertIsNone(monitor.snapshot()["trace"])
            Trace(sinks=[MonitorSink()]).record("turn_complete")
            self.assertEqual(monitor.snapshot()["trace"]["counts"], {"turn_complete": 1})


class DefaultTraceTests(unittest.TestCase):
    def test_runtime_trace_is_built_once_from_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rt.trace.jsonl")
            with patch.object(harness_trace, "_default", None), patch.dict(os.environ, {"EMBER_TRACE": path}):
                first = harness_trace.get()
                harness_trace.record("turn_complete")
                self.assertIs(harness_trace.get(), first)
                first.close()
            self.assertEqual([e["kind"] for e in harness_trace.read(path)], ["trace_start", "turn_complete"])

    def test_monitor_states_reach_the_trace(self):
        trace = Trace()
        with patch.object(harness_trace, "_default", trace):
            monitor.notify("thinking")
            monitor.notify("thinking")
            monitor.notify("speaking")
        self.assertEqual([e["state"] for e in trace.events], ["thinking", "speaking"])


# ── v10's real loops on a scripted fake Gemini ──────────────────────────────
def content(heard=None, said=None, audio=None, text=None, complete=False, interrupted=False, generated=False):
    parts = []
    if text:
        parts.append(SimpleNamespace(text=text, inline_data=None))
    if audio is not None:
        parts.append(SimpleNamespace(text=None, inline_data=SimpleNamespace(mime_type="audio/pcm;rate=24000",
                                                                           data=audio)))
    return message(server_content=SimpleNamespace(
        input_transcription=SimpleNamespace(text=heard) if heard is not None else None,
        output_transcription=SimpleNamespace(text=said) if said is not None else None,
        model_turn=SimpleNamespace(parts=parts) if parts else None,
        turn_complete=complete, interrupted=interrupted, generation_complete=generated))


def message(server_content=None, tool_call=None, session_resumption_update=None, go_away=None,
            tool_call_cancellation=None):
    return SimpleNamespace(server_content=server_content, tool_call=tool_call,
                           session_resumption_update=session_resumption_update, go_away=go_away,
                           tool_call_cancellation=tool_call_cancellation)


class ScriptedSession:
    """Yields the scripted messages, then ends the receive loop; records tool responses."""

    def __init__(self, *script):
        self.script, self.responses = list(script), []

    async def send_tool_response(self, function_responses):
        self.responses.append([(r.id, r.name, r.response) for r in function_responses])

    def receive(self):
        async def stream():
            for item in self.script:
                yield item
            raise asyncio.CancelledError()
        return stream()


def receive_namespace():
    """The names v10's receive_audio reads, with the real address gate and the real toolkit dispatcher."""
    ns = {
        "asyncio": asyncio, "time": time, "np": np,
        "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(), finish_transcripts=Mock(), timing=Mock()),
        "_address_gate": AddressGate(enabled=False, log=Mock()), "public_toolkit": gemini_tools,
        "_transcript_add": Mock(), "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
        "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
        "_recent_user_words": [], "MEMORY_WORD_WINDOW": 30, "_memory_embedder": None,
        "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
        "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_flush_playback": Mock(),
        "_append_playback": lambda data, **kw: None, "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
        "tracker_receive": SimpleNamespace(record=Mock()), "tracker_first_audio": SimpleNamespace(record=Mock()),
        "tracker_roundtrip": SimpleNamespace(record=Mock()), "_last_mic_send_ts": 0.0,
        "_last_robot_speech_ts": 0.0, "_last_user_speech_ts": 0.0, "_user_turn": None, "_user_turn_words": [],
        "_recalled": None, "_reply_owed_since": None, "_tool_calls_running": 0, "_go_away_at": None,
        "_turn_done_at": 0.0, "RECEIVE_FAILURES_BEFORE_RECONNECT": 3,
        "connection_lost": lambda error: True,
    }
    load_function("receive_audio", ns)
    return ns


class ScriptedSessionTraceTests(unittest.TestCase):
    def receive(self, session, env=None):
        trace, executed = Trace(), []
        ns = receive_namespace()
        with patch.object(harness_trace, "_default", trace), patch.object(gemini_tools, "_runner", None), \
                patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "1", **(env or {})}), \
                patch.object(gemini_tools, "execute",
                             lambda name, args: executed.append((name, args)) or {"temperature": 21}), \
                patch.object(monitor, "emit"), patch.object(monitor, "activity"), \
                patch("features.conversational_ability.session.on_server_message"), patch("builtins.print"):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(ns["receive_audio"](session))
        return trace, executed, ns

    def test_a_turn_with_a_tool_call_produces_the_expected_trace(self):
        reply_audio = np.full(4800, 300, dtype=np.int16).tobytes()  # 200 ms at 24 kHz
        call = SimpleNamespace(id="w1", name="get_weather", args={"units": "celsius"})
        session = ScriptedSession(
            message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="handle-1")),
            content(heard="What's the weather?"),
            message(tool_call=SimpleNamespace(function_calls=[call])),
            content(said="It's sunny.", audio=reply_audio, text="(thinking)"),
            content(generated=True),
            content(complete=True),
            message(tool_call_cancellation=SimpleNamespace(ids=["w1"])),
            content(said="Something else", interrupted=True),
            message(go_away=SimpleNamespace(time_left="50s")))
        trace, executed, ns = self.receive(session)
        self.assertEqual(executed, [("get_weather", {"units": "celsius"})])
        self.assertEqual(session.responses, [[("w1", "get_weather", {"temperature": 21})]])
        events = [e for e in trace.events if e["kind"] not in NOISE]
        self.assertEqual([e["kind"] for e in events], [
            "session_resumption", "input_transcription", "tool_call", "tool_start", "tool_end",
            "tool_response_sent", "output_transcription", "model_text", "audio_received", "generation_complete",
            "turn_complete", "tool_call_cancellation", "interrupted", "go_away"])
        by_kind = {e["kind"]: e for e in events}
        self.assertEqual((by_kind["session_resumption"]["resumable"], by_kind["session_resumption"]["has_handle"]),
                         (True, True))
        self.assertEqual(by_kind["tool_call"]["calls"], [{"id": "w1", "name": "get_weather"}])
        self.assertEqual((by_kind["tool_start"]["arg_keys"], by_kind["tool_start"]["blocking"],
                          by_kind["tool_start"]["limit_s"]), (["units"], True, 8.0))
        self.assertEqual(by_kind["tool_end"]["outcome"], "ok")
        self.assertEqual((by_kind["audio_received"]["bytes"], by_kind["audio_received"]["duration_ms"]),
                         (len(reply_audio), 200.0))
        self.assertEqual(by_kind["tool_call_cancellation"]["pending"], [])  # inline: the reply had gone
        self.assertEqual(by_kind["go_away"]["time_left"], "50s")
        dumped = json.dumps(list(trace.events))
        for private in ("handle-1", "weather?", "sunny", "celsius"):
            self.assertNotIn(private, dumped)
        self.assertEqual(ns["_tool_calls_running"], 0)

    def test_text_is_traced_only_when_enabled(self):
        session = ScriptedSession(content(heard="Hello Ember"), content(said="Hi there", complete=True))
        with patch.dict(os.environ, {"EMBER_TRACE_TEXT": "1"}):
            trace = Trace.from_env(monitor=False)
        ns = receive_namespace()
        with patch.object(harness_trace, "_default", trace), \
                patch("features.conversational_ability.session.on_server_message"), patch("builtins.print"):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(ns["receive_audio"](session))
        texts = [e.get("text") for e in trace.events if e["kind"].endswith("_transcription")]
        self.assertEqual(texts, ["Hello Ember", "Hi there"])

    def test_an_invalid_tool_call_is_answered_without_running(self):
        call = SimpleNamespace(id="r1", name="get_weather", args={"units": "kelvin"})
        session = ScriptedSession(message(tool_call=SimpleNamespace(function_calls=[call])))
        trace, executed, _ = self.receive(session)
        self.assertEqual(executed, [])
        self.assertEqual(session.responses[0][0][2]["error"], "Invalid tool arguments")
        self.assertIn("tool_invalid", trace.kinds())

    def test_the_mic_sender_traces_sizes_and_drops(self):
        trace = Trace()
        sent = []

        async def send(**kwargs):
            sent.append(kwargs)
            if len(sent) == 2:
                raise asyncio.CancelledError()
        helper = test_v10_runtime.ReconnectTests()
        queue = helper.mic(b"a" * 2048, b"b" * 2048)
        with patch.object(harness_trace, "_default", trace):
            sender = helper.sender(queue)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sender(SimpleNamespace(send_realtime_input=send)))
        chunks = [e for e in trace.events if e["kind"] == "mic_chunk_sent"]
        self.assertEqual([e["bytes"] for e in chunks], [2048])
        self.assertTrue(all(set(e) == set(COMMON) | {"bytes", "queue_ms", "send_ms"} for e in chunks))


class ConnectionTraceTests(unittest.TestCase):
    """v10's real run() (via test_v10_runtime's harness): a drop and a reconnect."""
    run_connections = test_v10_runtime.ReconnectTests.run_connections

    def test_a_lost_connection_is_traced_and_reconnects(self):
        async def receive(index, session, namespace):
            raise errors.APIError(1011, {"message": "internal error"})
        trace = Trace()
        with patch.object(harness_trace, "_default", trace):
            self.run_connections(1, receive)
        events = [e for e in trace.events if e["kind"] not in NOISE]
        self.assertEqual([e["kind"] for e in events],
                         ["connect_start", "connected", "session_closed", "connection_failed", "connect_start"])
        self.assertEqual([e["session"] for e in events], [1, 1, 1, 1, 2])
        self.assertEqual(events[0]["model"], "m")
        self.assertEqual(events[3]["error"], "ExceptionGroup")


if __name__ == "__main__":
    unittest.main()
