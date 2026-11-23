"""Run Gemini function calls: registry, argument checks, time limits, cancellation, trace.

Today's v10 path (features/toolkits/gemini_tools.handle_tool_call_v10)
dispatches each call inline in the receive loop, with no time limit (20 s for
experimental tools before this change) and passes whatever arguments Gemini sent to the tool. The
runner keeps v10's order of monitor events and its one reply per batch, and adds:

  * argument validation against the declared schema: a bad call is not run;
    Gemini gets {"error": "Invalid tool arguments", "problems": [...]} so it
    can correct itself or ask (problems name keys and types, never values);
  * per-tool time limits (features/toolkits/time_limits.py): the call runs on
    a daemon thread (or, for an async tool, as a task); on timeout Gemini gets
    the timed_out result and a late result is dropped;
  * cancellation: `cancel(ids)` for Gemini's tool_call_cancellation marks the
    call cancelled, stops waiting for it, sends no response for it and drops
    its late result;
  * BLOCKING / NON_BLOCKING per tool: a blocking call shows "calling_tool" /
    "thinking" and its result makes Gemini reply; a non-blocking call (the
    silent eye command) shows no state and its result carries a scheduling
    (SILENT by default), exactly as v10's "quiet" eye path;
  * a trace event for every step (tool_start / tool_end / tool_timeout /
    tool_invalid / tool_error / tool_cancelled / tool_late_result /
    tool_response_sent) with argument keys only; values only with
    EMBER_TRACE_TEXT=1.

`handle()` runs a batch inline (v10: the receive loop waits for it).
`dispatch()` runs it as a background task so the receive loop keeps reading,
which is what lets a cancellation arrive while a call is running; v10 uses
`handle()` until the turn manager lands.
"""
import asyncio
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Optional

from features.toolkits import time_limits

BLOCKING, NON_BLOCKING = "BLOCKING", "NON_BLOCKING"
MAX_CALLS_PER_BATCH = 8
BATCH_LIMIT_ERROR = {"error": "At most eight toolkit calls per batch"}
UNAVAILABLE = {"error": "Tool temporarily unavailable"}
UNKNOWN = {"error": "Unknown tool"}


@dataclass
class Tool:
    """One callable capability.

    execute(args) -> dict   blocking; runs on a daemon thread
    run(session, call)      instead of execute: an async tool, awaited on the event loop (call.args)
    behavior                BLOCKING or NON_BLOCKING, or a zero-argument callable returning one
    scheduling              reply scheduling for NON_BLOCKING results: SILENT, WHEN_IDLE or INTERRUPT
    limit_s                 time limit; None uses time_limits.limit_for(name)
    before(session, loop)   runs on the event loop just before execute (e.g. the audiobook's attach)
    """
    name: str
    declaration: dict
    execute: Optional[Callable[[dict], Any]] = None
    run: Optional[Callable] = None
    behavior: Any = BLOCKING
    scheduling: Optional[str] = "SILENT"
    limit_s: Optional[float] = None
    before: Optional[Callable] = None

    def mode(self):
        value = self.behavior() if callable(self.behavior) else self.behavior
        return NON_BLOCKING if value == NON_BLOCKING else BLOCKING

    def limit(self):
        return self.limit_s if self.limit_s is not None else time_limits.limit_for(self.name)


@dataclass
class Call:
    id: Any
    name: str
    cancelled: bool = False
    running: bool = False
    future: Optional[asyncio.Future] = None
    started: Optional[float] = None
    tasks: list = field(default_factory=list)


# ── Validation ───────────────────────────────────────────────────────────────
def validate(parameters, args):
    """Problems with `args` against a declared Gemini parameter schema ([] when valid).

    Messages name keys and expected types only, never the values sent. An
    explicit null for an optional argument is let through (v8 passed it on).
    """
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    parameters = parameters or {}
    properties = parameters.get("properties") or {}
    problems = [f"missing required '{key}'" for key in parameters.get("required") or []
                if args.get(key) is None]
    for key, value in args.items():
        if key not in properties:
            problems.append(f"unexpected argument '{key}'")
        elif value is not None:
            problems += _check(properties[key], value, key)
    return problems


