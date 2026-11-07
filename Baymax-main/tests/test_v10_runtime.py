"""The MVP launcher runs realtime_gemini_10.py: its real hooks, without devices or Gemini."""
import asyncio
import copy
import os
from pathlib import Path
import socket
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
import runtime_monitor as monitor
from unittest.mock import AsyncMock, Mock, patch

from google import genai
from google.genai import errors, live
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

import numpy as np

from features.conversational_ability import live_models
from features.conversational_ability.addressing import AddressGate
from features.conversational_ability.session import connection_lost, not_tried
from features.conversational_ability.timing import ConversationTiming
import test_waiting_cue
from test_public_core_integration import load_function
from test_session import NEW, NOT_FOUND, OLD, QUOTA  # Gemini's model names and close reasons

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"
REAL_SLEEP = asyncio.sleep  # before any test patches it


class V10WaitingCueTests(test_waiting_cue.WaitingCueTests):
    """Every waiting-cue test, against v10's speaker callback and microphone hook."""
    def setUp(self):
        super().setUp()
        patcher = patch.object(test_waiting_cue, "CORE", V10)
        patcher.start()
        self.addCleanup(patcher.stop)


class V10RuntimeTests(unittest.TestCase):
    def source(self):
        return V10.read_text(encoding="utf-8")

    def test_launcher_runs_v10(self):
        import run_pc
        self.assertEqual(run_pc.RUNTIME, V10)

    def test_builtin_fall_checkin_reengages_the_microphone(self):
        from features.conversational_ability.engage import engagement
        class FallEvent:
            count = 0
            def wait(self, timeout):
                self.count += 1
                if self.count > 1:
                    raise asyncio.CancelledError()
                return True
            def clear(self):
                pass
        with patch('test_public_core_integration.CORE', V10):
            alert = load_function('fall_alert_monitor', {'asyncio': asyncio, '_fall_detected_event': FallEvent(),
                                                               '_fall_held_note': ''})
        session = AsyncMock()
        with patch.multiple(engagement, disengaged=True, listener=None):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(alert(session))
            self.assertFalse(engagement.disengaged)
        session.send_client_content.assert_awaited_once()

    def test_builtin_fall_checkin_pauses_radio_so_the_answer_reaches_gemini(self):
        import numpy as np
        from features.toolkits import audiobooks
        from features.toolkits.audiobooks import Player
        from features.toolkits.listen import Station
        from tests.test_toolkit_audiobooks import FakeListener, wait_for
        from tests.test_toolkit_listen import STATION, live
        class FallEvent:
            count = 0
            def wait(self, timeout):
                self.count += 1
                if self.count > 1:
                    raise asyncio.CancelledError()
                return True
            def clear(self):
                pass
        with patch('test_public_core_integration.CORE', V10):
            alert = load_function('fall_alert_monitor', {'asyncio': asyncio, '_fall_detected_event': FallEvent(),
                                                               '_fall_held_note': ''})
        player = Player(listener_factory=FakeListener, notify=lambda text: None)
        self.addCleanup(player.control, "stop", {})
        player.play(Station(STATION, stream=live, minutes=0))   # 0: never pauses on its own.
        self.assertTrue(wait_for(lambda: len(player.buffer) >= 2048))
        mic = np.full(480, 7, dtype=np.int16)
        self.assertTrue(player.gate(mic))
        session = AsyncMock()
        with patch.object(audiobooks, "player", player), self.assertRaises(asyncio.CancelledError):
            asyncio.run(alert(session))
        self.assertTrue(player.paused)
        self.assertFalse(player.gate(mic))
        text = session.send_client_content.await_args.kwargs["turns"]["parts"][0]["text"]
        self.assertTrue(text.startswith("[FALL ALERT]"))
        self.assertIn("Jazz FM (live radio) is paused so they can hear you and answer", text)

    def test_queued_replies_have_a_gap_but_chunks_and_idle_replies_do_not(self):
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(), "_playback_taken": 0,
                     "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1, "monitor": monitor}
        append = load_function("_append_playback", namespace)
        flush = load_function("_flush_playback", namespace)
        first, second = b"\x01\x00" * 4, b"\x02\x00" * 4
        append(first, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], first)
        append(first)
        self.assertEqual(namespace['_playback_buffer'], first * 2)
        append(second, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], first * 2 + b"\x00" * 17280 + second)
        flush()
        append(second, new_turn=True)
        self.assertEqual(namespace['_playback_buffer'], second)

    def test_speaker_mixes_extra_sources_such_as_audiobooks(self):
        enhancer = SimpleNamespace(full_duplex=True, rendered=Mock(), mix_output=lambda chunk: b"\x07\x00" * (len(chunk) // 2))
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(), "_playback_taken": 0,
                     "_waiting_cue": None, "_audio_enhancer": enhancer, "np": __import__("numpy"), "OUTPUT_CHANNELS": 1,
                     "voice_volume": SimpleNamespace(apply=lambda chunk: chunk), "monitor": monitor, "RECEIVE_SAMPLE_RATE": 48000}
        with patch("test_public_core_integration.CORE", V10):
            callback = load_function("_output_callback", namespace)
        out = __import__("numpy").empty((4, 1), dtype="int16")
        callback(out, 4, None, None)
        self.assertEqual(out.tobytes(), b"\x07\x00" * 4)
        enhancer.rendered.assert_called_once_with(b"\x07\x00" * 4, None)

    def test_stale_microphone_audio_is_dropped(self):
        async def exercise(stale):
            mic_queue = asyncio.Queue()
            await mic_queue.put({"data": b"pcm-test", "mime_type": "audio/pcm;rate=48000"})
            sent = []
            async def send(**kwargs):
                sent.append(kwargs["audio"].data)
                raise asyncio.CancelledError()
            with patch("test_public_core_integration.CORE", V10):
                fn = load_function("send_audio_realtime", {"asyncio": asyncio, "time": time,
                    "MIC_SEND_TIMEOUT_S": 5, "audio_queue_mic": mic_queue, "tracker_mic_queue": SimpleNamespace(record=Mock()),
                    "tracker_send_audio": SimpleNamespace(record=Mock()), "_playback_lock": threading.Lock(),
                    "_playback_buffer": b"", "_audio_enhancer": SimpleNamespace(full_duplex=True, stale=lambda ms: stale),
                    "types": SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})
            try:
                await asyncio.wait_for(fn(SimpleNamespace(send_realtime_input=send)), .02)
            except (asyncio.CancelledError, TimeoutError):
                pass
            return sent
        self.assertEqual(asyncio.run(exercise(False)), [b"pcm-test"])
        self.assertEqual(asyncio.run(exercise(True)), [])

    def test_receive_loop_passes_resumption_and_goaway_to_session(self):
        update = SimpleNamespace(resumable=True, new_handle="h")
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 1:
                        raise asyncio.CancelledError()
                    yield SimpleNamespace(tool_call=None, server_content=None,
                                          session_resumption_update=update, go_away=None)
                return stream()
        with patch("test_public_core_integration.CORE", V10), \
                patch("features.conversational_ability.session.on_server_message") as seen:
            receive = load_function("receive_audio", {"asyncio": asyncio, "time": time,
                "_TurnTranscript": lambda *a: SimpleNamespace(flush=lambda: None), "monitor": monitor})
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(receive(Session()))
        self.assertEqual(seen.call_args.args[0].session_resumption_update, update)

    def test_mvp_persona_toolkit_and_session_hooks_are_wired(self):
        source = self.source()
        for hook in ("_BASE_SYSTEM_INSTRUCTION = persona.PERSONA",
                     "from features.toolkits import gemini_tools as public_toolkit",
                     "public_toolkit.prewarm()",
                     "fall_worker.submit(frame)",
                     "fall_worker.alert_monitor(live_session, _shutdown_event)",
                     "monitor.notify('fall_alert')",
                     "monitor.start(",
                     'if os.getenv("BAYMAX_VOICE_ANALYSIS", "1") != "0":\n                _run_voice_analysis()'):
            self.assertIn(hook, source)

    def test_only_when_addressed_adds_proactive_audio_and_instruction(self):
        source = self.source()
        self.assertIn('**({"proactivity": {"proactive_audio": True}} if ONLY_WHEN_ADDRESSED else {})', source)
        self.assertIn("[WHEN TO SPEAK]", source)
        for hook in ("live_session = _address_gate.watch(live_session)", "_address_gate.tool_call()",
                     "_address_gate.heard(text)", "_address_gate.reply(show_reply_text, text)",
                     "_address_gate.reply(play_reply_audio)", "_address_gate.end()",
                     "_address_gate.end(interrupted=True)"):
            self.assertIn(hook, source)
        import device_config
        self.assertIs(device_config.DEFAULTS["conversation"]["only_when_addressed"], True)

    def test_mic_blocks_are_whole_10ms_frames_for_the_audio_cleanup(self):
        import ast
        tree = ast.parse(self.source())
        values = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  for target in node.targets if isinstance(target, ast.Name)
                  and target.id in ("CHUNK_SIZE", "SEND_SAMPLE_RATE")}
        self.assertEqual(values["CHUNK_SIZE"], 960)    # 1024 kept 10 ms of every block for the next
        self.assertEqual(values["CHUNK_SIZE"] % (values["SEND_SAMPLE_RATE"] // 100), 0)
        listen = self.source()[self.source().index("async def listen_audio"):self.source().index("async def send_audio_realtime")]
        self.assertIn("blocksize=CHUNK_SIZE,", listen)

    def test_the_live_model_list_starts_with_model(self):
        # The preflight and evals/live_eval.py read MODEL from the source; it stays first by default.
        source = self.source()
        self.assertIn(f'MODEL = "{OLD}"\n', source)
        self.assertIn("_live_models = live_models.ModelChoice(live_models.configured(MODEL))", source)
        self.assertEqual(live_models.configured(OLD, {}), (OLD, NEW))
        run = source[source.index("async def run():"):source.index("def _boot_status")]
        self.assertIn("model=model, config=_live_models.adapt(_build_config())", run)

    def test_random_idle_initiation_is_opt_in(self):
        source = self.source()
        self.assertIn('RANDOM_INITIATION_ENABLED = os.getenv("BAYMAX_RANDOM_INITIATION", "0") == "1"', source)
        self.assertIn("if RANDOM_INITIATION_ENABLED:\n                            tg.create_task(initiation_monitor(live_session))", source)


class ReplyTimingTests(unittest.TestCase):
    """Conversation timing is diagnostics: it never stops, drops or holds up Ember's audio.
    v10's real playback queue and speaker callback, without a device."""

    def speaker(self, timing, lock=None):
        namespace = {"_playback_buffer": b"", "_playback_lock": lock or threading.Lock(), "_playback_taken": 0,
                     "_waiting_cue": None, "_audio_enhancer": None, "np": np, "OUTPUT_CHANNELS": 1,
                     "RECEIVE_SAMPLE_RATE": 48000, "voice_volume": SimpleNamespace(apply=lambda chunk: chunk),
                     "monitor": SimpleNamespace(timing=timing)}
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_append_playback", "_output_callback"):
                load_function(name, namespace)
        return namespace

    @staticmethod
    def play(namespace, now=None, frames=1024):
        """One speaker callback, its DAC 40 ms ahead; a fake clock `now` moves on by the block."""
        out = np.empty((frames, 1), dtype=np.int16)
        current = 5 + (now[0] if now else 0)
        namespace["_output_callback"](out, frames, SimpleNamespace(currentTime=current,
                                                                   outputBufferDacTime=current + .04), None)
        if now:
            now[0] += frames / 48000
        return out.tobytes()

    def test_a_timing_error_never_stops_the_speaker(self):
        # sounddevice stops the speaker stream for good once its callback raises.
        timing = ConversationTiming()
        timing.played = Mock(side_effect=RuntimeError("timing bug"))
        namespace = self.speaker(timing)
        namespace["_playback_buffer"] = b"\x05\x00" * 8
        with patch("builtins.print"):
            blocks = [self.play(namespace, frames=4), self.play(namespace, frames=4)]
        self.assertEqual(blocks, [b"\x05\x00" * 4] * 2)
        self.assertTrue(timing.failed)
        timing.played.assert_called_once()  # timing stays off for the rest of the run

    def test_a_timing_error_never_drops_reply_audio(self):
        timing = ConversationTiming()
        timing.transcript("user", 1)
        turn = timing.received_audio()
        timing.queued = Mock(side_effect=RuntimeError("timing bug"))
        namespace = self.speaker(timing)
        with patch("builtins.print"):
            namespace["_append_playback"](b"\x05\x00" * 4, new_turn=True, timing_id=turn)
            namespace["_append_playback"](b"\x06\x00" * 4, timing_id=turn)
        self.assertEqual(namespace["_playback_buffer"], b"\x05\x00" * 4 + b"\x06\x00" * 4)
        self.assertTrue(timing.failed)
        timing.queued.assert_called_once()

    def test_reply_timing_never_runs_under_the_lock_the_speaker_waits_for(self):
        # The speaker needs this lock every 21 ms. Timing work, and timing's own lock (taken
        # by the monitor thread five times a second), must not hold it.
        lock = threading.Lock()
        seen = []

        class Timing(ConversationTiming):
            def queued(self, *args):
                seen.append(("queued", lock.locked()))
                super().queued(*args)

            def played(self, *args):
                seen.append(("played", lock.locked()))
                super().played(*args)

        class Numpy:  # notes whether the reply's first sound is searched for under the lock
            def __getattr__(self, name):
                if name == "frombuffer":
                    seen.append(("search", lock.locked()))
                return getattr(np, name)
        timing = Timing()
        timing.transcript("user", 1)
        turn = timing.received_audio()
        namespace = self.speaker(timing, lock)
        namespace["np"] = Numpy()
        namespace["_append_playback"](b"\x00\x00\x07\x00" * 4, new_turn=True, timing_id=turn)
        self.play(namespace, frames=8)
        self.assertEqual({name for name, _ in seen}, {"search", "queued", "played"})
        self.assertEqual([entry for entry in seen if entry[1]], [])
        self.assertIn("playback", timing.snapshot()[0])

    def test_a_reply_queued_while_the_speaker_takes_a_block_keeps_its_own_time(self):
        # played() runs after the speaker released the lock, so the event loop can queue the
        # next reply in between: its time must not move by the bytes of that block.
        now = [0.0]

        class Timing(ConversationTiming):
            slip_in = None

            def played(self, *args):
                if self.slip_in is not None and not namespace["_playback_lock"].locked():
                    turn, self.slip_in = self.slip_in, None
                    namespace["_append_playback"](np.full(1024, 9, np.int16).tobytes(), new_turn=True, timing_id=turn)
                super().played(*args)
        timing = Timing(clock=lambda: now[0], wall=lambda: 1000 + now[0])
        namespace = self.speaker(timing)
        timing.transcript("user", 1)
        first = timing.received_audio()
        timing.complete_reply()
        timing.transcript("user", 2)
        timing.slip_in = timing.received_audio()
        namespace["_append_playback"](np.full(1024, 8, np.int16).tobytes(), new_turn=True, timing_id=first)
        self.play(namespace, now)  # takes the whole first reply; the second is queued meanwhile
        self.play(namespace, now)
        rows = timing.snapshot()
        self.assertAlmostEqual(rows[0]["playback"], 1000.04)
        self.assertAlmostEqual(rows[1]["playback"], 1000 + 1024 / 48000 + .04)

    def test_a_reply_is_timed_from_its_first_sound(self):
        # Every chunk carries the reply's timing id: the first can be digital silence, and
        # Gemini streams faster than real time, so dozens of chunks queue before one plays.
        now = [0.0]
        timing = ConversationTiming(clock=lambda: now[0], wall=lambda: 1000 + now[0])
        namespace = self.speaker(timing)
        timing.transcript("user", 1)
        turn = timing.received_audio()
        namespace["_append_playback"](bytes(2048), new_turn=True, timing_id=turn)  # 1024 silent samples
        for _ in range(50):
            namespace["_append_playback"](np.full(1024, 500, np.int16).tobytes(), timing_id=turn)
        while namespace["_playback_buffer"]:
            self.play(namespace, now)
        self.assertAlmostEqual(timing.snapshot()[0]["playback"], 1000 + 1024 / 48000 + .04)


class ReconnectTests(unittest.TestCase):
    """Any disconnect must end the session so run() reconnects: Ember must never sit
    on a dead connection, and audio from the old reply must not outlive it."""

    def sender(self, queue, emit=None, timeout=5):
        with patch("test_public_core_integration.CORE", V10):
            return load_function("send_audio_realtime", {"asyncio": asyncio, "time": time,
                "audio_queue_mic": queue, "tracker_mic_queue": SimpleNamespace(record=Mock()),
                "tracker_send_audio": SimpleNamespace(record=Mock()), "_playback_lock": threading.Lock(),
                "_playback_buffer": b"", "_audio_enhancer": None, "_last_mic_send_ts": 0.0,
                "monitor": SimpleNamespace(emit=emit or Mock()), "MIC_SEND_TIMEOUT_S": timeout,
                "types": SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})

    def receiver(self, transcripts=None):
        with patch("test_public_core_integration.CORE", V10):
            return load_function("receive_audio", {"asyncio": asyncio, "time": time,
                "RECEIVE_FAILURES_BEFORE_RECONNECT": 3, "connection_lost": connection_lost,
                "monitor": SimpleNamespace(emit=Mock(), timing=Mock()),
                "_TurnTranscript": lambda speaker: (transcripts or {}).get(speaker, SimpleNamespace(flush=lambda: None))})

    @staticmethod
    def mic(*blocks):
        queue = asyncio.Queue()
        for block in blocks:
            queue.put_nowait({"data": block, "mime_type": "audio/pcm;rate=48000", "ts": time.perf_counter()})
        return queue

    def test_any_send_failure_ends_the_session(self):
        async def exercise():
            emit, sent = Mock(), []
            async def send(**kwargs):
                sent.append(kwargs)
                raise RuntimeError("anything at all")
            with self.assertRaisesRegex(RuntimeError, "anything at all"):
                await asyncio.wait_for(self.sender(self.mic(b"a", b"b"), emit)(SimpleNamespace(send_realtime_input=send)), 1)
            self.assertEqual(len(sent), 1)  # gives up at once, not one error per mic block
            self.assertEqual(emit.call_args.args[0], "reconnecting")
        asyncio.run(exercise())

    def test_receive_keeps_the_session_after_an_error_handling_one_message(self):
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 4:
                        raise asyncio.CancelledError()
                    yield SimpleNamespace(tool_call=None, server_content=None, go_away=None,
                                          session_resumption_update=None)
                    raise ValueError("bug handling one message")
                return stream()
        session = Session()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.receiver()(session))
        self.assertEqual(session.count, 5)  # messages keep arriving, so no reconnect

    def test_receive_gives_up_when_nothing_arrives_after_unknown_errors(self):
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 10:
                        raise asyncio.CancelledError()  # kept retrying a dead connection: fails below
                    raise ValueError("unknown failure")
                    yield
                return stream()
        session = Session()
        with patch("asyncio.sleep", AsyncMock()), self.assertRaisesRegex(ValueError, "unknown failure"):
            asyncio.run(self.receiver()(session))
        self.assertEqual(session.count, 3)

    def test_cancelled_receive_still_posts_the_partial_transcript(self):
        said = {"user": SimpleNamespace(flush=Mock()), "gemini": SimpleNamespace(flush=Mock())}
        class Session:
            def receive(self):
                async def stream():
                    raise asyncio.CancelledError()
                    yield
                return stream()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.receiver(said)(Session()))
        said["user"].flush.assert_called_once()
        said["gemini"].flush.assert_called_once()

    def test_a_real_closed_websocket_ends_both_loops_for_every_close_code(self):
        """The real SDK session over a real local websocket, closed by the server."""
        async def session_over(code):
            async def close_it(connection):
                if code == 1006:
                    connection.transport.abort()  # no close frame: a dropped network
                else:
                    await connection.close(code, "bye")  # no word the old text filter matched
                await connection.wait_closed()
            async with ws_serve(close_it, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with ws_connect(f"ws://127.0.0.1:{port}", proxy=None) as socket:
                    await socket.wait_closed()
                    client = genai.Client(api_key="test", http_options={"api_version": "v1alpha"})
                    session = live.AsyncSession(api_client=client._api_client, websocket=socket)
                    tasks = {"send": self.sender(self.mic(b"\0\0" * 32)), "receive": self.receiver()}
                    for name, loop in tasks.items():
                        with self.subTest(code=code, loop=name), patch("builtins.print"):
                            # A timeout here means the loop kept retrying the dead connection.
                            with self.assertRaises((errors.APIError, ConnectionClosed)):
                                await asyncio.wait_for(loop(session), 2)
        for code in (1000, 1001, 1006, 1007, 1008, 1011, 1012, 1013):
            asyncio.run(session_over(code))

    def test_a_network_gone_silent_ends_the_session_and_the_socket_can_be_dropped(self):
        """No close, no reset: the peer just stops reading. A send then waits forever,
        and so would the polite close when leaving connect()."""
        async def exercise():
            release = asyncio.Event()
            async def never_read(connection):
                await release.wait()
                connection.transport.abort()
            async with ws_serve(never_read, "127.0.0.1", 0, max_queue=1, max_size=None, write_limit=1024) as server:
                port = server.sockets[0].getsockname()[1]
                socket = await ws_connect(f"ws://127.0.0.1:{port}", proxy=None, write_limit=1024,
                                          ping_interval=None, compression=None)
                client = genai.Client(api_key="test", http_options={"api_version": "v1alpha"})
                session = live.AsyncSession(api_client=client._api_client, websocket=socket)
                blocks = [os.urandom(1 << 20) for _ in range(40)]  # far beyond any socket buffer
                try:
                    with patch("builtins.print"), self.assertRaises(TimeoutError):
                        await asyncio.wait_for(self.sender(self.mic(*blocks), timeout=.2)(session), 10)
                    namespace = {}
                    with patch("test_public_core_integration.CORE", V10):
                        load_function("_abort_connection", namespace)(session)
                    await asyncio.wait_for(socket.wait_closed(), 2)
                finally:
                    release.set()
        asyncio.run(exercise())

    def abandon_namespace(self, gate, audio=b"old reply", speaking=True):
        namespace = {"_playback_lock": threading.Lock(), "_playback_buffer": audio, "_cut_off_prompts": [],
                     "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": speaking, "_address_gate": gate,
                     "monitor": SimpleNamespace(timing=Mock())}
        with patch("test_public_core_integration.CORE", V10):
            load_function("_flush_playback", namespace)
            load_function("_abandon_reply", namespace)
        return namespace

    def test_abandoning_a_reply_clears_its_audio_and_keeps_what_must_be_said_again(self):
        gate = SimpleNamespace(abandon=Mock(return_value=[("message", {})]))
        namespace = self.abandon_namespace(gate)
        namespace["_abandon_reply"]()
        self.assertEqual(namespace["_playback_buffer"], b"")
        self.assertFalse(namespace["_gemini_speaking"])
        gate.abandon.assert_called_once_with(True)
        self.assertEqual(namespace["_cut_off_prompts"], [("message", {})])

    def test_nothing_was_cut_off_when_the_reply_had_finished_playing(self):
        gate = SimpleNamespace(abandon=Mock(return_value=[]))
        self.abandon_namespace(gate, audio=b"", speaking=False)["_abandon_reply"]()
        gate.abandon.assert_called_once_with(False)

    FALL_ALERT = {"turns": {"role": "user", "parts": [{"text": "[FALL ALERT] check on them"}]}, "turn_complete": True}

    def run_connections(self, count, receive, failing=(), load=(), setup_errors=None, **overrides):
        """The real run(), with its real cleanup, the real gate and the real watched
        session, for `count` connections. `receive(index, session, namespace)` is each
        connection's receive loop; every send on a connection in `failing` fails, and
        connection i fails while being set up with `setup_errors[i]` (no session).
        `load` names more real runtime functions; `overrides` replace namespace entries.
        `self.pauses` lists every asyncio.sleep awaited (each returns after 1 ms);
        `self.models` the model each connection asked for."""
        class Stop(BaseException):
            pass
        sessions, setup_errors = [], setup_errors or {}
        asked = self.models = []
        self.addCleanup(setattr, live_models, "in_use", live_models.in_use)  # run() sets the model in use

        class Session:
            def __init__(self, index, model, config):
                self.index, self.model, self.config, self.attempted, self.sent = index, model, config, [], []
                self._ws = SimpleNamespace(transport=SimpleNamespace(abort=Mock()))
            async def send_client_content(self, **kwargs):
                self.attempted.append(kwargs)
                if self.index in failing:
                    raise ConnectionClosedError(None, None)
                self.sent.append(kwargs)

        class Connect:
            def __init__(self, model, config):
                self.model, self.config = model, config
            async def __aenter__(self):
                index = len(asked)
                if index == count:
                    raise Stop()
                asked.append(self.model)
                if index in setup_errors:
                    raise setup_errors[index]
                sessions.append(Session(index, self.model, self.config))
                return sessions[-1]
            async def __aexit__(self, *exc):
                return False

        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        namespace = {
            "asyncio": asyncio, "time": SimpleNamespace(sleep=Mock(), monotonic=time.monotonic),
            "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_cut_off_prompts": [],
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_initiation_ts_lock": threading.Lock(), "_last_robot_speech_ts": 0.0,
            "_address_gate": AddressGate(enabled=False), "monitor": Mock(), "public_toolkit": Mock(),
            "genai": SimpleNamespace(Client=lambda **kw: SimpleNamespace(aio=SimpleNamespace(
                live=SimpleNamespace(connect=lambda **kw: Connect(kw["model"], kw["config"]))))),
            "API_KEY": "test", "_live_models": live_models.ModelChoice(["m"]), "_turn_done_at": 0.0,
            "_build_config": dict, "_boot_status": Mock(),
            "_memory_embedder": None, "MEMORY_RETRIEVAL_ENABLED": False, "FALL_DETECTION_ENABLED": False,
            "FALL_ANGLE_THRESHOLD": 0, "FALL_STAY_DOWN_SECONDS": 0, "FALL_DETECTION_FPS": 0,
            "POSE_TRACKING_ENABLED": True, "RANDOM_INITIATION_ENABLED": False,
            "start_output_stream": Mock(), "sd": Mock(), "get_default_device_id": lambda: (0, 0),
            "listen_audio": forever, "send_audio_realtime": forever, "send_video_realtime": forever,
            "receive_audio": lambda session: receive(session.index, session, namespace),
            "fall_alert_monitor": forever, "monitor_queues": forever, "between_turns": forever,
            "fall_worker": SimpleNamespace(alert_monitor=forever), "_shutdown_event": None,
            "connection_lost": connection_lost, "not_tried": not_tried, "RECONNECT_DELAY_S": 0.3, "QUICK_SESSION_S": 60.0,
            "RECONNECT_MAX_DELAY_S": 4.8, "_go_away_at": None, "_connected_at": 0.0,
            "live_models": live_models,
        }
        namespace.update(overrides)
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_flush_playback", "_abandon_reply", "_send_cut_off_prompts", "_abort_connection",
                         "_SessionHandover", "_leaves", "_reconnect_plan", "_reselect_audio_devices",
                         "_reset_audio", "run", *load):
                load_function(name, namespace)
        self.pauses = []

        async def pause(seconds):
            self.pauses.append(seconds)
            if len(self.pauses) > 100:
                raise AssertionError("run() kept pausing without reaching the connections")
            await REAL_SLEEP(0.001)
        with patch("builtins.print"), patch("asyncio.sleep", pause), self.assertRaises(Stop):
            asyncio.run(namespace["run"]())
        return sessions, namespace

    async def cut_off_alert(self, index, session, namespace):
        """Gemini streams the fall check-in's reply; it is still queued when the line drops."""
        await session.send_client_content(**self.FALL_ALERT)
        namespace["_playback_buffer"] = b"are you okay?"
        raise errors.APIError(1008, {"message": "bye"})

    def test_a_fall_check_in_cut_off_by_a_disconnect_is_asked_again_through_run(self):
        async def receive(index, session, namespace):
            if index == 0:
                await self.cut_off_alert(index, session, namespace)
            raise errors.APIError(1008, {"message": "bye"})
        sessions, namespace = self.run_connections(2, receive)
        self.assertEqual(sessions[0].sent, [self.FALL_ALERT])
        self.assertEqual(sessions[1].sent, [self.FALL_ALERT])  # asked again on the new connection
        self.assertEqual(namespace["_playback_buffer"], b"")  # the old audio did not survive
        sessions[0]._ws.transport.abort.assert_called_once()

    def test_a_cut_off_check_in_whose_resend_fails_is_asked_on_the_connection_after(self):
        # The second connection drops before its first send: the check-in never reached
        # Gemini there, so the gate can't hand it back and its sender won't retry it.
        async def receive(index, session, namespace):
            if index == 0:
                await self.cut_off_alert(index, session, namespace)
            if index == 1:
                await asyncio.Event().wait()  # the failed resend ends this connection
            raise errors.APIError(1008, {"message": "bye"})
        sessions, namespace = self.run_connections(3, receive, failing={1})
        self.assertEqual(sessions[0].sent, [self.FALL_ALERT])
        self.assertEqual(sessions[1].attempted, [self.FALL_ALERT])
        self.assertEqual(sessions[1].sent, [])
        self.assertEqual(sessions[2].sent, [self.FALL_ALERT])  # asked once it can be heard
        self.assertEqual(sessions[2].attempted, [self.FALL_ALERT])  # and only once

    def test_a_fall_alert_that_cannot_reach_gemini_is_asked_again(self):
        event = threading.Event()
        event.set()
        attempts = []

        async def unreachable(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 2:
                raise asyncio.CancelledError()  # the session ends
            raise ConnectionResetError()
        namespace = {"asyncio": asyncio, "_fall_detected_event": event, "_fall_held_note": "",
                     "print": lambda *a, **k: None}
        with patch("test_public_core_integration.CORE", V10):
            monitor_fn = load_function("fall_alert_monitor", namespace)
        with patch("features.conversational_ability.engage.engagement"), patch("asyncio.sleep", AsyncMock()), \
                self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor_fn(SimpleNamespace(send_client_content=unreachable)))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(event.is_set())  # still pending for the next connection

    def test_a_retried_fall_alert_still_says_that_playback_was_paused(self):
        # The first try paused the radio; on the retry it is already paused, so
        # hold_for_answer has nothing to say, but Gemini still has to be told.
        event = threading.Event()
        event.set()
        texts = []

        async def unreachable(**kwargs):
            texts.append(kwargs["turns"]["parts"][0]["text"])
            if len(texts) == 2:
                raise asyncio.CancelledError()  # the session ends
            raise ConnectionResetError()
        namespace = {"asyncio": asyncio, "_fall_detected_event": event, "_fall_held_note": "",
                     "print": lambda *a, **k: None}
        with patch("test_public_core_integration.CORE", V10):
            monitor_fn = load_function("fall_alert_monitor", namespace)
        notes = iter([" Jazz FM is paused so they can hear you and answer.", ""])
        with patch("features.toolkits.audiobooks.hold_for_answer", side_effect=lambda reason: next(notes)), \
                patch("features.conversational_ability.engage.engagement"), patch("asyncio.sleep", AsyncMock()), \
                self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor_fn(SimpleNamespace(send_client_content=unreachable)))
        for text in texts:
            self.assertTrue(text.endswith(" Jazz FM is paused so they can hear you and answer."))
        # Kept for the next connection, and dropped once an alert is delivered.
        self.assertEqual(namespace["_fall_held_note"], " Jazz FM is paused so they can hear you and answer.")
        event.set()
        delivered = []

        async def reachable(**kwargs):
            delivered.append(kwargs["turns"]["parts"][0]["text"])
            if len(delivered) == 1:
                event.set()  # a second fall, with nothing playing any more
                return
            raise asyncio.CancelledError()
        with patch("features.toolkits.audiobooks.hold_for_answer", return_value=""), \
                patch("features.conversational_ability.engage.engagement"), patch("asyncio.sleep", AsyncMock()), \
                self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor_fn(SimpleNamespace(send_client_content=reachable)))
        self.assertTrue(delivered[0].endswith("is paused so they can hear you and answer."))
        self.assertNotIn("paused", delivered[1])

    def test_a_failed_resend_stays_queued_for_the_next_connection(self):
        first, second = ((), {"turn_complete": True, "n": 1}), ((), {"turn_complete": True, "n": 2})
        # The send fails, or the session ends (cancelling it) while it waits.
        for error in (ConnectionResetError, asyncio.CancelledError):
            with self.subTest(error=error.__name__):
                namespace = {"_cut_off_prompts": [first, second]}
                with patch("test_public_core_integration.CORE", V10):
                    send = load_function("_send_cut_off_prompts", namespace)
                async def fails(**kwargs):
                    raise error()
                with patch("builtins.print"), self.assertRaises(error):
                    asyncio.run(send(SimpleNamespace(send_client_content=fails)))
                self.assertEqual(namespace["_cut_off_prompts"], [first, second])  # in their order

    def test_a_resent_message_leaves_the_queue(self):
        first, second = ((), {"turn_complete": True, "n": 1}), ((), {"turn_complete": True, "n": 2})
        namespace = {"_cut_off_prompts": [first, second]}
        with patch("test_public_core_integration.CORE", V10):
            send = load_function("_send_cut_off_prompts", namespace)
        sent = []
        async def second_fails(**kwargs):
            if sent:
                raise ConnectionResetError()
            sent.append(kwargs)
        with patch("builtins.print"), self.assertRaises(ConnectionResetError):
            asyncio.run(send(SimpleNamespace(send_client_content=second_fails)))
        self.assertEqual(sent, [first[1]])
        self.assertEqual(namespace["_cut_off_prompts"], [second])  # the sent one isn't sent twice

    # ── Fast reconnect: Gemini resets every connection after about 10 minutes ──
    def test_a_lost_connection_reconnects_at_once_without_resetting_audio(self):
        async def receive(index, session, namespace):
            raise errors.APIError(1011, {"message": "internal error"})
        sessions, ns = self.run_connections(1, receive)   # the reconnect attempt ends the test
        self.assertEqual(self.pauses, [0.3])         # was 2 s, a 5 s block of the event loop and a reset
        ns["time"].sleep.assert_not_called()
        ns["sd"]._terminate.assert_not_called()
        ns["sd"]._initialize.assert_not_called()

    def test_a_device_error_still_resets_audio_but_never_blocks_the_event_loop(self):
        class PortAudioError(Exception):
            pass
        on_main_thread = []

        def select():
            on_main_thread.append(threading.current_thread() is threading.main_thread())
            return 3, 4

        async def receive(index, session, namespace):
            raise PortAudioError("Error opening InputStream: Device unavailable")
        sessions, ns = self.run_connections(1, receive, get_default_device_id=select)
        self.assertEqual(self.pauses, [2, 5])        # awaited: a stop still ends Ember at once
        ns["time"].sleep.assert_not_called()
        ns["sd"]._terminate.assert_called_once()
        ns["sd"]._initialize.assert_called_once()
        self.assertEqual(on_main_thread, [False])    # the device query runs on a worker thread
        self.assertEqual(ns["sd"].default.device, [3, 4])

    def test_connections_that_keep_failing_back_off_without_resetting_audio(self):
        # E.g. weak Wi-Fi: the reset (2 s, PortAudio restarted, 5 s) can't help a network.
        async def receive(index, session, namespace):
            raise ConnectionResetError()
        sessions, ns = self.run_connections(8, receive)
        self.assertEqual(self.pauses, [0.3, 0.6, 1.2, 2.4, 4.8, 4.8, 4.8, 4.8])  # was ... 4.8, 2, 5, 2, 5
        ns["sd"]._terminate.assert_not_called()

    def resumed_after(self, setup_error):
        """The real run() and session resumption: connection 0 gets resume handle h1, then
        drops; the next attempt fails while being set up with `setup_error`; the one after
        reaches Gemini. Returns the resume setting of each connection that reached it."""
        from features.conversational_ability import session as resumption
        resumption.forget()
        self.addCleanup(resumption.forget)

        async def receive(index, session, ns):
            if index == 0:
                resumption.remember(SimpleNamespace(resumable=True, new_handle="h1"))
                raise errors.APIError(1006, {"message": "Abnormal closure."})
            raise ConnectionResetError()
        with patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "1", "BAYMAX_CONTEXT_COMPRESSION": "0"}):
            sessions, ns = self.run_connections(3, receive, setup_errors={1: setup_error},
                                                _build_config=lambda: resumption.extend_config({}))
        return [s.config["session_resumption"] for s in sessions]

    def test_an_attempt_that_never_reached_gemini_keeps_the_resume_handle(self):
        # A Wi-Fi drop of a few seconds: the first retry, 0.3 s after it, fails before any setup
        # message is sent. That counted as the handle's one try, so the connection that did
        # reach Gemini started the conversation over.
        for error in (socket.gaierror(-3, "Temporary failure in name resolution"),
                      OSError(101, "Network is unreachable"), ConnectionRefusedError(111, "Connection refused"),
                      TimeoutError("timed out during opening handshake")):
            with self.subTest(error=error):
                self.assertEqual(self.resumed_after(error), [{}, {"handle": "h1"}])   # was [{}, {}]

    def test_a_refusal_from_gemini_still_uses_up_the_resume_handle(self):
        # Gemini closed the setup that offered the handle: a bad handle must not block reconnecting.
        self.assertEqual(self.resumed_after(errors.APIError(1008, {"message": "bye"})), [{}, {}])

    def test_a_network_still_down_at_the_retry_backs_off_without_resetting_audio(self):
        # The Wi-Fi is still down when run() reconnects: connect() raises socket.gaierror or
        # "Network is unreachable", which connection_lost doesn't cover, so each attempt ran
        # the device reset (2 s, PortAudio restarted, 5 s) that can't help a network.
        async def receive(index, session, namespace):
            if index == 0:
                raise errors.APIError(1006, {"message": "Abnormal closure."})
            raise ConnectionResetError()
        sessions, ns = self.run_connections(4, receive, setup_errors={
            1: socket.gaierror(-3, "Temporary failure in name resolution"), 2: OSError(101, "Network is unreachable")})
        self.assertEqual(self.pauses, [0.3, 0.6, 1.2, 2.4])   # was [0.3, 2, 5, 2, 5, 2.4]
        ns["sd"]._terminate.assert_not_called()
        ns["sd"]._initialize.assert_not_called()
        errors_shown = [c for c in ns["_boot_status"].call_args_list if c.kwargs.get("error")]
        self.assertEqual(errors_shown, [])                    # nor a "Connection failed" boot error

    def plan(self):
        ns = {"connection_lost": connection_lost, "RECONNECT_DELAY_S": 0.3, "QUICK_SESSION_S": 60.0,
              "RECONNECT_MAX_DELAY_S": 4.8}
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_SessionHandover", "_leaves", "_reconnect_plan"):
                load_function(name, ns)
        return ns

    def test_only_lost_connections_skip_the_reset_and_a_long_session_starts_the_backoff_over(self):
        ns = self.plan()
        plan, handover = ns["_reconnect_plan"], ns["_SessionHandover"]
        lost = ExceptionGroup("session", [errors.APIError(1011, {"message": "x"}), ConnectionClosedError(None, None)])
        device = ExceptionGroup("session", [errors.APIError(1011, {"message": "x"}), RuntimeError("PortAudio")])
        self.assertEqual(plan(lost, 600.0, 4), (0.3, 1))      # Gemini's routine reset after ~10 minutes
        self.assertEqual(plan(lost, 5.0, 1), (0.6, 2))        # failing again quickly: wait longer
        self.assertEqual(plan(lost, 5.0, 5), (4.8, 6))        # up to 4.8 s, never the device reset
        self.assertEqual(plan(lost, 5.0, 5000), (4.8, 5001))  # hours of failures in a row
        self.assertEqual(plan(device, 600.0, 0), (None, 1))   # any device error: the full reset
        self.assertEqual(plan(TimeoutError(), 600.0, 0), (0.3, 1))  # e.g. a mic send stuck 5 s
        # A reconnect while the network is down: DNS or no route, from connect().
        for down in (socket.gaierror(-3, "Temporary failure in name resolution"), OSError(101, "Network is unreachable"),
                     ExceptionGroup("session", [OSError(113, "No route to host"), ConnectionResetError()])):
            self.assertEqual(plan(down, 1.0, 1), (0.6, 2), down)
        # PortAudio's own error is no OSError (sounddevice: class PortAudioError(Exception)),
        # so a device error still gets the full reset.
        class PortAudioError(Exception):
            pass
        self.assertEqual(plan(PortAudioError("Error opening OutputStream"), 600.0, 0), (None, 1))
        try:
            import sounddevice
        except (ImportError, OSError):  # no PortAudio library on this machine
            pass
        else:
            self.assertFalse(issubclass(sounddevice.PortAudioError, OSError))
        moved = ExceptionGroup("session", [handover()])
        self.assertEqual(plan(moved, 600.0, 3), (0.0, 0))     # GoAway after a long connection: at once
        self.assertEqual(plan(moved, 1.0, 3), (2.4, 4))       # GoAway soon after setup: a quick failure
        self.assertTrue(issubclass(handover, Exception))      # run() catches it like the others

    # ── GoAway: move to a new connection at a quiet moment ──
    def test_the_receive_loop_notes_a_goaway_and_each_turn_end(self):
        class Session:
            def receive(self):
                async def stream():
                    yield SimpleNamespace(tool_call=None, server_content=None, session_resumption_update=None,
                                          go_away=SimpleNamespace(time_left="50s"))
                    yield SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=None,
                                          server_content=SimpleNamespace(
                                              input_transcription=None, output_transcription=None,
                                              model_turn=None, turn_complete=True, interrupted=False))
                    raise asyncio.CancelledError()
                return stream()
        ns = {"asyncio": asyncio, "time": time, "monitor": SimpleNamespace(emit=Mock(), finish_transcripts=Mock(),
                                                                         timing=Mock()),
              "_TurnTranscript": lambda *a: SimpleNamespace(flush=lambda: None), "_address_gate": Mock(),
              "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False, "_go_away_at": None}
        with patch("test_public_core_integration.CORE", V10), patch("builtins.print"):
            receive = load_function("receive_audio", ns)
            before = time.monotonic()
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(receive(Session()))
        self.assertGreaterEqual(ns["_go_away_at"], before)
        self.assertGreaterEqual(ns["_turn_done_at"], ns["_go_away_at"])

    def handed_over(self, quiet=True, handle_at=None, turn_done_at=0.0, go_away_at=100.0, now=101.0,
                    connected_at=0.0):
        """Whether the real between_turns() ends the connection within a few polls."""
        clock = SimpleNamespace(monotonic=lambda: now)
        ns = {"asyncio": asyncio, "time": clock, "_between_turns": lambda at: quiet, "_recalled": None,
              "_go_away_at": go_away_at, "_turn_done_at": turn_done_at, "handle_received_at": lambda: handle_at,
              "_connected_at": connected_at, "BETWEEN_TURNS_POLL_S": 0.001, "GO_AWAY_HANDLE_WAIT_S": 3.0,
              "_user_turn": None, "_looked_up": None, "_memory_embedder": None, "live_models": live_models,
              "MEMORY_NOTES_ENABLED": False}  # the default; the GoAway handover works either way
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_SessionHandover", "_handover_due", "between_turns"):
                load_function(name, ns)

        async def watch():
            try:
                await asyncio.wait_for(ns["between_turns"](Mock()), 0.05)
            except TimeoutError:
                return False
            except ns["_SessionHandover"]:
                return True
        with patch("builtins.print"):
            return asyncio.run(watch())

    def test_after_a_goaway_the_connection_ends_only_at_a_quiet_moment(self):
        self.assertTrue(self.handed_over())                                # quiet, nothing to resume yet
        self.assertFalse(self.handed_over(quiet=False))                    # replying, playing or someone talking
        self.assertFalse(self.handed_over(go_away_at=None))                # no GoAway: keep the connection
        # Wait for a resume handle that includes the last turn...
        self.assertFalse(self.handed_over(handle_at=99.0, turn_done_at=99.5))
        self.assertTrue(self.handed_over(handle_at=99.8, turn_done_at=99.5))
        # ...but not for ever (GO_AWAY_HANDLE_WAIT_S after the GoAway or the turn): Gemini resets it soon anyway.
        self.assertFalse(self.handed_over(handle_at=99.0, turn_done_at=99.5, now=102.9))
        self.assertTrue(self.handed_over(handle_at=99.0, turn_done_at=99.5, now=103.1))
        # A handle from before this connection was offered to set it up: session.py won't offer it
        # again, so moving would start the conversation over. Wait for this connection's own.
        self.assertFalse(self.handed_over(handle_at=40.0, connected_at=50.0, now=200.0))
        self.assertTrue(self.handed_over(handle_at=60.0, connected_at=50.0, now=200.0))

    def test_after_a_goaway_ember_moves_once_the_reply_has_played_and_resumes(self):
        from features.conversational_ability import session as resumption
        resumption.forget()
        self.addCleanup(resumption.forget)
        connected = []

        async def receive(index, session, ns):
            if index == 0:
                resumption.remember(SimpleNamespace(resumable=True, new_handle="h1"))
                ns["_playback_buffer"] = b"the end of a reply"
                ns["_go_away_at"] = time.monotonic()      # as receive_audio does on a GoAway
                await REAL_SLEEP(0.05)                   # still playing: no move yet
                connected.append(index)
                with ns["_playback_lock"]:
                    ns["_playback_buffer"] = b""         # played
                await REAL_SLEEP(2)
                raise AssertionError("still on the old connection")
            raise ConnectionResetError()                 # ends the test's second connection
        # The first connection counts as a long one (QUICK_SESSION_S), as after Gemini's ~10 minutes.
        sessions, ns = self.run_goaways(2, receive, QUICK_SESSION_S=0.01)
        self.assertEqual(connected, [0])                              # stayed while the reply played
        self.assertEqual(sessions[1].config["session_resumption"], {"handle": "h1"})
        self.assertEqual([p for p in self.pauses if p != 0.001], [0.3])   # at once; then the second one's loss
        ns["sd"]._terminate.assert_not_called()
        sessions[0]._ws.transport.abort.assert_called_once()

    def run_goaways(self, count, receive, **overrides):
        """run_connections() with the real between_turns() and session resumption."""
        from features.conversational_ability import session as resumption
        options = dict(load=("_between_turns", "_handover_due", "between_turns"),
                       _build_config=lambda: resumption.extend_config({}),
                       _recalled=None, _user_turn=None, _looked_up=None, _memories_given=set(),
                       _reply_owed_since=None, _tool_calls_running=0, _last_loud_mic_ts=0.0, _last_user_speech_ts=0.0, _turn_done_at=0.0,
                       handle_received_at=resumption.handle_received_at, BETWEEN_TURNS_POLL_S=0.001,
                       BETWEEN_TURNS_QUIET_S=1.0, REPLY_OWED_MAX_S=15.0, GO_AWAY_HANDLE_WAIT_S=3.0,
                       MEMORY_NOTES_ENABLED=False)
        options.update(overrides)
        with patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "1", "BAYMAX_CONTEXT_COMPRESSION": "0"}):
            return self.run_connections(count, receive, **options)

    def test_goaways_soon_after_every_setup_back_off_like_quick_losses(self):
        # E.g. a Gemini backend draining. Moving at once every time reconnected about once a
        # second, the mic closed for most of it, with no backoff and no limit.
        from features.conversational_ability import session as resumption
        resumption.forget()
        self.addCleanup(resumption.forget)

        async def receive(index, session, ns):
            resumption.remember(SimpleNamespace(resumable=True, new_handle=f"h{index}"))
            ns["_go_away_at"] = time.monotonic()         # as receive_audio does on a GoAway
            await REAL_SLEEP(2)
            raise AssertionError("not moved at the quiet moment")
        sessions, ns = self.run_goaways(7, receive)
        self.assertEqual([p for p in self.pauses if p != 0.001], [0.3, 0.6, 1.2, 2.4, 4.8, 4.8, 4.8])  # was none
        ns["sd"]._terminate.assert_not_called()          # the audio devices are fine
        self.assertEqual([s.config["session_resumption"] for s in sessions],
                         [{}] + [{"handle": f"h{i}"} for i in range(6)])  # each resumes the conversation

    def test_after_a_goaway_ember_waits_for_a_resume_handle_from_this_connection(self):
        # A resumed connection's GoAway before Gemini sent it a handle: moving with the old one
        # started the conversation over (session.py offers a handle once).
        from features.conversational_ability import session as resumption
        resumption.forget()
        self.addCleanup(resumption.forget)
        stayed = []

        async def receive(index, session, ns):
            if index == 0:
                resumption.remember(SimpleNamespace(resumable=True, new_handle="h1"))
                raise ConnectionResetError()             # lost: the next connection resumes with h1
            if index == 1:
                ns["_go_away_at"] = time.monotonic()
                await REAL_SLEEP(0.05)                   # quiet all along, but no handle of its own yet
                stayed.append(index)
                resumption.remember(SimpleNamespace(resumable=True, new_handle="h2"))
                await REAL_SLEEP(2)
                raise AssertionError("not moved once it had a handle")
            raise ConnectionResetError()
        sessions, ns = self.run_goaways(3, receive)
        self.assertEqual(stayed, [1])
        self.assertEqual([s.config["session_resumption"] for s in sessions],
                         [{}, {"handle": "h1"}, {"handle": "h2"}])   # was {} (fresh) for the third

    def test_a_stop_reaches_the_shutdown_summary_after_a_reconnect(self):
        # After a TaskGroup child fails, Python 3.11/3.12 end asyncio.run with
        # CancelledError on a stop, not KeyboardInterrupt; the summary must still run.
        source = V10.read_text(encoding="utf-8")
        main = source[source.index('if __name__ == "__main__":'):]
        self.assertIn("except (KeyboardInterrupt, asyncio.CancelledError):", main)
        self.assertLess(main.index("except (KeyboardInterrupt"), main.index("_save_memories_on_stop(memory_deadline)"))

    def test_every_session_end_abandons_the_reply_and_drops_the_socket_before_leaving_connect(self):
        source = V10.read_text(encoding="utf-8")
        run = source[source.index("async def run():"):source.index("def _boot_status")]
        cleanup = run[run.index("finally:"):]
        self.assertLess(cleanup.index("_abandon_reply()"), cleanup.index("_abort_connection(live_session)"))
        self.assertLess(cleanup.index("_abort_connection(live_session)"), cleanup.index("output_stream.stop()"))

    # ── Gemini Live model: the next one when this API key can't use one (live_models.py) ──
    def run_models(self, count, receive, **options):
        """run_connections() with the default model list (2.5, then 3.8) and a config with
        Ember's thinking budget and a function."""
        config = {"thinking_config": {"thinking_budget": 0}, "tools": [{"function_declarations": [{"name": "get_weather"}]}]}
        options.setdefault("_build_config", lambda: copy.deepcopy(config))
        return self.run_connections(count, receive, _live_models=live_models.ModelChoice([OLD, NEW]), **options)

    def test_a_model_this_key_cannot_use_is_replaced_at_once_and_kept(self):
        async def receive(index, session, ns):
            raise ConnectionResetError()  # an ordinary loss on the new model
        sessions, ns = self.run_models(3, receive, setup_errors={0: errors.APIError(1008, NOT_FOUND)})
        self.assertEqual(self.models, [OLD, NEW, NEW])  # at once, then kept
        self.assertEqual(self.pauses, [0.3, 0.6])       # no pause for the switch; losses back off as before
        config = sessions[0].config                     # adapted to 3.8
        self.assertNotIn("thinking_config", config)
        self.assertEqual(config["tools"][0]["function_declarations"][0]["behavior"], "BLOCKING")
        ns["sd"]._terminate.assert_not_called()

    def test_a_model_gone_before_the_first_turn_moves_on_without_the_old_models_handle(self):
        from features.conversational_ability import session as resumption
        resumption.forget()
        self.addCleanup(resumption.forget)

        async def receive(index, session, ns):
            if index == 0:  # 2.5 sets the connection up, sends a resume handle, then closes
                resumption.remember(SimpleNamespace(resumable=True, new_handle="issued by 2.5"))
                raise errors.APIError(1008, NOT_FOUND)
            raise ConnectionResetError()
        with patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "1", "BAYMAX_CONTEXT_COMPRESSION": "0"}):
            sessions, ns = self.run_models(2, receive, _build_config=lambda: resumption.extend_config({}))
        self.assertEqual(self.models, [OLD, NEW])
        self.assertEqual(sessions[1].config["session_resumption"], {})  # not offered to 3.8
        self.assertEqual(self.pauses, [0.3])

    def test_when_no_model_can_be_used_ember_backs_off_as_after_any_loss(self):
        async def receive(index, session, ns):
            raise errors.APIError(1008, NOT_FOUND)
        self.run_models(6, receive)
        self.assertEqual(self.models, [OLD, NEW] * 3)
        self.assertEqual(self.pauses, [0.3, 0.6, 1.2])  # one growing pause per round: no fast spin

    def test_ordinary_errors_never_change_the_model(self):
        async def receive(index, session, ns):
            raise (errors.APIError(1006, "Abnormal closure."), errors.APIError(1008, "bye"))[index - 3]
        self.run_models(5, receive, setup_errors={0: errors.APIError(1011, "Internal error encountered."),
                                                  1: ConnectionRefusedError(), 2: TimeoutError()})
        self.assertEqual(self.models, [OLD] * 5)
        self.assertEqual(self.pauses, [0.3, 0.6, 1.2, 2.4, 4.8])

    def test_a_model_that_finished_a_turn_is_kept_when_its_connection_ends(self):
        async def receive(index, session, ns):
            if index == 0:
                ns["_turn_done_at"] = time.monotonic()  # as receive_audio does at turn_complete
            if index < 2:
                raise errors.APIError(1008, NOT_FOUND)
            raise ConnectionResetError()
        self.run_models(3, receive)
        # The close on a connection that worked reconnects with 2.5; it moves on once a
        # connection fails before any turn.
        self.assertEqual(self.models, [OLD, OLD, NEW])
        self.assertEqual(self.pauses, [0.3, 0.6])

    def test_a_quota_close_retries_the_same_model(self):
        # "You exceeded your current quota" is also what a per-minute or concurrent-session
        # limit says, which clears in seconds: one such close kept Ember on the untested 3.8
        # until it restarted.
        async def receive(index, session, ns):
            ns["_turn_done_at"] = time.monotonic()      # each later connection answers, then drops
            raise ConnectionResetError()
        self.run_models(4, receive, setup_errors={0: errors.APIError(1011, QUOTA)})
        self.assertEqual(self.models, [OLD] * 4)        # was [OLD, NEW, NEW, NEW]
        self.assertEqual(self.pauses, [0.3, 0.6, 1.2, 2.4])


