"""How loud Ember's own voice is, on a 1-10 scale the person can change by asking.

"Ember, speak louder", "softer" or "set your volume to 8" reach the
set_voice_volume tool through Gemini; the reply confirming it is already played at
the new level. Level 7 is the original loudness, so nothing changes until someone
asks. Levels above 7 add gain with a soft limiter instead of hard clipping; the
lowest level is 1, never mute, so a fall check-in can't be silenced by accident.

Only Ember's voice (Gemini's replies and the waiting cue) is scaled, before the
echo canceller's reference, so echo cancellation keeps matching what is played.
Audiobooks, radio and recordings keep their own volume (the audiobook and listen
tools). The level is saved on the robot (voice_volume.json in BAYMAX_DATA_DIR) and
survives restarts; BAYMAX_VOICE_VOLUME (1-10) sets the level before anyone has
changed it. Nothing is sent anywhere and no device is opened: the speaker stream is
the existing one.
"""
import copy
import json
import os
import tempfile
import threading
from pathlib import Path

import numpy as np

NAME = "set_voice_volume"
DEFAULT_LEVEL = 7
LEVELS = range(1, 11)
# Gain per level: 7 is unchanged; each step is clearly audible (about 1.5-3.5 dB).
GAINS = {1: 0.2, 2: 0.3, 3: 0.4, 4: 0.55, 5: 0.7, 6: 0.85, 7: 1.0, 8: 1.25, 9: 1.5, 10: 2.0}
LIMIT = 0.7 * 32767  # The soft limiter starts here; it never reaches full scale.

DECLARATION = {
    "name": NAME,
    "description": "Change how loud Ember's own voice is (not an audiobook's, radio's or recording's), or report it.",
    "parameters": {"type": "OBJECT", "properties": {
        "change": {"type": "STRING", "enum": ["louder", "quieter", "default", "status"],
                   "description": "Relative change, back to the usual level, or just report it."},
        "level": {"type": "NUMBER", "description": "Exact level from 1 (quietest) to 10 (loudest); 7 is usual."}}},
}
INSTRUCTION = """
[VOICE VOLUME]
When someone asks you to speak louder or up, quieter or softer, to set your volume
to a number, or how loud you are, call set_voice_volume (change louder or quieter,
an exact level from 1 to 10, default for the usual level 7, or status). Answer
only after the tool succeeds. You can control your own voice with this tool:
never claim you cannot, merely promise to change it, or imitate a quieter voice
instead of calling the tool. Report the returned level; do not invent success.
Use one short sentence, such as "Is this better?": your reply already plays at the
new level. If the result says it is already the loudest, say so and suggest turning
up the speaker itself. Never change it unless asked. It is never fully muted. While
an audiobook, radio station or recording plays, "louder" or "quieter" about it means
the volume action of the audiobook or listen tool, not this one.
"""


def _path():
    root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[2]))
    return root / "voice_volume.json"


def _level(value):
    """An int in 1..10, or ValueError."""
    if isinstance(value, bool):
        raise ValueError("Give a level from 1 to 10")
    level = float(value)
    if level != level:
        raise ValueError("Give a level from 1 to 10")
    return int(min(10, max(1, round(level))))


class VoiceVolume:
    def __init__(self, path=None):
        self.path = Path(path) if path else _path()
        self.lock = threading.Lock()
        self.level = self._load()
        self._applied = GAINS[self.level]  # gain at the end of the last block played

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return _level(data["level"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            print(f"[VOLUME] Ignoring unreadable {self.path.name} ({error}).", flush=True)
        try:
            return _level(os.getenv("BAYMAX_VOICE_VOLUME", DEFAULT_LEVEL))
        except (TypeError, ValueError):
            print("[VOLUME] Invalid BAYMAX_VOICE_VOLUME; using 7.", flush=True)
            return DEFAULT_LEVEL

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".voice_volume.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"level": self.level}, f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def status(self):
        return {"level": self.level, "of": 10, "usual": DEFAULT_LEVEL,
                "loudest": self.level == 10, "quietest": self.level == 1}

    def execute(self, args):
        if not isinstance(args, dict):
            return {"error": "Expected change or level"}
        change, level = args.get("change"), args.get("level")
        with self.lock:
            before = self.level
            if level is not None:
                try:
                    self.level = _level(level)
                except (TypeError, ValueError):
                    return {"error": "Give a level from 1 to 10"}
            elif change == "louder":
                self.level = min(10, self.level + 1)
            elif change == "quieter":
                self.level = max(1, self.level - 1)
            elif change == "default":
                self.level = DEFAULT_LEVEL
            elif change != "status":
                return {"error": "Expected change (louder, quieter, default or status) or a level"}
            result = {**self.status(), "changed": self.level != before}
            if self.level != before:
                try:
                    self._save()
                except OSError as error:
                    result["saved"] = False
                    print(f"[VOLUME] Could not save the level ({error}); it lasts until restart.", flush=True)
                print(f"[VOLUME] Voice volume {before} -> {self.level} of 10.", flush=True)
            return result

    def apply(self, chunk):
        """Scale one block of Ember's int16 voice. Runs on the speaker callback."""
        target = GAINS[self.level]
        start = self._applied
        self._applied = target
        if start == target == 1.0 or not chunk:
            return chunk
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if start == target:
            samples *= target
        else:  # ramp across the block: no click when the level changes mid-reply
            samples *= np.linspace(start, target, len(samples), dtype=np.float32)
        loud = np.abs(samples) > LIMIT
        if loud.any():
            excess = (np.abs(samples[loud]) - LIMIT) / (32767 - LIMIT)
            samples[loud] = np.sign(samples[loud]) * (LIMIT + (32767 - LIMIT) * np.tanh(excess))
        return np.clip(np.round(samples), -32768, 32767).astype(np.int16).tobytes()


voice_volume = VoiceVolume()


def execute(args):
    return voice_volume.execute(args)


def enabled():
    return os.getenv("BAYMAX_VOICE_VOLUME_TOOL", "1") != "0"


def extend_config(config):
    """Offer the tool (core, like the eye tool: works with the toolkit off too)."""
    if not enabled() or any(d.get("name") == NAME for tool in config.get("tools", [])
                            for d in tool.get("function_declarations", [])):
        return config
    result = copy.deepcopy(config)
    result["system_instruction"] = result.get("system_instruction", "") + INSTRUCTION
    result.setdefault("tools", []).append({"function_declarations": [copy.deepcopy(DECLARATION)]})
    return result
