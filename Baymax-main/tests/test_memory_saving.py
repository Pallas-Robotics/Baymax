"""Memory saving in realtime_gemini_10: summaries during uptime, a bounded stop path,
the newest memories in the prompt and the initiation hint off the event loop.
Fakes stand in for Gemini and the memory store; one test uses a real ChromaDB
where it is installed."""
import asyncio
import importlib.util
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from features.conversational_ability import live_models
from test_public_core_integration import CORE, load_function

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now


def runtime(*names, **overrides):
    """The named runtime functions sharing one namespace of transcript state."""
    ns = {
        "time": time, "threading": threading, "uuid": uuid, "os": os,
        "_transcript": [], "_transcript_saved": 0, "_transcript_claimed": 0,
        "_transcript_unsaved_chars": 0, "_transcript_last_ts": 0.0,
        "_transcript_lock": threading.Lock(), "_transcript_user": [],
        "_VOICE_ANALYSIS_TRANSCRIPT": False,
        "MEMORY_SAVE_USER_TURNS": 3, "MEMORY_SAVE_CHARS": 500, "MEMORY_SAVE_IDLE_SECONDS": 180.0,
        "MEMORY_CONTEXT_UTTERANCES": 2, "MEMORY_CONTEXT_CHARS": 10,
        "MEMORY_MAX_UNSAVED_CHARS": 1000, "MEMORY_PROMPT_COUNT": 50,
    }
    ns.update(overrides)
    for name in names:
        load_function(name, ns)
    return ns


SAVING = ("_transcript_add", "_format_transcript", "_keep_transcript_unsaved",
          "_mark_transcript_saved", "_memory_save_due", "_summarise_and_embed", "_summarise_claimed")


class FakeStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = []

    def save(self, texts, ids=None, metadatas=None):
        if self.fail:
            raise RuntimeError("disk full")
        self.saved.append((list(texts), list(ids), list(metadatas)))


class FakeGemini:
    """Stands in for _generate_summary: records each prompt, replies in turn."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.timeouts = []

    def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def saving(gemini, store=None, **overrides):
    directory = tempfile.mkdtemp()
    ns = runtime(*SAVING, _generate_summary=gemini, _memory_store=lambda: store,
                 _TRANSCRIPT_FILE=os.path.join(directory, "transcript_20261028_090000.txt"), **overrides)
    ns["tmp"] = directory
    return ns


def say(ns, *lines):
    """Utterances as Gemini's transcription delivers them: fragments with their spacing."""
    for speaker, text in lines:
        for word in re.findall(r"\s*\S+", text):
            ns["_transcript_add"](speaker, word)


class TranscriptTests(unittest.TestCase):
    def test_fragments_join_into_utterances_in_order(self):
        ns = runtime("_transcript_add", "_format_transcript")
        for speaker, text in [("user", "Hi"), ("user", " A"), ("user", "mber,"), ("gemini", " Hello"),
                              ("gemini", " there!"), ("user", " I'm"), ("user", " Joe."), ("user", "  ")]:
            ns["_transcript_add"](speaker, text)
        self.assertEqual(ns["_format_transcript"](ns["_transcript"]),
                         "User: Hi Amber,\nGemini: Hello there!\nUser: I'm Joe.")
        self.assertEqual(ns["_transcript_unsaved_chars"], len("Hi Amber, Hello there! I'm Joe."))
        self.assertEqual(ns["_transcript_user"], [])  # voice analysis off (the MVP launcher)

    def test_voice_analysis_keeps_the_users_fragments(self):
        ns = runtime("_transcript_add", _VOICE_ANALYSIS_TRANSCRIPT=True)
        ns["_transcript_add"]("user", " my name")
        ns["_transcript_add"]("gemini", " Nice")
        self.assertEqual(ns["_transcript_user"], ["my name"])

    def test_an_utterance_being_summarised_is_not_extended(self):
        ns = runtime("_transcript_add")
        ns["_transcript_add"]("user", "I like")
        ns["_transcript_claimed"] = 1  # a save took it
        ns["_transcript_add"]("user", " tea")
        self.assertEqual(ns["_transcript"], [["user", "I like"], ["user", " tea"]])

    def test_unsaved_text_is_capped_while_nothing_is_saved(self):
        ns = runtime("_transcript_add", MEMORY_MAX_UNSAVED_CHARS=100)
        with patch("builtins.print") as printed:
            for i in range(30):
                ns["_transcript_add"]("user" if i % 2 else "gemini", f"utterance {i:02d}")
        self.assertLessEqual(ns["_transcript_unsaved_chars"], 100)
        self.assertEqual(ns["_transcript_unsaved_chars"], sum(len(t) for _, t in ns["_transcript"]))
        self.assertEqual(ns["_transcript"][-1], ["user", "utterance 29"])
        self.assertIn("Dropped the oldest", str(printed.call_args))

    def test_one_endless_utterance_is_cut_from_its_start(self):
        # People talking among themselves: user speech, no replies, no saves.
        ns = runtime("_transcript_add", MEMORY_MAX_UNSAVED_CHARS=100)
        with patch("builtins.print"):
            for i in range(50):
                ns["_transcript_add"]("user", f" w{i:02d}")
        self.assertEqual(len(ns["_transcript"]), 1)
        self.assertLessEqual(len(ns["_transcript"][0][1]), 100)
        self.assertTrue(ns["_transcript"][0][1].endswith(" w49"))


class SaveDueTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ns = runtime("_transcript_add", "_memory_save_due", time=self.clock)

    def due(self, after=0.0):
        return self.ns["_memory_save_due"](self.clock.now + after)

    def test_nothing_said_is_never_due(self):
        self.assertIsNone(self.due(after=10_000))

    def test_after_enough_user_turns(self):
        say(self.ns, ("user", "one"), ("gemini", "a"), ("user", "two"), ("gemini", "b"))
        self.assertIsNone(self.due())
        say(self.ns, ("user", "three"))
        self.assertEqual(self.due(), "3 user turns")

    def test_after_a_lot_of_text(self):
        say(self.ns, ("user", "word " * 120))  # 599 characters as fragments
        self.assertEqual(self.due(), "599 characters")

    def test_after_a_quiet_spell(self):
        say(self.ns, ("user", "hello"))
        self.assertIsNone(self.due(after=179))
        self.assertEqual(self.due(after=181), "quiet for 181 s")


class SummariseAndEmbedTests(unittest.TestCase):
    def run_save(self, ns, deadline=None):
        with patch("builtins.print"):
            return ns["_summarise_and_embed"](time.monotonic() + 20 if deadline is None else deadline)

    def test_saves_only_whats_new_with_a_short_context_tail(self):
        gemini = FakeGemini("User's name is Joe\nJoe likes green tea", "NOTHING_TO_REMEMBER")
        store = FakeStore()
        ns = saving(gemini, store)
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "Hi, I'm Joe."), ("gemini", "Hello Joe!"),
            ("user", "I really like green tea."), ("gemini", "Green tea is lovely."))
        self.assertTrue(self.run_save(ns))

        self.assertEqual(len(gemini.prompts), 1)
        self.assertIn("--- TRANSCRIPT START ---\nUser: Hi, I'm Joe.\nGemini: Hello Joe!\n"
                      "User: I really like green tea.\nGemini: Green tea is lovely.\n--- TRANSCRIPT END ---",
                      gemini.prompts[0])
        self.assertNotIn("EARLIER CONTEXT", gemini.prompts[0])
        # The memory embedder (all-MiniLM-L6-v2) is English-only.
        self.assertIn("Write each line in English", gemini.prompts[0])
        (texts, ids, metadatas), = store.saved
        self.assertEqual(texts, ["User's name is Joe", "Joe likes green tea"])
        self.assertEqual([m["line_index"] for m in metadatas], ["0", "1"])
        self.assertRegex(metadatas[0]["timestamp"], r"^\d{8}_\d{6}$")
        self.assertEqual(metadatas[0]["source"], "conversation_summary")
        # Saved: only the last two utterances stay, cut short, as context.
        self.assertEqual(ns["_transcript"], [["user", "green tea."], ["gemini", "is lovely."]])
        self.assertEqual((ns["_transcript_saved"], ns["_transcript_unsaved_chars"]), (2, 0))
        with open(ns["_TRANSCRIPT_FILE"]) as f:
            self.assertIn("User: I really like green tea.", f.read())

        say(ns, ("user", "Bye for now."))
        self.assertTrue(self.run_save(ns))
        second = gemini.prompts[1]
        self.assertIn("--- EARLIER CONTEXT START ---\nUser: green tea.\nGemini: is lovely.\n"
                      "--- EARLIER CONTEXT END ---", second)
        self.assertIn("--- TRANSCRIPT START ---\nUser: Bye for now.\n--- TRANSCRIPT END ---", second)
        self.assertNotIn("Joe", second)
        self.assertEqual(len(store.saved), 1)  # nothing to remember the second time
        self.assertEqual(ns["_transcript_saved"], 2)

    def test_failed_summary_keeps_everything_for_the_next_try(self):
        gemini = FakeGemini(TimeoutError("no reply within 20 s"), "Joe is 80")
        store = FakeStore()
        ns = saving(gemini, store)
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "I'm Joe, 80 years old."))
        self.assertFalse(self.run_save(ns))
        self.assertEqual((ns["_transcript_saved"], ns["_transcript_claimed"]), (0, 0))
        self.assertEqual(store.saved, [])
        say(ns, ("user", " Still here."))  # extends the unsaved utterance again
        self.assertTrue(self.run_save(ns))
        self.assertEqual(gemini.prompts[0].split("--- TRANSCRIPT START ---")[1],
                         "\nUser: I'm Joe, 80 years old.\n--- TRANSCRIPT END ---")
        self.assertIn("User: I'm Joe, 80 years old. Still here.", gemini.prompts[1])
        self.assertEqual(store.saved[0][0], ["Joe is 80"])

    def test_store_failure_keeps_the_transcript(self):
        ns = saving(FakeGemini("Joe is 80"), FakeStore(fail=True))
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "I'm 80."))
        self.assertFalse(self.run_save(ns))
        self.assertEqual(ns["_transcript_saved"], 0)
        self.assertFalse(os.path.exists(ns["_TRANSCRIPT_FILE"]))

    def test_speech_during_a_save_waits_for_the_next_one(self):
        ns = saving(None, FakeStore())
        self.addCleanup(shutil.rmtree, ns["tmp"])

        def gemini(prompt, timeout):
            say(ns, ("user", " and coffee"), ("gemini", " Noted."))  # arrives mid-call
            return "Joe likes tea"
        ns["_generate_summary"] = gemini
        say(ns, ("user", "I like tea"))
        self.assertTrue(self.run_save(ns))
        unsaved = ns["_transcript"][ns["_transcript_saved"]:]
        self.assertEqual(unsaved, [["user", " and coffee"], ["gemini", " Noted."]])
        self.assertEqual(ns["_transcript_unsaved_chars"], len(" and coffee Noted."))

    def test_an_unexpected_error_releases_the_claim(self):
        ns = saving(FakeGemini(), FakeStore(), MEMORY_MAX_UNSAVED_CHARS=100)
        self.addCleanup(shutil.rmtree, ns["tmp"])

        def store():
            raise PermissionError("chroma_store")
        ns["_memory_store"] = store
        say(ns, ("user", "Hello"))
        with patch("builtins.print") as printed:
            self.assertFalse(ns["_summarise_and_embed"](time.monotonic() + 20))
        self.assertIn("Saving memories failed", str(printed.call_args))
        self.assertEqual((ns["_transcript_saved"], ns["_transcript_claimed"]), (0, 0))
        # The cap works again, and the utterance is still there to save.
        with patch("builtins.print"):
            say(ns, ("gemini", "x" * 200))
        self.assertLessEqual(ns["_transcript_unsaved_chars"], 100)

    def test_nothing_from_the_user_sends_nothing(self):
        gemini = FakeGemini()
        ns = saving(gemini, FakeStore())
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("gemini", "Good morning! How did you sleep?"))  # an opener nobody answered
        self.assertTrue(self.run_save(ns))
        self.assertEqual(gemini.prompts, [])
        self.assertEqual(ns["_transcript_saved"], 1)
        self.assertTrue(self.run_save(ns))  # and with nothing at all

    def test_no_memory_store_means_no_transcript_to_gemini(self):
        gemini = FakeGemini()
        ns = saving(gemini, store=None)
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "My daughter is Amy."))
        self.assertFalse(self.run_save(ns))
        self.assertEqual(gemini.prompts, [])
        self.assertEqual(ns["_transcript_saved"], 0)

    def test_empty_or_blocked_reply_counts_as_nothing_to_remember(self):
        store = FakeStore()
        ns = saving(FakeGemini(None), store)
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "Hello"))
        self.assertTrue(self.run_save(ns))
        self.assertEqual((store.saved, ns["_transcript_saved"]), ([], 1))

    def test_past_deadline_fails_without_calling_gemini(self):
        gemini = FakeGemini()
        ns = saving(gemini, FakeStore())
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "Hello"))
        self.assertFalse(self.run_save(ns, deadline=time.monotonic() + 0.5))
        self.assertEqual(gemini.prompts, [])

    def test_gemini_gets_the_time_left_until_the_deadline(self):
        gemini = FakeGemini("NOTHING_TO_REMEMBER")
        ns = saving(gemini, FakeStore())
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "Hello"))
        self.run_save(ns, deadline=time.monotonic() + 6)
        self.assertTrue(5 < gemini.timeouts[0] <= 6)

    def test_two_saves_in_one_second_keep_both(self):
        store = FakeStore()
        ns = saving(FakeGemini("Fact one", "Fact two"), store,
                    time=SimpleNamespace(monotonic=time.monotonic, strftime=lambda f: "20261028_120000"))
        self.addCleanup(shutil.rmtree, ns["tmp"])
        say(ns, ("user", "one"))
        self.run_save(ns)
        say(ns, ("user", "two"))
        self.run_save(ns)
        first, second = store.saved[0][1], store.saved[1][1]
        self.assertNotEqual(first, second)
        self.assertTrue(first[0].startswith("memory_20261028_120000_"))


