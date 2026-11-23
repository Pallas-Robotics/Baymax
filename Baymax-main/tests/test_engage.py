"""'Ember disengage' / 'Ember engage': fake wake listener and DSP, no devices, model or Gemini."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import numpy as np

import runtime_monitor as monitor
from features import persona
from features.conversational_ability import engage, processing
from features.conversational_ability.engage import Engagement, heard
from features.conversational_ability.processing import InputEnhancer
from features.eyes.controller import Eyes

MIC = np.full(960, 1000, dtype=np.int16)


class FakeListener:
    def __init__(self, on_wake):
        self.on_wake, self.ready, self.failed, self.fed, self.cleared, self.started = on_wake, True, False, [], 0, 0

    def start(self):
        self.started += 1

    def feed(self, samples):
        self.fed.append(samples)

    def clear(self):
        self.cleared += 1


class FakeDSP:
    speech_probability = 0.5
    gain_db = 0

    def reset(self):
        pass

    def process(self, near, far):
        return near.copy()


def words(*pairs):
    return {"result": [{"word": w, "conf": c} for w, c in pairs]}


class EngageTests(unittest.TestCase):
    def setUp(self):
        self.listeners = []

        def factory(on_wake):
            listener = FakeListener(on_wake)
            self.listeners.append(listener)
            return listener
        self.ember = Engagement(listener_factory=factory, audio_ready=lambda: True)
        self.told = []
        self.ember.tell = self.told.append
        self.events = []
        patcher = patch.object(monitor, "event_observer", self.events.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, text, message_id=1, speaker="user"):
        self.ember.observe(speaker, {"id": message_id, "text": text})

    def test_disengage_withholds_the_mic_and_only_ember_engage_wakes(self):
        self.assertFalse(self.ember.gate(MIC))  # Engaged: Gemini hears everything, no name needed.
        self.say("Ember, disengage.")
        self.assertTrue(self.ember.disengaged)
        self.assertIn("disengaged", self.events)
        listener = self.listeners[0]
        self.assertEqual(listener.started, 1)
        self.assertTrue(self.ember.gate(MIC))     # Withheld from Gemini...
        self.assertIs(listener.fed[-1], MIC)      # ...but heard by the offline listener.
        listener.on_wake("ember engage")
        self.assertFalse(self.ember.disengaged)
        self.assertFalse(self.ember.gate(MIC))
        self.assertIn("engaged", self.events)
        self.assertEqual(self.told, [engage.ENGAGED_NOTE])

    def test_transcript_variants_and_non_matches(self):
        for text in ("Ember disengage", "amber, dis-engage", "OK EMBER... DISENGAGE please", "ember disengaged"):
            ember = Engagement(listener_factory=FakeListener, audio_ready=lambda: True)
            ember.observe("user", {"id": 1, "text": text})
            self.assertTrue(ember.disengaged, text)
        for text in ("disengage", "Ember, engage", "remember to disengage the brake", "Ember, how are you?"):
            ember = Engagement(listener_factory=FakeListener, audio_ready=lambda: True)
            ember.observe("user", {"id": 1, "text": text})
            self.assertFalse(ember.disengaged, text)
        self.say("Okay, I'll be quiet. Ember disengage.", speaker="ember")  # Ember's own words never count.
        self.assertFalse(self.ember.disengaged)

    def test_later_fragments_of_the_same_utterance_do_not_disengage_again(self):
        self.say("Ember disengage")
        self.listeners[0].on_wake("ember engage")
        self.say("Ember disengage now", message_id=1)  # Late fragment of the first message.
        self.assertFalse(self.ember.disengaged)
        self.say("Ember disengage", message_id=2)
        self.assertTrue(self.ember.disengaged)
        self.assertEqual(len(self.listeners), 1)  # One listener (and model) reused.

    def test_wake_rules_need_a_confident_ember_engage_pair(self):
        self.assertEqual(heard(words(("ember", 1.0), ("engage", 1.0)), True), "ember engage")
        self.assertEqual(heard(words(("[unk]", 1.0), ("ember", 0.95), ("engage", 0.93)), True), "ember engage")
        self.assertIsNone(heard({"partial": "ember engage"}, False))
        self.assertIsNone(heard(words(("ember", 1.0)), True))                       # Name alone.
        self.assertIsNone(heard(words(("engage", 1.0)), True))
        self.assertIsNone(heard(words(("ember", 1.0), ("disengage", 1.0)), True))
        self.assertIsNone(heard(words(("ember", 1.0), ("[unk]", 1.0), ("engage", 1.0)), True))
        self.assertIsNone(heard(words(("ember", 0.6), ("engage", 1.0)), True))

    def test_missing_wake_model_engages_again_instead_of_leaving_ember_deaf(self):
        self.say("Ember disengage")
        self.listeners[0].failed = True
        self.assertFalse(self.ember.gate(MIC))
        self.assertFalse(self.ember.disengaged)
        self.assertEqual(self.told, [engage.UNAVAILABLE_NOTE])

    def test_listener_still_loading_keeps_the_mic_withheld(self):
        self.say("Ember disengage")
        self.listeners[0].ready = False
        self.assertTrue(self.ember.gate(MIC))

    def test_loading_wake_listener_cannot_leave_microphone_withheld_forever(self):
        now = [100.0]
        self.ember.clock = lambda: now[0]
        self.say("Ember disengage")
        self.listeners[0].ready = False
        self.assertTrue(self.ember.gate(MIC))
        now[0] += 5.1
        self.assertFalse(self.ember.gate(MIC))
        self.assertFalse(self.ember.disengaged)
        self.assertEqual(self.told, [engage.UNAVAILABLE_NOTE])

    def test_wake_pair_can_span_two_final_results_but_not_an_old_utterance(self):
        previous = {"result": [{"word": "ember", "conf": .95, "start": 0, "end": .3}]}
        current = {"result": [{"word": "engage", "conf": .96, "start": .6, "end": 1.0}]}
        self.assertEqual(heard(current, True, previous=previous), "ember engage")
        current["result"][0]["start"] = 4.0
        self.assertIsNone(heard(current, True, previous=previous))
        current["result"][0].update(start=.6, conf=.6)
        self.assertIsNone(heard(current, True, previous=previous))

    def test_without_audio_cleanup_disengage_is_refused(self):
        self.ember.audio_ready = lambda: False
        self.say("Ember disengage")
        self.assertFalse(self.ember.disengaged)
        self.assertEqual(self.listeners, [])
        self.assertEqual(self.told, [engage.UNAVAILABLE_NOTE])

    def test_default_readiness_requires_a_healthy_active_processor(self):
        with patch.object(processing, 'enabled', return_value=True):
            for processor, expected in ((None, False), (SimpleNamespace(failed=True), False),
                                        (SimpleNamespace(failed=False), True)):
                with patch.object(processing, '_active_enhancer', processor):
                    self.assertEqual(engage.audio_ready(), expected)

    def test_dsp_failure_reengages_before_raw_fallback(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        enhancer.processor.process = lambda near, far: (_ for _ in ()).throw(RuntimeError('DSP unavailable'))
        told = []
        with patch.object(processing, '_active_enhancer', enhancer), patch.multiple(
                engage.engagement, disengaged=True, listener=None, tell=told.append):
            result = enhancer.process(MIC)
            self.assertTrue(enhancer.failed)
            self.assertFalse(engage.engagement.disengaged)
            self.assertTrue(np.any(result))
        self.assertEqual(told, [engage.UNAVAILABLE_NOTE])

    def test_disabled_ignores_disengage(self):
        with patch.dict("os.environ", {"BAYMAX_ENGAGE_WORDS": "0"}):
            self.say("Ember disengage")
        self.assertFalse(self.ember.disengaged)

    def test_engage_for_other_reasons_is_silent_and_only_when_disengaged(self):
        self.assertFalse(self.ember.engage("reminder due"))
        self.say("Ember disengage")
        self.assertTrue(self.ember.engage("reminder due"))
        self.assertEqual(self.told, [])  # The reminder or fall check-in itself is what Ember says.

    def test_gate_in_audio_cleanup_and_ahead_of_the_audiobook(self):
        self.say("Ember disengage")
        book_gate = lambda samples: False
        gates = [book_gate]
        engage.register(gates=gates, listeners=[], tasks=[])
        engage.register(gates=gates, listeners=[], tasks=[])
        self.assertEqual(gates, [engage.engagement.gate, book_gate])  # First, once.
        with patch.object(processing, "input_gates", [self.ember.gate]), \
                patch.dict("os.environ", {"BAYMAX_MUTE_MIC_DURING_REPLY": "1"}):
            enhancer = InputEnhancer(processor=FakeDSP())
            self.assertFalse(np.any(enhancer.process(MIC)))  # Gemini gets silence.
            self.assertTrue(np.any(self.listeners[0].fed[-1]))

    def test_registered_on_mic_transcripts_and_sessions(self):
        from features.toolkits.fall_detection import session_tasks
        self.assertEqual(processing.input_gates[0], engage.engagement.gate)
        self.assertIn(engage.engagement.observe, monitor.transcript_listeners)
        self.assertIn(engage.engagement.session_task, session_tasks)

    def test_transcribe_reaches_listeners(self):
        seen = []
        with patch.object(monitor, "transcript_listeners", [lambda speaker, message: seen.append(message["text"])]), \
                patch.object(monitor, "transcript_observer", None):
            monitor.transcribe("user", "Ember ")
            monitor.transcribe("user", "disengage")
            monitor.finish_transcripts()
        self.assertEqual(seen[-1], "Ember disengage")

    def test_engaged_note_goes_through_the_live_session(self):
        ember = Engagement(listener_factory=FakeListener, audio_ready=lambda: True)
        session = AsyncMock()

        async def run():
            await ember.session_task(session, None)
            ember.tell(engage.ENGAGED_NOTE)
            await asyncio.sleep(0.01)
        asyncio.run(run())
        text = session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertEqual(text, engage.ENGAGED_NOTE)

    def test_prompt_section_only_on_the_mvp_persona(self):
        config = engage.apply({"system_instruction": persona.PERSONA})
        self.assertIn(engage.MARKER, config["system_instruction"])
        self.assertEqual(engage.apply(config), config)  # Once.
        self.assertEqual(engage.apply({"system_instruction": "base"}), {"system_instruction": "base"})
        with patch.dict("os.environ", {"BAYMAX_ENGAGE_WORDS": "0"}):
            self.assertNotIn(engage.MARKER, engage.apply({"system_instruction": persona.PERSONA})["system_instruction"])

    def test_listener_uses_the_shared_model_with_engage_words(self):
        import scipy.signal  # Load DSP outside the listener's bounded startup wait.
        from features.toolkits import wakeword
        made = []
        with patch.object(wakeword, "_vosk_recognizer", lambda vocabulary=wakeword.VOCABULARY: made.append(vocabulary) or Recognizer()):
            woke = []
            listener = engage._listener(woke.append)
            listener.start()
            self.assertTrue(wait_for(lambda: listener.ready))
            for _ in range(10):
                listener.feed(np.zeros(1024, dtype=np.int16))
            self.assertTrue(wait_for(lambda: woke == ["ember engage"]))
        self.assertEqual(made, [engage.VOCABULARY])

    def test_custom_engage_rules_receive_previous_final_result(self):
        import scipy.signal
        from features.toolkits import wakeword
        class SplitRecognizer:
            count = 0
            def AcceptWaveform(self, pcm):
                self.count += 1
                return True
            def Result(self):
                word = 'ember' if self.count == 1 else 'engage'
                return json.dumps({'result': [{'word': word, 'conf': 1.0,
                    'start': self.count * .2, 'end': self.count * .2 + .1}]})
        with patch.object(wakeword, '_vosk_recognizer', lambda vocabulary: SplitRecognizer()):
            woke = []
            listener = engage._listener(woke.append)
            listener.start()
            self.assertTrue(wait_for(lambda: listener.ready))
            listener.feed(np.zeros(1024, dtype=np.int16))
            listener.feed(np.zeros(1024, dtype=np.int16))
            self.assertTrue(wait_for(lambda: woke == ['ember engage']))

    def test_wake_worker_failure_is_reported_to_the_microphone_gate(self):
        import scipy.signal
        from features.toolkits.wakeword import WakeListener
        class BrokenRecognizer:
            def AcceptWaveform(self, pcm):
                raise RuntimeError('decoder failed')
        listener = WakeListener(lambda phrase: None, recognizer_factory=BrokenRecognizer)
        listener.start()
        self.assertTrue(wait_for(lambda: listener.ready))
        listener.feed(np.zeros(1024, dtype=np.int16))
        self.assertTrue(wait_for(lambda: listener.failed))
        self.assertFalse(listener.ready)


class Recognizer:
    def __init__(self):
        self.bytes = 0

    def AcceptWaveform(self, pcm):
        self.bytes += len(pcm)
        return self.bytes >= 3200 * 2

    def Result(self):
        return json.dumps({"text": "ember engage", "result": [{"word": "ember", "conf": 1.0, "start": 0, "end": 0.1},
                                                              {"word": "engage", "conf": 1.0, "start": 0.1, "end": 0.2}]})


def wait_for(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


class EyesTests(unittest.TestCase):
    def test_sleep_eyes_while_disengaged(self):
        now = [100.0]
        eyes = Eyes(clock=lambda: now[0])
        eyes.event("listening")
        self.assertEqual(eyes.mode(), "normal")
        eyes.event("disengaged")
        self.assertEqual(eyes.mode(), "sleep")
        eyes.event("speaking")  # The short goodbye plays under sleep eyes.
        self.assertEqual(eyes.mode(), "sleep")
        eyes.event("engaged")
        self.assertEqual(eyes.mode(), "normal")
        eyes.event("disengaged")
        eyes.event("fall_alert")
        self.assertEqual(eyes.mode(), "surprised")  # Safety faces still show.


if __name__ == "__main__":
    unittest.main()
