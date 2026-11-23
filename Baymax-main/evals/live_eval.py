"""Gemini-in-the-loop checks: Ember's real MVP session, synthetic speech and images, no robot.

Builds the exact Gemini Live config the MVP connects with (the `_build_config`
of the runtime run_pc.py launches, realtime_gemini_10.py on the Orange Pi line,
plus the run_mvp.py / run_pc.py environment defaults and device.toml), then plays
scripted turns into a fresh session: text-to-speech audio at the robot's 48 kHz,
generated picture cards as camera frames, or text notes. It records what Gemini
heard, said and which tools it called, and checks each turn against the
scenario's expectations, several runs per scenario.

No microphone, camera or speaker is opened. Tool calls get the scenario's stub
results unless --real-tools is given. Nothing is sent until you confirm the data
listed by --dry-run (or pass --yes). Reports go to .local-review/ (gitignored).

    python evals/live_eval.py --dry-run
    python evals/live_eval.py --only greeting --repeat 3
    python evals/live_eval.py --root ../../Baymax-brain-games/Baymax-main   # another PR's checkout
"""
import argparse
import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LAUNCHERS = (ROOT / "run_pc.py", ROOT / "run_mvp.py")
FALLBACK_CORE = "realtime_gemini_8.py"   # Older MVP checkouts: run_pc.py runs v8 and names no RUNTIME.
SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"
REPORT_DIR = ROOT / ".local-review"
RATE = 48000                     # SEND_SAMPLE_RATE (v8 and v10)
CHUNK = 1024                     # CHUNK_SIZE (v8 and v10)
TRAILING_SILENCE_S = 1.5         # Lets Gemini's activity detection end the turn.
# A real mic always carries room tone. Gemini's LOW start sensitivity (the MVP's)
# ignored clean TTS framed by digital silence, but hears it over quiet noise.
ROOM_NOISE = 200                 # Std dev of the background noise, int16 units (about -44 dBFS).
LEAD_IN_S = 2.0
TURN_TIMEOUT_S = 25.0
STEP_KINDS = ("say", "text", "card", "image")
EXPECT_KEYS = {"heard", "reply", "not_reply", "tools", "not_tools", "no_tools", "max_words", "max_latency_s",
               "silence_ok"}
DEFAULT_TOOL_RESULT = {"result": "ok"}


# ── The MVP's real session config ────────────────────────────────────────────
# What the session config is built from. v10 extends the prompt with device.toml
# switches ([WHEN TO SPEAK], [MEMORY CONTEXT]) in top-level `if` blocks, turns on
# proactive audio with ONLY_WHEN_ADDRESSED, and names its toolkit public_toolkit.
CONFIG_NAMES = {"MODEL", "_BASE_SYSTEM_INSTRUCTION", "_BASE_CONFIG", "ONLY_WHEN_ADDRESSED",
                "MEMORY_RETRIEVAL_ENABLED"}
CONFIG_IMPORTS = {"audio_config", "persona", "device_config", "public_toolkit"}


def runtime(root=ROOT):
    """The runtime run_pc.py launches (`RUNTIME = ROOT / "realtime_gemini_10.py"`), else v8."""
    try:
        tree = ast.parse((Path(root) / "run_pc.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return Path(root) / FALLBACK_CORE
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RUNTIME" for t in node.targets)
                and isinstance(node.value, ast.BinOp) and isinstance(node.value.right, ast.Constant)):
            return Path(root) / node.value.right.value
    return Path(root) / FALLBACK_CORE


CORE = runtime()


def launcher_defaults(paths=LAUNCHERS):
    """os.environ.setdefault(...) / os.environ[...] = ... constants the MVP launchers apply."""
    defaults = {}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault" and ast.unparse(node.func.value) == "os.environ"
                    and len(node.args) == 2 and all(isinstance(a, ast.Constant) for a in node.args)):
                defaults.setdefault(node.args[0].value, node.args[1].value)
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)
                    and ast.unparse(node.targets[0].value) == "os.environ"
                    and isinstance(node.targets[0].slice, ast.Constant) and isinstance(node.value, ast.Constant)):
                defaults[node.targets[0].slice.value] = node.value.value
    return defaults