class GenerateSummaryTests(unittest.TestCase):
    def namespace(self, generate):
        clients = []

        class Client:
            def __init__(self, api_key, http_options):
                clients.append(http_options)
                self.models = SimpleNamespace(generate_content=generate)
        ns = {"threading": threading, "API_KEY": "test-key", "SUMMARY_MODEL": "gemini-3.5-flash",
              "genai": SimpleNamespace(Client=Client), "clients": clients}
        load_function("_generate_summary", ns)
        return ns

    def test_reply_text_and_request_timeout(self):
        ns = self.namespace(lambda model, contents: SimpleNamespace(text=f"{model}: {contents}"))
        self.assertEqual(ns["_generate_summary"]("prompt", 7.5), "gemini-3.5-flash: prompt")
        self.assertEqual(ns["clients"], [{"api_version": "v1alpha", "timeout": 7500}])

    def test_errors_reach_the_caller(self):
        def generate(model, contents):
            raise ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            self.namespace(generate)["_generate_summary"]("prompt", 5)

    def test_a_stalled_call_is_abandoned_at_the_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        ns = self.namespace(lambda model, contents: release.wait(30))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            ns["_generate_summary"]("prompt", 0.2)
        self.assertLess(time.monotonic() - started, 2)


class SaverThreadTests(unittest.TestCase):
    def test_saves_when_due_and_waits_after_a_failure(self):
        clock = Clock()
        ticks = iter(range(8))

        class Shutdown:
            def wait(self, timeout):
                clock.now += timeout
                return next(ticks, None) is None

            def is_set(self):
                return False
        results = iter([False])  # the first save fails, later ones succeed
        calls = []

        def summarise(deadline):
            calls.append((clock.now, deadline))
            return next(results, True)
        ns = {"time": clock, "_shutdown_event": Shutdown(), "_memory_save_lock": threading.Lock(),
              "_memory_save_due": lambda now: "3 user turns", "_summarise_and_embed": summarise,
              "MEMORY_SUMMARY_TIMEOUT": 20.0, "MEMORY_SAVE_RETRY_SECONDS": 12.0}
        load_function("_memory_saver_thread", ns)
        with patch("builtins.print"):
            ns["_memory_saver_thread"]()
        # Every 5 s while due; after the failed first save, nothing for 12 s.
        self.assertEqual([now for now, _ in calls], [1005.0, 1020.0, 1025.0, 1030.0, 1035.0, 1040.0])
        self.assertEqual(calls[0][1], 1025.0)  # the uptime call gets MEMORY_SUMMARY_TIMEOUT

    def test_an_error_does_not_end_the_saver(self):
        clock = Clock()
        ticks = iter(range(5))

        class Shutdown:
            def wait(self, timeout):
                clock.now += timeout
                return next(ticks, None) is None

            def is_set(self):
                return False
        calls = []

        def summarise(deadline):
            calls.append(clock.now)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            return True
        ns = {"time": clock, "_shutdown_event": Shutdown(), "_memory_save_lock": threading.Lock(),
              "_memory_save_due": lambda now: "due", "_summarise_and_embed": summarise,
              "MEMORY_SUMMARY_TIMEOUT": 20.0, "MEMORY_SAVE_RETRY_SECONDS": 10.0}
        load_function("_memory_saver_thread", ns)
        with patch("builtins.print") as printed:
            ns["_memory_saver_thread"]()
        self.assertEqual(calls, [1005.0, 1015.0, 1020.0, 1025.0])  # nothing for 10 s after the error
        self.assertFalse(ns["_memory_save_lock"].locked())
        self.assertIn("Saver error", " ".join(str(c) for c in printed.call_args_list))

    def test_stops_with_the_shutdown_event(self):
        shutdown = threading.Event()
        shutdown.set()
        summarise = Mock()
        ns = {"time": time, "_shutdown_event": shutdown, "_memory_save_lock": threading.Lock(),
              "_memory_save_due": lambda now: "due", "_summarise_and_embed": summarise}
        load_function("_memory_saver_thread", ns)
        ns["_memory_saver_thread"]()
        summarise.assert_not_called()


