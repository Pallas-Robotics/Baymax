# Live evaluation: Gemini in the loop, no robot

`evals/live_eval.py` checks how Gemini behaves with Ember's **real MVP session**
before anyone stands in front of a robot. It plays scripted turns into a fresh
Gemini Live session and checks what Gemini heard, said and which tools it called.
Each scenario runs several times (default 3), because Gemini isn't deterministic.

```bash
python evals/live_eval.py --dry-run                     # what would be sent; connects to nothing
python evals/live_eval.py                               # all scenarios that apply, 3 runs each
python evals/live_eval.py --only weather --repeat 5
python evals/live_eval.py --root ../../Baymax-look-closely/Baymax-main   # test another PR's checkout
```

## What is exercised
- **Session config.** It's built from the runtime `run_pc.py` launches
  (`RUNTIME`: `realtime_gemini_10.py` on the Orange Pi line; `realtime_gemini_8.py`
  for an older MVP checkout without it), parsed without importing devices: `MODEL`,
  the base prompt and the `if` blocks that extend it (v10's `[WHEN TO SPEAK]` and
  `[MEMORY CONTEXT]` from `device.toml`), `_BASE_CONFIG` (proactive audio when
  `only_when_addressed`), the audio-config hook and `_build_config`. That includes
  the MVP persona and the toolkit declarations. The environment defaults come from
  `run_pc.py` and `run_mvp.py`, `device.toml` (`EMBER_DEVICE_CONFIG`, defaults when
  absent) and the toggles from your `.env` apply, as they do on the robot.
- **Turns.**
  - `say`: macOS `say` (or `espeak-ng`) text-to-speech, sent as 16-bit 48 kHz PCM in
    real time over quiet room noise (2 s lead-in, 1.5 s after) so Gemini's activity detection starts and ends the turn. With the MVP's LOW start sensitivity, Gemini ignored clean speech framed by digital silence.
  - `text`: a client text turn, like the `[FALL ALERT]` and `[ENGAGED]` notes.
  - `card`: a generated 640×480 JPEG of text lines, sent as a camera frame.
  - `image`: a JPEG file next to the scenarios.
- **Checks per turn:**
  - `heard` regex on the input transcript, which catches transcript-matched commands
    such as "Ember disengage".
  - `reply` / `not_reply` regex.
  - `tools` / `not_tools` / `no_tools`.
  - `max_words`.
  - `max_latency_s`: the time from the end of speech to the first reply audio.
- **Feature scenarios** declare `requires` (a tool name or a prompt marker). They are
  skipped on branches without that feature, so one `scenarios.json` covers the MVP and
  the open PRs.

## What it does not test
The robot's microphone, echo cancellation, speaker, camera, eyes, fall detection, CPU
load, offline Vosk wake words, startup greetings and session tasks. Those still need
the robot (TESTING_PROTOCOL.md §2–3). A pass here is Gemini-behaviour evidence, not
release approval.

## Inputs, outputs and data flows
- **Sent to Google Gemini Live:** the MVP system prompt and tool declarations,
  synthetic speech of the scripted lines, the generated cards or images, text notes,
  and the stub tool results. `--dry-run` prints exactly this. A normal run asks before
  connecting unless you pass `--yes`.
- **Not used:** no microphone, camera or speaker is opened, and no recordings are made.
- **Tools.** Tool calls get the scenario's `tool_results` stubs (default `{"result": "ok"}`).
  `--real-tools` runs the real toolkit instead. That contacts its providers (weather,
  discovery services) and audio tools may play sound, so use it deliberately.
- **Key.** `GOOGLE_API_KEY` is read from the environment, else `Baymax-main/.env` of the tested branch, this checkout or the main checkout (when run from a git worktree). Never
  paste it into chat or a PR. Runs use your API quota.
- **Output.** `.local-review/live-eval.md` and `.json` (gitignored), holding transcripts
  of the synthetic speech. The process exits with 1 if any run failed.

## Dependencies
The desktop requirements (`google-genai`, `numpy`, `opencv`, `python-dotenv`), plus
macOS `say`/`afconvert` or `espeak-ng` for speech.

## Enabling and disabling
This is a developer tool. Nothing in the runtime imports it.

## Tests
`tests/test_live_eval.py` (core, offline) builds the real config, validates
`scenarios.json`, checks the expectation logic, and drives a fake Live session (spoken
turn, tool call with a stub, card frames, timeout). It also confirms that a dry run or a
declined prompt connects to nothing.

## Adding a scenario
For multilingual conversations, language switching and the full provider-candidate
catalog, use [languages/README.md](languages/README.md). `language_eval.py` reuses
this harness, optionally saves synthetic reply WAVs for human review and compiles
a model/config-specific language matrix. A regex match or fake-session test never
counts as validated speech support.

Add an entry to `scenarios.json`:
- `name`, `about`, `steps` (each with an `expect`).
- Optional `requires` and `tool_results`.

Keep the content synthetic. No real names, medicines or private details.
