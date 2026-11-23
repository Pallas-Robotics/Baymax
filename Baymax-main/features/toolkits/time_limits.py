"""Per-tool time limits so a hung service can't leave Ember silent.

Every Gemini function call that runs a blocking toolkit function goes through
run(): a daemon worker thread with the tool's limit. On timeout Gemini gets
timed_out() and Ember says the service couldn't be reached; the worker's late
result is dropped. search_web keeps its own budget (web_search.TOTAL_S) and its
limit here is only a backstop just above it.
"""
import asyncio
import logging
import threading

from . import web_search

log = logging.getLogger(__name__)

DEFAULT = 10.0
PUBLIC_DEFAULT = 12.0  # Unlisted experimental public_* web lookups.
LIMITS = {
    # Local, no network: should be near instant.
    "local_clock": 5.0, "reminder": 5.0, "household_list": 5.0,
    "set_eye_expression": 5.0, "set_voice_volume": 5.0,
    "public_capabilities": 5.0, "public_feed_preferences": 5.0,
    # Location and weather.
    "get_location": 8.0, "set_location": 8.0, "get_weather": 8.0,
    "public_find_place": 8.0, "public_weather": 8.0, "public_air_quality": 8.0,
    # Search-style lookups (brain_game, which may fetch trivia, takes DEFAULT).
    "public_knowledge": 12.0, "public_books": 12.0, "public_headlines": 12.0,
    # LibriVox search and stream start; live radio station lookup and start.
    "audiobook": 15.0, "listen": 15.0,
    # Google Search lookup session: its own TOTAL_S (13 s) ends it first.
    web_search.NAME: web_search.TOTAL_S + 1.0,
}


def limit_for(name):
    if name in LIMITS:
        return LIMITS[name]
    return PUBLIC_DEFAULT if str(name).startswith("public_") else DEFAULT


def timed_out(name):
    return {"error": "timed_out",
            "message": "The service for this request couldn't be reached just now. "
                       "Tell the person briefly and offer to try again."}


async def run(name, func, *args):
    """Run blocking func(*args) in a daemon thread with name's time limit.

    On timeout the worker keeps running, but its result is discarded: the
    future is already cancelled and this coroutine has returned the timeout
    result, so a late value can never reach Gemini. A daemon thread (not the
    default executor, which asyncio.run joins on exit) means a hung service
    cannot block shutdown.
    """
    seconds = limit_for(name)
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(ok, value):
        if not future.done():  # Cancelled by the timeout: drop the late result.
            future.set_result(value) if ok else future.set_exception(value)

    def work():
        try:
            outcome = (True, func(*args))
        except BaseException as exc:  # Re-raised in the awaiting coroutine.
            outcome = (False, exc)
        try:
            loop.call_soon_threadsafe(settle, *outcome)
        except RuntimeError:
            pass  # Loop already closed (shutdown): nothing to deliver.

    threading.Thread(target=work, name=f"tool-{name}", daemon=True).start()
    try:
        return await asyncio.wait_for(future, timeout=seconds)
    except asyncio.TimeoutError:
        log.warning("Toolkit call %s timed out after %.0f s", name, seconds)
        return timed_out(name)
