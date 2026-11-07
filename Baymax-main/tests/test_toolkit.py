import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import requests
from features.toolkits.location import Location
from features.toolkits.weather import Weather
from features.toolkits import gemini_tools

IP = {"success": True, "city": "Sampletown", "region": "State", "country": "Country", "country_code": "US", "latitude": 40., "longitude": -75., "ip": "private-unused-value"}
CITY = {"name": "Elsewhere", "admin1": "Other State", "country": "Country", "country_code": "US", "latitude": 41., "longitude": -76.}
WEATHER = {"current": {"time": "2026-10-07T12:00", "temperature_2m": 20, "weather_code": 0}, "daily": {"time": ["2026-10-07"], "temperature_2m_max": [22]}, "timezone": "America/New_York", "current_units": {"temperature_2m": "°C"}}


def _lookup_client(answers, refuse=None):
    """A stand-in for genai.Client's Live API. Each question sent on a session queues the next of
    `answers`: a string (an answer), None (never answers), an exception (raised), or a dict with
    "text" (or "chunks", the transcript's pieces) and any of "late" (a transcription chunk after
    turn_complete), "generation_complete" and "turn_complete" (default True). `refuse`: a setup error for every model, or {model: error}."""
    opened, closed, configs = [], [], []

    def content(**fields):
        return SimpleNamespace(server_content=SimpleNamespace(**{
            "output_transcription": None, "model_turn": None, "grounding_metadata": None,
            "generation_complete": False, "turn_complete": False, **fields}))

    def said(text):
        return SimpleNamespace(text=text, finished=False)

    def messages(answer):
        if answer is None:
            return []
        if isinstance(answer, Exception):
            return [answer]
        spec = answer if isinstance(answer, dict) else {"text": answer}
        pieces = spec.get("chunks") or (spec["text"][:4], spec["text"][4:])
        queued = [SimpleNamespace(server_content=None)]          # e.g. a session resumption update
        queued += [content(output_transcription=said(piece)) for piece in pieces if piece]
        if spec.get("generation_complete"):
            queued.append(content(generation_complete=True))
        if spec.get("turn_complete", True):
            chunks = [SimpleNamespace(web=SimpleNamespace(title="Example News")), SimpleNamespace(web=None)]
            queued.append(content(grounding_metadata=SimpleNamespace(grounding_chunks=chunks), turn_complete=True))
        if spec.get("late"):
            queued.append(content(output_transcription=said(spec["late"])))
        return queued

    class Session:
        gone = False

        def __init__(self):
            self.sent, self.queue = [], []

        async def send_client_content(self, turns, turn_complete):
            if self.gone:
                raise ConnectionError("closed by Google")
            self.sent.append(turns[0]["parts"][0]["text"])
            self.queue.extend(messages(answers.pop(0)))

        async def receive(self):          # like AsyncSession.receive: stops after turn_complete
            while True:
                while not self.queue:
                    await asyncio.sleep(0.002)
                message = self.queue.pop(0)
                if isinstance(message, Exception):
                    raise message
                yield message
                if message.server_content is not None and message.server_content.turn_complete:
                    return

    class Connection:
        def __init__(self, model):
            self.model = model

        async def __aenter__(self):
            error = refuse.get(self.model) if isinstance(refuse, dict) else refuse
            if error:
                raise RuntimeError(error)
            self.session = Session()
            opened.append(self.session)
            return self.session

        async def __aexit__(self, *exc):
            closed.append(self.session)

    def connect(model, config):
        configs.append((model, config))
        return Connection(model)
    return SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect))), opened, closed, configs


def _soon(condition, timeout=2.0):
    """Whether `condition()` becomes true within `timeout` (sessions close in the background)."""
    import time
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True

