"""Read what a branch ships, without starting it: its runtime core, tools, prompt and runtime env.

Run with the branch's own interpreter, so its features and dependencies are the ones
imported:

    <branch>/.venv/bin/python evals/robot/probe.py --root <branch>/Baymax-main

Prints one JSON object. It reuses evals/live_eval.py (loaded from this rig's
checkout by path, not from the branch) to build the session config from the core's
source; no device is opened and nothing is contacted. Only BAYMAX_*/EMBER_* settings
are reported, so a key in .env never appears in the output.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

RIG_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_PREFIXES = ("BAYMAX_", "EMBER_")
OVERRIDE = "EMBER_PROBE_ENV_"


def runtime_core(root):
    """The realtime_gemini_N.py that the branch's run_pc.py starts."""
    try:
        found = re.findall(r"realtime_gemini_\d+\.py", (Path(root) / "run_pc.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return found[0] if found else None


def load_live_eval():
    spec = importlib.util.spec_from_file_location("rig_live_eval", RIG_ROOT / "evals" / "live_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(root):
    root = Path(root).resolve()
    result = {"root": str(root), "core": runtime_core(root), "tools": [], "prompt": "", "env": {}}
    os.chdir(root)
    try:
        from dotenv import dotenv_values
        live_eval = load_live_eval()
        # live_eval puts the rig's checkout first on sys.path; the branch's features must win.
        sys.path[:] = [str(root)] + [p for p in sys.path if p and Path(p).resolve() not in (RIG_ROOT, root)]
        for name in [m for m in sys.modules if m == "features" or m.startswith("features.")]:
            del sys.modules[name]
        launchers = [root / "run_pc.py", root / "run_mvp.py"]
        env = {name: value for name, value in live_eval.launcher_defaults([p for p in launchers if p.exists()]).items()}
        # The rig replaces the branch's .env with the robot's settings (passed as EMBER_PROBE_ENV_*);
        # without them (a plain dry run) the branch's own .env applies.
        dotenv = {k[len(OVERRIDE):]: v for k, v in os.environ.items() if k.startswith(OVERRIDE)}
        if not dotenv and (root / ".env").is_file():
            dotenv = {k: v for k, v in dotenv_values(root / ".env", interpolate=False).items() if v is not None}
        env.update(dotenv)  # The launchers load .env with override=True before their setdefaults.
        for name, value in env.items():
            if name.startswith(PUBLIC_PREFIXES):
                os.environ[name] = value
                result["env"][name] = value
        if not result["core"]:
            raise RuntimeError("run_pc.py names no realtime_gemini_N.py")
        model, config = live_eval.core_session(root / result["core"])
        result.update(model=model, tools=sorted(live_eval.declared_tools(config)),
                      prompt=config.get("system_instruction", ""))
    except Exception as error:  # Reported, so the rig skips the scenarios it cannot gate.
        result["error"] = f"{type(error).__name__}: {error}"[:300]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe(args.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