class RealTimeThreadTests(unittest.TestCase):
    """Nothing slow on PortAudio's capture thread or the event loop that sends the mic."""

    def test_the_mic_level_is_printed_by_the_queue_monitor_not_the_capture_thread(self):
        import numpy as np
        import queue
        samples = np.full((960, 1), 1500, dtype=np.int16)

        class Stream:
            latency = .02

            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                for _ in range(120):                  # the old callback printed every 50th block
                    self.callback(samples, 960, None, None)

            def __exit__(self, *args):
                pass
        ns = {"asyncio": asyncio, "np": np, "queue": queue, "time": time, "_audio_enhancer": None,
              "_waiting_cue": None, "INPUT_CHANNELS": 1, "_audio_record_queue": queue.Queue(),
              "audio_queue_mic": asyncio.Queue(), "INTERRUPT_RMS_THRESHOLD": 1000,
              "_initiation_ts_lock": threading.Lock(), "_last_loud_mic_ts": 0.0,
              "sd": SimpleNamespace(InputStream=Stream), "SEND_SAMPLE_RATE": 48000, "CHUNK_SIZE": 960,
              "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_transcript_lock": threading.Lock(),
              "_transcript": [], "_transcript_saved": 0, "_mic_statuses": 0, "_mic_statuses_shown": 0}
        with patch("test_public_core_integration.CORE", V10):
            for name in ("listen_audio", "monitor_queues"):
                load_function(name, ns)

        async def capture():
            try:
                await asyncio.wait_for(ns["listen_audio"](), .02)
            except TimeoutError:
                pass
        with patch("builtins.print") as printed:
            asyncio.run(capture())
        self.assertFalse([c for c in printed.call_args_list if "[MIC LEVEL]" in str(c.args)])
        self.assertEqual(ns["_mic_level"], (1500.0, 1500))
        ticks = []

        async def tick(seconds):                      # one report, then stop
            ticks.append(seconds)
            if len(ticks) > 1:
                raise asyncio.CancelledError()
        with patch("asyncio.sleep", tick), patch("builtins.print") as printed, \
                self.assertRaises(asyncio.CancelledError):
            asyncio.run(ns["monitor_queues"](interval=3.0))
        self.assertIn("[MIC LEVEL] rms=1500  peak=1500  (max=32767)", [c.args[0] for c in printed.call_args_list])

    def test_mic_statuses_are_counted_on_the_capture_thread_and_printed_by_the_queue_monitor(self):
        import numpy as np
        import queue
        samples = np.full((960, 1), 10, dtype=np.int16)
        overflow = type("Flags", (), {"__bool__": lambda self: True, "__str__": lambda self: "input overflow"})()

        class Stream:
            latency = .02

            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                for _ in range(3):
                    self.callback(samples, 960, None, overflow)  # e.g. the event loop fell behind
                self.callback(samples, 960, None, None)

            def __exit__(self, *args):
                pass
        ns = {"asyncio": asyncio, "np": np, "queue": queue, "time": time, "_audio_enhancer": None,
              "_waiting_cue": None, "INPUT_CHANNELS": 1, "_audio_record_queue": queue.Queue(),
              "audio_queue_mic": asyncio.Queue(), "INTERRUPT_RMS_THRESHOLD": 1000,
              "_initiation_ts_lock": threading.Lock(), "_last_loud_mic_ts": 0.0, "_mic_level": None,
              "_mic_statuses": 0, "_mic_status": "", "_mic_statuses_shown": 0,
              "sd": SimpleNamespace(InputStream=Stream), "SEND_SAMPLE_RATE": 48000, "CHUNK_SIZE": 960,
              "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_transcript_lock": threading.Lock(),
              "_transcript": [], "_transcript_saved": 0}
        with patch("test_public_core_integration.CORE", V10):
            for name in ("listen_audio", "monitor_queues"):
                load_function(name, ns)

        async def capture():
            try:
                await asyncio.wait_for(ns["listen_audio"](), .02)
            except TimeoutError:
                pass
        with patch("builtins.print") as printed:
            asyncio.run(capture())
        self.assertEqual(printed.call_count, 0)       # was a flushed [MIC STATUS] line per status

        async def ticks(count):
            seen = []

            async def tick(seconds):
                seen.append(seconds)
                if len(seen) > count:
                    raise asyncio.CancelledError()
            with patch("asyncio.sleep", tick), self.assertRaises(asyncio.CancelledError):
                await ns["monitor_queues"](interval=3.0)
        with patch("builtins.print") as printed:
            asyncio.run(ticks(2))                     # two reports: the second has nothing new
        statuses = [c.args[0] for c in printed.call_args_list if "[MIC STATUS]" in c.args[0]]
        self.assertEqual(statuses, ["[MIC STATUS] input overflow (3 since the last report)"])

    def test_profile_summaries_print_about_every_ten_seconds(self):
        import collections
        import numpy as np
        clock = SimpleNamespace(now=0.0)
        ns = {"time": SimpleNamespace(monotonic=lambda: clock.now), "collections": collections, "np": np,
              "_playback_lock": threading.Lock(), "_playback_buffer": b"", "audio_queue_mic": asyncio.Queue(),
              "PROFILE_WINDOW": 50, "PROFILE_REPORT_S": 10.0}
        with patch("test_public_core_integration.CORE", V10):
            tracker = load_function("LatencyTracker", ns)("mic_queue_wait")
        with patch("builtins.print") as printed:
            for block in range(1500):                 # 30 s of 20 ms mic blocks
                clock.now = block * 0.02
                tracker.record(1.0)
        self.assertEqual(printed.call_count, 3)       # was 30: one every 50 blocks
        self.assertEqual(len(tracker.samples), 50)    # each summary still covers the last 50

    def test_the_camera_frame_for_gemini_is_encoded_off_the_event_loop(self):
        import numpy as np
        on_main_thread = []

        class FakeCv2:
            IMWRITE_JPEG_QUALITY = 1

            @staticmethod
            def resize(frame, size):
                on_main_thread.append(threading.current_thread() is threading.main_thread())
                return frame

            @staticmethod
            def imencode(ext, image, params):
                return True, np.frombuffer(b"jpeg", dtype=np.uint8)
        sent = []

        async def send(**kwargs):
            sent.append(kwargs["video"].data)
            raise asyncio.CancelledError()            # one frame is enough

        async def no_wait(seconds):
            pass
        ns = {"asyncio": asyncio, "time": time, "cv2": FakeCv2, "_latest_frame_lock": threading.Lock(),
              "_latest_frame": "frame", "types": SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw)),
              "tracker_send_video": SimpleNamespace(record=Mock())}
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_encode_video_frame", "send_video_realtime"):
                load_function(name, ns)
        with patch("asyncio.sleep", no_wait), patch("builtins.print"), self.assertRaises(asyncio.CancelledError):
            asyncio.run(ns["send_video_realtime"](SimpleNamespace(send_realtime_input=send)))
        self.assertEqual(on_main_thread, [False])     # the event loop runs on the main thread here
        self.assertEqual(sent, [b"jpeg"])


