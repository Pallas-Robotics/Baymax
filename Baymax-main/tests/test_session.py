"""Context compression, session resumption config, disconnect detection and the Live model
list; no Gemini connection."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google import genai
from google.genai import errors, types
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from features.conversational_ability import live_models, session


def update(handle, resumable=True):
    return SimpleNamespace(new_handle=handle, resumable=resumable)


class ConnectionLostTests(unittest.TestCase):
    def test_every_close_code_counts_as_either_exception_type(self):
        for code in (1000, 1001, 1006, 1007, 1008, 1011, 1012, 1013, 4000):
            with self.subTest(code=code):
                self.assertTrue(session.connection_lost(ConnectionClosedError(Close(code, "bye"), None)))
                self.assertTrue(session.connection_lost(errors.APIError(code, {"message": "bye"})))
        self.assertTrue(session.connection_lost(ConnectionClosedOK(Close(1000, ""), None)))
        self.assertTrue(session.connection_lost(ConnectionClosedError(None, None)))  # no close frame at all

    def test_transport_failures_count(self):
        self.assertTrue(session.connection_lost(ConnectionResetError()))
        self.assertTrue(session.connection_lost(BrokenPipeError()))
        self.assertTrue(session.connection_lost(TimeoutError()))

    def test_a_bug_handling_one_message_is_not_a_disconnect(self):
        for error in (ValueError("bad frame"), KeyError("text"), AttributeError("x"), RuntimeError("closed")):
            self.assertFalse(session.connection_lost(error))


class SessionTests(unittest.TestCase):
    def setUp(self):
        session.forget()
        self.addCleanup(session.forget)
        self.now = 1000.0
        env = patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "1", "BAYMAX_CONTEXT_COMPRESSION": "1"})
        env.start()
        self.addCleanup(env.stop)

    def build(self):
        return session.extend_config({"response_modalities": ["AUDIO"]}, clock=lambda: self.now)

    def test_first_connection_compresses_and_asks_for_resume_handles(self):
        config = self.build()
        self.assertEqual(config["context_window_compression"],
                         {"trigger_tokens": 32000, "sliding_window": {"target_tokens": 16000}})
        self.assertEqual(config["session_resumption"], {})  # Empty: start fresh, but send handles.

    def test_reconnect_resumes_with_the_newest_handle(self):
        session.remember(update("h1"), clock=lambda: self.now)
        session.remember(update("h2"), clock=lambda: self.now)
        session.remember(update("h3", resumable=False))       # Not resumable right now: keep h2.
        session.remember(SimpleNamespace(new_handle="", resumable=True))
        self.assertEqual(self.build()["session_resumption"], {"handle": "h2"})

    def test_the_runtime_can_tell_when_the_newest_handle_arrived(self):
        # After a GoAway the runtime waits for a handle newer than the last turn.
        self.assertIsNone(session.handle_received_at())
        session.remember(update("h1"), clock=lambda: 5.0)
        session.remember(update("h2", resumable=False), clock=lambda: 9.0)   # not resumable: ignored
        self.assertEqual(session.handle_received_at(), 5.0)
        session.forget()
        self.assertIsNone(session.handle_received_at())

    def test_failed_resume_falls_back_to_a_fresh_session(self):
        session.remember(update("h1"), clock=lambda: self.now)
        self.assertEqual(self.build()["session_resumption"], {"handle": "h1"})
        # That connection failed before any new handle arrived: next attempt starts fresh.
        self.assertEqual(self.build()["session_resumption"], {})
        session.remember(update("h2"), clock=lambda: self.now)  # A live session sends new ones.
        self.assertEqual(self.build()["session_resumption"], {"handle": "h2"})

    def test_expired_handles_are_not_used(self):
        session.remember(update("old"), clock=lambda: self.now)
        self.now += 2 * 3600
        self.assertEqual(self.build()["session_resumption"], {})

    def test_each_part_can_be_disabled_and_input_is_not_mutated(self):
        base = {"response_modalities": ["AUDIO"]}
        with patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "0", "BAYMAX_CONTEXT_COMPRESSION": "0"}):
            self.assertEqual(session.extend_config(base), base)
        self.assertEqual(base, {"response_modalities": ["AUDIO"]})
        with patch.dict(os.environ, {"BAYMAX_CONTEXT_TRIGGER_TOKENS": "nonsense"}):
            self.assertEqual(self.build()["context_window_compression"]["trigger_tokens"], 32000)

    def test_server_messages_store_handles_and_note_go_away(self):
        with patch("runtime_monitor.record") as record:
            session.on_server_message(SimpleNamespace(session_resumption_update=update("h9"), go_away=None))
            session.on_server_message(SimpleNamespace(session_resumption_update=None,
                                                      go_away=SimpleNamespace(time_left="10s")))
        record.assert_called_once()
        session.extend_config({})  # Offers h9 once.
        self.assertEqual(session._state["handle"], "h9")

    def test_full_ember_config_is_accepted_by_the_sdk(self):
        from google.genai import types
        from features.toolkits import gemini_tools
        session.remember(update("h1"))  # Real clock, like the runtime.
        config = gemini_tools.extend_config({"system_instruction": "base", "response_modalities": ["AUDIO"]})
        validated = types.LiveConnectConfig.model_validate(config)
        self.assertEqual(validated.session_resumption.handle, "h1")
        self.assertEqual(validated.context_window_compression.sliding_window.target_tokens, 16000)


# ── Which Gemini Live model (live_models.py) ──
OLD, NEW = "gemini-2.5-flash-native-audio-preview-12-2025", "gemini-3.8-live"
# Google's close reasons for a model it doesn't serve, and for a key out of quota (or over a
# rate or session limit) for one, cut to the 123 bytes a websocket close frame can carry.
NOT_FOUND = (f"models/{OLD} is not found for API version v1alpha, or is not supported for "
             "bidiGenerateContent. Call ListModels to see the list of available models and their supported methods.")[:123]
QUOTA = ("You exceeded your current quota, please check your plan and billing details. For more information "
         "on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.")[:123]


def api_error(code, reason):
    """What the SDK raises when Gemini closes a Live connection, in connect() or receive()."""
    try:
        errors.APIError.raise_error(code, reason, None)
    except errors.APIError as error:
        return error


def closed(code, reason):
    """What a send raises on a connection Gemini closed."""
    return ConnectionClosedError(Close(code, reason), Close(code, reason), True)


ORDINARY = (api_error(1011, "Internal error encountered."), api_error(1006, "Abnormal closure."),
            api_error(1000, ""), api_error(1001, "going away"), api_error(1013, "Try again later."),
            closed(1011, "Internal error encountered."), ConnectionClosedError(None, None),
            ConnectionResetError(), TimeoutError(), OSError("Network is unreachable"), ValueError("a bug"),
            ExceptionGroup("session", [ConnectionResetError(), TimeoutError()]))


class LiveModelTests(unittest.TestCase):
    def setUp(self):
        session.forget()
        self.addCleanup(session.forget)
        self.printed = patch("builtins.print").start()
        self.recorded = patch("runtime_monitor.record").start()
        self.addCleanup(patch.stopall)

    def test_the_list_is_baymax_live_models_else_model_then_gemini_3_8_live(self):
        self.assertEqual(live_models.configured(OLD, {}), (OLD, NEW))
        for blank in ("", " , "):
            self.assertEqual(live_models.configured(OLD, {"BAYMAX_LIVE_MODELS": blank}), (OLD, NEW))
        # Trimmed, "models/" dropped, each once, in the order given.
        self.assertEqual(live_models.configured(OLD, {"BAYMAX_LIVE_MODELS": f" {NEW}, models/{OLD},{NEW} "}),
                         (NEW, OLD))
        self.assertEqual(live_models.configured(OLD, {"BAYMAX_LIVE_MODELS": f"no such/model,{NEW}"}), (NEW,))
        self.assertEqual(live_models.configured(OLD, {"BAYMAX_LIVE_MODELS": "a b"}), (OLD, NEW))  # nothing usable
        self.assertEqual(self.printed.call_count, 2)  # each bad entry is named
        self.assertEqual(live_models.configured(NEW, {}), (NEW,))

    def test_gemini_3_gets_no_thinking_config_and_blocking_functions_and_2_5_is_unchanged(self):
        from features.toolkits import gemini_tools
        config = gemini_tools.extend_config({"system_instruction": "base", "response_modalities": ["AUDIO"],
                                             "thinking_config": {"thinking_budget": 0}})
        config["tools"].append({"function_declarations": [{"name": "eyes", "behavior": "NON_BLOCKING"}]})
        before = copy.deepcopy(config)
        self.assertIs(live_models.adapt(config, OLD), config)
        adapted = live_models.adapt(config, NEW)
        self.assertEqual(config, before)  # the runtime's own config is untouched
        self.assertNotIn("thinking_config", adapted)
        validated = types.LiveConnectConfig.model_validate(adapted)
        behaviors = {d.name: d.behavior for tool in validated.tools for d in tool.function_declarations or []}
        self.assertIn("set_voice_volume", behaviors)  # offered even with the toolkit off
        self.assertEqual(behaviors.pop("eyes"), types.Behavior.NON_BLOCKING)  # an explicit behavior is kept
        self.assertEqual(set(behaviors.values()), {types.Behavior.BLOCKING})  # as 2.5 runs them
        self.assertIsNone(validated.thinking_config)
        unchanged = lambda c: {k: v for k, v in c.items() if k not in ("thinking_config", "tools")}
        self.assertEqual(unchanged(adapted), unchanged(before))
        for name, version in ((OLD, 2), (NEW, 3), ("gemini-live-2.5-flash-preview", 2),
                              ("models/gemini-3.8-live", 3), ("learnlm-live", 0)):
            self.assertEqual(live_models.generation(name), version, name)

    def test_errors_that_mean_this_key_cannot_use_the_model(self):
        gone = (api_error(1008, NOT_FOUND), closed(1008, NOT_FOUND), api_error(1011, NOT_FOUND),
                api_error(1008, f"models/{OLD} is no longer available. Use a newer model."),
                api_error(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                          "message": f"models/{OLD} is not found for API version v1beta."}}),
                ExceptionGroup("session", [ConnectionResetError(), api_error(1008, NOT_FOUND)]))
        for error in gone:
            for set_up in (False, True):  # before setup, or before Gemini finished a turn
                with self.subTest(error=error, set_up=set_up):
                    self.assertTrue(live_models.unavailable(error, set_up))
        self.assertEqual(live_models.unavailable(api_error(1011, f"models/{OLD} is no longer available. Use a newer model.")),
                         f"1011 models/{OLD} is no longer available")
        # Google refusing the setup itself (1007/1008) is about this model and config; the
        # same codes later in a connection are not.
        for error in (api_error(1008, "Operation is not implemented, or supported, or enabled."),
                      api_error(1007, "Request contains an invalid argument."), closed(1007, "Invalid argument.")):
            with self.subTest(error=error):
                self.assertTrue(live_models.unavailable(error, set_up=False))
                self.assertIsNone(live_models.unavailable(error, set_up=True))

    def test_ordinary_failures_are_never_about_the_model(self):
        for error in ORDINARY:
            for set_up in (False, True):
                with self.subTest(error=error, set_up=set_up):
                    self.assertIsNone(live_models.unavailable(error, set_up))

    def test_a_quota_close_is_never_taken_for_a_model_this_key_cannot_use(self):
        # A per-minute or concurrent-session limit says the same as a key with no quota, and passes:
        # one such close used to keep Ember on the next model, untested, until it restarted.
        for error in (api_error(1011, QUOTA), closed(1011, QUOTA), api_error(1008, QUOTA), closed(1007, QUOTA),
                      ExceptionGroup("session", [ConnectionResetError(), api_error(1011, QUOTA)])):
            for set_up in (False, True):
                with self.subTest(error=error, set_up=set_up):
                    self.assertIsNone(live_models.unavailable(error, set_up))
        choice = live_models.ModelChoice([OLD, NEW])
        self.assertFalse(choice.move_on(api_error(1011, QUOTA), set_up=False, served=False))
        self.assertEqual(choice.current, OLD)

    def test_a_model_this_key_cannot_use_is_replaced_at_once_and_kept(self):
        choice = live_models.ModelChoice([OLD, NEW])
        session.remember(update("issued by 2.5"))
        self.assertTrue(choice.move_on(api_error(1008, NOT_FOUND), set_up=False, served=False))
        self.assertEqual(choice.current, NEW)
        self.assertIsNone(session.handle_received_at())  # a handle belongs to the model that issued it
        for error in ORDINARY:  # kept: lost connections reconnect with it, as before
            self.assertFalse(choice.move_on(error, set_up=True, served=False))
        self.assertEqual(choice.current, NEW)
        self.printed.assert_called_once()
        self.assertIn(f"{OLD} can't be used with this API key (1008 models/{OLD} is not found",
                      self.printed.call_args.args[0])
        self.assertIn(f"switching to {NEW} until Ember restarts", self.printed.call_args.args[0])
        self.recorded.assert_called_once()

    def test_when_no_model_can_be_used_each_round_waits_and_starts_again(self):
        choice = live_models.ModelChoice([OLD, NEW])
        rounds = [[choice.move_on(api_error(1008, NOT_FOUND), set_up=True, served=False) for _ in range(2)]
                  for _ in range(3)]
        # The next model at once; when both have failed, run()'s usual pause, then the next round.
        self.assertEqual(rounds, [[True, False]] * 3)
        self.assertEqual(choice.current, OLD)
        self.assertEqual(self.printed.call_count, 2)  # the switch and "no model", once each

    def test_a_model_that_finished_a_turn_is_kept_and_a_new_count_starts(self):
        choice = live_models.ModelChoice([OLD, NEW])
        gone = api_error(1008, NOT_FOUND)
        self.assertFalse(choice.move_on(gone, set_up=True, served=True))  # it worked: reconnect with it
        self.assertEqual(choice.current, OLD)
        self.assertTrue(choice.move_on(gone, set_up=True, served=False))
        self.assertFalse(choice.move_on(gone, set_up=True, served=True))  # NEW worked too
        self.assertTrue(choice.move_on(gone, set_up=True, served=False))  # so OLD is next, at once
        self.assertEqual(choice.current, OLD)
        self.assertEqual(self.printed.call_count, 2)  # one line per switch after a model worked

    def test_a_refused_resume_is_not_taken_for_a_model_this_key_cannot_use(self):
        choice = live_models.ModelChoice([OLD, NEW])
        refused = api_error(1007, "Request contains an invalid argument.")
        choice.adapt({"session_resumption": {"handle": "h1"}})  # every reconnect resumes like this
        self.assertFalse(choice.move_on(refused, set_up=False, served=False))
        self.assertEqual(choice.current, OLD)  # session.py starts the next one fresh
        choice.adapt({"session_resumption": {}})
        self.assertTrue(choice.move_on(refused, set_up=False, served=False))  # refused fresh: the model's
        choice = live_models.ModelChoice([OLD, NEW])
        choice.adapt({"session_resumption": {"handle": "h1"}})
        self.assertTrue(choice.move_on(api_error(1008, NOT_FOUND), set_up=False, served=False))  # its words count

    def test_with_one_model_ember_just_retries_it_and_keeps_its_handle(self):
        choice = live_models.ModelChoice([OLD])
        session.remember(update("h1"))
        self.assertFalse(choice.move_on(api_error(1008, NOT_FOUND), set_up=False, served=False))
        self.assertEqual(choice.current, OLD)
        self.assertIsNotNone(session.handle_received_at())  # session.py decides about it, as before


class LiveModelSdkTests(unittest.TestCase):
    """The real SDK's connect(), receive() and send, against a local websocket in Gemini's
    place: the errors it raises for a model Google doesn't serve or a key out of quota,
    and the setup it sends for each model."""

    def connect(self, answer, model=OLD, config=None, then=None):
        """Connects with the real SDK. `answer(connection)` is the local Gemini's reply to
        the setup; `then(session)` runs on an open session. Returns (what was raised, or
        None; the setup messages)."""
        setups = []

        async def gemini(connection):
            setups.append(json.loads(await connection.recv())["setup"])
            await answer(connection)

        async def exercise():
            async with ws_serve(gemini, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                local = lambda uri, **kwargs: ws_connect(f"ws://127.0.0.1:{port}", proxy=None)
                client = genai.Client(api_key="test", http_options={"api_version": "v1alpha"})
                with patch("google.genai.live.ws_connect", local):
                    try:
                        async with client.aio.live.connect(model=model, config=config or {}) as live:
                            if then is not None:
                                await then(live)
                    except Exception as error:
                        return error
        return asyncio.run(asyncio.wait_for(exercise(), 5)), setups

    @staticmethod
    def set_up_then_close(code, reason):
        async def answer(connection):
            await connection.send(json.dumps({"setupComplete": {}}))
            await connection.close(code, reason)
        return answer

    def test_a_model_google_does_not_serve_fails_while_setting_up(self):
        async def not_found(connection):
            await connection.close(1008, NOT_FOUND)
        error, setups = self.connect(not_found)
        self.assertIsInstance(error, errors.APIError)
        self.assertEqual(setups[0]["model"], f"models/{OLD}")
        self.assertTrue(live_models.unavailable(error, set_up=False))

    def test_a_gone_model_after_setup_is_recognised_in_receive_and_send_and_a_quota_close_is_not(self):
        for code, reason, gone in ((1008, NOT_FOUND, True), (1011, QUOTA, False)):
            raised = []

            async def receive_then_send(live):
                for step in (lambda: anext(live.receive()),
                             lambda: live.send_realtime_input(audio=types.Blob(data=b"\0\0", mime_type="audio/pcm;rate=48000"))):
                    try:
                        await step()
                    except Exception as error:
                        raised.append(error)
            self.connect(self.set_up_then_close(code, reason), then=receive_then_send)
            self.assertEqual([type(e) for e in raised], [errors.APIError, ConnectionClosedError])
            for error in raised:
                with self.subTest(reason=reason, error=error):
                    self.assertEqual(bool(live_models.unavailable(error, set_up=True)), gone)

    def test_an_internal_error_after_setup_is_not_about_the_model(self):
        raised = []

        async def receive(live):
            try:
                await anext(live.receive())
            except Exception as error:
                raised.append(error)
        self.connect(self.set_up_then_close(1011, "Internal error encountered."), then=receive)
        self.assertIsInstance(raised[0], errors.APIError)
        self.assertIsNone(live_models.unavailable(raised[0], set_up=True))

    def test_each_model_gets_its_settings_in_the_setup_message(self):
        config = {"response_modalities": ["AUDIO"], "thinking_config": {"thinking_budget": 0},
                  "tools": [{"function_declarations": [{"name": "get_weather"},
                                                       {"name": "eyes", "behavior": "NON_BLOCKING"}]}]}
        sent = {}
        for model in (OLD, NEW):
            _, setups = self.connect(self.set_up_then_close(1000, ""), model, live_models.adapt(config, model))
            sent[model] = setups[0]
        self.assertEqual(sent[OLD]["generationConfig"]["thinkingConfig"], {"thinking_budget": 0})
        self.assertEqual([d.get("behavior") for d in sent[OLD]["tools"][0]["functionDeclarations"]],
                         [None, "NON_BLOCKING"])
        self.assertEqual(sent[NEW]["model"], f"models/{NEW}")
        self.assertNotIn("thinkingConfig", sent[NEW]["generationConfig"])
        self.assertEqual([d["behavior"] for d in sent[NEW]["tools"][0]["functionDeclarations"]],
                         ["BLOCKING", "NON_BLOCKING"])


if __name__ == "__main__":
    unittest.main()