class StopPathTests(unittest.TestCase):
    def namespace(self):
        calls = []
        ns = {"time": time, "_memory_save_lock": threading.Lock(),
              "_summarise_and_embed": lambda deadline: calls.append((time.monotonic(), deadline)), "calls": calls}
        load_function("_save_memories_on_stop", ns)
        return ns

    def test_waits_for_a_save_in_progress_then_saves_the_rest(self):
        ns = self.namespace()
        ns["_memory_save_lock"].acquire()
        threading.Timer(0.2, ns["_memory_save_lock"].release).start()
        deadline = time.monotonic() + 5
        ns["_save_memories_on_stop"](deadline)
        (called_at, summary_deadline), = ns["calls"]
        self.assertGreaterEqual(called_at, deadline - 5 + 0.15)
        self.assertEqual(summary_deadline, deadline - 1.0)  # a second to store the lines
        self.assertFalse(ns["_memory_save_lock"].locked())

    def test_gives_up_at_the_deadline(self):
        ns = self.namespace()
        ns["_memory_save_lock"].acquire()
        self.addCleanup(ns["_memory_save_lock"].release)
        started = time.monotonic()
        with patch("builtins.print") as printed:
            ns["_save_memories_on_stop"](started + 0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(ns["calls"], [])
        self.assertIn("Out of time", str(printed.call_args))

    def test_mvp_budget_fits_the_services_stop_timeout(self):
        unit = (ROOT.parent / "devops/mvp/install_autostart.py").read_text(encoding="utf-8")
        stop_timeout = int(re.search(r"TimeoutStopSec=(\d+)", unit).group(1))
        launcher = (ROOT / "run_mvp.py").read_text(encoding="utf-8")
        budget = float(re.search(r'setdefault\("BAYMAX_MEMORY_STOP_SECONDS", "([\d.]+)"\)', launcher).group(1))
        eyes = (ROOT / "features/eyes/controller.py").read_text(encoding="utf-8")
        self.assertIn("self.thread.join(timeout=3)", eyes)  # run_mvp closes the eyes after Ember
        # Counted from the stop's start; a second to store the lines and exit.
        self.assertLessEqual(budget + 3 + 1, stop_timeout)
        self.assertIn('MEMORY_STOP_SECONDS = float(os.getenv("BAYMAX_MEMORY_STOP_SECONDS", "60"))',
                      CORE.read_text(encoding="utf-8"))

    def test_stop_path_saves_only_the_rest_within_the_budget(self):
        main = CORE.read_text(encoding="utf-8").split('if __name__ == "__main__":')[1]
        stop = main[main.index("except (KeyboardInterrupt, asyncio.CancelledError):"):]
        self.assertIn("memory_deadline = (_stop_started_ts or time.monotonic()) + MEMORY_STOP_SECONDS", stop)
        self.assertIn("_save_memories_on_stop(memory_deadline)", stop)
        self.assertNotIn("_summarise_and_embed(", main)
        # The saver runs only where memory is installed (not the desktop profiles).
        self.assertIn("if not missing_packages():\n        threading.Thread(target=_memory_saver_thread", main)


class TeardownTests(unittest.TestCase):
    """run()'s real teardown: the stop clock starts when the session is cancelled."""
    def namespace(self):
        async def forever(*args, **kwargs):
            await asyncio.sleep(3600)

        class Connect:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *exc):
                return False
        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kw: Connect())))
        ns = {"asyncio": asyncio, "time": time, "monitor": Mock(), "public_toolkit": SimpleNamespace(prewarm=Mock()),
              "genai": SimpleNamespace(Client=lambda **kw: client), "API_KEY": "k",
              "_live_models": live_models.ModelChoice(["m"]),  # run()'s Gemini Live model
              "_build_config": dict, "_boot_status": Mock(), "_address_gate": SimpleNamespace(watch=lambda s: s),
              "_memory_embedder": None, "MEMORY_RETRIEVAL_ENABLED": False, "FALL_DETECTION_ENABLED": False,
              "POSE_TRACKING_ENABLED": True, "IDLE_THRESHOLD_SECONDS": 60.0, "F_MIDPOINT": 30.0,
              # run() reads it (opi4-demo); missing, its reconnect loop would swallow the
              # NameError and never return.
              "RANDOM_INITIATION_ENABLED": False,
              "_initiation_ts_lock": threading.Lock(), "_last_robot_speech_ts": 0.0, "_stop_started_ts": 0.0,
              "_shutdown_event": threading.Event(), "start_output_stream": Mock,
              "listen_audio": forever, "send_audio_realtime": forever, "send_video_realtime": forever,
              "receive_audio": forever, "fall_alert_monitor": forever, "initiation_monitor": forever,
              "fall_worker": SimpleNamespace(alert_monitor=forever), "monitor_queues": forever,
              "between_turns": forever,
              # The reconnect fix's per-session steps (tested in test_v10_runtime.ReconnectTests).
              "_send_cut_off_prompts": forever, "_abandon_reply": Mock(), "_abort_connection": Mock()}
        load_function("run", ns)
        return ns

    def test_a_stop_starts_the_clock_and_skips_the_reconnect_pause(self):
        ns = self.namespace()

        async def stop():
            task = asyncio.create_task(ns["run"]())
            await asyncio.sleep(0.05)
            task.cancel()
            cancelled_at = time.monotonic()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return cancelled_at, time.monotonic()
        with patch("builtins.print"):
            cancelled_at, done_at = asyncio.run(stop())
        self.assertLess(done_at - cancelled_at, 1.0)  # no 2 s pause: nothing reconnects
        ns["_abandon_reply"].assert_called_once()  # a stop ends the session like a disconnect
        ns["_abort_connection"].assert_called_once()
        self.assertTrue(ns["_shutdown_event"].is_set())  # no new uptime save starts
        self.assertGreaterEqual(ns["_stop_started_ts"], cancelled_at)
        self.assertLessEqual(ns["_stop_started_ts"], done_at)


