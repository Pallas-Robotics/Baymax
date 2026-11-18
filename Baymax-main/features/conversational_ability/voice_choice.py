"""Which of Gemini's voices Ember speaks with, chosen by the people it lives with.

"Ember, can you use a deeper voice?", "talk like a woman", "try a different voice" or
"go back to your usual voice" reach the change_voice tool through Gemini, which picks
one of Gemini Live's 30 prebuilt voices from the catalogue below (style from Google's
voice list, grouped by measured pitch: VOICE_GROUPS). The choice is saved on the robot
(ember_voice.json in BAYMAX_DATA_DIR) and outlasts restarts; BAYMAX_VOICE still sets the
voice until someone chooses one, and "default" goes back to it.

Gemini fixes a session's voice when it starts: a resumed session keeps its old voice
(tested 2026-11-15, gemini-2.5-flash-native-audio and gemini-3.8-live: resumed with
Charon it still spoke with Kore's 214-240 Hz, a fresh Charon session at 110-175 Hz).
So after Ember's short "switching now" reply the runtime starts a fresh session
(switch_due, then session.forget) whose instructions carry the last few turns of the
conversation (recap), and Ember then says a line in its new voice (session_task).
Memories and what the family wrote are in every session's instructions anyway.

Nothing is sent anywhere new: the voice name is part of the existing Gemini session
config, and the recap is the same conversation Gemini already heard, kept in RAM only.
"""
import copy
import difflib
import json
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

NAME = "change_voice"

# Gemini Live's prebuilt voices with Google's one-word style.
VOICE_STYLE = {
    "Zephyr": "bright", "Puck": "upbeat", "Charon": "informative", "Kore": "firm",
    "Fenrir": "excitable", "Leda": "youthful", "Orus": "firm", "Aoede": "breezy",
    "Callirrhoe": "easy-going", "Autonoe": "bright", "Enceladus": "breathy", "Iapetus": "clear",
    "Umbriel": "easy-going", "Algieba": "smooth", "Despina": "smooth", "Erinome": "clear",
    "Algenib": "gravelly", "Rasalgethi": "informative", "Laomedeia": "upbeat", "Achernar": "soft",
    "Alnilam": "firm", "Schedar": "even", "Gacrux": "mature", "Pulcherrima": "forward",
    "Achird": "friendly", "Zubenelgenubi": "casual", "Vindemiatrix": "gentle", "Sadachbia": "lively",
    "Sadaltager": "knowledgeable", "Sulafat": "warm",
}
# By pitch: the median of two or three readings of a test passage per voice on
# gemini-3.8-live (librosa pYIN, 2026-11-15), lowest first. Google doesn't say which
# voices sound like men or women; under ~135 Hz they are deep, men's voices, above
# ~180 Hz women's, and the three in between were 137-172 Hz.
VOICE_GROUPS = {
    "deep": ["Alnilam", "Algieba", "Charon", "Algenib", "Schedar", "Umbriel", "Zubenelgenubi",
             "Sadaltager", "Puck", "Enceladus", "Achird", "Orus", "Fenrir", "Iapetus", "Rasalgethi",
             "Sadachbia"],
    "in between": ["Pulcherrima", "Gacrux", "Laomedeia"],
    "higher": ["Vindemiatrix", "Zephyr", "Leda", "Aoede", "Achernar", "Kore", "Sulafat", "Autonoe",
               "Callirrhoe", "Despina", "Erinome"],
}

DECLARATION = {
    "name": NAME,
    "description": "Change the voice Ember speaks with (one of its prebuilt voices), go back to the usual "
                   "one, or report the current voice and some options.",
    "parameters": {"type": "OBJECT", "properties": {
        "voice": {"type": "STRING", "description": "A voice name from [CHANGING YOUR VOICE], 'default' for "
                                                    "the usual voice, or 'status' to report the current one."}},
        "required": ["voice"]},
}


def _catalogue():
    line = lambda names: ", ".join(f"{v} ({VOICE_STYLE[v]})" for v in names)
    return (f"Deep, men's voices, deepest first: {line(VOICE_GROUPS['deep'])}.\n"
            f"In between, lower women's voices: {line(VOICE_GROUPS['in between'])}.\n"
            f"Higher, women's voices, lowest first: {line(VOICE_GROUPS['higher'])}.")