class ToolkitTests(unittest.TestCase):
    def test_location_cache_expires_and_never_returns_ip(self):
        clock = [0]
        calls = []
        def fetch(*args):
            calls.append(args)
            return IP
        location = Location(fetch, lambda: clock[0])
        self.assertTrue(location.get()["approximate"])
        self.assertNotIn("ip", location.get())
        self.assertEqual(len(calls), 1)
        clock[0] = 3601
        location.get()
        self.assertEqual(len(calls), 2)

    def test_ambiguous_city_does_not_replace_location(self):
        location = Location(lambda *args: {"results": [CITY, {**CITY, "admin1": "Another State"}]})
        result = location.set_city("Elsewhere")
        self.assertTrue(result["needs_clarification"])
        self.assertIsNone(location.confirmed)

    def test_region_selects_confirmed_city_without_ip_lookup(self):
        location = Location(lambda *args: {"results": [CITY, {**CITY, "admin1": "Another State"}]})
        result = location.set_city("Elsewhere", "US", "Other State")
        self.assertEqual(location.get(), result)
        self.assertEqual(result["latitude"], 41.)

    def test_location_failure_retries_after_short_cooldown(self):
        now = [0]
        calls = []
        def fetch(*args):
            calls.append(1)
            raise requests.Timeout()
        location = Location(fetch, lambda: now[0])
        self.assertIn("error", location.get())
        location.get()
        self.assertEqual(len(calls), 1)
        now[0] = 31
        location.get()
        self.assertEqual(len(calls), 2)

    def test_weather_cache_units_and_city_change(self):
        location = Location(lambda *args: IP)
        calls = []
        def fetch(url, params):
            calls.append(params)
            return WEATHER
        weather = Weather(location, fetch)
        self.assertFalse(weather.get()["cached"])
        self.assertTrue(weather.get()["cached"])
        weather.get("fahrenheit")
        self.assertEqual(calls[-1]["temperature_unit"], "fahrenheit")
        location.confirmed = {**CITY, "city": "Elsewhere"}
        weather.get()
        self.assertEqual(calls[-1]["latitude"], 41.)
        self.assertEqual(len(calls), 3)

    def test_weather_is_fetched_again_before_it_expires_in_each_unit_asked(self):
        clock = [0.0]
        calls = []
        fail = [False]

        def fetch(url, params):
            calls.append(params["temperature_unit"])
            if fail[0]:
                raise requests.Timeout()
            return WEATHER
        weather = Weather(Location(lambda *args: IP, lambda: clock[0]), fetch, lambda: clock[0])
        weather.get()
        weather.get("fahrenheit")                    # asked in both units
        clock[0] = 540                                # prewarm's 9-minute refresh
        weather.refresh()
        self.assertEqual(sorted(calls[2:]), ["celsius", "fahrenheit"])
        clock[0] = 1000                               # the first copies expired at 600
        self.assertTrue(weather.get()["cached"])
        self.assertTrue(weather.get("fahrenheit")["cached"])
        self.assertEqual(len(calls), 4)               # the questions did not wait on a fetch
        fail[0] = True                                # Open-Meteo down at the next refresh:
        clock[0] = 1080
        weather.refresh()
        self.assertNotIn("error", weather.get())      # the copy still valid is kept
        clock[0] = 1200                               # and expires as before
        self.assertIn("error", weather.get())

    def test_location_is_looked_up_again_before_the_hour_is_up(self):
        clock = [0.0]
        calls = []
        fail = [False]

        def fetch(*args):
            calls.append(clock[0])
            if fail[0]:
                raise requests.Timeout()
            return IP
        location = Location(fetch, lambda: clock[0])
        location.get()
        clock[0] = 3240                               # prewarm's refresh, before 3600
        location.refresh()
        clock[0] = 3700
        self.assertEqual(location.get()["city"], "Sampletown")
        self.assertEqual(calls, [0.0, 3240])          # no lookup for the question
        fail[0] = True
        clock[0] = 6480
        location.refresh()
        self.assertEqual(location.get()["city"], "Sampletown")   # a failed lookup keeps the valid copy
        location.confirmed = {**CITY, "city": "Elsewhere"}
        location.refresh()                            # a city the user gave needs no lookup
        self.assertEqual(len(calls), 3)

    def test_prewarm_keeps_location_and_weather_fresh(self):
        clock = [0.0]
        waits = []

        class Ticks:                                  # stands in for the stop event
            def wait(self, seconds):
                waits.append(seconds)
                clock[0] += seconds
                return len(waits) > 12               # 12 refreshes (108 minutes), then stop
        weather, location = Mock(), Mock()
        weather.refresh.side_effect = [RuntimeError("offline")] + [None] * 11   # one bad refresh
        with patch.object(gemini_tools, "weather", weather), patch.object(gemini_tools, "location", location), \
                patch.object(gemini_tools.monitor, "activity") as activity:
            gemini_tools._keep_warm(Ticks(), clock=lambda: clock[0])
        weather.get.assert_called_once_with()         # at start, shown in the monitor
        self.assertEqual(activity.call_count, 2)      # routine refreshes add no monitor events
        self.assertEqual(set(waits), {540})
        self.assertEqual(weather.refresh.call_count, 12)
        self.assertEqual(location.refresh.call_count, 2)   # at 54 and 108 minutes

    def test_prewarm_starts_one_refresh_thread(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1"}), \
                patch.object(gemini_tools, "_warm_stop", stop), patch.object(gemini_tools, "_warm_thread", None), \
                patch.object(gemini_tools.family, "start"), \
                patch.object(gemini_tools, "_keep_warm", lambda event: event.wait(5)):
            gemini_tools.prewarm()
            first = gemini_tools._warm_thread
            gemini_tools.prewarm()                    # run() starting again after an error
            self.assertIs(gemini_tools._warm_thread, first)
            self.assertTrue(first.is_alive())
            stop.set()
            first.join(2)

    def test_weather_failure_has_no_invented_conditions(self):
        weather = Weather(Location(lambda *args: IP), lambda *args: {})
        self.assertIn("error", weather.get())
        self.assertNotIn("current", weather.get())

    def test_other_city_weather_does_not_move_user(self):
        location = Location(lambda url, params: {"results": [CITY]} if 'geocoding' in url else IP)
        weather = Weather(location, lambda *args: WEATHER)
        self.assertEqual(weather.get(city="Elsewhere")["location"]["latitude"], 41.)
        self.assertIsNone(location.confirmed)
        self.assertEqual(location.get()["city"], "Sampletown")

    def test_invalid_coordinates_are_rejected(self):
        self.assertIn("error", Location(lambda *args: {**IP, "latitude": float('nan')}).get())

    def test_disabled_toolkit_preserves_original_config(self):
        # The eye and voice-volume tools are core (they stay with the toolkit off): off here.
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "0", "BAYMAX_VOICE_VOLUME_TOOL": "0"}), \
                patch.object(gemini_tools.monitor, 'eye_expression_handler', None):
            config = {"system_instruction": "base"}
            result = gemini_tools.extend_config(config)
        # Only the persona's voice is applied; no toolkit tools or instructions.
        self.assertEqual(config, {"system_instruction": "base"})
        self.assertEqual(result["system_instruction"], "base")
        self.assertNotIn("tools", result)

    def test_google_search_is_on_by_default_as_the_search_web_tool(self):
        # Gemini's built-in google_search tool next to Ember's function tools closed the
        # session (1011) on a tool call unless Gemini could think, which cost every reply about
        # a second. search_web runs the search in a session of its own (web_search.py), so
        # the conversation keeps no thinking (live eval, 2026-11-07).
        import os
        from runtime_monitor import _search_status
        base = {"system_instruction": "base", "tools": [], "thinking_config": {"thinking_budget": 0}}
        for value, on in ((None, True), ("1", True), ("yes", True), ("0", False)):
            env = {key: v for key, v in os.environ.items() if key != "BAYMAX_GOOGLE_SEARCH"}
            if value is not None:
                env["BAYMAX_GOOGLE_SEARCH"] = value
            with self.subTest(value=value), patch.dict('os.environ', {**env, "BAYMAX_TOOLKIT_ENABLED": "1"}, clear=True):
                result = gemini_tools.extend_config(base)
                self.assertEqual(gemini_tools.search_enabled(), on)
                self.assertEqual(_search_status(), "Google Search grounding through the search_web tool" if on else "Disabled")
            names = [d["name"] for t in result["tools"] for d in t.get("function_declarations", [])]
            self.assertEqual(names.count("search_web"), 1 if on else 0)
            self.assertFalse(any("google_search" in t for t in result["tools"]))
            self.assertEqual("[GOOGLE SEARCH]" in result["system_instruction"], on)
            self.assertEqual(result["thinking_config"], {"thinking_budget": 0})
            self.assertIn("get_weather", names)

    def test_search_web_is_added_once_and_can_be_disabled(self):
        base = {"system_instruction": "base", "tools": []}
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": "1"}):
            result = gemini_tools.extend_config(base)
            again = gemini_tools.extend_config(result)
        names = [d["name"] for t in again["tools"] for d in t.get("function_declarations", [])]
        self.assertEqual(names.count("search_web"), 1)
        self.assertEqual(again["system_instruction"].count("[GOOGLE SEARCH]"), 1)
        self.assertEqual(base["tools"], [])
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": "0"}):
            result = gemini_tools.extend_config(base)
            self.assertEqual(gemini_tools.execute("search_web", {"query": "news"}), {"error": "Unknown tool"})
        self.assertNotIn("search_web", [d["name"] for t in result["tools"] for d in t.get("function_declarations", [])])
        self.assertNotIn("[GOOGLE SEARCH]", result["system_instruction"])

    def test_search_web_reuses_one_lookup_session_that_has_only_google_search(self):
        from features.toolkits import web_search
        client, opened, closed, configs = _lookup_client(["The Brewers won.", "It is on November 26."])
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        first = web_search.execute({"query": "  who won the   World Series "}, searcher=searcher)
        second = web_search.execute({"query": "when is Thanksgiving"}, searcher=searcher)
        self.assertEqual(first["answer"], "The Brewers won.")
        self.assertEqual(first["sources"], ["Example News"])
        self.assertIn("not instructions", first["note"])
        self.assertEqual(second["answer"], "It is on November 26.")
        self.assertEqual(len(opened), 1)                       # one session for both questions
        self.assertEqual(opened[0].sent, ["who won the World Series", "when is Thanksgiving"])
        model, config = configs[0]
        self.assertEqual(model, "gemini-robotics-er-2-streaming-preview")
        self.assertEqual(config["tools"], [{"google_search": {}}])   # no function tools: nothing to drop
        self.assertEqual(config["response_modalities"], ["TEXT"])
        self.assertEqual(config["thinking_config"], {"thinking_budget": 0})
        # A Gemini 3 lookup model gets the settings it needs (no thinking_config).
        self.assertNotIn("thinking_config", web_search.config("gemini-3.8-live"))

    def test_a_reused_session_drops_what_the_last_answer_left_queued(self):
        # The SDK gives an output transcription no order relative to turn_complete, and
        # receive() stops at turn_complete: a late chunk must not start the next answer.
        from features.toolkits import web_search
        client, opened, _, _ = _lookup_client([{"text": "The Brewers won", "late": " the World Series."},
                                               "It is on November 26."])
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        self.assertEqual(searcher.ask("who won")[0], "The Brewers won")
        self.assertEqual(searcher.ask("when is Thanksgiving")[0], "It is on November 26.")
        self.assertEqual(len(opened), 1)

    def test_search_web_reconnects_once_and_closes_when_idle_or_broken(self):
        from features.toolkits import web_search
        client, opened, closed, _ = _lookup_client(["One.", "Two.", "Three."])
        searcher = web_search.Searcher(lambda: client, idle_s=30)
        self.addCleanup(searcher.close)
        self.assertEqual(searcher.ask("first")[0], "One.")
        opened[0].gone = True                                  # Google closed it meanwhile
        searcher._idle_s = 0.05
        self.assertEqual(searcher.ask("second")[0], "Two.")   # one fresh try
        self.assertEqual(len(opened), 2)
        self.assertTrue(_soon(lambda: opened[0] in closed))
        self.assertTrue(_soon(lambda: opened[1] in closed))    # closed after idle_s without a search
        self.assertEqual(searcher.ask("third")[0], "Three.")
        self.assertEqual(len(opened), 3)
        # A fresh session that fails is not retried, and is closed.
        client, opened, closed, _ = _lookup_client([RuntimeError("1011 internal error")])
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        with patch("builtins.print"):
            self.assertEqual(web_search.execute({"query": "q"}, searcher=searcher),
                             {"error": "Search is unavailable right now; say you could not check it"})
        self.assertEqual(len(opened), 1)
        self.assertTrue(_soon(lambda: closed == opened))

    def test_search_web_falls_back_to_the_conversation_model_when_the_text_model_is_refused(self):
        # The text model's free-tier quota ran out after 25 sessions in a morning; the
        # conversation's native-audio model still answers, from the first sentence of its
        # transcript (it speaks at talking pace), and that session is closed right away.
        from features.toolkits import web_search
        quota = "1011 None. You exceeded your current quota"
        client, opened, closed, configs = _lookup_client(
            ["The Brewers won it. They beat the Rays in five games."], refuse={web_search.MODEL: quota})
        searcher = web_search.Searcher(lambda: client, spoken_model_name="gemini-2.5-flash-native-audio-x")
        self.addCleanup(searcher.close)
        with patch("builtins.print") as printed, patch.object(web_search, "_told", set()):
            result = web_search.execute({"query": "who won the World Series"}, searcher=searcher)
        self.assertEqual(result["answer"], "The Brewers won it.")
        self.assertEqual([m for m, _ in configs], [web_search.MODEL, "gemini-2.5-flash-native-audio-x"])
        spoken = configs[1][1]
        self.assertEqual((spoken["response_modalities"], spoken["output_audio_transcription"]), (["AUDIO"], {}))
        self.assertEqual(spoken["tools"], [{"google_search": {}}])
        self.assertTrue(_soon(lambda: closed == opened))       # closed after the first sentence
        self.assertIn("refused a lookup session", str(printed.call_args_list))
        self.assertNotIn("World Series", str(printed.call_args_list))   # the query is never logged
        for text, sentence in (("The U.S. Census says 1.5 million. More text", "The U.S. Census says 1.5 million."),
                               ("Dr. Smith won the prize in 2026! Next", "Dr. Smith won the prize in 2026!"),
                               ("It is open until 5 p.m. today. And", "It is open until 5 p.m. today."),
                               ("Short. And then a longer one", None), ("No sentence end yet", None)):
            end = web_search.first_sentence_end(text)
            self.assertEqual(text[:end].strip() if end else None, sentence)
        # Gemini 3 models get the settings they need (no thinking_config).
        self.assertNotIn("thinking_config", web_search.spoken_config("gemini-3.8-live"))
        with patch("features.conversational_ability.live_models.in_use", None):
            self.assertEqual(web_search.spoken_model(), web_search.AUDIO_MODEL)
        with patch("features.conversational_ability.live_models.in_use", "gemini-3.8-live"):
            self.assertEqual(web_search.spoken_model(), "gemini-3.8-live")
        # A text model that is gone (1008) falls back too; a slow or broken lookup does not.
        self.assertTrue(web_search.refused(RuntimeError(quota)))
        self.assertTrue(web_search.refused(type("Gone", (Exception,), {"code": 1008})("not found")))
        self.assertFalse(web_search.refused(TimeoutError()))
        self.assertFalse(web_search.refused(RuntimeError("1011 internal error")))

    def test_the_fallback_answers_once_generation_is_done_and_keeps_a_whole_sentence_at_the_deadline(self):
        import time
        from features.toolkits import web_search
        refuse = {web_search.MODEL: "You exceeded your current quota"}
        sentence = "The Brewers won the 2026 World Series."
        cases = (({"text": sentence, "generation_complete": True, "turn_complete": False}, sentence, 0.0, 0.8),
                 ({"text": sentence, "turn_complete": False}, sentence, 1.0, 2.2),   # stalls: kept at the deadline
                 ({"text": "The Brewers won the", "turn_complete": False}, None, 1.0, 2.2),
                 (None, None, 1.0, 2.2))                                         # never answers
        for answer, expected, at_least, at_most in cases:
            with self.subTest(answer=answer), patch.object(web_search, "TOTAL_S", 1.2), patch("builtins.print"):
                client, opened, closed, _ = _lookup_client([answer], refuse=refuse)
                searcher = web_search.Searcher(lambda: client, spoken_model_name="native-audio")
                started = time.monotonic()
                result = web_search.execute({"query": "who won"}, searcher=searcher)
                took = time.monotonic() - started
                searcher.close()
                self.assertEqual(result.get("answer"), expected)
                if expected is None:
                    self.assertEqual(result, {"error": "The search took too long; say you could not check it just now"})
                self.assertTrue(at_least <= took <= at_most, took)   # one deadline for the whole search
                self.assertEqual(closed, opened)

    def test_the_fallback_reads_sentences_in_any_script_and_waits_out_numbers_and_abbreviations(self):
        from features.toolkits import web_search
        # Mid-answer, a sentence ends at . ! ? (or । 。) with more text after it; never at a point
        # after a digit (an ordinal date or a decimal) or an abbreviation.
        for text, sentence in (("Die Brewers haben am 1. November 2026 die World Series gewonnen. Sie",
                                "Die Brewers haben am 1. November 2026 die World Series gewonnen."),
                               ("Der Louvre hat heute, am 7. November", None),
                               ("Les Brewers ont gagné la Série mondiale cette année. Ils",
                                "Les Brewers ont gagné la Série mondiale cette année."),
                               ("Les Brewers ont gagné la Série mondiale 2026. Ils", None),   # waits for the end
                               ("ब्रूअर्स ने 2026 की वर्ल्ड सीरीज़ जीती। उन्होंने", "ब्रूअर्स ने 2026 की वर्ल्ड सीरीज़ जीती।"),
                               ("ブルワーズが2026年のワールドシリーズで優勝しました。次は", "ブルワーズが2026年のワールドシリーズで優勝しました。"),
                               ("Tokyo's population is about 14. ", None)):
            end = web_search.first_sentence_end(text)
            self.assertEqual(text[:end].strip() if end else None, sentence)
        self.assertEqual(web_search.complete_sentence("ब्रूअर्स ने 2026 की वर्ल्ड सीरीज़ जीती।"),
                         "ब्रूअर्स ने 2026 की वर्ल्ड सीरीज़ जीती।")
        # Once generation is done, the whole transcript is the answer when it has gone quiet:
        # endings in a number, an abbreviation or a script with no full stop, and a decimal split
        # across two transcript chunks.
        refuse = {web_search.MODEL: "You exceeded your current quota"}
        for chunks in (["The museum is open today until 6 p.m."], ["Spain won 2-1."],
                       ["Tokyo's population is about 14.", "2 million as of 2026."],
                       ["บริวเวอร์สชนะเวิลด์ซีรีส์ปี 2026"]):
            with self.subTest(chunks=chunks), patch("builtins.print"):
                client, _, _, _ = _lookup_client([{"chunks": chunks, "generation_complete": True,
                                                   "turn_complete": False}], refuse=refuse)
                searcher = web_search.Searcher(lambda: client, spoken_model_name="native-audio")
                result = web_search.execute({"query": "q"}, searcher=searcher)
                searcher.close()
                self.assertEqual(result.get("answer"), "".join(chunks))

    def test_a_stalled_setup_ends_on_time_and_closes_in_the_background(self):
        # Cancelling a stalled setup runs the SDK's websocket close (up to 10 s); it must not
        # hold the search past its time limit.
        import time
        from features.toolkits import web_search
        cleaned = []

        class Stalled:
            async def __aenter__(self):
                try:
                    await asyncio.Event().wait()             # the upgrade went through, then nothing
                except asyncio.CancelledError:
                    await asyncio.sleep(0.6)                 # the close handshake times out
                    cleaned.append(time.monotonic())
                    raise

        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda model, config: Stalled())))
        searcher = web_search.Searcher(lambda: client)
        started = time.monotonic()
        with patch.object(web_search, "TIMEOUT_S", 0.2), patch("builtins.print"):
            result = web_search.execute({"query": "q"}, searcher=searcher)
        took = time.monotonic() - started
        self.assertEqual(result, {"error": "The search took too long; say you could not check it just now"})
        self.assertLess(took, 0.5)                               # not 0.2 s + the 0.6 s close
        self.assertTrue(_soon(lambda: cleaned))                  # the close still finished
        searcher.close()

    def test_a_search_waiting_for_an_earlier_one_keeps_its_own_deadline(self):
        import time
        from features.toolkits import web_search
        client, _, _, _ = _lookup_client([None])                # the first search never answers
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        first = threading.Thread(target=lambda: self.assertRaises(TimeoutError, searcher.ask, "a", 1.5))
        with patch.object(web_search, "TIMEOUT_S", 1.5), patch("builtins.print"):
            first.start()
            time.sleep(0.1)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                searcher.ask("b", 0.5)                           # waits for the lock, not past 0.5 s
            self.assertLess(time.monotonic() - started, 1.0)
            first.join(5)

    def test_search_web_failures_become_short_errors(self):
        from features.toolkits import web_search
        self.assertEqual(web_search.execute({"query": "  "}), {"error": "No search query"})
        with patch.object(web_search, "_searcher", None), patch.dict('os.environ', {"GOOGLE_API_KEY": ""}):
            self.assertEqual(web_search.execute({"query": "news"}), {"error": "Search is not set up on this robot"})
        client, opened, closed, _ = _lookup_client([], refuse="1011 None. You exceeded your current quota")
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        with patch("builtins.print") as printed, patch.object(web_search, "_told", set()):
            self.assertEqual(web_search.execute({"query": "news"}, searcher=searcher),
                             {"error": "Search is busy for a minute; say you could not check it just now"})
            web_search.execute({"query": "news"}, searcher=searcher)
        told = [str(c) for c in printed.call_args_list]
        self.assertEqual(len(told), 2)                         # each kind of failure once: refused, then the fallback's
        self.assertTrue(all("[SEARCH]" in line and "news" not in line for line in told))
        client, opened, closed, _ = _lookup_client([None])     # never answers
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        with patch.object(web_search, "TIMEOUT_S", 0.1), patch("builtins.print"):
            self.assertEqual(web_search.execute({"query": "news"}, searcher=searcher),
                             {"error": "The search took too long; say you could not check it just now"})
        self.assertTrue(_soon(lambda: closed == opened))       # a half-read turn is not reused
        client, opened, closed, _ = _lookup_client([""])
        searcher = web_search.Searcher(lambda: client)
        self.addCleanup(searcher.close)
        self.assertEqual(web_search.execute({"query": "news"}, searcher=searcher), {"error": "The search found no answer"})
        for value, model in (("gemini-other-live", "gemini-other-live"), ("bad model/..", web_search.MODEL),
                             ("", web_search.MODEL)):
            with self.subTest(value=value), patch.dict('os.environ', {"BAYMAX_SEARCH_MODEL": value}):
                self.assertEqual(web_search.model(), model)

    def test_search_web_calls_reach_web_search_and_are_monitored_by_name(self):
        from features.toolkits import web_search
        answer = {"answer": "The Brewers won."}
        session = SimpleNamespace(send_tool_response=AsyncMock())
        calls = SimpleNamespace(function_calls=[SimpleNamespace(name="search_web", id="s-1", args={"query": "who won"})])
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": "1"}), \
                patch.object(web_search, "execute", return_value=answer) as run:
            self.assertEqual(gemini_tools.execute("search_web", {"query": "who won"}), answer)
            with patch.object(gemini_tools.monitor, "activity") as activity:
                asyncio.run(gemini_tools.handle_tool_call(session, calls))
        run.assert_called_with({"query": "who won"})
        reply = session.send_tool_response.call_args.kwargs["function_responses"][0]
        self.assertEqual((reply.id, reply.response), ("s-1", answer))
        activity.assert_any_call("search_web")

    def test_show_ideas_offer_google_search_only_when_it_is_on(self):
        # The public tools' instruction offered "(or Google Search)" with the search tool off too.
        from features.toolkits import experimental
        base = {"system_instruction": "base", "tools": []}
        for search, offered in (("0", False), ("1", True)):
            with self.subTest(search=search), patch.dict('os.environ', {
                    "BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_DISCOVERY_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": search}):
                result = gemini_tools.extend_config(base)
                self.assertIn("[EXPERIMENTAL PUBLIC TOOLS]", result["system_instruction"])
                self.assertEqual("own\nknowledge (or search_web);" in result["system_instruction"], offered)
                self.assertEqual("Google Search" in result["system_instruction"], offered)
                self.assertEqual(experimental.extend_config(result), result)  # added once

    def test_location_result_points_weather_questions_at_get_weather(self):
        # Gemini sometimes answered the weather after get_location alone, inventing conditions.
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1"}), \
                patch.object(gemini_tools.location, 'get', return_value={"city": "Sampletown"}):
            result = gemini_tools.execute("get_location", {})
        self.assertEqual(result["city"], "Sampletown")
        self.assertIn("get_weather", result["note"])
        declarations = {d["name"]: d["description"] for d in gemini_tools.DECLARATIONS}
        self.assertIn("Not needed for weather", declarations["get_location"])
        self.assertIn("call it directly", declarations["get_weather"])
        self.assertIn("call\nget_weather directly", gemini_tools.INSTRUCTION)

    def test_tool_response_keeps_gemini_call_id(self):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = SimpleNamespace(function_calls=[SimpleNamespace(name="get_location", id="call-42", args={})])
        with patch.object(gemini_tools, 'execute', return_value={"city": "Sampletown"}):
            asyncio.run(gemini_tools.handle_tool_call(session, call))
        reply = session.send_tool_response.call_args.kwargs['function_responses'][0]
        self.assertEqual(reply.id, "call-42")
        self.assertEqual(reply.response, {"city": "Sampletown"})


if __name__ == '__main__':
    unittest.main()
