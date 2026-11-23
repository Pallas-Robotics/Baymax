"""The MVP launcher runs realtime_gemini_10.py: its real hooks, without devices or Gemini."""
import asyncio
import os
from pathlib import Path
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

from features.conversational_ability.addressing import AddressGate
from features.conversational_ability.session import connection_lost
import test_waiting_cue
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


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
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(),
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
        namespace = {"_playback_buffer": b"", "_playback_lock": threading.Lock(), "_waiting_cue": None,
                     "_audio_enhancer": enhancer, "np": __import__("numpy"), "OUTPUT_CHANNELS": 1,
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

    def test_random_idle_initiation_is_opt_in(self):
        source = self.source()
        self.assertIn('RANDOM_INITIATION_ENABLED = os.getenv("BAYMAX_RANDOM_INITIATION", "0") == "1"', source)
        self.assertIn("if RANDOM_INITIATION_ENABLED:\n                            tg.create_task(initiation_monitor(live_session))", source)


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

    def run_connections(self, count, receive, failing=(), setup_errors=(), build_config=dict):
        """The real run(), with its real cleanup, the real gate and the real watched
        session, for `count` connections. `receive(index, session, namespace)` is each
        connection's receive loop; every send on a connection in `failing` fails."""
        class Stop(BaseException):
            pass
        sessions = []
        configs = []

        class Session:
            def __init__(self, index):
                self.index, self.attempted, self.sent = index, [], []
                self._ws = SimpleNamespace(transport=SimpleNamespace(abort=Mock()))
            async def send_client_content(self, **kwargs):
                self.attempted.append(kwargs)
                if self.index in failing:
                    raise ConnectionClosedError(None, None)
                self.sent.append(kwargs)

        class Connect:
            async def __aenter__(self):
                if len(configs) <= len(setup_errors):
                    raise setup_errors[len(configs) - 1]
                if len(sessions) == count:
                    raise Stop()
                sessions.append(Session(len(sessions)))
                return sessions[-1]
            async def __aexit__(self, *exc):
                return False

        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        def connect(**kwargs):
            configs.append(kwargs["config"])
            return Connect()

        namespace = {
            "asyncio": asyncio, "time": SimpleNamespace(sleep=Mock(), monotonic=time.monotonic),
            "_playback_lock": threading.Lock(), "_playback_buffer": b"", "_cut_off_prompts": [],
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_initiation_ts_lock": threading.Lock(), "_last_robot_speech_ts": 0.0,
            "_address_gate": AddressGate(enabled=False), "monitor": Mock(), "public_toolkit": Mock(),
            "genai": SimpleNamespace(Client=lambda **kw: SimpleNamespace(aio=SimpleNamespace(
                live=SimpleNamespace(connect=connect)))),
            "API_KEY": "test", "MODEL": "m", "_build_config": build_config, "_boot_status": Mock(),
            "_memory_embedder": None, "MEMORY_RETRIEVAL_ENABLED": False, "FALL_DETECTION_ENABLED": False,
            "FALL_ANGLE_THRESHOLD": 0, "FALL_STAY_DOWN_SECONDS": 0, "FALL_DETECTION_FPS": 0,
            "POSE_TRACKING_ENABLED": True, "RANDOM_INITIATION_ENABLED": False,
            "start_output_stream": Mock(), "sd": Mock(), "get_default_device_id": lambda: (0, 0),
            "listen_audio": forever, "send_audio_realtime": forever, "send_video_realtime": forever,
            "receive_audio": lambda session: receive(len(sessions) - 1, session, namespace),
            "fall_alert_monitor": forever, "monitor_queues": forever,
            "fall_worker": SimpleNamespace(alert_monitor=forever), "_shutdown_event": None,
        }
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_flush_playback", "_abandon_reply", "_send_cut_off_prompts", "_abort_connection", "run"):
                load_function(name, namespace)
        with patch("builtins.print"), patch("asyncio.sleep", AsyncMock()) as sleep, self.assertRaises(Stop):
            asyncio.run(namespace["run"]())
        namespace["retry_delays"] = [call.args[0] for call in sleep.await_args_list]
        namespace["configs"] = configs
        return sessions, namespace

    def test_network_reconnect_uses_bounded_delay_without_audio_reset(self):
        async def receive(*args):
            raise ConnectionError("lost network")
        _, namespace = self.run_connections(7, receive)
        self.assertEqual(namespace["retry_delays"], [.3, .6, 1.2, 2.4, 4.8, 4.8, 4.8])
        namespace["sd"]._terminate.assert_not_called()
        namespace["time"].sleep.assert_not_called()

    def test_device_failure_retains_reset_with_nonblocking_delays(self):
        async def receive(*args):
            raise RuntimeError("device failure")
        _, namespace = self.run_connections(1, receive)
        self.assertEqual(namespace["retry_delays"], [2, 5])
        namespace["sd"]._terminate.assert_called_once()
        namespace["sd"]._initialize.assert_called_once()
        namespace["time"].sleep.assert_not_called()

    def test_pre_setup_network_failure_preserves_resume_until_server_refusal(self):
        from features.conversational_ability import session as state
        state.forget()
        self.addCleanup(state.forget)
        state.remember(SimpleNamespace(new_handle="h1", resumable=True))
        async def receive(*args):
            raise errors.APIError(1008, {"message": "resume rejected"})
        _, namespace = self.run_connections(1, receive,
            setup_errors=(OSError("unreachable"), TimeoutError("setup timed out")),
            build_config=lambda: state.extend_config({}))
        self.assertEqual([c["session_resumption"] for c in namespace["configs"]],
                         [{"handle": "h1"}, {"handle": "h1"}, {"handle": "h1"}, {}])

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

    def run_thread(self, children, frames=5, pose_tracking=True, download_error=None):
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
            "POSE_TRACKING_ENABLED": pose_tracking, "FALL_DETECTION_ENABLED": False, "FallDetector": Mock(),
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