def instruction():
    return f"""
[CHANGING YOUR VOICE]
People can choose the voice you speak with. When someone asks you to change your
voice (deeper, higher, a man's or a woman's voice, calmer, livelier, a different
one, or back to your usual voice), pick the voice from this list that best fits
what they asked and call change_voice with its name ('default' for your usual
voice). If they are unsure, suggest two or three that fit and let them pick.
{_catalogue()}
After calling it, say only one short line such as "Alright, switching my voice now",
with no question: the change happens right after that line, and then you will try
out the new voice.
Never change your voice unless someone asks. Use 'status' when they ask which
voice you have.
"""


def _path():
    root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[2]))
    return root / "ember_voice.json"


def match(name):
    """The prebuilt voice `name` means (case and small typos forgiven), or None."""
    names = {v.lower(): v for v in VOICE_STYLE}
    wanted = str(name or "").strip().lower()
    if wanted in names:
        return names[wanted]
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.8)  # "Archernar" -> Achernar
    return names[close[0]] if close else None


class VoiceChoice:
    def __init__(self, path=None, default=None, clock=time.time):
        self.path = Path(path) if path else _path()
        self._default = default
        self.clock = clock
        self.lock = threading.Lock()
        self.chosen = self._load()
        self.pending = None          # (old, new): a change the runtime still has to make
        self.requested_at = None     # time.monotonic() of that change
        self.carry = None            # recap for the fresh session after a change
        self.announce = None         # the new voice, to try out once the fresh session is up
        self.by = "spoken"           # who chose it: "spoken" (asked Ember) or a name from the app
        self.turns = deque(maxlen=RECAP_TURNS)

    def default(self):
        """The voice before anyone chose: BAYMAX_VOICE, else persona.DEFAULT_VOICE."""
        if self._default:
            return self._default
        from features import persona
        return match(os.getenv("BAYMAX_VOICE", "")) or persona.DEFAULT_VOICE

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            voice = match(data.get("voice"))
            if voice is None:
                print(f"[VOICE] {self.path.name} names no known voice; using the usual one.", flush=True)
            return voice
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            print(f"[VOICE] Ignoring unreadable {self.path.name} ({error}).", flush=True)
            return None

    def _save(self, by):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ember_voice.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"voice": self.chosen, "changed_by": by, "at": self.clock()}, f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def current(self):
        with self.lock:
            return self.chosen or self.default()

    def choose(self, voice, by="spoken"):
        """Make `voice` ('default' for the usual one) Ember's voice. Returns the tool result."""
        usual = voice is not None and str(voice).strip().lower() in ("default", "usual", "original")
        new = None if usual else match(voice)
        if not usual and new is None:
            return {"error": f"No voice called {voice!r}. Choose one from the list."}
        with self.lock:
            old = self.chosen or self.default()
            self.chosen = new
            effective = new or self.default()
            try:
                self._save(by)
                saved = True
            except OSError as error:
                saved = False
                print(f"[VOICE] Could not save the voice ({error}); it lasts until restart.", flush=True)
            if effective == old:
                return {"changed": False, "voice": effective, "note": "That is already your voice."}
            self.pending = (old, effective)
            self.requested_at = time.monotonic()
            self.by = by
            print(f"[VOICE] {by}: voice {old} -> {effective}; switching at the next quiet moment.", flush=True)
            result = {"changed": True, "voice": effective, "style": VOICE_STYLE[effective],
                      "note": "Say only one short line now, like 'Alright, switching my voice now.', "
                              "with no question: the switch happens right after it, and then you "
                              "will try out the new voice."}
            if not saved:
                result["saved"] = False
            return result

    def execute(self, args):
        if not isinstance(args, dict) or not args.get("voice"):
            return {"error": "Expected a voice name, 'default' or 'status'"}
        if str(args["voice"]).strip().lower() == "status":
            voice = self.current()
            return {"voice": voice, "style": VOICE_STYLE[voice], "usual": self.default()}
        return self.choose(args["voice"])

    # ── The switch (runtime) ─────────────────────────────────────────────────
    def switch_due(self):
        with self.lock:
            return self.pending is not None

    def start_switch(self):
        """The runtime is about to start the fresh session: keep the recap for its
        instructions and the new voice to try out. Returns (old, new) or None."""
        with self.lock:
            if self.pending is None:
                return None
            change, self.pending = self.pending, None
            self.carry = self._recap()
            self.announce = change[1]
            return change

    # ── Recap of the conversation (RAM only) ────────────────────────────────
    def remember_turn(self, speaker, text):
        text = " ".join(str(text or "").split())
        if text:
            with self.lock:
                self.turns.append(("Them" if speaker == "user" else "You", text[:RECAP_TURN_CHARS]))

    def _recap(self):
        if not self.turns:
            return None
        lines = [f"{who}: {text}" for who, text in self.turns]
        while sum(len(l) for l in lines) > RECAP_CHARS and len(lines) > 1:
            lines.pop(0)
        return "\n".join(lines)

    def extend_config(self, config):
        """The tool and its instructions, and after a switch the recap. (The voice itself
        is set by persona.voice, which takes the saved choice first.)"""
        result = copy.deepcopy(config)
        with self.lock:
            carry = self.carry
        if enabled() and not any(d.get("name") == NAME for tool in result.get("tools", [])
                                 for d in tool.get("function_declarations", [])):
            result["system_instruction"] = result.get("system_instruction", "") + instruction()
            result.setdefault("tools", []).append({"function_declarations": [copy.deepcopy(DECLARATION)]})
        if carry:
            result["system_instruction"] += (
                "\n[CONVERSATION SO FAR]\nYour voice was just changed, which started a new "
                "session. This is how the conversation went until then; "
                "carry on from it naturally:\n" + carry + "\n")
        return result

    def take_announcement(self):
        """The new voice to try out (once), after the fresh session started."""
        with self.lock:
            voice, self.announce = self.announce, None
            return voice

    def settled(self):
        """The fresh session is up and resumable: its own handle carries the conversation."""
        with self.lock:
            self.carry = None