def _assigned(node):
    """Names a top-level assignment (=, +=, annotated) binds."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    else:
        return set()
    return {t.id for t in targets if isinstance(t, ast.Name)}


def _imported(node):
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return set()
    return {(a.asname or a.name).split(".")[0] for a in node.names}


def core_session(core=CORE):
    """(model, config) from the runtime's own source, without importing its devices."""
    from google.genai import types
    tree = ast.parse(core.read_text(encoding="utf-8"))
    body = []
    for node in tree.body:
        if _assigned(node) & CONFIG_NAMES or _imported(node) & CONFIG_IMPORTS:
            body.append(node)
        elif (isinstance(node, ast.If) and not node.orelse
              and all(_assigned(n) and _assigned(n) <= CONFIG_NAMES for n in node.body)):
            body.append(node)   # v10: `if ONLY_WHEN_ADDRESSED: _BASE_SYSTEM_INSTRUCTION += ...`
        elif isinstance(node, ast.FunctionDef) and node.name == "_build_config":
            body.append(node)
    namespace = {"types": types, "os": os, "_memory_embedder": None}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(core), "exec"), namespace)
    return namespace["MODEL"], namespace["_build_config"]()


def mvp_session(environ=os.environ, root=ROOT):
    """Apply the launcher defaults the way the MVP does, then build its config."""
    for name, value in launcher_defaults([root / path.name for path in LAUNCHERS]).items():
        if name != "BAYMAX_HARDWARE_PROFILE":  # Device selection only; nothing is opened here.
            environ.setdefault(name, value)
    return core_session(runtime(root))


def declared_tools(config):
    return {d["name"] for tool in config.get("tools", []) for d in tool.get("function_declarations", [])}


# ── Scenarios ────────────────────────────────────────────────────────────────
def load_scenarios(path=SCENARIOS):
    scenarios = json.loads(Path(path).read_text(encoding="utf-8"))
    for scenario in scenarios:
        validate(scenario)
    return scenarios


def validate(scenario):
    name = scenario.get("name")
    if not name or not scenario.get("steps"):
        raise ValueError(f"Scenario needs a name and steps: {scenario}")
    for step in scenario["steps"]:
        kinds = [k for k in STEP_KINDS if k in step]
        if len(kinds) != 1:
            raise ValueError(f"{name}: each step needs exactly one of {STEP_KINDS}")
        unknown = set(step.get("expect", {})) - EXPECT_KEYS
        if unknown:
            raise ValueError(f"{name}: unknown expectations {sorted(unknown)}")
        for key in ("heard", "reply", "not_reply"):
            if key in step.get("expect", {}):
                re.compile(step["expect"][key])


def missing_requirements(scenario, config):
    """Why the scenario does not apply to this branch (a feature it tests is absent), else None."""
    needs = scenario.get("requires", {})
    absent = [t for t in needs.get("tools", []) if t not in declared_tools(config)]
    if absent:
        return "tool not on this branch: " + ", ".join(absent)
    prompt = config.get("system_instruction", "")
    absent = [p for p in needs.get("prompt", []) if p not in prompt]
    if absent:
        return "prompt section not on this branch: " + ", ".join(absent)
    return None


def check(expect, turn):
    """Failed expectations for one recorded turn, as short sentences."""
    failures = []
    heard, reply = turn["heard"], turn["reply"]
    called = [call["name"] for call in turn["tools"]]
    if "heard" in expect and not re.search(expect["heard"], heard, re.IGNORECASE):
        failures.append(f"Gemini heard {heard!r}, expected /{expect['heard']}/")
    if "reply" in expect and not re.search(expect["reply"], reply, re.IGNORECASE):
        failures.append(f"reply {reply!r} does not match /{expect['reply']}/")
    if "not_reply" in expect and re.search(expect["not_reply"], reply, re.IGNORECASE):
        failures.append(f"reply {reply!r} matches forbidden /{expect['not_reply']}/")
    for name in expect.get("tools", []):
        if name not in called:
            failures.append(f"{name} not called (called: {called or 'none'})")
    for name in expect.get("not_tools", []):
        if name in called:
            failures.append(f"{name} called but should not be")
    if expect.get("no_tools") and called:
        failures.append(f"expected no tool calls, got {called}")
    if "max_words" in expect and len(reply.split()) > expect["max_words"]:
        failures.append(f"reply has {len(reply.split())} words, max {expect['max_words']}")
    if "max_latency_s" in expect:
        latency = turn["latency_s"]
        if latency is None or latency > expect["max_latency_s"]:
            failures.append(f"first audio after {latency}s, max {expect['max_latency_s']}s")
    if not reply and not turn["audio_bytes"] and not expect.get("silence_ok"):
        failures.append("no reply before timeout" if turn["timed_out"] else "empty reply")
    return failures