class MemoryStoreTests(unittest.TestCase):
    def namespace(self, present=True, error=None, embedder=None):
        made = []

        def make(**kwargs):
            made.append(kwargs)
            if error:
                raise error
            return SimpleNamespace(**kwargs)
        ns = {"_memory_embedder": embedder, "_summary_embedder": None, "SemanticEmbedder": make,
              "model_present": lambda path: present, "ONNX_MODEL_DIR": "/m", "CHROMA_DIR": "/c", "made": made,
              "threading": threading}
        load_function("_memory_store", ns)
        return ns

    def test_retrievals_store_when_loaded(self):
        retrieval = object()
        ns = self.namespace(embedder=retrieval)
        self.assertIs(ns["_memory_store"](), retrieval)
        self.assertEqual(ns["made"], [])

    def test_otherwise_loaded_once(self):
        ns = self.namespace()
        first = ns["_memory_store"]()
        self.assertIs(ns["_memory_store"](), first)
        self.assertEqual(ns["made"], [{"model_dir": "/m", "chroma_dir": "/c", "collection_name": "user_memories"}])

    def test_none_while_the_first_start_download_loads_retrievals(self):
        ns = self.namespace()
        ns["threading"] = SimpleNamespace(enumerate=lambda: [SimpleNamespace(name="memory-download")])
        self.assertIsNone(ns["_memory_store"]())
        self.assertEqual(ns["made"], [])
        retrieval = object()
        ns["_memory_embedder"] = retrieval
        self.assertIs(ns["_memory_store"](), retrieval)

    def test_none_while_the_model_downloads_or_wont_load(self):
        self.assertIsNone(self.namespace(present=False)["_memory_store"]())
        ns = self.namespace(error=ImportError("chromadb"))
        with patch("builtins.print"):
            self.assertIsNone(ns["_memory_store"]())
            self.assertIsNone(ns["_memory_store"]())
        self.assertEqual(len(ns["made"]), 2)  # tried again next time


class FakeCollection:
    """Chroma's get(): rows in the order they were added, whatever ids asked for."""
    def __init__(self, rows):
        self.rows = rows  # (id, document, metadata)

    def count(self):
        return len(self.rows)

    def get(self, ids=None, include=(), limit=None):
        rows = [r for r in self.rows if ids is None or r[0] in ids][:limit]
        got = {"ids": [r[0] for r in rows]}
        if "documents" in include:
            got["documents"] = [r[1] for r in rows]
        if "metadatas" in include:
            got["metadatas"] = [r[2] for r in rows]
        return got


def memory_rows():
    """60 memories; the newest were added first (e.g. a store copied from another robot)."""
    rows = [(f"new_{i}", f"new fact {i}", {"timestamp": f"20261020_1200{i:02d}", "line_index": "0"})
            for i in range(30)]
    rows += [(f"old_{i}", f"old fact {i}", {"timestamp": f"20250101_0900{i:02d}", "line_index": "0"})
             for i in range(30)]
    rows.append(("odd", "no metadata", None))
    # Two saves in one second: lines keep the order they were added in.
    rows += [(f"same_{i}", f"same second line {i}", {"timestamp": "20261020_120100", "line_index": str(i % 2)})
             for i in (1, 2, 3)]
    return rows


