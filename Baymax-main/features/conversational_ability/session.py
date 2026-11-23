"""Keep long Gemini Live conversations fast and remembered across reconnects.

Without these, Google limits a Live session to 15 minutes of audio (2 with
video, and Ember sends a camera frame every 3 s), resets every connection after
about 10 minutes, and each turn re-reads the whole growing history. So:

- context window compression: a sliding window trims the oldest context once it
  passes BAYMAX_CONTEXT_TRIGGER_TOKENS, keeping replies fast with no time limit;
- session resumption: the receive loop passes every resume handle Gemini sends
  to `remember`, and the next connection's config resumes that same
  conversation instead of starting fresh. A handle that fails to resume (no
  update arrives after using it) or is older than Google's 2 h validity is
  dropped, so a bad handle can never block reconnecting.

BAYMAX_SESSION_RESUMPTION=0 / BAYMAX_CONTEXT_COMPRESSION=0 disable each part.
Nothing new is sent anywhere: both are settings of the existing Gemini session.

`connection_lost` tells the runtime's receive loop when an error means the
connection is gone, whatever close code Gemini used, so it reconnects. The send
loop doesn't need it: any failed send ends the session.
"""
import copy
import os
import threading
import time

from google.genai import errors
from websockets.exceptions import ConnectionClosed

HANDLE_LIFETIME_S = 110 * 60  # Google keeps handles 2 h after a session ends; stay inside it.
# A closed connection reaches a send as ConnectionClosed and a receive as APIError
# (the SDK wraps it, with the close code in place of an HTTP status).
CONNECTION_ERRORS = (ConnectionClosed, errors.APIError, ConnectionError, TimeoutError)

_lock = threading.Lock()
_state = {"handle": None, "at": 0.0, "offered": 0}


def connection_lost(error):
    """True when this exception means the Live connection is gone. Decided by type,
    not by matching close codes in its text: a code missing from such a list left
    Ember retrying a dead connection, deaf and silent, until the service restarted."""
    return isinstance(error, CONNECTION_ERRORS)


def _tokens(name, default):
    try:
        return max(1000, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def remember(update, clock=time.monotonic):
    """Store the newest resumable handle from a session_resumption_update."""
    if update is None or not getattr(update, "resumable", False) or not getattr(update, "new_handle", None):
        return
    with _lock:
        _state.update(handle=update.new_handle, at=clock(), offered=0)


def forget():
    with _lock:
        _state.update(handle=None, at=0.0, offered=0)


def not_tried():
    """Undo an offer when the network failed before Gemini accepted a connection.

    Adapted from Joe Kojima's 7f21fab; server refusals still consume the offer.
    """
    with _lock:
        _state["offered"] = 0


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


def retry_plan(error, lasted, quick_failures):
    """Joe Kojima's bounded network backoff, adapted without proactive handover.

    Sources: 30c7d7b and c3deba5. None retains the existing device-error reset.
    Pure network failures never need a global PortAudio reset.
    """
    quick_failures = quick_failures + 1 if lasted < 60 else 1
    if all(isinstance(e, OSError) or connection_lost(e) for e in _leaves(error)):
        return min(0.3 * 2 ** min(quick_failures - 1, 16), 4.8), quick_failures
    return None, quick_failures


def connection_not_started(error):
    """Only transport failures before setup preserve an untried resume handle."""
    return all(isinstance(e, OSError) for e in _leaves(error))


def extend_config(config, clock=time.monotonic):
    result = copy.deepcopy(config)
    if os.getenv("BAYMAX_CONTEXT_COMPRESSION", "1") != "0":
        trigger = _tokens("BAYMAX_CONTEXT_TRIGGER_TOKENS", 32000)
        result["context_window_compression"] = {"trigger_tokens": trigger,
                                                "sliding_window": {"target_tokens": trigger // 2}}
    if os.getenv("BAYMAX_SESSION_RESUMPTION", "1") != "0":
        with _lock:
            handle = _state["handle"]
            if handle and (_state["offered"] >= 1 or clock() - _state["at"] > HANDLE_LIFETIME_S):
                # Already tried once with no fresh handle since (resume failed), or expired.
                print("[SESSION] Starting a fresh conversation (previous one could not be resumed).", flush=True)
                _state.update(handle=None, offered=0)
                handle = None
            if handle:
                _state["offered"] += 1
                print("[SESSION] Resuming the previous conversation.", flush=True)
        result["session_resumption"] = {"handle": handle} if handle else {}
    return result


def on_server_message(response):
    """Called by the receive loop for resumption updates and GoAway warnings."""
    remember(getattr(response, "session_resumption_update", None))
    go_away = getattr(response, "go_away", None)
    if go_away is not None:
        import runtime_monitor as monitor
        print(f"[SESSION] Gemini will reset this connection soon ({go_away.time_left}); "
              "the next one resumes the conversation.", flush=True)
        monitor.record("session", "Gemini connection reset soon; conversation will resume")
