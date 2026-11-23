"""The harness tool runner (features/harness/tools.py): validation, time limits, cancellation, BLOCKING flags,
trace, and parity with v10's dispatcher (gemini_tools.handle_tool_call_v10)."""
import asyncio
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import types

import runtime_monitor as monitor
from features.conversational_ability import volume
from features.eyes import tools as eye_tools
from features.harness import toolkit
from features.harness.tools import BLOCKING, NON_BLOCKING, Tool, ToolRunner, validate
from features.harness.trace import Trace
from features.toolkits import audiobooks, experimental, gemini_tools, listen, look_closely, time_limits, web_search
from features.toolkits.everyday import tools as everyday
from features.toolkits.family import tools as family

WEATHER = {"name": "get_weather", "parameters": {"type": "OBJECT", "properties": {
    "units": {"type": "STRING", "enum": ["celsius", "fahrenheit"]}, "city": {"type": "STRING"}}}}
REMINDER = {"name": "reminder", "parameters": {"type": "OBJECT", "properties": {
    "action": {"type": "STRING", "enum": ["create", "list"]}, "seconds": {"type": "INTEGER"},
    "tags": {"type": "ARRAY", "items": {"type": "STRING"}}, "loud": {"type": "BOOLEAN"},
    "lat": {"type": "NUMBER"}}, "required": ["action"]}}


def call(name, args=None, call_id="c1"):
    return SimpleNamespace(id=call_id, name=name, args=args)


def batch(*calls):
    return SimpleNamespace(function_calls=list(calls))


class Session:
    def __init__(self):
        self.sent = []

    async def send_tool_response(self, function_responses):
        self.sent.append([(r.id, r.name, r.response, r.scheduling) for r in function_responses])


class Monitor:
    """Records the monitor calls the runner makes (emit / activity / record)."""

    def __init__(self):
        self.calls = []

    def emit(self, state, message):
        self.calls.append(("emit", state, message))

    def activity(self, name, running=True, background=False):
        self.calls.append(("activity", name, running))

    def record(self, state, message):
        self.calls.append(("record", state, message))


def runner(*tools, **kwargs):
    trace = kwargs.pop("trace", None) or Trace()
    return ToolRunner(tools, trace=trace, monitor=kwargs.pop("monitor", None) or Monitor(), types=types, **kwargs)


class ValidationTests(unittest.TestCase):
    def test_valid_arguments_pass(self):
        self.assertEqual(validate(WEATHER["parameters"], {}), [])
        self.assertEqual(validate(WEATHER["parameters"], {"units": "celsius", "city": "Paris"}), [])
        self.assertEqual(validate(REMINDER["parameters"], {"action": "create", "seconds": 60, "tags": ["a"],
                                                           "loud": True, "lat": 1}), [])
        self.assertEqual(validate(REMINDER["parameters"], {"action": "list", "seconds": 60.0, "lat": 2.5}), [])
        self.assertEqual(validate(WEATHER["parameters"], {"city": None}), [])  # Optional null passes, as v8.
        self.assertEqual(validate(None, {}), [])

    def test_problems_name_keys_and_types_never_values(self):
        problems = validate(REMINDER["parameters"], {"seconds": "secret-60", "tags": ["x", 7], "loud": 1,
                                                     "lat": True, "extra": "private-text"})
        self.assertIn("missing required 'action'", problems)
        self.assertIn("'seconds' should be an integer", problems)
        self.assertIn("'tags[1]' should be a string", problems)
        self.assertIn("'loud' should be a boolean", problems)
        self.assertIn("'lat' should be a number", problems)
        self.assertIn("unexpected argument 'extra'", problems)
        self.assertNotIn("secret", " ".join(problems))
        self.assertNotIn("private", " ".join(problems))
        self.assertEqual(validate(WEATHER["parameters"], {"units": "kelvin"}),
                         ["'units' must be one of: celsius, fahrenheit"])
        self.assertEqual(validate(REMINDER["parameters"], {"action": "create", "seconds": 1.5}),
                         ["'seconds' should be an integer"])
        self.assertEqual(validate(WEATHER["parameters"], ["not", "an", "object"]), ["arguments must be an object"])

    def test_invalid_call_is_not_run_and_gets_a_clear_error(self):
        ran = []
        trace = Trace()
        tools = runner(Tool("get_weather", WEATHER, lambda args: ran.append(args) or {"ok": 1}), trace=trace)
        session = Session()
        asyncio.run(tools.handle(session, batch(call("get_weather", {"units": "kelvin", "zip": "1"}))))
        self.assertEqual(ran, [])
        (reply,), = session.sent
        self.assertEqual(reply[2]["error"], "Invalid tool arguments")
        self.assertEqual(reply[2]["problems"], ["'units' must be one of: celsius, fahrenheit", "unexpected argument 'zip'"])
        kinds = trace.kinds()
        self.assertIn("tool_invalid", kinds)
        end = next(e for e in trace.events if e["kind"] == "tool_end")
        self.assertEqual(end["outcome"], "invalid")