# ── Synthetic inputs ─────────────────────────────────────────────────────────
def synthesize(text, voice=None):
    """16-bit mono PCM at 48 kHz of `text` from the OS speech synthesizer."""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "speech.wav"
        if shutil.which("say") and shutil.which("afconvert"):  # macOS
            source = Path(folder) / "speech.aiff"
            subprocess.run(["say", "-o", str(source)] + (["-v", voice] if voice else []) + [text],
                           check=True, timeout=60)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(source), str(out)],
                           check=True, timeout=60)
        elif shutil.which("espeak-ng"):
            subprocess.run(["espeak-ng", "-w", str(out)] + (["-v", voice] if voice else []) + [text],
                           check=True, timeout=60)
        else:
            raise RuntimeError("No speech synthesizer: use macOS or install espeak-ng.")
        return read_wav(out)


def read_wav(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
        if audio.getnchannels() > 1:
            samples = samples.reshape(-1, audio.getnchannels()).mean(axis=1).astype(np.int16)
        return resample(samples, audio.getframerate())


def resample(samples, rate):
    if rate == RATE or len(samples) == 0:
        return samples.astype(np.int16)
    count = int(round(len(samples) * RATE / rate))
    positions = np.linspace(0, len(samples) - 1, count)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.int16)


def room_tone(count, rng=np.random.default_rng(0)):
    return (rng.standard_normal(count) * ROOM_NOISE).astype(np.int16)