class SpeakerStreamTests(unittest.TestCase):
    """BAYMAX_OUTPUT_BLOCKSIZE / BAYMAX_OUTPUT_LATENCY: knobs for a board A/B of speaker
    latency. Unset, the speaker stream is opened exactly as before."""

    def settings(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in ("BAYMAX_OUTPUT_BLOCKSIZE", "BAYMAX_OUTPUT_LATENCY")}
        with patch("test_public_core_integration.CORE", V10):
            read = load_function("_output_stream_settings", {"os": os})
        with patch.dict(os.environ, {**clean, **env}, clear=True), patch("builtins.print") as printed:
            return read(), printed.call_count

    def test_unset_keeps_todays_stream_and_bad_values_fall_back(self):
        self.assertEqual(self.settings(), ((1024, None), 0))
        self.assertEqual(self.settings(BAYMAX_OUTPUT_BLOCKSIZE="480", BAYMAX_OUTPUT_LATENCY=" Low ")[0], (480, "low"))
        self.assertEqual(self.settings(BAYMAX_OUTPUT_BLOCKSIZE="64", BAYMAX_OUTPUT_LATENCY="0.03")[0], (64, 0.03))
        self.assertEqual(self.settings(BAYMAX_OUTPUT_BLOCKSIZE="4096", BAYMAX_OUTPUT_LATENCY="0.5")[0], (4096, 0.5))
        self.assertEqual(self.settings(BAYMAX_OUTPUT_LATENCY="high")[0], (1024, "high"))
        for bad in ({"BAYMAX_OUTPUT_BLOCKSIZE": "big"}, {"BAYMAX_OUTPUT_BLOCKSIZE": "-1"},
                    {"BAYMAX_OUTPUT_LATENCY": "fast"}, {"BAYMAX_OUTPUT_LATENCY": "nan"},
                    {"BAYMAX_OUTPUT_LATENCY": "0"},
                    # Past what the audio cleanup can follow: the echo reference keeps 250 ms
                    # (12000 frames), AEC3's delay range and the reply mute end at 0.5 s.
                    {"BAYMAX_OUTPUT_BLOCKSIZE": "16384"}, {"BAYMAX_OUTPUT_BLOCKSIZE": "4097"},
                    {"BAYMAX_OUTPUT_BLOCKSIZE": "63"}, {"BAYMAX_OUTPUT_BLOCKSIZE": "0"},
                    {"BAYMAX_OUTPUT_LATENCY": "1"}, {"BAYMAX_OUTPUT_LATENCY": "0.51"}):
            with self.subTest(**bad):
                self.assertEqual(self.settings(**bad), ((1024, None), 1))   # warned once, default kept

    def test_on_the_rk3588_fast_cores_the_speaker_gets_512_frames_and_low_latency(self):
        # Measured on the Orange Pi 5 Max's USB speaker: 42 ms of output latency instead of
        # about 96 ms, no underflows. pc_hardware sets it before the runtime reads it.
        import pc_hardware
        names = ("BAYMAX_OUTPUT_BLOCKSIZE", "BAYMAX_OUTPUT_LATENCY", "BAYMAX_CPU_PINNING", "BAYMAX_GIL_SWITCH_MS",
                 *pc_hardware.LIBRARY_THREADS, pc_hardware.FAST_CPUS_ENV, pc_hardware.SLOW_CPUS_ENV)
        clean = {k: v for k, v in os.environ.items() if k not in names}
        with patch("test_public_core_integration.CORE", V10):
            read = load_function("_output_stream_settings", {"os": os})
        for rk3588, expected in ((True, (512, "low")), (False, (1024, None))):   # the 4 Pro: as before
            with self.subTest(rk3588=rk3588), patch.dict(os.environ, clean, clear=True), \
                    patch.object(pc_hardware, "is_rk3588", return_value=rk3588), \
                    patch.object(pc_hardware, "performance_cpus", return_value={4, 5, 6, 7}), \
                    patch.object(pc_hardware, "pin_threads"), \
                    patch.object(pc_hardware.os, "sched_getaffinity", Mock(return_value=set(range(8))), create=True), \
                    patch.object(pc_hardware.os, "sched_setaffinity", Mock(), create=True), \
                    patch.object(sys, "setswitchinterval"), patch("builtins.print"):
                pc_hardware.use_performance_cpus()
                self.assertEqual(read(), expected)

    def test_the_speaker_stream_is_opened_with_them(self):
        opened = []

        class Stream:
            latency = 0.04

            def __init__(self, **kwargs):
                opened.append(kwargs)

            def start(self):
                pass
        ns = {"sd": SimpleNamespace(OutputStream=Stream), "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
              "_output_callback": Mock(), "_audio_enhancer": None, "OUTPUT_BLOCKSIZE": 1024, "OUTPUT_LATENCY": None}
        with patch("test_public_core_integration.CORE", V10):
            start = load_function("start_output_stream", ns)
        start()
        self.assertEqual(opened[-1]["blocksize"], 1024)
        self.assertNotIn("latency", opened[-1])        # sounddevice's own default, as before
        ns.update(OUTPUT_BLOCKSIZE=480, OUTPUT_LATENCY="low")
        with patch("builtins.print"):
            start()
        self.assertEqual((opened[-1]["blocksize"], opened[-1]["latency"]), (480, "low"))