RECAP_TURNS = 16
RECAP_TURN_CHARS = 400
RECAP_CHARS = 3000

choice = VoiceChoice()


def enabled():
    """BAYMAX_VOICE_CHOICE=0: no change_voice tool (the voice is BAYMAX_VOICE's, or a saved choice)."""
    return os.getenv("BAYMAX_VOICE_CHOICE", "1") != "0"


def execute(args):
    return choice.execute(args)


def extend_config(config):
    return choice.extend_config(config)


def _can_speak():
    try:
        from features.toolkits.everyday.tools import available_to_speak
        return available_to_speak()
    except Exception:
        return True


async def session_task(session, shutdown_event, can_speak=_can_speak, poll=0.5, handle_at=None):
    """On the fresh session after a switch: Ember tries its new voice out loud, once; then,
    when Gemini sends this session's first resume handle, the recap is no longer needed
    (a reconnect resumes this session, which has it)."""
    import asyncio
    if handle_at is None:
        from features.conversational_ability.session import handle_received_at as handle_at
    voice = choice.take_announcement()
    if not voice:
        return
    started = time.monotonic()
    while not shutdown_event.is_set() and not can_speak():
        await asyncio.sleep(poll)
    who = ("the person asked for" if choice.by == "spoken"
           else f"{choice.by} chose for you in the Ember app")
    try:
        await session.send_client_content(turns={"role": "user", "parts": [{"text":
            f"[VOICE CHANGED] You now speak with the {voice} voice {who}. "
            "Say one short, natural line in it, for example asking how they like it."}]},
            turn_complete=True)
    except Exception:
        with choice.lock:
            choice.announce = choice.announce or voice   # the next connection tries again
        raise
    while not shutdown_event.is_set():
        at = handle_at()
        if at is not None and at >= started:
            choice.settled()
            return
        await asyncio.sleep(poll)


def register():
    from features.toolkits.fall_detection import session_tasks
    if session_task not in session_tasks:
        session_tasks.append(session_task)


try:
    register()
except ImportError:   # a checkout without the session-task list
    pass
