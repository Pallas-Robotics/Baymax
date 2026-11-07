"""Which Gemini Live model Ember talks to, and the settings each one needs.

Google retires preview models. gemini-2.5-flash-native-audio-preview-12-2025, the model
Ember was built and tested on (the runtime's MODEL), shuts down as early as 2026-11-17
(Google's deprecations page; the exact date is announced later). Its replacement,
gemini-3.8-live, needs Live quota on the API key's own project.

BAYMAX_LIVE_MODELS lists the models to use, in order, separated by commas; by default
the runtime's MODEL, then gemini-3.8-live. run() connects with the first, and with the
next one at once when this API key can't use it:

- Google refused to set up a fresh connection (close 1007 or 1008: the model is not
  found or not supported for Live, or rejects a setting; refusing a resumed one can be
  about its resume handle instead), or
- the connection ended before Gemini finished a turn on it, with Google saying the model
  is gone ("is not found for API version", "not supported for bidiGenerateContent", "no
  longer available") or out of quota for this key (close 1011 "You exceeded your current
  quota").

The new model is kept until Ember restarts. The old one's resume handle is dropped:
a handle belongs to the model that issued it. When every model has failed like that
in a row, run() waits as after any lost connection (0.3 s, doubling up to 4.8 s; no
fast spin) and tries the next one again, so Ember comes back with whichever
model returns. Any other error (a dropped network, Google's routine resets, "1011
internal error") never changes the model.

`adapt` gives a model Ember's config with the settings that model needs, for run() and
evals/live_eval.py --model alike. Gemini 3 Live models think by themselves (Google: omit
thinking_config) and run functions in the background (NON_BLOCKING) unless told
otherwise, so every function Ember declares without a behavior is made BLOCKING, as on
2.5; an explicit behavior (the eye tool's NON_BLOCKING) is kept. Gemini 2.5 gets the
config unchanged.
"""
import copy
import os
import re

from google.genai import errors
from websockets.exceptions import ConnectionClosed

from . import session

FALLBACK = "gemini-3.8-live"  # Google's replacement for the 2.5 native-audio preview
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a model name as gemini_preflight accepts one
# Google's words (in any close code) for a model that is gone, or out of quota for this key.
# They open its messages: a close frame carries only 123 bytes of reason, so the rest is cut.
_GONE = ("is not found for api version", "not supported for bidigeneratecontent",
         "no longer available", "exceeded your current quota")


def configured(model, environ=os.environ):
    """The Live models to use, in order: BAYMAX_LIVE_MODELS, else `model` (the runtime's
    MODEL) then FALLBACK. An entry that isn't a model name is skipped with a warning."""
    names = []
    for name in environ.get("BAYMAX_LIVE_MODELS", "").split(","):
        name = name.strip().removeprefix("models/")
        if name and _NAME.fullmatch(name):
            names.append(name)
        elif name:
            print(f"[SESSION] BAYMAX_LIVE_MODELS: {name!r} is not a model name; skipped.", flush=True)
    return tuple(dict.fromkeys(names or (model, FALLBACK)))


def generation(model):
    """The Gemini version a Live model's name starts with: 3 for gemini-3.8-live, 2 for
    gemini-2.5-flash-native-audio-preview-12-2025 or gemini-live-2.5-flash-preview."""
    match = re.match(r"(?:models/)?gemini-(?:live-)?(\d+)", model)
    return int(match.group(1)) if match else 0


def adapt(config, model):
    """Ember's config for `model`: unchanged for Gemini 2.5; for Gemini 3 and later,
    without thinking_config and with every function declared without a behavior BLOCKING."""
    if generation(model) < 3:
        return config
    result = copy.deepcopy(config)
    result.pop("thinking_config", None)
    for tool in result.get("tools", []):
        for declaration in tool.get("function_declarations", []):
            declaration.setdefault("behavior", "BLOCKING")
    return result


def _close(error):
    """(close code, reason) Google gave for `error`, a closed Live connection, else None."""
    if isinstance(error, errors.APIError):  # the SDK's form, from connect() and receive()
        return error.code, str(error.message or error.details)
    if isinstance(error, ConnectionClosed) and error.rcvd is not None:  # from a send
        return error.rcvd.code, error.rcvd.reason
    return None


def unavailable(error, set_up=True):
    """Why `error`, or one in its group, means this API key can't use the model, as
    "code reason", else None. `set_up`: Google had set the connection up when it failed."""
    if isinstance(error, BaseExceptionGroup):
        return next(filter(None, (unavailable(e, set_up) for e in error.exceptions)), None)
    close = _close(error)
    if close is None:
        return None
    code, reason = close
    if any(words in reason.lower() for words in _GONE) or (not set_up and code in (1007, 1008)):
        return f"{code} {reason.split('. ')[0][:160]}"
    return None


class ModelChoice:
    """run()'s Live model: the first configured one, then the next whenever this API key
    can't use the current one, kept until Ember restarts."""

    def __init__(self, models):
        self.models = tuple(models)
        self.index = 0
        self.failed = 0     # models found unavailable in a row since one last finished a turn
        self._told = set()  # what has been logged since then, once each
        self._resuming = False  # the connection being set up offers a resume handle

    @property
    def current(self):
        return self.models[self.index]

    def adapt(self, config):
        """`config` for the current model, for the connection about to be set up."""
        self._resuming = bool((config.get("session_resumption") or {}).get("handle"))
        return adapt(config, self.current)

    def move_on(self, error, set_up, served):
        """After a connection with the current model ended with `error`. `set_up`: Google
        set it up; `served`: Gemini finished a turn on it. True when run() should connect
        again at once, with the next model, because this API key can't use the current one.
        False to go on as after any other error; also when every model has now failed in a
        row: the next one is tried after the usual pause."""
        if served:
            self.failed = 0
            self._told.clear()
            return False
        why = unavailable(error, set_up or self._resuming)  # a refused resume is the handle's
        if why is None:
            return False
        old = self.current
        self.failed += 1
        self.index = (self.index + 1) % len(self.models)
        if self.current != old:
            session.forget()  # a resume handle belongs to the model that issued it
        if self.failed < len(self.models):
            self._tell(old, f"{old} can't be used with this API key ({why}); "
                            f"switching to {self.current} until Ember restarts.")
            return True
        self.failed = 0
        self._tell(None, f"No Gemini Live model can be used with this API key now ({old}: {why}); "
                         f"trying {self.current} again after the usual pause.")
        return False

    def _tell(self, key, message):
        if key in self._told:
            return
        self._told.add(key)
        print("[SESSION] " + message, flush=True)
        import runtime_monitor as monitor
        monitor.record("session", message)