class TranscriptsStayOnRobotTests(unittest.TestCase):
    def test_cloud_never_queues_transcripts_and_drops_old_ones(self):
        with patch("dotenv.load_dotenv"):  # not this machine's .env, for every later test
            import baymax_cloud
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(baymax_cloud, "DEVICE_KEY", "999.test"), \
                patch.object(baymax_cloud, "QUEUE_DIR", temp), \
                patch.object(baymax_cloud, "start", lambda: None):
            baymax_cloud.notify("/api/transcript", {"speaker": "user", "text": "private"})
            baymax_cloud.notify("/api/fall", {"trunk_angle": 80})
            baymax_cloud.notify("/api/voice-analysis", {"session": {}, "summary": {}})
            import json
            queued = [json.loads(p.read_text())["endpoint"] for p in sorted(Path(temp).glob("*.json"))]
        self.assertEqual(queued, ["/api/device/fall", "/api/device/voice-analysis"])
        self.assertTrue(baymax_cloud._send({"endpoint": "/api/device/transcript", "data": {}, "queued_at": 0}))

    def test_v10_sends_transcripts_to_the_local_dashboard_in_cloud_mode(self):
        cloud = SimpleNamespace(notify=Mock(), ROBOT_ONLY={"/api/transcript"})
        posted = []
        with patch("test_public_core_integration.CORE", V10):
            notify = load_function("_notify_webapp", {
                "_CLOUD_ENABLED": True, "baymax_cloud": cloud, "WEBAPP_URL": "http://localhost:5000",
                "http_requests": SimpleNamespace(post=lambda url, **kw: posted.append(url)),
                "threading": SimpleNamespace(Thread=lambda target, daemon: SimpleNamespace(start=target))})
        notify("/api/transcript", {"speaker": "user", "text": "private"})
        notify("/api/fall", {})
        self.assertEqual(posted, ["http://localhost:5000/api/transcript"])
        cloud.notify.assert_called_once_with("/api/fall", {})