class NewestMemoriesTests(unittest.TestCase):
    def newest(self, collection, limit=50):
        ns = {"_memory_embedder": SimpleNamespace(_collection=collection), "MEMORY_PROMPT_COUNT": limit}
        load_function("_newest_memories", ns)
        return ns["_newest_memories"]()

    def test_newest_by_timestamp_oldest_first(self):
        docs = self.newest(FakeCollection(memory_rows()))
        self.assertEqual(len(docs), 50)
        self.assertEqual(docs[-3:], ["same second line 1", "same second line 2", "same second line 3"])
        self.assertEqual(docs[-4], "new fact 29")
        self.assertEqual(docs[:2], ["old fact 13", "old fact 14"])
        self.assertNotIn("no metadata", docs)

    def test_empty_or_missing_store(self):
        self.assertEqual(self.newest(FakeCollection([])), [])
        self.assertEqual(self.newest(None), [])

    def test_system_instruction_lists_the_newest(self):
        ns = {"_BASE_CONFIG": {"speech_config": "unchanged"}, "_BASE_SYSTEM_INSTRUCTION": "base",
              "_memory_embedder": SimpleNamespace(_collection=FakeCollection(memory_rows())),
              "MEMORY_PROMPT_COUNT": 50, "public_toolkit": SimpleNamespace(extend_config=lambda c: c)}
        for name in ("_newest_memories", "_build_config"):
            load_function(name, ns)
        with patch("builtins.print"):
            config = ns["_build_config"]()
        instruction = config["system_instruction"]
        self.assertIn("- new fact 29\n", instruction)
        self.assertNotIn("old fact 0\n", instruction)
        self.assertTrue(instruction.endswith("\n\nbase"))
        self.assertEqual(config["speech_config"], "unchanged")
        # This connection's Gemini has them: recalled memories skip them.
        self.assertEqual(ns["_memories_given"], set(ns["_newest_memories"]()))
        self.assertIn("new fact 29", ns["_memories_given"])
        ns["_memories_given"].add("given on the last connection")
        ns["_memory_embedder"] = None
        ns["_build_config"]()
        self.assertEqual(ns["_memories_given"], set())   # a fresh conversation starts from its own prompt

    def test_a_connection_resuming_the_conversation_keeps_the_memories_given_on_it(self):
        resuming = [False]
        ns = {"_BASE_CONFIG": {}, "_BASE_SYSTEM_INSTRUCTION": "base", "_memories_given": set(),
              "_memory_embedder": SimpleNamespace(_collection=FakeCollection(memory_rows()[:2])),
              "MEMORY_PROMPT_COUNT": 50, "public_toolkit": SimpleNamespace(extend_config=lambda c: {
                  **c, "session_resumption": {"handle": "h1"} if resuming[0] else {}})}  # as session.py adds it
        for name in ("_newest_memories", "_build_config"):
            load_function(name, ns)
        with patch("builtins.print"):
            ns["_build_config"]()
            ns["_memories_given"].add("given on the last connection")
            resuming[0] = True                             # e.g. after a GoAway or a dropped network
            ns["_build_config"]()
            self.assertEqual(ns["_memories_given"], {"given on the last connection", "new fact 0", "new fact 1"})
            resuming[0] = False
            ns["_build_config"]()
        self.assertEqual(ns["_memories_given"], {"new fact 0", "new fact 1"})

    @unittest.skipUnless(importlib.util.find_spec("chromadb"), "chromadb not installed (desktop profiles)")
    def test_with_chromadb(self):
        import chromadb
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        collection = chromadb.PersistentClient(path=directory).get_or_create_collection(
            "user_memories", metadata={"hnsw:space": "cosine"})
        rows = [r for r in memory_rows() if r[2]]
        collection.add(ids=[r[0] for r in rows], documents=[r[1] for r in rows],
                       metadatas=[r[2] for r in rows], embeddings=[[float(i), 1.0, 0.0] for i in range(len(rows))])
        # What the prompt used before: the first 50 added, here the 30 newest then 20 old.
        self.assertEqual(collection.get(limit=50)["ids"][:2], ["new_0", "new_1"])
        docs = self.newest(collection, limit=5)
        self.assertEqual(docs, ["new fact 28", "new fact 29", "same second line 1",
                                "same second line 2", "same second line 3"])


class InitiationHintTests(unittest.TestCase):
    def test_hint_is_looked_up_off_the_event_loop(self):
        threads = []

        def hint():
            threads.append(threading.current_thread())
            return "\n- likes tea"
        sent = []

        async def send_client_content(turns, turn_complete):
            sent.append(turns["parts"][0]["text"])
        safe = [True]
        ns = {"asyncio": asyncio, "time": time, "_initiation_memory_hint": hint, "_notify_webapp": Mock(),
              "_safe_to_initiate": lambda now: safe[0]}
        load_function("_fire_initiation", ns)
        session = SimpleNamespace(send_client_content=send_client_content)
        with patch("builtins.print"):
            asyncio.run(ns["_fire_initiation"](session))
        self.assertIsNot(threads[0], threading.main_thread())
        self.assertTrue(sent[0].startswith("[INITIATE CONVERSATION]"))
        self.assertTrue(sent[0].endswith("\n- likes tea"))
        # The user started talking while the lookup ran: no opener over them.
        safe[0] = False
        with patch("builtins.print"):
            asyncio.run(ns["_fire_initiation"](session))
        self.assertEqual(len(sent), 1)

    def hint(self, results, newest):
        ns = {"_memory_embedder": SimpleNamespace(search=lambda q, n_results: results),
              "_recent_user_words_lock": threading.Lock(), "_recent_user_words": "we talked about tea".split(),
              "MEMORY_WORD_WINDOW": 30, "MEMORY_TOP_K": 3, "MEMORY_MAX_DISTANCE": 1.0,
              "_newest_memories": lambda: newest, "random": __import__("random")}
        load_function("_initiation_memory_hint", ns)
        return ns["_initiation_memory_hint"]()

    def test_close_memories_first_then_recent_ones(self):
        close = [{"document": "Joe likes tea", "distance": 0.3}, {"document": "far", "distance": 1.5}]
        self.assertTrue(self.hint(close, ["unused"]).endswith(":\n- Joe likes tea"))
        hint = self.hint([{"document": "far", "distance": 1.5}], ["a", "b", "c"])
        self.assertEqual(sorted(hint.split("\n")[2:]), ["- a", "- b", "- c"])
        self.assertEqual(self.hint([], []), "")


class ReceiveLoopTests(unittest.TestCase):
    def test_both_speakers_reach_the_transcript_unstripped(self):
        added = []

        def content(user=None, ember=None):
            return SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=None,
                                   server_content=SimpleNamespace(
                                       input_transcription=SimpleNamespace(text=user) if user else None,
                                       output_transcription=SimpleNamespace(text=ember) if ember else None,
                                       model_turn=None, turn_complete=False, interrupted=False))

        class Session:
            def receive(self):
                async def stream():
                    yield content(user=" A")
                    yield content(user="mber")
                    yield content(ember=" Hi!")
                    raise asyncio.CancelledError()
                return stream()
        ns = {"asyncio": asyncio, "time": time, "_transcript_add": lambda *a: added.append(a),
              "_address_gate": SimpleNamespace(heard=lambda text: [], reply=lambda show, *a: show()),
              "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(), timing=Mock()),
              "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
              "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
              "_recent_user_words": [], "MEMORY_WORD_WINDOW": 30, "_memory_embedder": None}
        load_function("receive_audio", ns)
        with patch("builtins.print"), self.assertRaises(asyncio.CancelledError):
            asyncio.run(ns["receive_audio"](Session()))
        self.assertEqual(added, [("user", " A"), ("user", "mber"), ("gemini", " Hi!")])