def card(lines, size=(640, 480)):
    """JPEG of black text on a white card: a stand-in label, letter or sign."""
    import cv2
    width, height = size
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    scale = 1.1 if max(len(line) for line in lines) <= 22 else 0.8
    step = int(48 * scale)
    top = max(step, (height - step * len(lines)) // 2 + step // 2)
    for index, line in enumerate(lines):
        (text_width, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.putText(image, line, (max(10, (width - text_width) // 2), top + index * step),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 2, cv2.LINE_AA)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("Could not encode the card")
    return data.tobytes()


def image_file(path):
    path = Path(path) if Path(path).is_absolute() else Path(__file__).resolve().parent / path
    return path.read_bytes()


# ── One session ──────────────────────────────────────────────────────────────
class Recorder:
    """Collects one turn's server messages: transcripts, audio, tool calls, completion."""

    def __init__(self, capture_audio=False):
        self.turn = None
        self.capture_audio = capture_audio
        self.changed = asyncio.Event()
        self.speech_ended = None
        self.closed = False

    def start(self):
        self.audio_pcm = bytearray()
        self.audio_rate = 24000
        self.turn = {"heard": "", "reply": "", "tools": [], "audio_bytes": 0, "latency_s": None,
                     "complete": False, "timed_out": False}
        self.speech_ended = None
        self.pending_tools = 0

    def finish_speaking(self):
        self.speech_ended = time.perf_counter()


async def receive(session, recorder, tool_results, real_tools):
    try:
        await _receive(session, recorder, tool_results, real_tools)
    finally:
        recorder.closed = True  # Wakes wait_for_turn at once when the connection drops.
        recorder.changed.set()


async def _receive(session, recorder, tool_results, real_tools):
    from google.genai import types
    while True:
        async for response in session.receive():
            turn = recorder.turn
            if turn is None:
                continue
            if response.tool_call:
                replies = []
                for call in response.tool_call.function_calls or []:
                    args = dict(call.args or {})
                    turn["tools"].append({"name": call.name, "args": args})
                    result = await tool_result(call.name, args, tool_results, real_tools)
                    replies.append(types.FunctionResponse(id=call.id, name=call.name, response=result))
                turn["complete"] = False
                await session.send_tool_response(function_responses=replies)
            content = response.server_content
            if content is not None:
                if content.input_transcription and content.input_transcription.text:
                    turn["heard"] += content.input_transcription.text
                if content.output_transcription and content.output_transcription.text:
                    turn["reply"] += content.output_transcription.text
                for part in (content.model_turn.parts or []) if content.model_turn else []:
                    if part.inline_data and (part.inline_data.mime_type or "").startswith("audio/"):
                        if turn["latency_s"] is None and recorder.speech_ended is not None:
                            turn["latency_s"] = round(time.perf_counter() - recorder.speech_ended, 2)
                        turn["audio_bytes"] += len(part.inline_data.data or b"")
                        if recorder.capture_audio:
                            mime = part.inline_data.mime_type or ''
                            if not mime.startswith('audio/pcm'):
                                raise ValueError('Reply review recording requires PCM output')
                            rate = re.search(r'rate=(\d+)', mime)
                            rate = int(rate[1]) if rate else 24000
                            if recorder.audio_pcm and rate != recorder.audio_rate:
                                raise ValueError('Reply audio rate changed during a turn')
                            recorder.audio_rate = rate
                            recorder.audio_pcm.extend(part.inline_data.data or b'')
                            if len(recorder.audio_pcm) > 24000 * 2 * 120:
                                raise ValueError('Reply review recording exceeded two-minute limit')
                if content.turn_complete and (turn["audio_bytes"] or turn["reply"]):
                    turn["complete"] = True
            recorder.changed.set()


async def tool_result(name, args, stubs, real_tools):
    if real_tools:
        from features.toolkits import gemini_tools
        return await asyncio.to_thread(gemini_tools.execute, name, args)
    return copy.deepcopy(stubs.get(name, DEFAULT_TOOL_RESULT))


async def send_step(session, step, recorder, speak=synthesize):
    from google.genai import types
    if "say" in step:
        speech = speak(step["say"], step.get("voice"))
        lead, tail = room_tone(int(RATE * LEAD_IN_S)), room_tone(int(RATE * TRAILING_SILENCE_S))
        speech = np.clip(speech.astype(np.int32) + room_tone(len(speech)), -32768, 32767).astype(np.int16)
        mime = f"audio/pcm;rate={RATE}"
        for index, block in enumerate((lead, speech, tail)):
            for start in range(0, len(block), CHUNK):
                chunk = block[start:start + CHUNK]
                await session.send_realtime_input(audio=types.Blob(data=chunk.tobytes(), mime_type=mime))
                await asyncio.sleep(len(chunk) / RATE)  # Real time, as from the robot's mic.
            if index == 1:
                recorder.finish_speaking()
    elif "text" in step:
        recorder.finish_speaking()
        await session.send_client_content(turns={"role": "user", "parts": [{"text": step["text"]}]},
                                          turn_complete=True)
    else:
        jpeg = card(step["card"]) if "card" in step else image_file(step["image"])
        await session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))


async def wait_for_turn(recorder, timeout=TURN_TIMEOUT_S, settle=1.0):
    """Until the reply has completed and nothing more arrived for `settle` seconds, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not recorder.closed:
        recorder.changed.clear()
        try:
            await asyncio.wait_for(recorder.changed.wait(), timeout=settle)
        except asyncio.TimeoutError:
            if recorder.turn["complete"]:
                return
    if not recorder.closed:
        recorder.turn["timed_out"] = True


async def run_scenario(connect, model, config, scenario, real_tools=False, speak=synthesize,
                       turn_timeout=TURN_TIMEOUT_S, audio_dir=None):
    """Fresh session for one run. Returns its turns with failures per turn."""
    recorder = Recorder(capture_audio=audio_dir is not None)
    turns = []
    async with connect(model=model, config=config) as session:
        receiver = asyncio.create_task(receive(session, recorder, scenario.get("tool_results", {}), real_tools))
        try:
            for step in scenario["steps"]:
                if "card" in step or "image" in step:
                    await send_step(session, step, recorder, speak)
                    continue
                recorder.start()
                await send_step(session, step, recorder, speak)
                await wait_for_turn(recorder, turn_timeout)
                if receiver.done():
                    receiver.result()  # Raise the connection error.
                turn = dict(recorder.turn, step=step.get("say") or step.get("text"))
                turn["failures"] = check(step.get("expect", {}), turn)
                if audio_dir is not None and recorder.audio_pcm:
                    path = Path(audio_dir) / f'turn-{len(turns) + 1}.wav'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with wave.open(str(path), 'wb') as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(recorder.audio_rate)
                        output.writeframes(recorder.audio_pcm)
                    turn['audio_file'] = str(path.resolve())
                turns.append(turn)
                recorder.turn = None
        except Exception as error:
            # Where it dropped: which line, and what had arrived (tool calls, reply so far).
            turn = recorder.turn or {}
            error.during = {"step": len(turns) + 1, "said": (step.get("say") or step.get("text") or "").strip(),
                            "tools": [c["name"] for c in turn.get("tools", [])], "heard": turn.get("heard", ""),
                            "reply_started": bool(turn.get("reply") or turn.get("audio_bytes"))}
            raise
        finally:
            receiver.cancel()
            try:
                await receiver
            except (asyncio.CancelledError, Exception):
                pass
    return turns


# ── Report ───────────────────────────────────────────────────────────────────
def summarize(results):
    lines = ["# Ember live evaluation (Gemini in the loop, no robot)", "",
             f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", "",
             "Synthetic speech and pictures only. This is Gemini-behaviour evidence; the robot's "
             "microphone, echo cancellation, speaker, camera and eyes are NOT exercised.", "",
             "| Scenario | Passed | Median first audio | Notes |", "|---|---|---|---|"]
    detail = []
    for name, outcome in results.items():
        if "skipped" in outcome:
            lines.append(f"| {name} | skipped | | {outcome['skipped']} |")
            continue
        runs = outcome["runs"]
        passed = sum(1 for run in runs if run.get("turns") and not any(t["failures"] for t in run["turns"]))
        latencies = [t["latency_s"] for run in runs for t in run.get("turns", []) if t["latency_s"] is not None]
        median = f"{statistics.median(latencies):.2f}s" if latencies else "-"
        errors = sum(1 for run in runs if "error" in run)
        lines.append(f"| {name} | {passed}/{len(runs)} | {median} | {f'{errors} errors' if errors else ''} |")
        detail += ["", f"## {name}"]
        for index, run in enumerate(runs, 1):
            if "error" in run:
                detail.append(f"- Run {index}: ERROR {run['error']}")
                if run.get("during"):
                    detail.append(f"  - during: {run['during']}")
                continue
            for turn in run["turns"]:
                status = "FAIL" if turn["failures"] else "ok"
                tools = ", ".join(t["name"] for t in turn["tools"]) or "none"
                detail.append(f"- Run {index} {status}: said {turn['step']!r} | heard {turn['heard'].strip()!r} "
                              f"| tools {tools} | reply {turn['reply'].strip()!r} | first audio {turn['latency_s']}s")
                detail += [f"  - {failure}" for failure in turn["failures"]]
    return "\n".join(lines + detail) + "\n"


def disclosure(model, config, plan, repeat, real_tools):
    tools = sorted(declared_tools(config))
    built_in = [name for tool in config.get("tools", []) for name in tool if name != "function_declarations"]
    lines = [f"Model: {model}",
             f"Sent to Google Gemini Live for each of {len(plan)} scenarios x {repeat} runs:",
             f"  - Ember's MVP system prompt ({len(config.get('system_instruction', ''))} characters) and "
             f"{len(tools)} tool declarations{' plus ' + ', '.join(built_in) if built_in else ''}",
             "  - Synthetic text-to-speech audio of the scripted lines, generated picture cards, text notes",
             "  - " + ("REAL tool results (network providers may be contacted; audio tools may play)"
                       if real_tools else "Stub tool results from the scenarios (no providers contacted)"),
             "No microphone, camera or speaker is opened. Uses your Gemini API quota.", ""]
    for scenario in plan:
        lines.append(f"  {scenario['name']}:")
        for step in scenario["steps"]:
            kind = next(k for k in STEP_KINDS if k in step)
            lines.append(f"    {kind}: {step[kind]}")
    return "\n".join(lines)


def main_checkout(root):
    """Baymax-main of the repository's main checkout, when `root` is a git worktree."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                                capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(common).parent / ROOT.name


def api_key(root=ROOT):
    """From the environment, else the tested branch's, this branch's or the main checkout's .env."""
    from dotenv import dotenv_values
    if os.getenv("GOOGLE_API_KEY"):
        return os.getenv("GOOGLE_API_KEY")
    for folder in (root, ROOT, main_checkout(root)):
        if folder is not None and (folder / ".env").is_file():
            key = dotenv_values(folder / ".env").get("GOOGLE_API_KEY")
            if key:
                return key
    return ""


async def run_all(plan, model, config, repeat, real_tools, connect, speak=synthesize, log=print):
    results = {}
    for scenario in plan:
        runs = []
        for index in range(repeat):
            try:
                runs.append({"turns": await run_scenario(connect, model, config, scenario, real_tools, speak)})
            except Exception as error:  # A dropped session fails that run, not the batch.
                runs.append({"error": f"{type(error).__name__}: {error}", "during": getattr(error, "during", None)})
            run = runs[-1]
            ok = "turns" in run and not any(t["failures"] for t in run["turns"])
            log(f"[EVAL] {scenario['name']} run {index + 1}/{repeat}: {'pass' if ok else 'FAIL'}")
        results[scenario["name"]] = {"runs": runs}
    return results


def main(argv=None, connect=None, confirm=input):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--only", action="append", help="Scenario name (repeatable)")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--real-tools", action="store_true", help="Run real toolkit calls instead of stubs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent; connect to nothing")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument("--report", type=Path, default=REPORT_DIR / "live-eval.md")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="Baymax-main of the branch to test, e.g. a PR worktree (default: this one)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if root != ROOT:
        if "features" in sys.modules:
            print("--root must be used from the command line (features are already imported).", file=sys.stderr)
            return 2
        sys.path.insert(0, str(root))  # That branch's features, prompts and tools.
    from dotenv import load_dotenv
    load_dotenv(root / ".env", override=False, interpolate=False)  # The user's toggles, as on the robot.
    model, config = mvp_session(root=root)
    print(f"Branch under test: {root}")
    scenarios = [s for s in load_scenarios(args.scenarios) if not args.only or s["name"] in args.only]
    if args.only and len(scenarios) != len(set(args.only)):
        print(f"Unknown scenario in {args.only}", file=sys.stderr)
        return 2
    results, plan = {}, []
    for scenario in scenarios:
        reason = missing_requirements(scenario, config)
        if reason:
            results[scenario["name"]] = {"skipped": reason}
        else:
            plan.append(scenario)
    print(disclosure(model, config, plan, args.repeat, args.real_tools))
    for name, outcome in results.items():
        print(f"  {name}: skipped ({outcome['skipped']})")
    if args.dry_run or not plan:
        return 0
    if connect is None:
        key = api_key(root)
        if not key:
            print("Set GOOGLE_API_KEY in Baymax-main/.env (never paste it into a PR).", file=sys.stderr)
            return 2
        from google import genai
        connect = genai.Client(api_key=key, http_options={"api_version": "v1alpha"}).aio.live.connect
    if not args.yes and confirm("Send this to Gemini? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was sent.")
        return 1
    results.update(asyncio.run(run_all(plan, model, config, args.repeat, args.real_tools, connect)))
    report = summarize(results)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    args.report.with_suffix(".json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n" + report.split("\n## ")[0])
    print(f"Report saved: {args.report} (local only; transcripts are of synthetic speech)")
    failed = any("runs" in r and any("error" in run or any(t["failures"] for t in run["turns"]) for run in r["runs"])
                 for r in results.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