class LiveViewTests(unittest.TestCase):
    """The dashboard's annotated picture costs 7-20 ms a frame: drawn only for a viewer."""

    def test_camera_loop_draws_only_while_watched_and_shares_pose(self):
        source = V10.read_text(encoding="utf-8")
        start = source.index("def _fall_detection_thread")
        loop = source[start:source.index("\n# ───", start)]
        self.assertIn("watched = _mjpeg_viewers > 0", loop)
        self.assertIn("viz = _draw_annotated(frame, landmarks, result, fps_history, angle_history)", loop)
        self.assertNotIn("_draw_skeleton(", loop)               # all drawing lives in _draw_annotated
        self.assertIn("fall_worker.share(landmarks", loop)      # the fall toolkit reuses this pose
        self.assertIn("shared_pose=POSE_TRACKING_ENABLED", loop)

    def test_stream_counts_viewers_reuses_jpegs_and_draws_a_skipped_frame(self):
        import socket
        import urllib.request
        import cv2
        import numpy as np
        drawn = []
        def draw(frame, *rest):
            drawn.append(rest)
            return np.full((48, 64, 3), 90, np.uint8)
        ns = {"cv2": cv2, "time": time, "_shutdown_event": threading.Event(), "_annotated_frame_lock": threading.Lock(),
              "_annotated_frame": None, "_annotated_inputs": None, "_mjpeg_viewers": 0, "_draw_annotated": draw}
        start = load_function("_start_mjpeg_server", ns)
        self.addCleanup(ns["_shutdown_event"].set)
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        with patch("builtins.print"):
            threading.Thread(target=start, args=(port,), daemon=True).start()
            time.sleep(0.3)

        def parts(count, between=None):
            stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=5)
            seen = []
            for i in range(count):
                if between and i in between:
                    between[i]()
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    byte = stream.read(1)
                    self.assertTrue(byte, "stream closed")
                    header += byte
                seen.append(stream.read(int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0])))
                stream.read(2)
            stream.close()
            return seen

        def show(value):
            with ns["_annotated_frame_lock"]:
                ns["_annotated_frame"] = np.full((48, 64, 3), value, np.uint8)
        # Nobody was watching, so the latest frame wasn't drawn: the viewer draws it once.
        ns["_annotated_inputs"] = ("frame", None, {"fall_active": False}, (15.0,), ())
        first = parts(2)
        self.assertEqual(len(drawn), 1)
        self.assertEqual(first[0], first[1])                     # re-sent, not re-drawn or re-encoded
        time.sleep(0.3)
        self.assertEqual(ns["_mjpeg_viewers"], 0)               # the viewer left
        # A new picture each frame while watched: the stream follows it.
        show(30)
        seen = parts(8, {3: lambda: show(200)})
        means = [int(cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR).mean()) for b in seen]
        self.assertLessEqual(abs(means[0] - 30), 2)
        self.assertLessEqual(abs(means[-1] - 200), 2)
        time.sleep(0.3)
        self.assertEqual(ns["_mjpeg_viewers"], 0)