def _check(schema, value, path):
    kind = str(schema.get("type", "")).upper()
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    ok = {"STRING": isinstance(value, str),
          "INTEGER": is_number and float(value).is_integer(),
          "NUMBER": is_number,
          "BOOLEAN": isinstance(value, bool),
          "ARRAY": isinstance(value, list),
          "OBJECT": isinstance(value, dict)}.get(kind, True)
    if not ok:
        return [f"'{path}' should be {'an' if kind[:1] in 'AEIOU' else 'a'} {kind.lower()}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"'{path}' must be one of: {', '.join(map(str, schema['enum']))}"]
    if kind == "ARRAY" and isinstance(schema.get("items"), dict):
        return [problem for index, item in enumerate(value)
                for problem in _check(schema["items"], item, f"{path}[{index}]")]
    if kind == "OBJECT" and "properties" in schema:
        return [f"'{path}': {problem}" for problem in validate(schema, value)]
    return []


def invalid(problems):
    return {"error": "Invalid tool arguments", "problems": problems,
            "message": "Nothing was done. Correct the arguments and call again, or ask the person."}


# ── Runner ───────────────────────────────────────────────────────────────────
class ToolRunner:
    def __init__(self, tools=(), trace=None, monitor=None, types=None, fallback=None, clock=time.monotonic,
                 max_calls=MAX_CALLS_PER_BATCH):
        if monitor is None:
            import runtime_monitor as monitor
        if trace is None:
            from features.harness import trace as harness_trace
            trace = harness_trace.get()
        self.trace, self.monitor, self._types = trace, monitor, types
        self.fallback = fallback  # execute(name, args) for undeclared names; v10 answers "Unknown tool".
        self.clock, self.max_calls = clock, max_calls
        self.tools = {}
        self.pending = {}
        self.tasks = set()
        for tool in tools:
            self.register(tool)

    @property
    def types(self):
        if self._types is None:
            from google.genai import types
            self._types = types
        return self._types

    # ── Registry ─────────────────────────────────────────────────────────────
    def register(self, tool):
        if tool.name in self.tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self.tools[tool.name] = tool
        return tool

    def declarations(self, names=None):
        """Declarations for `names` (default: all), with behavior set for non-blocking tools."""
        result = []
        for tool in self.tools.values():
            if names is not None and tool.name not in names:
                continue
            declaration = dict(tool.declaration)
            if tool.mode() == NON_BLOCKING:
                declaration["behavior"] = NON_BLOCKING
            result.append(declaration)
        return result

    def public_name(self, name):
        """v8 logs declared names only; anything else is 'unknown_tool'."""
        return name if name in self.tools else "unknown_tool"

    # ── Entry points ─────────────────────────────────────────────────────────
    async def handle(self, session, tool_call):
        """Run one tool_call batch and send its replies in one tool response (v8)."""
        calls = list(getattr(tool_call, "function_calls", None) or [])
        records = []
        for call in calls:
            record = Call(call.id, self.public_name(call.name))
            self.pending[call.id] = record
            records.append(record)
        replies = []
        try:
            for index, (call, record) in enumerate(zip(calls, records)):
                reply = await self._one(session, call, record, index)
                if reply is not None:
                    replies.append(reply)
        finally:
            for record in records:
                if self.pending.get(record.id) is record:
                    del self.pending[record.id]
        if replies:
            await session.send_tool_response(function_responses=replies)
            self.trace.record("tool_response_sent", ids=[reply.id for reply in replies], count=len(replies))
        return replies

    def dispatch(self, session, tool_call):
        """Run the batch in the background; the caller keeps receiving (so cancellations can arrive)."""
        task = asyncio.get_running_loop().create_task(self.handle(session, tool_call))
        self.tasks.add(task)

        def done(finished):
            self.tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                print(f"[TOOLS] Tool batch failed: {finished.exception()}", flush=True)
        task.add_done_callback(done)
        return task

    def cancel(self, ids):
        """Gemini cancelled these call ids: no response is sent for them and late results are dropped."""
        found = []
        for call_id in ids or []:
            record = self.pending.get(call_id)
            if record is None or record.cancelled:
                continue
            record.cancelled = True
            found.append(call_id)
            self.trace.record("tool_cancelled", id=call_id, name=record.name, running=record.running)
            if record.future is not None and not record.future.done():
                record.future.cancel()
        return found

    # ── One call ─────────────────────────────────────────────────────────────
    async def _one(self, session, call, record, index):
        tool = self.tools.get(call.name)
        name = record.name
        args = call.args or {}
        quiet = tool is not None and tool.mode() == NON_BLOCKING
        if record.cancelled:
            self.trace.record("tool_end", id=call.id, name=name, outcome="cancelled", duration_ms=0.0)
            return None
        monitor = self.monitor
        # v8 order: state, activity on, run, activity off, then "thinking" for blocking calls.
        if not quiet:
            monitor.emit('calling_tool', 'Calling toolkit ' + name)
        monitor.activity(name)
        limit = tool.limit() if tool is not None else time_limits.limit_for(name)
        arg_keys = sorted(args) if isinstance(args, dict) else []
        self.trace.record("tool_start", id=call.id, name=name, arg_keys=arg_keys, arg_values=args,
                          blocking=not quiet, limit_s=limit)
        started = self.clock()
        error = None
        try:
            problems = validate(tool.declaration.get("parameters"), args) if tool is not None else []
            if index >= self.max_calls:
                outcome, result = "rejected", dict(BATCH_LIMIT_ERROR)
            elif problems:
                outcome, result = "invalid", invalid(problems)
                self.trace.record("tool_invalid", id=call.id, name=name, problems=problems)
            else:
                if tool is not None and tool.before is not None:
                    tool.before(session, asyncio.get_running_loop())
                if tool is not None and tool.run is not None:
                    outcome, result, error = await self._limited_async(record, tool.run(session, call), limit)
                else:
                    func = tool.execute if tool is not None else self._unknown(call.name)
                    outcome, result, error = await self._limited(record, func, args, limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # A tool must never take the receive loop down.
            outcome, result, error = "error", dict(UNAVAILABLE), type(exc).__name__
            self.trace.record("tool_error", id=call.id, name=name, error=error)
        finally:
            monitor.activity(name, running=False)
        self.trace.record("tool_end", id=call.id, name=name, outcome=outcome,
                          duration_ms=round((self.clock() - started) * 1000, 3),
                          error=error or (result.get("error") if isinstance(result, dict) else None))
        if outcome == "cancelled" or record.cancelled:
            return None
        types = self.types
        if quiet:
            scheduling = getattr(types.FunctionResponseScheduling, tool.scheduling or "SILENT")
            return types.FunctionResponse(id=call.id, name=call.name, response=result, scheduling=scheduling)
        monitor.emit('thinking', 'Toolkit finished; waiting for Gemini reply')
        return types.FunctionResponse(id=call.id, name=call.name, response=result)

    def _unknown(self, name):
        fallback = self.fallback
        return (lambda args: fallback(name, args)) if fallback is not None else (lambda args: dict(UNKNOWN))

    async def _limited_async(self, record, coroutine, limit):
        """(outcome, result, error) of an async tool, within `limit` seconds; cancellable."""
        task = asyncio.ensure_future(coroutine)
        record.future, record.running, record.started = task, True, self.clock()
        try:
            return "ok", await asyncio.wait_for(asyncio.shield(task), timeout=limit), None
        except asyncio.TimeoutError:
            task.cancel()
            self.trace.record("tool_timeout", id=record.id, name=record.name, limit_s=limit)
            time_limits.log.warning("Toolkit call %s timed out after %.0f s", record.name, limit)
            return "timeout", time_limits.timed_out(record.name), None
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if record.cancelled and not (current is not None and current.cancelling()):
                return "cancelled", None, None
            task.cancel()
            raise
        except Exception as exc:
            self.trace.record("tool_error", id=record.id, name=record.name, error=type(exc).__name__)
            return "error", dict(UNAVAILABLE), type(exc).__name__
        finally:
            record.running = False

    async def _limited(self, record, func, args, limit):
        """(outcome, result, error) of func(args) on a daemon thread, within `limit` seconds."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        record.future, record.running, record.started = future, True, self.clock()
        trace = self.trace

        def settle(ok, value):
            if future.done():  # Timed out or cancelled: the late result is dropped, never sent.
                trace.record("tool_late_result", id=record.id, name=record.name,
                             duration_ms=round((self.clock() - record.started) * 1000, 3),
                             reason="cancelled" if record.cancelled else "timed_out")
                return
            if ok:
                future.set_result(value)
            else:
                future.set_exception(value)

        def work():
            try:
                outcome = (True, func(args))
            except Exception as exc:
                outcome = (False, exc)
            try:
                loop.call_soon_threadsafe(settle, *outcome)
            except RuntimeError:
                pass  # Loop closed (shutdown): nothing to deliver.

        threading.Thread(target=work, name=f"tool-{record.name}", daemon=True).start()
        try:
            return "ok", await asyncio.wait_for(future, timeout=limit), None
        except asyncio.TimeoutError:
            trace.record("tool_timeout", id=record.id, name=record.name, limit_s=limit)
            time_limits.log.warning("Toolkit call %s timed out after %.0f s", record.name, limit)
            return "timeout", time_limits.timed_out(record.name), None
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if record.cancelled and not (task is not None and task.cancelling()):
                return "cancelled", None, None
            raise  # The session itself is stopping.
        except Exception as exc:
            trace.record("tool_error", id=record.id, name=record.name, error=type(exc).__name__)
            return "error", dict(UNAVAILABLE), type(exc).__name__
        finally:
            record.running = False