def message(user=None, ember=None, audio=None, done=False):
    """One Gemini Live message for the real receive loop."""
    turn = SimpleNamespace(parts=[SimpleNamespace(text=None, inline_data=SimpleNamespace(
        mime_type="audio/pcm;rate=24000", data=audio))]) if audio else None
    return SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=None,
                           server_content=SimpleNamespace(
                               input_transcription=SimpleNamespace(text=user) if user else None,
                               output_transcription=SimpleNamespace(text=ember) if ember else None,
                               model_turn=turn, turn_complete=done, interrupted=False))


def wait(seconds):
    async def step():
        await asyncio.sleep(seconds)
    return step


class RecalledMemoryTests(unittest.TestCase):
    """Memories recalled for what the person says reach Gemini only between turns:
    anything sent as client content interrupts a reply Gemini is generating. They are
    looked up from the person's whole turn once its reply has played. The real receive
    loop and between_turns() on a scripted session, with a fake store."""

    PCM = b"\x01\x00\x02\x00"
    TOMATOES = {"document": "Joe grows tomatoes", "distance": 0.2}
    REX = {"document": "Joe's dog is Rex", "distance": 0.4}

    def runtime(self, found):
        """The receive loop and between_turns() sharing one namespace; `found(query)`
        is what the memory store finds. `self.searched` lists (time.monotonic(), query)."""
        import numpy as np
        from features.conversational_ability.addressing import AddressGate
        self.played = []  # time.monotonic() when each reply chunk was queued for the speaker
        self.searched = []

        def search(query, n_results):
            self.searched.append((time.monotonic(), query))
            return found(query)
        ns = {
            "asyncio": asyncio, "time": time, "np": np,
            "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(), finish_transcripts=Mock(), timing=Mock()),
            "_address_gate": AddressGate(enabled=False, log=Mock()), "public_toolkit": SimpleNamespace(),
            "_transcript_add": Mock(), "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
            "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
            "_recent_user_words": [], "MEMORY_WORD_WINDOW": 30, "MEMORY_TOP_K": 3, "MEMORY_MAX_DISTANCE": 1.0,
            "_memory_embedder": SimpleNamespace(search=search),
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_flush_playback": Mock(),
            "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
            "tracker_receive": SimpleNamespace(record=Mock()), "tracker_first_audio": SimpleNamespace(record=Mock()),
            "tracker_roundtrip": SimpleNamespace(record=Mock()), "_last_mic_send_ts": 0.0,
            "_last_robot_speech_ts": 0.0, "_last_user_speech_ts": 0.0, "_last_loud_mic_ts": 0.0,
            "_user_turn": None, "_user_turn_words": [], "_looked_up": None, "_recalled": None,
            "_memories_given": set(), "_reply_owed_since": None,
            "BETWEEN_TURNS_POLL_S": 0.01, "BETWEEN_TURNS_QUIET_S": 0.05, "REPLY_OWED_MAX_S": 15.0,
            "_go_away_at": None, "_turn_done_at": 0.0,  # no GoAway here (see test_v10_runtime)
        }

        def append_playback(data, *, new_turn=False, timing_id=None):
            with ns["_playback_lock"]:
                ns["_playback_buffer"] += data
            self.played.append(time.monotonic())
        ns["_append_playback"] = append_playback
        for name in ("receive_audio", "_retrieve_memories", "_recall_memories", "_memory_context",
                     "_between_turns", "between_turns"):
            load_function(name, ns)
        return ns

    def converse(self, ns, steps, between_turns=True):
        """Plays `steps` (messages, or coroutine functions run in between them) through
        the real receive loop, with between_turns() alongside. Returns what was sent,
        each with whether Gemini was replying, audio was still queued or a reply owed."""
        class Done(BaseException):
            pass
        sent = []

        class Session:
            def receive(self):
                async def stream():
                    for step in steps:
                        if callable(step):
                            await step()
                        else:
                            yield step
                    raise Done()
                return stream()

            async def send_client_content(self, **kwargs):
                sent.append({**kwargs, "speaking": ns["_gemini_speaking"], "playing": bool(ns["_playback_buffer"]),
                             "owed": ns["_reply_owed_since"] is not None})

        async def talk():
            session = Session()
            helper = asyncio.create_task(ns["between_turns"](session)) if between_turns else None
            try:
                await ns["receive_audio"](session)
            except Done:
                pass
            finally:
                if helper is not None:
                    helper.cancel()
        with patch("builtins.print"):
            asyncio.run(talk())
        return sent

    def played_out(self, ns):
        async def step():  # the speaker has played the queued reply
            with ns["_playback_lock"]:
                ns["_playback_buffer"] = b""
        return step

    def reply(self, ns, words):
        # Each wait gives the lookup and between_turns() (polling every 10 ms) time to act.
        return [message(user=words), wait(0.1), message(ember="Mm.", audio=self.PCM), wait(0.05),
                message(done=True), wait(0.05), self.played_out(ns), wait(0.2)]

    def test_nothing_is_sent_or_looked_up_during_the_reply(self):
        ns = self.runtime(lambda query: [self.TOMATOES])
        sent = self.converse(ns, [message(user="Ember, how is my garden doing?"), wait(0.1),
                                  message(ember="Your tomatoes", audio=self.PCM), message(done=True)],
                             between_turns=False)
        self.assertEqual(sent, [])               # an older loop sent the memories here, cutting the reply off
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.searched, [])      # the receive loop leaves the lookup to between_turns()
        self.assertIsNone(ns["_recalled"])

    def test_the_lookup_waits_until_the_reply_has_played(self):
        # A lookup (ONNX on the robot) still running when a reply starts holds back its first
        # audio (u3, Orange Pi 5 Max); once the reply has played nothing is waiting on the cores.
        ns = self.runtime(lambda query: [self.TOMATOES])
        played_out = []

        async def speaker_done():
            with ns["_playback_lock"]:
                ns["_playback_buffer"] = b""
            played_out.append(time.monotonic())
        sent = self.converse(ns, [message(user="Ember, how is my garden doing?"), wait(0.1),
                                  message(ember="Your tomatoes", audio=self.PCM), wait(0.05), message(done=True),
                                  wait(0.05), speaker_done, wait(0.2)])
        self.assertEqual(len(self.searched), 1)
        self.assertGreater(self.searched[0][0], played_out[0])   # was at the first words, before the reply
        self.assertEqual(len(sent), 1)
        self.assertIn("Joe grows tomatoes", sent[0]["turns"]["parts"][0]["text"])

    def test_the_lookup_uses_the_whole_turn_not_its_first_words(self):
        # Gemini transcribes a turn in fragments. The lookup used to start on the first one,
        # with the previous turn's words: "We planted tomatoes Ember," found nothing new.
        ns = self.runtime(lambda query: [self.REX] if "dog" in query else [self.TOMATOES])
        sent = self.converse(ns, self.reply(ns, "We planted tomatoes")
                             + [message(user="Ember,"), message(user=" what is my dog's"), message(user=" name?"),
                                *self.reply(ns, None)[1:]])
        self.assertEqual([query for _, query in self.searched], ["We planted tomatoes", "Ember, what is my dog's name?"])
        self.assertEqual(len(sent), 2)
        self.assertIn("Joe's dog is Rex", sent[1]["turns"]["parts"][0]["text"])
        self.assertNotIn("tomatoes", sent[1]["turns"]["parts"][0]["text"])

    def test_recalled_memories_are_given_after_the_reply_has_played(self):
        ns = self.runtime(lambda query: [self.TOMATOES])
        sent = self.converse(ns, self.reply(ns, "Ember, how is my garden doing?"))
        self.assertEqual(len(sent), 1)
        note = sent[0]
        self.assertTrue(note["turns"]["parts"][0]["text"].startswith("[MEMORY CONTEXT]"))
        self.assertIn("- Joe grows tomatoes (relevance: 0.80)", note["turns"]["parts"][0]["text"])
        self.assertIs(note["turn_complete"], False)
        self.assertFalse(note["speaking"] or note["playing"] or note["owed"])
        self.assertEqual(ns["_memories_given"], {"Joe grows tomatoes"})

    def test_memories_gemini_already_has_are_not_given_again(self):
        ns = self.runtime(lambda query: [self.TOMATOES, self.REX])
        ns["_memories_given"] = {"Joe's dog is Rex"}       # in this connection's instructions
        sent = self.converse(ns, self.reply(ns, "Ember, how is my garden doing?")
                             + self.reply(ns, "Ember, what should I plant next?"))
        self.assertEqual(len(sent), 1)                   # the second turn found nothing new
        self.assertIn("Joe grows tomatoes", sent[0]["turns"]["parts"][0]["text"])
        self.assertNotIn("Rex", sent[0]["turns"]["parts"][0]["text"])

    def test_a_recall_is_dropped_once_the_person_starts_a_new_turn(self):
        ns = self.runtime(lambda query: [self.REX] if query.endswith("dog?") else [self.TOMATOES])
        sent = self.converse(ns, [message(user="Ember, how is my garden doing?"), wait(0.1),
                                  message(ember="Mm.", audio=self.PCM), message(done=True), wait(0.05),
                                  # They talk again while the reply is still playing.
                                  *self.reply(ns, "And my dog?")])
        self.assertEqual(len(sent), 1)
        self.assertIn("Rex", sent[0]["turns"]["parts"][0]["text"])
        self.assertNotIn("tomatoes", sent[0]["turns"]["parts"][0]["text"])

    def test_a_quiet_moment_needs_no_reply_owed_queued_or_heard(self):
        clock = Clock(100.0)
        gate_clock = Clock(100.0)
        from features.conversational_ability.addressing import AddressGate
        ns = {"time": clock, "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
              "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_initiation_ts_lock": threading.Lock(),
              "_last_loud_mic_ts": 0.0, "_last_user_speech_ts": 0.0, "_reply_owed_since": None,
              "BETWEEN_TURNS_QUIET_S": 1.0, "REPLY_OWED_MAX_S": 15.0,
              "_address_gate": AddressGate(enabled=False, clock=gate_clock.monotonic, log=Mock())}
        quiet = load_function("_between_turns", ns)
        self.assertTrue(quiet(100.0))
        for name, value in (("_gemini_speaking", True), ("_playback_buffer", b"\x01\x00"),
                            ("_reply_owed_since", 90.0), ("_last_loud_mic_ts", 99.5), ("_last_user_speech_ts", 99.5)):
            with self.subTest(name=name), patch.dict(ns, {name: value}):
                self.assertFalse(quiet(100.0))
        ns["_reply_owed_since"] = 80.0                   # no reply came within REPLY_OWED_MAX_S
        self.assertTrue(quiet(100.0))
        ns["_address_gate"].prompted()                   # e.g. a reminder sent, its reply not over
        self.assertFalse(quiet(100.0))
        ns["_address_gate"].end()
        gate_clock.now += 31                             # unanswered for PROMPT_TTL_S
        self.assertTrue(quiet(100.0))


if __name__ == "__main__":
    unittest.main()
