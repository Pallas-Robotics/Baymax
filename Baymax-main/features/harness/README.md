# Agent harness

The harness turns `realtime_gemini_10.py`'s orchestration loop into parts that can be tested and controlled separately. This first increment adds two of them:
- **Event trace** (`trace.py`): records every runtime event with timestamps.
- **Tool runner** (`tools.py`, with the built-in tools in `toolkit.py`): owns every Gemini function call.

The other parts come later. The seams for them are noted below.

## Target architecture
| Part | Job | Status |
|---|---|---|
| Model adapter | Gemini Live behind an interface | planned (`live_models.py` picks models today) |
| I/O ports | mic, camera and speaker swappable: real, virtual, file | planned |
| Turn manager | end of turn, barge-in, drop stale replies | planned (`addressing.py` holds replies today) |
| **Tool runner** | registry, validation, limits, blocking, cancellation | **this increment** |
| Context builder | system instruction, memories, notes | planned (`_build_config`, `between_turns`) |
| Safety layer | code-gated actions | planned |
| Memory writer | summaries and embeddings | planned (`_memory_saver_thread`) |
| Supervisor | reconnect, resume, "I'm ready" | planned (`run()`, `session.py`) |
| **Event trace** | every runtime event, timestamped | **this increment** |

## Inputs
- **`trace.record(kind, **fields)` calls from `realtime_gemini_10.py`:**
  - `send_audio_realtime` and `send_video_realtime`;
  - `receive_audio`;
  - `run()`, for connect, close and fail.
- **`runtime_monitor.notify`:** state changes.
- **The tool runner:** every tool call.
- **Gemini messages:**
  - `tool_call`, through `gemini_tools.handle_tool_call`;
  - `tool_call_cancellation`, through `gemini_tools.cancel_tool_calls`.
- **Settings:**
  - `EMBER_TRACE=path`: also write the trace as JSONL to `path`. Off by default.
  - `EMBER_TRACE_TEXT=1`: also record transcript text and tool argument values. Off by default.
  - `EMBER_TOOL_RUNNER=0`: use v10's own dispatcher (`gemini_tools.handle_tool_call_v10`) instead of the runner.

## Outputs
### Trace sinks
| Sink | When | What |
|---|---|---|
| Memory (`trace.get().events`) | always | The last 2000 events, in process (tests, in-process evals) |
| JSONL file | `EMBER_TRACE` set | One JSON object per line; appended; its directories are created |
| Monitor | always | Per-kind counts go to the `trace` section of `.runtime-status.json`. Tool problems are added to the status `events` list as `toolkit_issue`: timeout, invalid arguments, error, cancelled, late result dropped. Existing state and toolkit start/finish events are emitted exactly as before. |

You can add a sink with `trace.get().add_sink(callable)`. A sink that raises is removed and logged once. It never interrupts the conversation.

### Tool runner
- **Gemini:** one `send_tool_response` per batch, as v10.
- **Monitor:**
  - `calling_tool` and `thinking` states for blocking calls;
  - `activity` start/finish for every call.

## Privacy
- **Never recorded:** audio and image bytes. `Trace.record` drops byte values and numpy arrays itself, so a caller cannot leak them by mistake. Only sizes and durations are kept.
- **Off by default:**
  - transcript text (`text`);
  - tool argument values (`arg_values`). Only argument names (`arg_keys`) are recorded.

  Set `EMBER_TRACE_TEXT=1` to include them. Tool results are never recorded, only an `error` string when the result has one.
- **Never recorded:** resumption handles. Only `has_handle` is kept.
- **Names:** tool names are the declared ones. Anything else is `unknown_tool`, as the monitor shows it.
- **Local only:** nothing uploads a trace. `*.trace.jsonl` and `traces/` are gitignored. Keep traces out of PRs, issues and chats, like recordings.

