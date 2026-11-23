"""Ember plays a reply only when spoken to: the gate alone, then v10's real receive loop."""
import asyncio
import ast
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from features.conversational_ability import addressing
from features.conversational_ability.addressing import AddressGate
from test_public_core_integration import load_function
from test_public_core_integration import CORE


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def gate(**kwargs):
    clock = Clock()
    log = Mock()
    return AddressGate(follow_up=20, clock=clock, log=log, **kwargs), clock, log


def turn(g, user, reply="ok"):
    """One exchange: the user's words, then Gemini's reply. Returns what played."""
    played = []
    for word in user:
        for held in g.heard(word):
            held()
    g.reply(lambda: played.append(reply), reply)
    g.end()
    return played


class AddressGateTests(unittest.TestCase):
    def test_runtime_default_allows_direct_requests_after_a_long_pause(self):
        # Evaluate the actual runtime binding without importing devices or providers.
        tree = ast.parse(CORE.read_text(encoding='utf-8'))
        binding = next(n for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == '_address_gate' for t in n.targets))
        clock, log = Clock(), Mock()
        for setting, only_addressed, expected_enabled in [(None, True, False), ('0', True, False),
                                                         ('1', True, True), ('1', False, False)]:
            namespace = {'os': os, 'ONLY_WHEN_ADDRESSED': only_addressed,
                         'AddressGate': lambda **kw: AddressGate(clock=clock, log=log, **kw)}
            with self.subTest(setting=setting, only_addressed=only_addressed), patch.dict(os.environ, {}, clear=True):
                if setting is not None:
                    os.environ['BAYMAX_ADDRESS_GATE'] = setting
                exec(compile(ast.Module(body=[binding], type_ignores=[]), str(CORE), 'exec'), namespace)
                g = namespace['_address_gate']
                self.assertEqual(g.enabled, expected_enabled)
                clock.now = 1000
                g.spoke(clock.now)
                clock.now += 84
                expected = [] if expected_enabled else ['ok']
                self.assertEqual(turn(g, ['Tell me about yourself.']), expected)
                clock.now += 14
                self.assertEqual(turn(g, ['Tell me about yourself.']), expected)

    def test_name_lets_a_reply_through_and_mishearings_count(self):
        for words in (["Ember,", " can you hear me?"], ["Hey", " A", "mber"], ["amber what time is it"],
                      ["Hey A mber , wha t's the weather"], ["Hey , E mperor , what 's the weather"],
                      ["E mber"]):
            g, _, _ = gate()
            self.assertEqual(turn(g, words), ["ok"], words)

    def test_side_conversation_and_noise_are_dropped_and_logged(self):
        g, _, log = gate()
        self.assertEqual(turn(g, ["thanks for calling. I did finish my homework."], "That's good to hear."), [])
        self.assertEqual(turn(g, ["<noise>"]), [])
        self.assertIn("That's good to hear.", log.call_args_list[0].args[0])
        self.assertTrue(addressing.names_ember("Ember's here"))
        for text in ("remember this", "a member of the club", "the member", "in November", "a chamber"):
            self.assertFalse(addressing.names_ember(text), text)

    def test_follow_up_window_starts_when_ember_stops_talking(self):
        g, clock, _ = gate()
        self.assertEqual(turn(g, ["Ember, hello"]), ["ok"])
        g.spoke(clock.now + 5)          # five seconds of reply still buffered
        clock.now += 24                 # the person answers 19 s after Ember finished
        self.assertEqual(turn(g, ["is my voice clear?"]), ["ok"])
        g.spoke(clock.now)
        clock.now += 21
        self.assertEqual(turn(g, ["is my voice clear?"]), [])

    def test_noise_inside_the_window_is_not_an_answer(self):
        g, clock, _ = gate()
        turn(g, ["Ember, hi"])
        g.spoke(clock.now)
        clock.now += 5
        self.assertEqual(turn(g, ["<noise>"], "You look tired."), [])

    def test_embers_own_messages_always_get_their_reply_once(self):
        g, clock, _ = gate()
        g.prompted()
        self.assertEqual(turn(g, []), ["ok"])
        clock.now += 100
        self.assertEqual(turn(g, ["people chatting"]), [])
        g.prompted()
        clock.now += addressing.PROMPT_TTL_S + 1   # Gemini never answered that one
        self.assertEqual(turn(g, ["people chatting"]), [])

    def test_late_transcription_releases_the_held_reply_in_order(self):
        g, _, _ = gate()
        played = []
        g.heard("Hey")
        g.reply(lambda: played.append(1), "one")
        g.reply(lambda: played.append(2), "two")
        self.assertEqual(played, [])
        for held in g.heard(" Ember"):
            held()
        g.reply(lambda: played.append(3))
        g.end()
        self.assertEqual(played, [1, 2, 3])

    def test_prompt_sent_mid_reply_does_not_release_an_overheard_reply(self):
        g, _, _ = gate()
        played = []
        g.heard("people chatting")
        g.reply(lambda: played.append("overheard"))
        g.prompted()
        g.reply(lambda: played.append("overheard"))
        g.end()
        self.assertEqual(played, [])
        self.assertEqual(turn(g, [], "alert answer"), ["alert answer"])  # the next reply answers it
        self.assertEqual(turn(g, ["people chatting"]), [])

    def test_tool_call_from_an_addressed_turn_keeps_its_answer(self):
        g, _, _ = gate()
        g.heard("Ember, what's the weather?")
        g.tool_call()
        g.end()                         # the tool-call turn ends before the result is spoken
        self.assertEqual(turn(g, [], "Sunny."), ["Sunny."])
        g.heard("they said it's sunny")
        g.tool_call()
        g.end()
        self.assertEqual(turn(g, [], "Sunny."), [])

    @staticmethod
    def deliver(g, args=(), kwargs=None):
        g.prompted()
        g.sent(args, {"turn_complete": True} if kwargs is None else kwargs)

    def test_message_whose_reply_was_still_playing_is_sent_again(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                g, clock, _ = gate(enabled=enabled)
                self.deliver(g, ("hello",))
                self.assertTrue(g.reply(lambda: None))
                g.spoke(clock.now + 5)
                g.end()  # all of it arrived, but it was still queued for the speaker
                self.assertEqual(g.abandon(True), [(("hello",), {"turn_complete": True})])
                self.assertEqual(g.abandon(True), [])  # handed over once

    def test_message_with_no_reply_yet_is_sent_again_within_the_prompt_window(self):
        g, clock, _ = gate()
        self.deliver(g)
        self.assertEqual(g.abandon(False), [((), {"turn_complete": True})])
        self.deliver(g)
        clock.now += addressing.PROMPT_TTL_S + 1
        self.assertEqual(g.abandon(False), [])

    def test_message_whose_reply_was_fully_heard_is_not_sent_again(self):
        g, clock, _ = gate()
        self.deliver(g)
        self.assertTrue(g.reply(lambda: None))
        g.spoke(clock.now + 1)
        g.end()
        clock.now += 2
        self.assertEqual(g.abandon(False), [])

    def test_an_earlier_heard_message_is_not_repeated_with_a_cut_off_one(self):
        g, clock, _ = gate()
        self.deliver(g, ("greeting",))
        self.assertTrue(g.reply(lambda: None))
        g.spoke(clock.now + 1)
        g.end()
        clock.now += 3
        self.deliver(g, ("family message",))
        self.assertTrue(g.reply(lambda: None))
        g.spoke(clock.now + 5)
        g.end()
        self.assertEqual(g.abandon(True), [(("family message",), {"turn_complete": True})])

    def test_a_message_that_never_reached_gemini_is_left_to_its_sender(self):
        g, clock, _ = gate()
        class Dead:
            async def send_client_content(self, **kwargs):
                raise ConnectionResetError()
        session = g.watch(Dead())
        with self.assertRaises(ConnectionResetError):
            asyncio.run(session.send_client_content(turns={"text": "hi"}, turn_complete=True))
        self.assertEqual(g._sent, [])
        self.assertEqual(g.abandon(True), [])

    def test_once_the_person_speaks_a_cut_off_reply_is_theirs_not_the_message(self):
        g, clock, _ = gate()
        self.deliver(g)
        self.assertTrue(g.reply(lambda: None))
        g.spoke(clock.now + 5)
        g.end()
        g.heard("<noise>")
        self.assertEqual(len(g._sent), 1)  # noise isn't the person answering
        g.heard("I'm fine, thanks")
        self.assertEqual(g.abandon(True), [])

    def test_only_recent_messages_are_sent_again(self):
        g, clock, _ = gate()
        self.deliver(g, ("old",), {})
        clock.now += addressing.RESEND_WINDOW_S + 1
        self.deliver(g, ("new",), {})
        self.assertEqual(g.abandon(True), [(("new",), {})])

    def test_abandon_ends_a_held_reply_without_counting_it_as_answered(self):
        g, clock, log = gate()
        self.assertFalse(g.reply(lambda: None, "overheard"))
        self.assertEqual(g.abandon(False), [])
        self.assertIn("overheard", str(log.mock_calls) if hasattr(log, "mock_calls") else str(log))

    def test_disabled_gate_plays_everything(self):
        g, _, _ = gate(enabled=False)
        self.assertEqual(turn(g, ["<noise>"]), ["ok"])

    def test_watched_session_notes_complete_messages_only(self):
        sent = []
        async def send_client_content(**kwargs):
            sent.append(kwargs)
        g, _, _ = gate()
        session = g.watch(SimpleNamespace(send_client_content=send_client_content, other="kept"))
        asyncio.run(session.send_client_content(turns={"parts": [{"text": "memory"}]}, turn_complete=False))
        self.assertEqual(turn(g, ["chatting"]), [])
        asyncio.run(session.send_client_content(turns={"parts": [{"text": "[FALL ALERT]"}]}, turn_complete=True))
        self.assertEqual(turn(g, []), ["ok"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(session.other, "kept")

    def test_follow_up_setting(self):
        with patch.dict(os.environ, {"BAYMAX_FOLLOW_UP_SECONDS": "35"}):
            self.assertEqual(addressing.follow_up_seconds(), 35)
        with patch.dict(os.environ, {"BAYMAX_FOLLOW_UP_SECONDS": "soon"}):
            self.assertEqual(addressing.follow_up_seconds(), 20)


def content(user=None, ember=None, audio=None, done=False, interrupted=False):
    model_turn = None
    if audio is not None:
        model_turn = SimpleNamespace(parts=[SimpleNamespace(
            text=None, inline_data=SimpleNamespace(mime_type="audio/pcm;rate=24000", data=audio))])
    return SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=None,
                           server_content=SimpleNamespace(
                               input_transcription=SimpleNamespace(text=user) if user else None,
                               output_transcription=SimpleNamespace(text=ember) if ember else None,
                               model_turn=model_turn, turn_complete=done, interrupted=interrupted))


class V10ReceiveLoopTests(unittest.TestCase):
    """The 2026-10-21 ember-002 transcript through v10's real receive_audio."""

    def run_receive(self, responses):
        class Session:
            def receive(self):
                async def stream():
                    for response in responses:
                        if callable(response):
                            response(clock)  # time passes between messages
                            continue
                        yield response
                    raise asyncio.CancelledError()
                return stream()
        played, said, heard = [], [], []
        self.heard = heard  # the user's transcript, now recorded through _transcript_add
        self.playback_states = []
        self.playback_starts = []
        def append_playback(data, *, new_turn=False, timing_id=None):
            played.append(data)
            self.playback_states.append(namespace['_gemini_speaking'])
            self.playback_starts.append(new_turn)
        g, clock, log = gate()
        namespace = {
            "asyncio": asyncio, "np": np, "_address_gate": g,
            "time": SimpleNamespace(monotonic=clock, perf_counter=time.perf_counter),
            "public_toolkit": SimpleNamespace(), "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(),
                                                                           finish_transcripts=Mock(), timing=Mock()),
            "_transcript_add": lambda speaker, text: (said if speaker == "gemini" else heard).append(text.strip()),
            "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
            "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
            "_recent_user_words": [], "MEMORY_WORD_WINDOW": 50, "_memory_embedder": None,
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_append_playback": append_playback,
            "_playback_buffer": b"", "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
            "tracker_receive": SimpleNamespace(record=Mock()), "tracker_first_audio": SimpleNamespace(record=Mock()),
            "tracker_roundtrip": SimpleNamespace(record=Mock()), "_last_mic_send_ts": 0.0,
            "_last_robot_speech_ts": 0.0, "_last_user_speech_ts": 0.0, "_flush_playback": Mock(),
        }
        self.namespace = namespace
        receive = load_function("receive_audio", namespace)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(receive(Session()))
        return played, said, log

    def test_addressed_reply_plays_and_side_talk_is_silent(self):
        pcm = np.array([1, 2], dtype=np.int16).tobytes()
        played, said, log = self.run_receive([
            content(user="Ember, can you hear me?"), content(ember="Yes, I can hear you.", audio=pcm),
            content(done=True), lambda clock: setattr(clock, "now", clock.now + 60),
            content(user="thanks for calling. I did finish my homework."),
            content(ember="That's good to hear.", audio=pcm), content(done=True),
        ])
        self.assertEqual(played, [np.repeat(np.frombuffer(pcm, dtype=np.int16), 2).tobytes()])
        self.assertEqual(said, ["Yes, I can hear you."])
        self.assertIn("That's good to hear.", log.call_args.args[0])

    def test_interruption_drops_co_delivered_output_but_preserves_new_user_input(self):
        pcm = np.array([1, 2], dtype=np.int16).tobytes()
        played, said, _ = self.run_receive([
            content(user="Ember, tell me about cats"),
            content(ember="Cats are", audio=pcm),
            content(user="Ember, wait, I meant dogs", ember="old sentence fragment",
                    audio=pcm, interrupted=True, done=True),
            content(ember="Dogs are", audio=pcm), content(done=True),
        ])
        self.assertEqual(len(played), 2)  # No audio chunk from the cancellation message.
        self.assertEqual(said, ["Cats are", "Dogs are"])
        self.namespace['_flush_playback'].assert_called_once()
        self.assertIn("Ember, wait, I meant dogs", self.heard)
        self.assertEqual(self.playback_states, [True, True])
        self.assertFalse(self.namespace['_gemini_speaking'])

    def test_audio_chunks_mark_only_the_start_of_each_reply(self):
        pcm = np.array([1, 2], dtype=np.int16).tobytes()
        self.run_receive([
            content(user="Ember, tell me about cats"), content(audio=pcm), content(audio=pcm),
            content(done=True), content(user="What about dogs?"), content(audio=pcm),
            content(audio=pcm), content(done=True),
        ])
        self.assertEqual(self.playback_starts, [True, False, True, False])


if __name__ == "__main__":
    unittest.main()