class TimeLimitTests(unittest.TestCase):
    def test_limits_come_from_the_shared_time_limit_table(self):
        tool = Tool("get_weather", WEATHER, lambda args: {})
        self.assertEqual(tool.limit(), time_limits.limit_for("get_weather"))
        self.assertEqual(Tool("x", {}, lambda args: {}, limit_s=1.5).limit(), 1.5)
        limits = {t.name: t.limit() for t in toolkit.tools()}
        self.assertEqual(limits["audiobook"], 15)
        self.assertEqual(limits["public_trivia"], time_limits.PUBLIC_DEFAULT)

    def test_timeout_answers_once_and_drops_the_late_result(self):
        release, finished = threading.Event(), threading.Event()

        def slow(args):
            release.wait(2)
            finished.set()
            return {"late": True}
        trace = Trace()
        tools = runner(Tool("get_weather", WEATHER, slow, limit_s=0.05), trace=trace)
        session = Session()

        async def go():
            await tools.handle(session, batch(call("get_weather", {})))
            release.set()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "tool_late_result" in trace.kinds():
                    break
        asyncio.run(go())
        self.assertTrue(finished.is_set())
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.sent[0][0][2]["error"], "timed_out")
        late = next(e for e in trace.events if e["kind"] == "tool_late_result")
        self.assertEqual(late["reason"], "timed_out")
        outcomes = [e["outcome"] for e in trace.events if e["kind"] == "tool_end"]
        self.assertEqual(outcomes, ["timeout"])
        self.assertIn("tool_timeout", trace.kinds())

    def test_hung_tool_does_not_block_shutdown(self):
        start = time.monotonic()
        tools = runner(Tool("hung", {}, lambda args: time.sleep(5), limit_s=0.05))
        asyncio.run(tools.handle(Session(), batch(call("hung"))))
        self.assertLess(time.monotonic() - start, 2)

    def test_tool_exception_becomes_an_error_result(self):
        trace = Trace()
        tools = runner(Tool("boom", {}, lambda args: 1 / 0), trace=trace)
        session = Session()
        asyncio.run(tools.handle(session, batch(call("boom"))))
        self.assertEqual(session.sent[0][0][2], {"error": "Tool temporarily unavailable"})
        self.assertEqual(next(e for e in trace.events if e["kind"] == "tool_error")["error"], "ZeroDivisionError")


