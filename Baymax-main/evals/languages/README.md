# Multilingual conversations and validated language list

`catalog.json` records all **99 entries** in Google's current
[Live API language catalog](https://ai.google.dev/gemini-api/docs/live-api/capabilities#supported-languages),
checked 2026-11-06. Regional/script entries are kept separate; Norwegian `nb`
is an alias of `no`. This is a candidate catalog, not proof that Ember's pinned
`gemini-2.5-flash-native-audio-preview-12-2025` supports every entry. Keep model,
config hash and date with results. Do not silently upgrade the runtime model.

`scenarios.json` supplies 26 language/script/region scenarios and one Hindi-English
mixed scenario. Each ordinary case asks a native-language question, continues in
that language, then switches to English. Content is synthetic (sky/grass/banana
colors), without real people or private facts. Run `python evals/languages/fixtures.py`
to regenerate it after changing `fixtures.py`. Prompts/TTS pronunciation still need
review by a competent speaker; the starter translations are not certified.

## Commands (from Baymax-main)

```bash
# Offline catalog/report preview: no keys, provider calls or devices.
python evals/language_eval.py

# Real Gemini session, synthetic local TTS, three runs per scenario.
python evals/language_eval.py --run --only hi --only es --repeat 3

# All scripted languages; uses API quota. No extra provider/judge model.
python evals/language_eval.py --run --repeat 3

# Useful for response-language checks when TTS isn't installed.
python evals/language_eval.py --run --mode text --only ja --repeat 3
```

Audio mode uses existing `espeak-ng`, or macOS `say`/`afconvert` with an explicit
`--voices voices.json` map, e.g. `{"en":"Samantha","es":"Monica"}`. Voice names
must actually be installed. TTS failures fail that run; they are not language
support failures by Gemini. Windows can run offline/text checks; use a Linux/macOS
test host with the needed voices for speech simulation. Never substitute English
TTS for an unsupported voice and claim that language tested successfully.

The key is read by the existing evaluator from `GOOGLE_API_KEY`/local `.env`.
`--run` sends the actual MVP prompt/config, scripted text/synthetic speech and
stub function results to Google Gemini Live, using the configured API quota.
The real config's built-in capabilities remain available. No microphone, camera
or speaker is opened. It does **not** run the robot's local address gate, echo
cancellation, wake words or device playback; those need separate Pi trials.
The shared persona now includes the `[LANGUAGE]` adaptation instruction. Results
still record whether the tested config contains it; the evaluator does not quietly
rewrite prompts on other branches. The recorded Pi matrix tested the identical
instruction in a temporary copy before source integration. No personal
conversations are required.
Authentication, invalid/unsupported model requests and quota rejection stop the
batch after the first rejected request. Remaining scenarios stay `NOT_RUN`;
the runner never silently switches models or retries every language.

## Review and compile the full list

Each run saves its results and output WAVs in a new ignored
`Baymax-main/.local-review/languages/<timestamp>/` directory. It also writes a
review template with null decisions. A speaker competent in the language must
check every turn: response language, actual answer, natural switch/mix, input
transcription, and audible pronunciation. Keyword regexes alone cannot do this:
an English sentence containing “azul” must not count as valid Spanish.

Copy `review-template.json` to `review.json`, fill each decision with `true` or
`false` after listening, then compile locally:

```bash
python evals/language_eval.py --results .local-review/languages/<timestamp>/results.json --review .local-review/languages/<timestamp>/review.json
```

Reviews are tied to hashes of the exact model/config, input, transcript and saved
audio. Changing evidence invalidates the review. Output `LANGUAGES.md`/`.json`
always includes the full candidate list, with explicit states:

- `NO_SCENARIO`: no translated fixture yet; add and review one.
- `NOT_RUN`: fixture exists, no real results.
- `FAIL`: incomplete/error run, failed automatic check or negative human review.
- `INSUFFICIENT_REPEATS`: fewer than three complete runs.
- `NEEDS_REVIEW` / `MISSING_AUDIO_EVIDENCE` / `STALE_SCENARIO`: not validated.
- `REVIEWED_TEXT_ONLY`: text-to-audio response checked; speech input not validated.
- `VALIDATED_AUDIO_SIMULATION`: all three-or-more runs and every turn passed
  checks and exact-evidence human review, with the saved reply audio intact.

The matrix separately shows automatically passed/attempted runs. Audio input
transcripts must contain letters; punctuation-only ASR cannot pass. Compiling
stored results rechecks current expectations, so older permissive checks do not
certify missing recognition text. These automatic counts are useful evidence
without pretending language/pronunciation review has happened.

Only the final state enters `valid_audio_languages`. It establishes that scenario
on the recorded model/config, not blanket proficiency, every dialect, or robot
hardware compatibility. Add longer conversations, accents/noise and actual Pi
checks before a release claim. Use `devops/performance/README.md` alongside live
Pi trials for CPU/RAM/NPU/temperature and added latency. The 15-second synthetic
turn ceiling is a generous example, not the production latency budget.

The runner returns 1 for failed scenarios and 0 for a preview/capture/review-needed
report; zero alone does **not** mean all languages validated. Inspect statuses and
the valid-language list. Raw transcripts/audio stay local; publish only a reviewed
aggregate report. The committed `docs/features/LANGUAGES.md` distinguishes recorded
live checks, unrun candidates and completed language/audio review. Do not fill
review fields automatically merely to obtain a green report.

## Offline regression checks

From the repository root: `python tests/run.py --feature languages`. Fake sessions
test the harness/reporting and do not count as real language validation.