class PoseProcessRestartTests(unittest.TestCase):
    """The camera thread with a fake camera and fake pose children (_open_pose): a pose
    child that cannot start or dies is started again once, on the CPU, and video never
    stops. A None child is one that could not start (_open_pose returned None)."""

    class Dying:
        device = "NPU"
        closed = False

        def detect(self, frame):
            raise RuntimeError("vision process stopped (exited)")

        def close(self):
            self.closed = True

    class Working:
        device = "CPU"
        closed = False

        def detect(self, frame):
            return ["pose of " + frame]

        def close(self):
            self.closed = True

    def run_thread(self, children, frames=5, pose_tracking=True, download_error=None, fall_detection=False,
                   **names):
        """names: replacements for the thread's globals (e.g. time, FallDetector)."""
        import collections
        shutdown = threading.Event()

        class Capture:
            reads = 0

            def isOpened(self):
                return True

            def read(self):
                Capture.reads += 1
                if Capture.reads > frames:          # the first read is the camera probe
                    shutdown.set()
                return True, f"frame{Capture.reads}"

            def set(self, *args):
                pass

            def release(self):
                pass

        ns = {}
        latest, faces, opened = [], [], []

        class FrameLock:                            # records each _latest_frame the thread sets
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                latest.append(ns["_latest_frame"])

        def open_pose(force_cpu=False):
            opened.append(force_cpu)
            return children.pop(0) if children else None   # None: could not start

        ns.update({
            "os": os, "time": time, "collections": collections, "CAMERA_INDICES": [0],
            "cv2": SimpleNamespace(VideoCapture=lambda index: Capture(), CAP_PROP_FRAME_WIDTH=3,
                                   CAP_PROP_FRAME_HEIGHT=4),
            "POSE_TRACKING_ENABLED": pose_tracking, "FALL_DETECTION_ENABLED": fall_detection, "FallDetector": Mock(),
            "_ensure_pose_model": Mock(side_effect=download_error), "_open_pose": open_pose, "fall_worker": Mock(),
            "FALL_DETECTION_FPS": 1000, "FALL_ANGLE_THRESHOLD": 60.0, "FALL_ANG_VEL_THRESHOLD": 1.0,
            "FALL_HIP_VEL_THRESHOLD": 1.0, "FALL_HISTORY_FRAMES": 6, "FALL_CONFIRMATION_FRAMES": 2,
            "FALL_COOLDOWN_SECONDS": 30.0, "FALL_STAY_DOWN_SECONDS": 2.0, "FALL_DBG_INTERVAL_S": 1.0,
            "_shutdown_event": shutdown, "_latest_frame_lock": FrameLock(), "_latest_frame": None,
            "_update_face_visibility": faces.append, "_annotated_frame_lock": threading.Lock(),
            "_annotated_frame": None, "_annotated_inputs": None, "_mjpeg_viewers": 0,
            "_draw_annotated": Mock(), "_fall_detected_event": threading.Event(), "monitor": Mock(),
            "_notify_webapp": Mock(),
        })
        ns.update(names)
        thread = load_function("_fall_detection_thread", ns)
        with patch.dict(os.environ, {"BAYMAX_HARDWARE_PROFILE": "test"}), patch("builtins.print") as printed:
            thread()
        logged = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        return ns, latest, faces, opened, logged

    def test_a_dead_pose_process_restarts_once_on_the_cpu_and_video_continues(self):
        dying, working = self.Dying(), self.Working()
        ns, latest, faces, opened, logged = self.run_thread([dying, working])
        self.assertEqual(opened, [False, True])     # NPU allowed first, then forced to the CPU
        self.assertIn("Pose process failed (vision process stopped (exited)); restarting it on the CPU.", logged)
        self.assertEqual(latest, ["frame2", "frame3", "frame4", "frame5", "frame6", None])  # None: stopped
        self.assertEqual(faces, [None, ["pose of frame3"], ["pose of frame4"], ["pose of frame5"],
                                 ["pose of frame6"]])
        self.assertEqual({c.kwargs["device"] for c in ns["fall_worker"].share.call_args_list}, {"CPU"})
        self.assertTrue(dying.closed and working.closed)
        ns["fall_worker"].stop.assert_called_once()

    def test_a_second_failure_leaves_video_only(self):
        first, second = self.Dying(), self.Dying()
        ns, latest, faces, opened, logged = self.run_thread([first, second])
        self.assertEqual(opened, [False, True])     # restarted once, never again
        self.assertIn("Pose process failed again (vision process stopped (exited)); video only", logged)
        self.assertEqual(latest[:-1], ["frame2", "frame3", "frame4", "frame5", "frame6"])
        self.assertEqual(faces, [None] * 5)
        ns["fall_worker"].share.assert_not_called()
        self.assertTrue(first.closed and second.closed)

    def test_a_pose_process_that_cannot_start_is_started_once_on_the_cpu(self):
        # E.g. librknnrt crashed the child while it set up the NPU, before its first reply.
        working = self.Working()
        ns, latest, faces, opened, logged = self.run_thread([None, working])
        self.assertEqual(opened, [False, True])     # NPU allowed first, then forced to the CPU
        self.assertIn("Starting the pose process again, on the CPU.", logged)
        self.assertNotIn("Pose unavailable", logged)
        self.assertEqual(latest, ["frame2", "frame3", "frame4", "frame5", "frame6", None])
        self.assertEqual(faces, [["pose of frame%d" % n] for n in range(2, 7)])  # from the first frame
        self.assertEqual({c.kwargs["device"] for c in ns["fall_worker"].share.call_args_list}, {"CPU"})
        self.assertTrue(working.closed)

    def test_the_start_retry_is_the_one_restart(self):
        dying = self.Dying()                        # the CPU child started, then dies
        ns, latest, faces, opened, logged = self.run_thread([None, dying])
        self.assertEqual(opened, [False, True])     # no third process
        self.assertIn("Pose process failed again (vision process stopped (exited)); video only", logged)
        self.assertEqual(latest[:-1], ["frame2", "frame3", "frame4", "frame5", "frame6"])
        self.assertEqual(faces, [None] * 5)
        self.assertTrue(dying.closed)

    def test_pose_that_cannot_start_on_the_cpu_either_leaves_video_only(self):
        ns, latest, faces, opened, logged = self.run_thread([])   # _open_pose returned None twice
        self.assertEqual(opened, [False, True])
        self.assertIn("Pose unavailable; video only", logged)
        self.assertEqual(latest[:-1], ["frame2", "frame3", "frame4", "frame5", "frame6"])
        self.assertEqual(faces, [None] * 5)
        ns["fall_worker"].share.assert_not_called()

    def test_a_pose_model_that_cannot_be_downloaded_leaves_video_running(self):
        # A new release's first start without network: the thread used to end, taking video with it.
        ns, latest, faces, opened, logged = self.run_thread([self.Working()], download_error=OSError("offline"))
        self.assertEqual(opened, [])
        self.assertIn("Could not download the pose model (offline); video only", logged)
        self.assertEqual(latest, ["frame2", "frame3", "frame4", "frame5", "frame6", None])
        self.assertEqual(faces, [None] * 5)
        ns["fall_worker"].stop.assert_called_once()

    def test_pose_tracking_off_starts_no_pose_process(self):
        ns, latest, faces, opened, logged = self.run_thread([], pose_tracking=False)
        self.assertEqual(opened, [])
        self.assertEqual(latest[:-1], ["frame2", "frame3", "frame4", "frame5", "frame6"])

    @staticmethod
    def detector(stamps):
        """A FallDetector stand-in that records each update's timestamp and sees no fall."""
        class Detector:
            _suspicious_streak = 0

            def __init__(self, **settings):
                pass

            def update(self, landmarks, timestamp=None):
                stamps.append(timestamp)
                return dict(fall_detected=False, fall_active=False, fall_pending=False,
                            trunk_angle=None, angular_vel=None, hip_descent_vel=None)
        return Detector

    def test_fall_velocities_use_each_frames_capture_time_not_its_pose_round_trip(self):
        clock = SimpleNamespace(now=100.0)
        clock.monotonic = lambda: clock.now
        clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
        captured, stamps = [], []

        class SlowPose(self.Working):
            def detect(self, frame):
                captured.append(clock.now)                 # the frame was read just now
                clock.now += (0.02, 0.09)[len(captured) % 2]   # 20 or 90 ms in the pose process
                return super().detect(frame)
        ns, latest, faces, opened, logged = self.run_thread(
            [SlowPose()], fall_detection=True, time=clock, FallDetector=self.detector(stamps))
        self.assertEqual(len(captured), 5)
        self.assertEqual(stamps, captured)
        # The fall toolkit's own detector (Monitor, or check-ins when the built-in one is off) too.
        self.assertEqual([c.kwargs["captured_at"] for c in ns["fall_worker"].share.call_args_list], captured)

    def test_the_fall_toolkit_leaves_the_check_in_to_builtin_fall_detection(self):
        # Setup's checkbox and device.toml's fall_detection.enabled both on: one check-in per fall.
        for builtin in (True, False):
            with self.subTest(builtin=builtin):
                ns, latest, faces, opened, logged = self.run_thread(
                    [self.Working()], fall_detection=builtin, FallDetector=self.detector([]))
                start = ns["fall_worker"].start.call_args.kwargs
                self.assertIs(start["builtin"], builtin)
                self.assertIs(start["shared_pose"], True)        # the toolkit still gets the core's pose

    HANGING = textwrap.dedent('''
        import time

        class Pose:
            """Like ChildPose: the NPU child answers its first frame and never its second (as
            if librknnrt were stuck in the driver); the CPU child (allow_npu=False) answers."""
            def __init__(self, model_path, allow_npu=True):
                self.device = "NPU" if allow_npu else "CPU"
                self.frames = 0

            def detect(self, frame):
                self.frames += 1
                if self.device == "NPU" and self.frames == 2:
                    time.sleep(3600)
                return None
    ''')

    def real_pose_processes(self, name, source, allow_npu, timeout=0.5):
        """Real vision processes (features.vision_process.Remote), one per allow_npu value,
        running `source`'s Pose (saved as module `name`), with a frame limit of `timeout` s
        checked every 50 ms. Hung children are killed when the test ends."""
        from functools import partial
        from features.vision_process import Remote
        import test_toolkit_vision_process as vision_tests
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, name + ".py").write_text(source)
        sys.path.insert(0, directory.name)
        self.addCleanup(sys.path.remove, directory.name)
        self.addCleanup(sys.modules.pop, name, None)
        module = __import__(name)
        vision_tests.quick_watchdog(self, timeout=timeout)
        path = os.pathsep.join(filter(None, (directory.name, os.environ.get("PYTHONPATH"))))
        poses = []
        with patch.dict(os.environ, {"PYTHONPATH": path}):
            for allowed in allow_npu:
                poses.append(Remote(partial(module.Pose, "pose_landmarker_lite.task", allow_npu=allowed)))
                self.addCleanup(poses[-1].close)
        for pose in poses:         # these run first: never leave a hung child behind
            self.addCleanup(lambda child=pose.child: child.poll() is None and child.kill())
        return poses

    def test_a_hung_pose_process_is_stopped_and_restarted_once_on_the_cpu(self):
        # Real vision processes: before the watchdog, the camera thread (and so video to
        # Gemini, face presence and fall detection) waited on a hung child for good.
        import test_toolkit_vision_process as vision_tests
        npu, cpu = self.real_pose_processes("fake_hanging_pose", self.HANGING, allow_npu=(True, False))
        outcome, took = vision_tests.finishes(self, lambda: self.run_thread([npu, cpu]))
        ns, latest, faces, opened, logged = outcome["value"]
        self.assertEqual(opened, [False, True])     # NPU allowed first, then forced to the CPU
        self.assertIn("Pose process failed (vision process hung: no reply in 0.5 s, so it was stopped); "
                      "restarting it on the CPU.", logged)
        self.assertEqual(latest, ["frame2", "frame3", "frame4", "frame5", "frame6", None])
        self.assertEqual([c.kwargs["device"] for c in ns["fall_worker"].share.call_args_list],
                         ["NPU", "CPU", "CPU", "CPU"])
        self.assertIsNotNone(npu.child.wait(timeout=2))      # stopped, not left hanging
        self.assertLess(took, 5)

    SWITCHING = textwrap.dedent('''
        import time

        from features.pose.backend import FallbackBackend

        class Npu:
            """Its second frame fails, as an RKNN error mid-run would."""
            name, device = "NPU (fake)", "NPU"
            frames = 0

            def detect(self, frame, timestamp_ms):
                self.frames += 1
                if self.frames == 2:
                    raise RuntimeError("RKNN inference failed")
                return None

            def close(self):
                pass

        class Cpu:
            """MediaPipe as the NPU child loads it then: 1 s here (seconds on the 5 Max),
            longer than a frame may take."""
            name, device = "CPU (fake)", "CPU"

            def __init__(self):
                time.sleep(1.0)

            def detect(self, frame, timestamp_ms):
                return None

            def close(self):
                pass

        class Pose:
            """Like ChildPose on the NPU: the real FallbackBackend, from Npu to Cpu."""
            def __init__(self, model_path, allow_npu=True):
                self.backend = FallbackBackend(Npu(), Cpu, log=lambda message: None)

            device = property(lambda self: self.backend.device)

            def detect(self, frame):
                return self.backend.detect(frame, 0)
    ''')

    def test_an_npu_error_mid_run_switches_to_the_cpu_in_the_same_pose_process(self):
        # FallbackBackend loads MediaPipe inside that frame. With a frame's limit the watchdog
        # stopped the child while it recovered and the camera thread spent its one restart on
        # a second process; a later failure then left video only until Ember restarted.
        import test_toolkit_vision_process as vision_tests
        npu, = self.real_pose_processes("fake_switching_pose", self.SWITCHING, allow_npu=(True,))
        outcome, took = vision_tests.finishes(self, lambda: self.run_thread([npu]))
        ns, latest, faces, opened, logged = outcome["value"]
        self.assertEqual(opened, [False])           # no second process: the one restart is still there
        self.assertNotIn("Pose process failed", logged)
        self.assertEqual([c.kwargs["device"] for c in ns["fall_worker"].share.call_args_list],
                         ["NPU", "CPU", "CPU", "CPU", "CPU"])
        self.assertEqual(latest, ["frame2", "frame3", "frame4", "frame5", "frame6", None])
        self.assertIsNotNone(npu.child.wait(timeout=2))      # closed with the thread