class CancellationTests(unittest.TestCase):
    def slow_tool(self, release, name="slow"):
        return Tool(name, {}, lambda args: (release.wait(2), {"done": name})[1], limit_s=5)

    def test_cancel_running_call_sends_nothing_and_drops_late_result(self):
        release = threading.Event()
        trace = Trace()
        tools = runner(self.slow_tool(release), trace=trace)
        session = Session()

        async def go():
            task = tools.dispatch(session, batch(call("slow", call_id="a")))
            await asyncio.sleep(0.05)
            self.assertEqual(tools.cancel(["a", "never-seen"]), ["a"])
            await task
            release.set()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "tool_late_result" in trace.kinds():
                    break
        asyncio.run(go())
        self.assertEqual(session.sent, [])
        cancelled = next(e for e in trace.events if e["kind"] == "tool_cancelled")
        self.assertTrue(cancelled["running"])
        self.assertEqual([e["outcome"] for e in trace.events if e["kind"] == "tool_end"], ["cancelled"])
        self.assertEqual(next(e for e in trace.events if e["kind"] == "tool_late_result")["reason"], "cancelled")
        self.assertEqual(tools.pending, {})

    def test_cancel_a_queued_call_in_a_batch_keeps_the_others(self):
        release = threading.Event()
        tools = runner(self.slow_tool(release, "first"), Tool("second", {}, lambda args: {"done": "second"}),
                       Tool("third", {}, lambda args: {"done": "third"}))
        session = Session()

        async def go():
            task = tools.dispatch(session, batch(call("first", call_id="1"), call("second", call_id="2"),
                                                 call("third", call_id="3")))
            await asyncio.sleep(0.05)
            tools.cancel(["2"])
            release.set()
            await task
        asyncio.run(go())
        self.assertEqual([(r[0], r[2]) for r in session.sent[0]], [("1", {"done": "first"}), ("3", {"done": "third"})])

    def test_session_stop_still_cancels_the_batch(self):
        release = threading.Event()
        tools = runner(self.slow_tool(release))

        async def go():
            task = asyncio.create_task(tools.handle(Session(), batch(call("slow"))))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(go())
        release.set()
        self.assertEqual(tools.pending, {})


class BehaviourTests(unittest.TestCase):
    def test_blocking_and_non_blocking_calls(self):
        quiet = Tool("eyes", {"name": "eyes"}, lambda args: {"ok": 1}, behavior=NON_BLOCKING, scheduling="SILENT")
        loud = Tool("get_weather", WEATHER, lambda args: {"ok": 2})
        mon, trace = Monitor(), Trace()
        tools = runner(quiet, loud, monitor=mon, trace=trace)
        session = Session()
        asyncio.run(tools.handle(session, batch(call("eyes", call_id="e"), call("get_weather", call_id="w"))))
        self.assertEqual(session.sent, [[("e", "eyes", {"ok": 1}, types.FunctionResponseScheduling.SILENT),
                                         ("w", "get_weather", {"ok": 2}, None)]])
        self.assertEqual(mon.calls, [("activity", "eyes", True), ("activity", "eyes", False),
                                     ("emit", "calling_tool", "Calling toolkit get_weather"),
                                     ("activity", "get_weather", True), ("activity", "get_weather", False),
                                     ("emit", "thinking", "Toolkit finished; waiting for Gemini reply")])
        self.assertEqual([e["blocking"] for e in trace.events if e["kind"] == "tool_start"], [False, True])
        self.assertEqual(tools.declarations(), [{"name": "eyes", "behavior": NON_BLOCKING}, WEATHER])
        self.assertEqual(tools.declarations({"get_weather"}), [WEATHER])

    def test_eye_tool_follows_its_silent_switch(self):
        eye = next(t for t in toolkit.tools() if t.name == eye_tools.NAME)
        with patch.dict(os.environ, {"BAYMAX_EYE_TOOL_SILENT": "1"}):
            self.assertEqual(eye.mode(), NON_BLOCKING)
        with patch.dict(os.environ, {"BAYMAX_EYE_TOOL_SILENT": "0"}):
            self.assertEqual(eye.mode(), BLOCKING)

    def test_duplicate_registration_is_refused(self):
        tools = runner(Tool("a", {}, lambda args: {}))
        with self.assertRaises(ValueError):
            tools.register(Tool("a", {}, lambda args: {}))

    def test_argument_values_only_traced_when_text_is_enabled(self):
        for include_text in (False, True):
            trace = Trace(include_text=include_text)
            tools = runner(Tool("get_weather", WEATHER, lambda args: {}), trace=trace)
            asyncio.run(tools.handle(Session(), batch(call("get_weather", {"city": "Private Town"}))))
            start = next(e for e in trace.events if e["kind"] == "tool_start")
            self.assertEqual(start["arg_keys"], ["city"])
            self.assertEqual("arg_values" in start, include_text)
            self.assertEqual("Private Town" in str(list(trace.events)), include_text)


