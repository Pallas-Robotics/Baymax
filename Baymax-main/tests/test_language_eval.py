import asyncio
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from evals import language_eval as languages
from evals import live_eval
from evals.languages.fixtures import scenarios
from test_live_eval import FakeSession, connector, server


class LanguageEvaluationTests(unittest.TestCase):
    def test_real_mvp_session_includes_language_adaptation(self):
        _, config = live_eval.mvp_session()
        prompt = config['system_instruction']
        self.assertIn('[LANGUAGE]', prompt)
        self.assertIn('switch with them on your next reply', prompt)
        self.assertIn('reply in the same mix', prompt)

    def test_language_policy_is_immediate_and_respects_explicit_override(self):
        _, config = live_eval.mvp_session()
        prompt = ' '.join(config['system_instruction'].split())
        self.assertEqual(prompt.count('[LANGUAGE]'), 1)
        self.assertIn('from the first word of your very next reply', prompt)
        self.assertIn('An explicit request for a response language overrides', prompt)
        self.assertIn('Otherwise, choose from the person\'s latest spoken turn', prompt)
        self.assertIn('Do not wait for another turn, ask permission, or announce the switch', prompt)
        self.assertIn('A language requested in an earlier turn does not lock later replies', prompt)
        self.assertNotIn('Honor an explicitly requested ongoing', prompt)

    def test_voice_followup_covers_direct_korean_to_spanish_text_and_audio(self):
        cases = live_eval.load_scenarios(Path(__file__).parents[1] / 'evals' / 'voice_controls.json')
        for mode in ('text', 'audio'):
            case = next(case for case in cases if case['name'] == 'korean-to-spanish-' + mode)
            live_eval.validate(case)
            key = 'text' if mode == 'text' else 'say'
            self.assertIn('Korean', case['steps'][1][key])
            self.assertIn('하늘', case['steps'][2][key])
            self.assertIn('hierba', case['steps'][3][key])
            self.assertNotIn('Spanish', case['steps'][3][key])
            self.assertEqual(case['steps'][3]['expect']['not_reply'], '[가-힣]')

    def setUp(self):
        self.catalog, self.cases = languages.load_inputs()
        self.case = next(case for case in self.cases if case['name'] == 'es')
        self.meta = dict(format=1, model='fake-offline-test', config_sha256='0' * 64, mode='text',
                         scenario_sha256={case['name']: languages.digest(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()) for case in self.cases})

    def results(self, count=3, mode='text', audio=None):
        meta = dict(self.meta, mode=mode)
        turns = [dict(step=step['text'], heard='synthetic input', reply=reply,
                      audio_bytes=10, failures=[], complete=True, timed_out=False, tools=[], latency_s=1, **(audio or {}))
                 for step, reply in zip(self.case['steps'], ['El cielo es azul.', 'La hierba es verde.', 'A ripe banana is yellow.'])]
        return dict(metadata=meta, scenarios={'es': {'runs': [{'turns': copy.deepcopy(turns)} for _ in range(count)]}})

    def reviewed(self, results):
        template = languages.review_template(self.cases, results)
        for row in template.values():
            row.update({key: True for key in languages.REVIEW_FIELDS})
        return template

    def test_sourced_catalog_is_complete_unique_and_not_a_validated_list(self):
        self.assertEqual(len(self.catalog['languages']), 99)
        self.assertTrue(self.catalog['source'].startswith('https://ai.google.dev/'))
        self.assertEqual(next(row for row in self.catalog['languages'] if row['code'] == 'no')['aliases'], ['nb'])
        report, _ = languages.compile_report(self.catalog, self.cases)
        self.assertEqual(report['valid_audio_languages'], [])
        self.assertEqual(report['scenarios']['es'], 'NOT_RUN')
        self.assertIn('NO_SCENARIO', {row['status'] for row in report['languages']})

    def test_every_fixture_and_both_modes_use_existing_harness(self):
        self.assertEqual(self.cases, scenarios())
        self.assertEqual(len(self.cases), 27)
        for case in self.cases:
            for mode in ('text', 'audio'):
                scenario = languages.scenario_for(case, mode)
                live_eval.validate(scenario)
                self.assertIn('say' if mode == 'audio' else 'text', scenario['steps'][0])
                if mode == 'audio':
                    pattern = scenario['steps'][0]['expect']['heard']
                    self.assertNotRegex(' . 123 _ ', pattern)
                    self.assertRegex('नमस्ते', pattern)
        self.assertEqual(self.case['steps'][-1]['expected_languages'], ['en'])
        self.assertTrue(any(len(step['expected_languages']) > 1 for case in self.cases for step in case['steps']))

    def test_keyword_success_without_language_review_never_validates(self):
        results = self.results()
        report, _ = languages.compile_report(self.catalog, self.cases, results)
        self.assertEqual(report['scenarios']['es'], 'NEEDS_REVIEW')
        self.assertFalse(report['valid_audio_languages'])
        row = next(row for row in report['languages'] if row['code'] == 'es')
        self.assertEqual((row['automatic_passed_runs'], row['attempted_runs']), (3, 3))

    def test_text_review_cannot_certify_spoken_language(self):
        results = self.results()
        report, _ = languages.compile_report(self.catalog, self.cases, results, self.reviewed(results))
        self.assertEqual(report['scenarios']['es'], 'REVIEWED_TEXT_ONLY')
        self.assertFalse(report['valid_audio_languages'])

    def test_insufficient_runs_empty_turns_and_errors_cannot_pass(self):
        variants = [(self.results(count=1), 'INSUFFICIENT_REPEATS'),
                    (dict(metadata=self.meta, scenarios={'es': {'runs': [{'error': 'timeout'}]}}), 'FAIL')]
        partial = self.results()
        partial['scenarios']['es']['runs'][0]['turns'].pop()
        variants.append((partial, 'FAIL'))
        empty = self.results()
        empty['scenarios']['es']['runs'][0]['turns'][0]['audio_bytes'] = 0
        variants.append((empty, 'FAIL'))
        timed_out = self.results()
        timed_out['scenarios']['es']['runs'][0]['turns'][0]['timed_out'] = True
        variants.append((timed_out, 'FAIL'))
        punctuation = self.results(mode='audio')
        punctuation['scenarios']['es']['runs'][0]['turns'][0]['heard'] = ' . 123 _ '
        variants.append((punctuation, 'FAIL'))
        for results, status in variants:
            report, _ = languages.compile_report(self.catalog, self.cases, results, self.reviewed(results))
            self.assertEqual(report['scenarios']['es'], status)

    def test_changed_transcript_invalidates_old_review(self):
        results = self.results()
        reviews = self.reviewed(results)
        results['scenarios']['es']['runs'][0]['turns'][0]['reply'] = 'El cielo es muy azul.'
        report, _ = languages.compile_report(self.catalog, self.cases, results, reviews)
        self.assertEqual(report['scenarios']['es'], 'NEEDS_REVIEW')

    def test_audio_evidence_and_pronunciation_review_required(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reply.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b'\x01\x00' * 5)
            results = self.results(mode='audio', audio=dict(audio_file=str(path), audio_sha256=languages.digest(path.read_bytes())))
            reviews = self.reviewed(results)
            report, _ = languages.compile_report(self.catalog, self.cases, results, reviews)
            self.assertEqual(report['valid_audio_languages'], ['es'])
            reviews['es/1/1']['pronunciation_intelligible'] = False
            report, _ = languages.compile_report(self.catalog, self.cases, results, reviews)
            self.assertEqual(report['scenarios']['es'], 'FAIL')
            reviews['es/1/1']['pronunciation_intelligible'] = True
            path.write_bytes(b'changed audio')
            report, _ = languages.compile_report(self.catalog, self.cases, results, reviews)
            self.assertEqual(report['scenarios']['es'], 'MISSING_AUDIO_EVIDENCE')

    def test_changed_scenario_invalidates_prior_results(self):
        results = self.results()
        changed = copy.deepcopy(self.cases)
        next(case for case in changed if case['name'] == 'es')['steps'][0]['text'] = 'Different input'
        report, _ = languages.compile_report(self.catalog, changed, results, self.reviewed(results))
        self.assertEqual(report['scenarios']['es'], 'STALE_SCENARIO')

    def test_preview_and_saved_result_compilation_never_connect(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(live_eval, 'api_key', side_effect=AssertionError('must stay offline')), \
                patch.object(live_eval, 'mvp_session', side_effect=AssertionError('must stay offline')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(languages.main(['--output', folder]), 0)
            self.assertIn('NOT_RUN', (Path(folder) / 'LANGUAGES.md').read_text())
            results = Path(folder) / 'results.json'
            results.write_text(json.dumps(self.results()))
            self.assertEqual(languages.main(['--results', str(results), '--output', folder]), 0)
            self.assertEqual(json.loads((Path(folder) / 'languages.json').read_text())['valid_audio_languages'], [])

    def test_optional_reply_recording_preserves_pcm_and_rate(self):
        async def exercise(folder):
            session = FakeSession([[server(heard='hola', said='azul', audio=b'\x01\x00' * 12, complete=True)]])
            scenario = dict(name='offline', steps=[dict(text='hola', expect=dict(reply='azul'))])
            original_wait = live_eval.wait_for_turn
            async def fast_wait(recorder, timeout):
                await original_wait(recorder, timeout=1, settle=.01)
            with patch.object(live_eval, 'wait_for_turn', side_effect=fast_wait):
                return await live_eval.run_scenario(connector(session, []), 'fake', {}, scenario, audio_dir=folder)
        with tempfile.TemporaryDirectory() as folder:
            turns = asyncio.run(exercise(Path(folder)))
            self.assertFalse(turns[0]['failures'])
            with wave.open(turns[0]['audio_file'], 'rb') as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
                self.assertEqual(audio.readframes(12), b'\x01\x00' * 12)

    def test_provider_rejection_stops_batch_without_retrying_every_language(self):
        class Rejected(Exception):
            code = 403
        with tempfile.TemporaryDirectory() as folder, patch.object(live_eval, 'run_scenario', side_effect=Rejected()) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            results = asyncio.run(languages.simulate(self.cases, 'fake', {}, 'text', 3, None, Path(folder)))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(results[self.cases[0]['name']]['runs'][0]['provider_code'], 403)

    def test_missing_key_does_not_connect_or_fabricate_results(self):
        with patch.object(live_eval, 'api_key', return_value=''), \
                patch.object(live_eval, 'mvp_session', return_value=('fake', {})), \
                patch('dotenv.load_dotenv'), patch('google.genai.Client') as client, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            languages.main(['--run', '--only', 'es'])
        self.assertEqual(stopped.exception.code, 2)
        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