## Schema (version 1)
Every event has these fields, in this order:

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | 1, 2, 3, … per process |
| `t` | float | `time.monotonic()` seconds: use for ordering and durations |
| `wall` | float | Unix time seconds: use to line up with logs |
| `kind` | str | one of the kinds below |
| `session` | int | Gemini connection number in this process (0 before the first connect) |

The kinds and their fields:

| Kind | Fields | Notes |
|---|---|---|
| `trace_start` | `schema`, `pid`, `text_enabled` | First event; says whether text is recorded |
| `connect_start` | `attempt`, `model` | Before each Gemini connect |
| `connected` | `attempt` | Session open |
| `session_closed` | `attempt` | Session tasks ended (drop or stop) |
| `connection_failed` | `attempt`, `error`, `message` | Exception type and first 200 characters; a reconnect follows |
| `state` | `state` | Monitor state as the eyes see it (`listening`, `user_speech`, `thinking`, `calling_tool`, `speaking`, `reply_complete`, `reply_playback_done`, `reconnecting`, …); repeats collapsed |
| `mic_chunk_sent` | `bytes`, `queue_ms`, `send_ms` | One per mic chunk sent to Gemini |
| `mic_chunk_dropped` | `bytes`, `queue_ms`, `reason` | `stale` (queue too old) or `playback` (half-duplex while Ember speaks) |
| `mic_send_failed` | `bytes`, `error` | |
| `video_frame_sent` | `bytes`, `encode_ms`, `send_ms` | The 320×240 JPEG sent every 3 s (look_closely's photo is not traced here) |
| `video_frame_failed` | `error` | |
| `input_transcription` | `chars`, `text`* | What Gemini heard (one fragment) |
| `output_transcription` | `chars`, `text`* | What Ember said (one fragment) |
| `model_text` | `chars`, `text`* | Text parts of a model turn |
| `audio_received` | `bytes`, `duration_ms` | Reply audio chunk (24 kHz before upsampling) |
| `generation_complete` | | |
| `turn_complete` | | |
| `interrupted` | | Barge-in; playback flushed |
| `tool_call` | `calls` | `[{"id", "name"}]`; undeclared names are `unknown_tool` |
| `tool_call_cancellation` | `ids`, `pending` | `pending`: the ids that were still running or queued |
| `go_away` | `time_left` | |
| `session_resumption` | `resumable`, `has_handle` | |
| `receive_error` | `error`, `message` | |
| `tool_start` | `id`, `name`, `arg_keys`, `arg_values`*, `blocking`, `limit_s` | |
| `tool_end` | `id`, `name`, `outcome`, `duration_ms`, `error` | `outcome`: `ok`, `invalid`, `timeout`, `error`, `cancelled`, `rejected` (over 8 per batch) |
| `tool_timeout` | `id`, `name`, `limit_s` | |
| `tool_error` | `id`, `name`, `error` | Exception type |
| `tool_invalid` | `id`, `name`, `problems` | Key/type problems; no values |
| `tool_cancelled` | `id`, `name`, `running` | |
| `tool_late_result` | `id`, `name`, `duration_ms`, `reason` | A result after a timeout or cancel; dropped, never sent |
| `tool_response_sent` | `ids`, `count` | One per batch reply |

\* Only with `EMBER_TRACE_TEXT=1`.

`features.harness.trace.KINDS` is the same table in code, and `tests/test_harness_trace.py` checks that this README lists every kind and field. New kinds and new fields are compatible changes. Renaming or removing one needs `SCHEMA_VERSION` bumped.

### Example
This excerpt is from a weather question with `EMBER_TRACE=traces/today.trace.jsonl`. Mic chunks are abbreviated.

```json
{"seq":1,"t":5021.3,"wall":1763900000.10,"kind":"trace_start","session":0,"schema":1,"pid":4242,"text_enabled":false}
{"seq":2,"t":5021.9,"wall":1763900000.70,"kind":"connect_start","session":1,"attempt":1,"model":"gemini-…"}
{"seq":4,"t":5022.4,"wall":1763900001.20,"kind":"connected","session":1,"attempt":1}
{"seq":5,"t":5022.4,"wall":1763900001.20,"kind":"state","session":1,"state":"listening"}
{"seq":6,"t":5022.42,"wall":1763900001.22,"kind":"mic_chunk_sent","session":1,"bytes":2048,"queue_ms":0.4,"send_ms":0.2}
{"seq":310,"t":5029.1,"wall":1763900007.90,"kind":"input_transcription","session":1,"chars":19}
{"seq":311,"t":5029.1,"wall":1763900007.90,"kind":"state","session":1,"state":"user_speech"}
{"seq":330,"t":5029.8,"wall":1763900008.60,"kind":"tool_call","session":1,"calls":[{"id":"w1","name":"get_weather"}]}
{"seq":332,"t":5029.8,"wall":1763900008.60,"kind":"tool_start","session":1,"id":"w1","name":"get_weather","arg_keys":["units"],"blocking":true,"limit_s":8.0}
{"seq":333,"t":5030.3,"wall":1763900009.10,"kind":"tool_end","session":1,"id":"w1","name":"get_weather","outcome":"ok","duration_ms":512.7,"error":null}
{"seq":335,"t":5030.3,"wall":1763900009.10,"kind":"tool_response_sent","session":1,"ids":["w1"],"count":1}
{"seq":352,"t":5031.0,"wall":1763900009.80,"kind":"audio_received","session":1,"bytes":9600,"duration_ms":200.0}
{"seq":401,"t":5033.2,"wall":1763900012.00,"kind":"turn_complete","session":1}
```

You can read a trace with `features.harness.trace.read(path)`, which returns a list of dicts.

## Tool runner
`features/toolkits/gemini_tools.handle_tool_call` now runs each batch through `ToolRunner.handle()`. The receive loop's call and its `_tool_calls_running` / `_reply_owed_since` bookkeeping are unchanged. v10's dispatcher is kept as `handle_tool_call_v10`: it is the rollback (`EMBER_TOOL_RUNNER=0`) and the reference for the parity tests.

### The same as v10
- the order of monitor calls;
- one reply per batch;
- the limit of 8 calls per batch;
- `unknown_tool` naming;
- the audiobook/listen `attach`;
- experimental tools through `experimental.run_call`;
- `look_closely.run(session)` on the event loop, and its "Toolkit is disabled" answer;
- the silent eye command (NON_BLOCKING, SILENT result, no state). `BAYMAX_EYE_TOOL_SILENT=0` makes it blocking.

### Intended changes
1. **Argument validation** against the declared schema:
   - It checks required keys, unexpected keys, types, enum values and array items. An explicit null on an optional argument is allowed, as v10 passed it on.
   - A bad call is not run, and its `before` hook does not run either. Gemini gets `{"error": "Invalid tool arguments", "problems": [...], "message": ...}`.
   - Problems name keys and types, never values.
2. **Per-tool time limits** from `features/toolkits/time_limits.py`:
   - The limits are local 5 s, location/weather 8 s, search-style 12 s, audiobook/listen 15 s, `search_web` just above its own 13 s budget, otherwise 10 s.
   - A blocking tool runs on a daemon thread, so a hung service cannot block shutdown. An async tool runs as a task.
   - A timed-out call answers `timed_out`, and its late result is dropped.
   - v10 had no limit, except 20 s inside `experimental.run_call`. That function now uses `time_limits.run` with the same limit as the runner, so the rollback path (`EMBER_TOOL_RUNNER=0`) keeps the public tools' limit.
3. **A tool exception becomes `{"error": "Tool temporarily unavailable"}`** instead of escaping into the receive loop. `execute` already catches everything, so this only matters for hooks and async tools.

### Cancellation
`cancel(ids)` marks calls as cancelled:
- it stops waiting for them;
- it sends no response for them;
- it drops their late results;
- it skips queued calls in the same batch.

v10 runs a batch inline: the receive loop waits for it. So the loop reads a cancellation only after the reply has gone, and it is traced with `pending: []`. `ToolRunner.dispatch()` runs a batch as a background task so receiving continues. The turn manager increment will switch to it.

### Registering a tool
A toolkit declares its tool for Gemini, as today, in `gemini_tools.extend_config`. It also adds one `Tool` to `features/harness/toolkit.py`, which the runner registers:

```python
from features.harness.tools import BLOCKING, NON_BLOCKING, Tool
from features.toolkits import brain_games  # your toolkit module

Tool(
    name="brain_game",
    declaration=brain_games.DECLARATION,          # the same dict Gemini gets; args are validated against it
    execute=lambda args: brain_games.execute(args),   # blocking; runs on a daemon thread
    # run=async_fn,                               # or: async run(session, call) on the event loop
    limit_s=10.0,                                 # None: time_limits.limit_for(name)
    behavior=BLOCKING,                            # NON_BLOCKING: no "thinking" state, reply needs no follow-up
    scheduling="SILENT",                          # NON_BLOCKING replies only: SILENT, WHEN_IDLE or INTERRUPT
    before=lambda session, loop: brain_games.attach(session, loop),   # optional, runs just before execute
)
```

Each field:
- **`execute`:** called with the validated arguments. Return a JSON-able dict, using `{"error": ...}` for failures.
- **`behavior`:** may be a zero-argument callable, so it can follow an env switch (see the eye tool). For a NON_BLOCKING tool, also add `"behavior": "NON_BLOCKING"` to the declaration in `extend_config`. `ToolRunner.declarations()` produces it.
- **`limit_s`:** tune it there, or add the name to `time_limits.LIMITS`.

A tool never opens a microphone or camera stream.

## Enabling and disabling
- **Trace memory and monitor sinks:** always on. They are cheap: one dict per event and no I/O.
- **Trace file:** `EMBER_TRACE=path`.
- **Text and argument values:** `EMBER_TRACE_TEXT=1`.
- **Tool runner:** on by default. `EMBER_TOOL_RUNNER=0` restores v10's dispatcher, which still has no validation or limits.
- `BAYMAX_TOOLKIT_ENABLED` and each toolkit's own switch work as before.

## Dependencies and data flows
- **Dependencies:** standard library only, plus `google-genai`'s `FunctionResponse` types, which are already installed.
- **Network:** none.
- **Disk:** only the trace file you name.
- Each tool's own data flows are unchanged; see its README.

## Tests
- **`tests/test_harness_trace.py` (runtime suite):**
  - the schema and README coverage;
  - the sinks;
  - privacy: no text, argument values, bytes or handles by default; gitignore;
  - the monitor feed;
  - v10's real `receive_audio` on a scripted fake Gemini session, producing the expected trace sequence;
  - the mic sender;
  - v10's real `run()` dropping and reconnecting.
- **`tests/test_toolkit_runner.py` (information suite):**
  - validation;
  - time limits for blocking and async tools, timeouts and late-result drop;
  - hung-tool shutdown;
  - errors;
  - cancellation: running, queued, async, and a session stop;
  - BLOCKING/NON_BLOCKING and the eye switch;
  - argument-value privacy;
  - parity with `handle_tool_call_v10`: mixed batches, the v10-only tools (look_closely, volume, search_web, family), the disabled toolkit, a non-silent eye tool and more than 8 calls;
  - the intended differences;
  - the `EMBER_TOOL_RUNNER` switch.
- **`tests/test_toolkit_time_limits.py` (information suite):**
  - the per-tool values;
  - every registered tool's limit staying within v10's `REPLY_OWED_MAX_S`, read from `realtime_gemini_10.py`;
  - timeouts through the runner and `experimental.run_call`.