class ToolkitParityTests(unittest.TestCase):
    """The runner with the built-in toolkit against v10's handle_tool_call_v10, valid calls."""

    def run_both(self, tool_call, env=None):
        def execute(name, args):
            return {"tool": name, "args": dict(args)}

        async def public(call):
            return {"public": call.name}

        async def look(session):
            return {"photo": "sent"}
        results = []
        for core in ("v10", "runner"):
            mon, session, attached = Monitor(), Session(), []
            with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_LOOK_CLOSELY": "1", **(env or {})}), \
                    patch.object(gemini_tools, "execute", execute), \
                    patch.object(experimental, "run_call", public), patch.object(look_closely, "run", look), \
                    patch.object(look_closely, "enabled", lambda: True), \
                    patch.object(audiobooks, "attach", lambda s, loop: attached.append(s)), \
                    patch.object(monitor, "emit", mon.emit), patch.object(monitor, "activity", mon.activity):
                if core == "v10":
                    asyncio.run(gemini_tools.handle_tool_call_v10(session, tool_call))
                else:
                    tools = ToolRunner(toolkit.tools(), trace=Trace(), fallback=gemini_tools.execute, types=types)
                    asyncio.run(tools.handle(session, tool_call))
            results.append((mon.calls, session.sent, len(attached)))
        self.assertEqual(results[0], results[1])
        return results[0]

    def test_mixed_batch_matches_v10(self):
        calls, sent, attached = self.run_both(batch(
            call("get_weather", {"units": "celsius"}, "1"), call("set_location", {"city": "Paris"}, "2"),
            call("no_such_tool", {}, "3"), call("public_trivia", None, "4"), call("local_clock", {}, "5"),
            call("audiobook", {"action": "pause"}, "6"), call(eye_tools.NAME, {"expression": "happy"}, "7"),
            call(listen.NAME, {"action": "status"}, "8")))
        self.assertEqual(attached, 2)  # audiobook and listen attach the player
        self.assertEqual(len(sent[0]), 8)
        self.assertIn(("emit", "calling_tool", "Calling toolkit unknown_tool"), calls)
        self.assertEqual(sent[0][6][3], types.FunctionResponseScheduling.SILENT)

    def test_v10_only_tools_match(self):
        _, sent, _ = self.run_both(batch(
            call("look_closely", {}, "1"), call(volume.NAME, {"change": "louder"}, "2"),
            call(web_search.NAME, {"query": "weather records"}, "3"),
            call(family.DECLARATIONS[0]["name"], {"message_id": "m1", "text": "Love you too"}, "4")))
        self.assertEqual(sent[0][0][2], {"photo": "sent"})
        self.assertEqual(sent[0][2][2], {"tool": web_search.NAME, "args": {"query": "weather records"}})

    def test_look_closely_disabled_matches_v10(self):
        _, sent, _ = self.run_both(batch(call("look_closely", {}, "1")), env={"BAYMAX_TOOLKIT_ENABLED": "0"})
        self.assertEqual(sent[0][0][2], {"error": "Toolkit is disabled"})

    def test_eye_tool_blocking_when_not_silent_matches_v10(self):
        calls, sent, _ = self.run_both(batch(call(eye_tools.NAME, {"expression": "sad"})),
                                       env={"BAYMAX_EYE_TOOL_SILENT": "0"})
        self.assertIsNone(sent[0][0][3])
        self.assertIn(("emit", "thinking", "Toolkit finished; waiting for Gemini reply"), calls)

    def test_more_than_eight_calls_matches_v10(self):
        _, sent, _ = self.run_both(batch(*[call("local_clock", {}, str(i)) for i in range(9)]))
        self.assertEqual(sent[0][-1][2], {"error": "At most eight toolkit calls per batch"})

    def test_registry_names_match_v10s_declared_names(self):
        names = {t.name for t in toolkit.tools()}
        declared = {d["name"] for d in gemini_tools.DECLARATIONS + everyday.DECLARATIONS + experimental.DECLARATIONS
                    + family.DECLARATIONS} | {eye_tools.NAME, look_closely.DECLARATION["name"], listen.NAME,
                                              volume.NAME, web_search.NAME}
        self.assertEqual(names, declared)
        for tool in toolkit.tools():
            self.assertIsNone(tool.declaration.get("behavior"))  # Behaviour comes from the runner, not shared dicts.

    def test_declared_schemas_accept_the_toolkits_own_examples(self):
        tools = {t.name: t for t in toolkit.tools()}
        examples = {"get_weather": {"units": "fahrenheit", "city": "Austin", "region": "Texas"},
                    "set_location": {"city": "Springfield", "region": "Illinois", "country_code": "US"},
                    "reminder": {"action": "create", "text": "Tea", "seconds": 300},
                    "household_list": {"action": "add", "name": "shopping", "text": "milk"},
                    "public_weather": {"latitude": 1.5, "longitude": 2, "units": "celsius"},
                    volume.NAME: {"level": 8},
                    listen.NAME: {"action": "play_radio", "query": "jazz", "country_code": "US"},
                    family.DECLARATIONS[0]["name"]: {"message_id": "m1", "text": "Thanks"},
                    web_search.NAME: {"query": "who won the match"},
                    eye_tools.NAME: {"expression": "happy"}}
        for name, args in examples.items():
            self.assertEqual(validate(tools[name].declaration.get("parameters"), args), [], name)

    def test_search_limit_sits_just_above_its_own_budget(self):
        self.assertGreater({t.name: t for t in toolkit.tools()}[web_search.NAME].limit(), web_search.TOTAL_S)


