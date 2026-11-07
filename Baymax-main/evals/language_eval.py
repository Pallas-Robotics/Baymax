"""Multilingual Ember simulations and a model-specific, evidence-backed language list."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals import live_eval

DATA = Path(__file__).with_name('languages')
DEFAULT_OUTPUT = ROOT / '.local-review/languages'
REVIEW_FIELDS = ('reply_language_correct', 'answer_correct', 'switch_or_mix_correct',
                 'input_transcription_correct', 'pronunciation_intelligible')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_inputs():
    catalog = json.loads((DATA / 'catalog.json').read_text(encoding='utf-8'))
    cases = json.loads((DATA / 'scenarios.json').read_text(encoding='utf-8'))
    codes = [row['code'] for row in catalog['languages']]
    if len(codes) != len(set(codes)):
        raise ValueError('Duplicate language code')
    names = set()
    for case in cases:
        if case['name'] in names or case['language'] not in codes:
            raise ValueError('Duplicate scenario or unknown language')
        names.add(case['name'])
        live_eval.validate(case)
        for step in case['steps']:
            if not step.get('expected_languages') or not all(code in codes for code in step['expected_languages']):
                raise ValueError('Every turn needs catalog-backed expected languages')
    return catalog, cases


def scenario_for(case, mode, voices=None):
    scenario = dict(case, steps=[])
    for step in case['steps']:
        item = dict(step)
        item['text' if mode == 'text' else 'say'] = item.pop('text')
        item['expect'] = dict(item['expect'])
        if mode == 'audio':
            item['expect']['heard'] = r'[^\W\d_]'
            code = step['expected_languages'][0]
            item['voice'] = (voices or {}).get(code, step.get('tts_voice', code))
        scenario['steps'].append(item)
    return scenario


def evidence(turn, meta, step):
    payload = {key: turn.get(key) for key in ('step', 'heard', 'reply', 'audio_sha256')}
    payload.update(model=meta['model'], config_sha256=meta['config_sha256'], mode=meta['mode'],
                   expected_languages=step['expected_languages'])
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())


def run_passes(case, run, mode):
    if 'error' in run or mode not in ('audio', 'text') or len(run.get('turns', [])) != len(case['steps']):
        return False
    expected = scenario_for(case, mode)['steps']
    try:
        return all('failures' in turn and not turn['failures'] and turn.get('complete')
                   and not turn.get('timed_out') and turn.get('reply') and turn.get('audio_bytes')
                   and not live_eval.check(step['expect'], turn)
                   for step, turn in zip(expected, run['turns']))
    except (KeyError, TypeError, ValueError):
        return False


def classify(case, outcome, meta, reviews):
    if not outcome:
        return 'NOT_RUN'
    runs = outcome.get('runs', [])
    if not runs:
        return 'NOT_RUN'
    scenario_hash = digest(json.dumps(case, sort_keys=True, ensure_ascii=False).encode())
    if meta.get('scenario_sha256', {}).get(case['name']) != scenario_hash:
        return 'STALE_SCENARIO'
    # Recheck stored turns too: an old permissive check must not certify punctuation as ASR.
    if any(not run_passes(case, run, meta.get('mode')) for run in runs):
        return 'FAIL'
    if len(runs) < 3:
        return 'INSUFFICIENT_REPEATS'
    for index, run in enumerate(runs, 1):
        for number, (turn, step) in enumerate(zip(run['turns'], case['steps']), 1):
            review = reviews.get(f'{case["name"]}/{index}/{number}', {})
            if review.get('evidence_sha256') != evidence(turn, meta, step):
                return 'NEEDS_REVIEW'
            fields = REVIEW_FIELDS if meta['mode'] == 'audio' else REVIEW_FIELDS[:3]
            if any(review.get(field) is not True for field in fields):
                return 'NEEDS_REVIEW' if not any(review.get(field) is False for field in fields) else 'FAIL'
            # A spoken-language pass requires the exact saved reply, not just its transcript.
            if meta['mode'] == 'audio':
                path = Path(turn.get('audio_file', ''))
                if not path.is_file() or digest(path.read_bytes()) != turn.get('audio_sha256'):
                    return 'MISSING_AUDIO_EVIDENCE'
                try:
                    with wave.open(str(path), 'rb') as audio:
                        if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                                or audio.getnframes() * 2 != turn['audio_bytes'] or audio.getnframes() <= 0):
                            return 'MISSING_AUDIO_EVIDENCE'
                except (OSError, EOFError, wave.Error):
                    return 'MISSING_AUDIO_EVIDENCE'
    return 'VALIDATED_AUDIO_SIMULATION' if meta['mode'] == 'audio' else 'REVIEWED_TEXT_ONLY'


def compile_report(catalog, cases, results=None, reviews=None):
    results, reviews = results or {}, reviews or {}
    meta = results.get('metadata', {})
    outcomes = results.get('scenarios', {})
    statuses = {case['name']: classify(case, outcomes.get(case['name']), meta, reviews) for case in cases}
    by_code = {case['language']: case for case in cases if not case.get('mixed')}
    rows, valid = [], []
    for language in catalog['languages']:
        case = by_code.get(language['code'])
        status = statuses[case['name']] if case else 'NO_SCENARIO'
        runs = outcomes.get(case['name'], {}).get('runs', []) if case else []
        passed = sum(run_passes(case, run, meta.get('mode')) for run in runs) if case and status != 'STALE_SCENARIO' else 0
        rows.append(dict(language, status=status, automatic_passed_runs=passed, attempted_runs=len(runs)))
        if status == 'VALIDATED_AUDIO_SIMULATION':
            valid.append(language['code'])
    lines = ['# Ember language validation', '',
             'Candidate source: ' + catalog['source'] + ' (checked ' + catalog['checked_date'] + ').',
             'The provider catalog is not proof of support by Ember’s pinned model or robot.',
             '', 'Model: ' + meta.get('model', 'NOT RUN'), 'Mode: ' + meta.get('mode', 'NOT RUN'),
             'Config SHA256: ' + meta.get('config_sha256', 'NOT RUN'),
             'Source revision: ' + meta.get('source_commit', meta.get('commit', 'NOT RUN')),
             'Test-only language patch: ' + str(meta.get('staged_language_fix', 'none recorded')),
             '', 'Validated audio simulations: ' + (', '.join(valid) if valid else '**None yet.**'),
             '', 'At least three complete runs, automatic checks and exact-evidence human review',
             'are required. Text-only checks do not validate speech. Physical Pi validation is separate.',
             'Automatic runs count scripted content/latency checks, not pronunciation or full language proficiency.',
             '', '| Language | Code | Automatic runs | Status |', '|---|---|---|---|']
    lines.extend(f'| {row["name"]} | {row["code"]} | {row["automatic_passed_runs"]}/{row["attempted_runs"]} | {row["status"]} |' for row in rows)
    lines += ['', '## Mixed-language scenarios', '']
    lines.extend(f'- {case["name"]}: {statuses[case["name"]]}' for case in cases if case.get('mixed'))
    return dict(metadata=meta, valid_audio_languages=valid, languages=rows, scenarios=statuses), '\n'.join(lines) + '\n'


async def simulate(cases, model, config, mode, repeat, connect, directory, voices=None):
    outcomes = {}
    for case in cases:
        runs = []
        for index in range(1, repeat + 1):
            try:
                turns = await live_eval.run_scenario(connect, model, config, scenario_for(case, mode, voices),
                    audio_dir=directory / 'audio' / case['name'] / str(index))
                for turn in turns:
                    if turn.get('audio_file'):
                        turn['audio_sha256'] = digest(Path(turn['audio_file']).read_bytes())
                run = {'turns': turns}
            except Exception as error:
                # Do not persist SDK exception strings that may include request/config details.
                run = {'error': type(error).__name__}
                code = getattr(error, 'code', None)
                if type(code) is int:
                    run['provider_code'] = code
            runs.append(run)
            print(f'{case["name"]} {index}/{repeat}: ' + ('ERROR' if 'error' in run else 'captured; review required'), flush=True)
            if run.get('provider_code') in (400, 401, 403, 404, 429):
                outcomes[case['name']] = {'runs': runs}
                print('Provider rejected the request; stopping the batch. Remaining scenarios are NOT_RUN.', flush=True)
                return outcomes
        outcomes[case['name']] = {'runs': runs}
    return outcomes


def review_template(cases, results):
    template = {}
    for case in cases:
        for index, run in enumerate(results['scenarios'].get(case['name'], {}).get('runs', []), 1):
            for number, (turn, step) in enumerate(zip(run.get('turns', []), case['steps']), 1):
                template[f'{case["name"]}/{index}/{number}'] = dict(
                    evidence_sha256=evidence(turn, results['metadata'], step),
                    expected_languages=step['expected_languages'], input=step['text'],
                    heard=turn.get('heard', ''), reply=turn.get('reply', ''), audio_file=turn.get('audio_file'),
                    **{field: None for field in REVIEW_FIELDS})
    return template


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='Send synthetic scenarios to Google Gemini Live')
    action.add_argument('--results', type=Path, help='Compile saved results without any network calls')
    parser.add_argument('--mode', choices=('audio', 'text'), default='audio')
    parser.add_argument('--only', action='append', help='Scenario name, repeatable; default all scripted scenarios')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--voices', type=Path, help='JSON mapping language codes to local OS TTS voice names')
    parser.add_argument('--review', type=Path, help='Completed review JSON; compiling only, never overwrite it')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    catalog, cases = load_inputs()
    selected = [case for case in cases if not args.only or case['name'] in args.only]
    if args.only and len(selected) != len(set(args.only)):
        parser.error('Unknown scenario')
    results = json.loads(args.results.read_text(encoding='utf-8')) if args.results else {}
    reviews = json.loads(args.review.read_text(encoding='utf-8')) if args.review else {}
    if args.run:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False, interpolate=False)
        model, config = live_eval.mvp_session()
        key = live_eval.api_key()
        if not key:
            parser.error('No Gemini key configured; set GOOGLE_API_KEY locally. No scenarios sent.')
        mac_tts = bool(shutil.which('say') and shutil.which('afconvert'))
        if args.mode == 'audio' and not (shutil.which('espeak-ng') or (mac_tts and args.voices)):
            parser.error('Audio simulation needs espeak-ng, or macOS say plus --voices. Text mode is not speech validation.')
        voices = json.loads(args.voices.read_text(encoding='utf-8')) if args.voices else None
        if args.mode == 'audio' and mac_tts and any(
                code not in (voices or {}) for case in selected for step in case['steps'] for code in step['expected_languages']):
            parser.error('macOS needs an explicit voice mapping for every expected language')
        from google import genai
        model_config = json.dumps(config, sort_keys=True, default=str)
        try:
            commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            commit = 'unknown'
        results = {'metadata': dict(format=1, model=model, mode=args.mode, commit=commit,
                    config_sha256=digest(model_config.encode()),
                    scenario_sha256={case['name']: digest(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()) for case in selected},
                    language_prompt_present='[LANGUAGE]' in config.get('system_instruction', ''),
                    recorded_utc=datetime.now(timezone.utc).isoformat()), 'scenarios': {}}
        # A new directory per run preserves reply WAVs and previous review evidence.
        run_dir = args.output / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        print(f'Sending {len(selected)} synthetic scenarios x {args.repeat} runs to Google Gemini Live ({model}).')
        print('Real Ember prompt/tools are used; tools return stubs. No microphone/camera/speaker is opened.')
        connect = genai.Client(api_key=key, http_options={'api_version': 'v1alpha'}).aio.live.connect
        results['scenarios'] = asyncio.run(simulate(selected, model, config, args.mode, args.repeat, connect, run_dir, voices))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        (run_dir / 'review-template.json').write_text(json.dumps(review_template(cases, results), indent=2), encoding='utf-8')
        print('Raw results and reply audio: ' + str(run_dir))
    report, markdown = compile_report(catalog, cases, results, reviews)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'languages.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'LANGUAGES.md').write_text(markdown, encoding='utf-8')
    print(f'{len(catalog["languages"])} documented candidates; {len(cases)} scripted scenarios; '
          f'{len(report["valid_audio_languages"])} validated audio languages. Report: {args.output / "LANGUAGES.md"}')
    if not args.run and not args.results:
        print('Preview only; nothing sent. --run uses Gemini quota and sends the real prompt plus synthetic turns.')
    return 1 if any(status == 'FAIL' for status in report['scenarios'].values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