class PoseModelDownloadTests(unittest.TestCase):
    """_ensure_pose_model writes through a link (the 4 Pro image links each release's
    .task into ~/ember-deployment/shared) and never leaves a cut download as the model."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "shared").mkdir()
        (root / "release").mkdir()
        self.shared = root / "shared" / "pose_landmarker_lite.task"
        self.path = root / "release" / "pose_landmarker_lite.task"
        self.path.symlink_to(self.shared)                  # dangling until the first download

    def ensure(self, retrieve):
        ns = {"os": os, "urllib": SimpleNamespace(request=SimpleNamespace(urlretrieve=retrieve)),
              "_POSE_MODEL_PATH": str(self.path), "_POSE_MODEL_URL": "https://example.invalid/pose.task"}
        with patch("builtins.print"):
            load_function("_ensure_pose_model", ns)()

    def test_the_download_lands_in_shared_and_the_link_stays(self):
        def retrieve(url, filename):
            Path(filename).write_bytes(b"model")
        self.ensure(retrieve)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(self.shared.read_bytes(), b"model")
        self.assertEqual(sorted(p.name for p in self.shared.parent.iterdir()), ["pose_landmarker_lite.task"])
        self.ensure(Mock(side_effect=AssertionError("downloaded again")))   # the next release has it

    def test_a_cut_download_is_not_taken_for_the_model(self):
        def retrieve(url, filename):
            Path(filename).write_bytes(b"mod")
            raise OSError("connection reset")
        with self.assertRaises(OSError):
            self.ensure(retrieve)
        self.assertFalse(self.path.exists())             # so the next start downloads it again


class OpenPoseTests(unittest.TestCase):
    """The real _open_pose and vision process, with a stand-in for ChildPose whose NPU
    setup kills the process before its first reply, as a librknnrt crash in
    init_runtime or the warm-up would (Python exceptions there fall back inside it)."""

    FAKE = textwrap.dedent('''
        import os

        class NpuCrashPose:
            def __init__(self, model_path, allow_npu=True):
                if allow_npu:
                    os._exit(1)
                self.device = "CPU"

            def detect(self, frame):
                return None
    ''')

    def test_a_child_that_dies_setting_up_the_npu_gives_none_and_the_cpu_child_starts(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "fake_npu_crash.py").write_text(self.FAKE)
        sys.path.insert(0, directory.name)
        self.addCleanup(sys.path.remove, directory.name)
        self.addCleanup(sys.modules.pop, "fake_npu_crash", None)
        import fake_npu_crash
        open_pose = load_function("_open_pose", {"_POSE_MODEL_PATH": "pose_landmarker_lite.task",
                                                 "_POSE_MODEL": "lite"})
        path = os.pathsep.join(filter(None, (directory.name, os.environ.get("PYTHONPATH"))))
        with patch.dict(os.environ, {"PYTHONPATH": path}), \
                patch("features.pose.backend.ChildPose", fake_npu_crash.NpuCrashPose), \
                patch("builtins.print") as printed:
            self.assertIsNone(open_pose())                      # the camera thread then retries
            pose = open_pose(force_cpu=True)
        self.assertIsNotNone(pose)
        self.addCleanup(pose.close)
        self.assertEqual(pose.device, "CPU")
        self.assertIsNone(pose.detect("frame"))
        self.assertIn("[CAM] Pose process could not start (vision process stopped", printed.call_args_list[0].args[0])
        self.assertEqual(len(printed.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