class IntendedDifferenceTests(unittest.TestCase):
    """Where the runner differs from v10 on purpose: invalid arguments are not run."""

    def test_invalid_arguments_never_reach_the_tool_or_attach_the_player(self):
        ran, attached = [], []
        session = Session()
        with patch.object(gemini_tools, "execute", lambda name, args: ran.append(name) or {}), \
                patch.object(audiobooks, "attach", lambda s, loop: attached.append(s)), \
                patch.object(monitor, "emit"), patch.object(monitor, "activity"):
            tools = ToolRunner(toolkit.tools(), trace=Trace(), fallback=gemini_tools.execute, types=types)
            asyncio.run(tools.handle(session, batch(call(listen.NAME, {}, "1"),
                                                    call("get_weather", {"units": "kelvin"}, "2"))))
        self.assertEqual((ran, attached), ([], []))
        self.assertEqual([r[2]["problems"] for r in session.sent[0]],
                         [["missing required 'action'"], ["'units' must be one of: celsius, fahrenheit"]])


class SwitchTests(unittest.TestCase):
    """gemini_tools.handle_tool_call: the runner by default, v10's dispatcher with EMBER_TOOL_RUNNER=0."""

    def test_default_uses_the_runner_and_the_switch_restores_v10(self):
        tool_call = batch(call("get_weather", {"units": "kelvin"}))
        for value, error in (("1", "Invalid tool arguments"), ("0", "from v10")):
            session = Session()
            with patch.dict(os.environ, {"EMBER_TOOL_RUNNER": value}), \
                    patch.object(gemini_tools, "execute", lambda name, args: {"error": "from v10"}), \
                    patch.object(monitor, "emit"), patch.object(monitor, "activity"):
                asyncio.run(gemini_tools.handle_tool_call(session, tool_call))
            self.assertEqual(session.sent[0][0][2]["error"], error)

    def test_cancel_with_nothing_pending(self):
        self.assertEqual(gemini_tools.cancel_tool_calls(["x"]), [])
        self.assertEqual(gemini_tools.public_name("get_weather"), "get_weather")
        self.assertEqual(gemini_tools.public_name("private thing"), "unknown_tool")


class AsyncToolTests(unittest.TestCase):
    def test_async_tool_timeout_and_cancel(self):
        async def slow(session, call):
            await asyncio.sleep(2)
            return {"late": True}
        trace = Trace()
        tools = runner(Tool("slow", {}, run=slow, limit_s=0.05), trace=trace)
        session = Session()
        asyncio.run(tools.handle(session, batch(call("slow", call_id="a"))))
        self.assertEqual(session.sent[0][0][2]["error"], "timed_out")

        session = Session()

        async def go():
            task = tools.dispatch(session, batch(call("slow", call_id="b")))
            await asyncio.sleep(0.02)
            tools.cancel(["b"])
            await task
        tools.tools["slow"].limit_s = 5
        asyncio.run(go())
        self.assertEqual(session.sent, [])
        self.assertEqual([e["outcome"] for e in trace.events if e["kind"] == "tool_end"], ["timeout", "cancelled"])


if __name__ == "__main__":
    unittest.main()
