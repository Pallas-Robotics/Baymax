# OPi4 V1 feature log

An inventory of the Orange Pi repository, not a claim that every feature works on
every board. Runtime data, models, credentials, recordings and SD images stay out
of this inventory and Git.

## Board support and evidence

| Target | Runtime path | Evidence | Remaining checks |
|---|---|---|---|
| Orange Pi 4 Pro, Allwinner A733 | CPU MediaPipe pose in a child process | OPI4 Linux offline gate: 552 core / 241 toolkit tests, 4 skipped; Linux ARM64 CI passed; earlier individual conversation/monitor trial | Combined V1 live checks, fresh-card boot, eye hardware, sustained mixed-feature load |
| Orange Pi 5 Max, RK3588 | Same runtime; optional validated NPU pose backend | Backend/conversion checks and source present | Combined V1 board checks, NPU availability/conversion/accuracy |
| Other Orange Pi models | No blanket compatibility promise | No V1 hardware evidence recorded | Board-specific OS, drivers, audio/camera, dependencies, boot image and inference validation |

The SD image is specifically for the **Orange Pi 4 Pro**. The 5 Max uses its
documented Armbian/clickables installation, not the 4 Pro image.

## Core features

Paths below are relative to `Baymax-main/`.

| Feature | Organized source | Behavior and limits | Tests / evidence |
|---|---|---|---|
| Startup | `run_mvp.py`, `run_pc.py`, `device_config.py` | Saved-device readiness, single instance, eye lifecycle and v10 startup | `tests/test_mvp.py` |
| Conversation | `features/persona.py`, `features/conversational_ability/`, `realtime_gemini_10.py` | Gemini conversation, language adaptation/switches/mixed speech, audio cleanup, turn-taking, wake/disengage and volume; remote processing requires connectivity | `tests/test_conversational_ability.py`, `tests/test_address_gate.py`, `tests/test_session.py`, `tests/test_voice_volume.py`, `tests/test_language_eval.py` |
| Speech and latency | `features/conversational_ability/timing.py`, `runtime_monitor.py`, `static/emberdevUI.html` | Independent local speech indicator, updating chat timings, reply wait, output-underrun counter; speech/DAC times are estimates | `tests/test_conversation_timing.py`, `tests/test_runtime_monitor.py`; repository-root `test/monitor-chat.cjs` |
| Memories | Memory hooks in `realtime_gemini_10.py`, `semantic_embedder.py` | Existing retrieval: the newest 50 memories in the instructions (used live 5/5); per-turn recall notes between turns are opt-in (`BAYMAX_MEMORY_NOTES=1`) because Gemini 2.5 ignored them live (0/10), never during a reply and incremental persistence; settings control enablement, models download separately | `tests/test_memory_saving.py`, `tests/test_semantic_embedder.py`, `tests/test_live_eval.py` |
| Person recognition boundary | `features/person_recognition/`; face-presence handling in `realtime_gemini_10.py` | Namespace reserved; face presence is not verified personal identity. This folder does not implement an identity engine | Person-recognition README and camera/pose tests |
| Pose and fall processing | `features/pose/`, `features/vision_process.py`, `fall_detection/` | Child-process inference and CPU fallback; a hung child is stopped (5 s per frame, 60 s to start or to switch from the NPU to MediaPipe mid-run) and restarted like a dead one; fall velocities from capture times; one check-in per fall with the fall toolkit on too. RK3588 NPU requires conversion/checks. Enabled detection does not establish accuracy | `tests/test_v10_runtime.py`, `tests/test_toolkit_vision_process.py`, `tests/test_toolkit_fall_detection.py`, `features/pose/VALIDATION.md` |
| Physical eyes | `features/eyes/` | Optional matrix driver, animation and lifecycle; missing serial hardware does not prove working eyes | `tests/test_eyes.py`; physical checks pending |
| Setup and debugger | `devUI.py`, `dev_settings.py`, `pc_hardware.py`, `static/` | Saved devices, protected local API, chat and grouped toolkit events | `tests/test_devUI_http.py`, `tests/test_dev_settings.py`, `tests/test_pc_hardware.py` |

`features/memory_palace/` and `features/person_recognition/` are integration
namespaces, not duplicate implementations of the root runtime's existing memory
and camera hooks. Keep established import and data paths until a separately
reviewed refactor moves them.

## Callable tools

Paths below are relative to `Baymax-main/features/toolkits/`.

| Group | Source | Scope | Verification |
|---|---|---|---|
| Dispatch and information | `gemini_tools.py`, `location.py`, `weather.py`, `public_sources.py`, `feeds.py` | Bounded provider calls and information sources | `tests/test_toolkit*.py`; live provider accuracy/integration remains separate |
| Everyday support | `everyday/`, `checkins.py` | Reminders, follow-ups and check-ins using local time/settings | Everyday/check-in toolkit tests |
| Family | `family/` | Explicit family records and linked messages | Family tests; recipients/access require configuration |
| Media and activities | `audiobooks.py`, `listen.py`, `librivox.py`, `general_media_entertainment/` | Audiobooks, live radio, recordings, trivia, brain games and wake/stop handling | Audiobook, listening, media and game tests |
| Looking closely / optional fall toolkit | `look_closely.py`, `fall_detection.py`, `experimental.py` | Existing camera context and bounded actions, not additional capture streams | Toolkit tests; physical accuracy/alerts require explicit validation |

Each toolkit invocation has one stable centered chat bubble that updates from
start to finish. Repeated invocations remain distinct. Feature imports do not
start device capture.

## Installation and distribution

Paths below are relative to the repository root.

| Area | Source | Scope |
|---|---|---|
| OPI4 SD builder | `devops/opi4-flash/` | Per-card private image, verified upstream assets, source branch, Wi-Fi/key/timezone and generated access details |
| Services / updates | `devops/opi4-flash/payload/`, `Baymax-main/deploy/` | Existing installer/update lifecycle; deployment approval is separate from an offline pass |
| 5 Max setup | `Baymax-main/ORANGE_PI_SETUP.md`, `devops/clickables/` | Board-appropriate installation; not the 4 Pro image |
| Graphify | `graphify-out/`, this log | Source-linked graph/report and organized feature inventory; documentation only, outside the robot audio/inference loop |
| Shared feature performance | `devops/performance/`, `tests/performance/` | Baseline/active/combined CPU, RAM, NPU load/availability, temperature and speech latency; explicit budgets and added p95 latency gate. Sampler offline tests are included in each feature selection; board measurements remain pending |
| Multilingual simulations | `Baymax-main/evals/language_eval.py`, `Baymax-main/evals/languages/`, `docs/features/LANGUAGES.md`, `docs/features/OPI4_LANGUAGE_RESULTS.md` | Pi text-to-audio matrix: 80/81 automated runs passed across 240 turns, using a test-only language prompt patch; Polish switching and synthetic-speech transcription failures remain. 99 provider candidates stay separate from fully reviewed/validated languages |

## Change record

| Version | Change | Status |
|---|---|---|
| Baseline `551f80a` | Consolidated Orange Pi runtime, tools, pose backends and SD builder | Existing code; hardware coverage is board-specific |
| OPi4 V1 | Speech indicator, chat latency, grouped toolkit bubbles, underrun diagnostics, speech-end override | OPI4 offline and ARM64 CI passed; combined live release checklist pending |
| OPi4 V1 documentation | Organized source inventory and Graphify relationships | Public source/docs; not hardware certification |
| Conversation latency and robustness | Google Search off by default; recalled memories and GoAway reconnects only between turns; 0.3 s reconnect without an audio reset (backoff, full reset for device errors); 960-sample mic blocks; logging and video encoding off the real-time threads; `BAYMAX_OUTPUT_BLOCKSIZE`/`BAYMAX_OUTPUT_LATENCY` speaker knobs; weather and location kept cached; family profile note waits for a quiet moment | Offline gate passed; Orange Pi 5 Max A/B and live reconnect/GoAway timing pending |
| Gemini Live model fallback | `BAYMAX_LIVE_MODELS` (default: 2.5, then `gemini-3.8-live`): the next model at once when the key can't use one (setup refused, not found, retired; a quota close retries the same model), kept until restart; Gemini 3 settings (no `thinking_config`, `BLOCKING` functions) in the runtime and `evals/live_eval.py --model`; startup key check accepts the first model Google lists; a model turn without parts keeps its turn end | Offline gate passed; `gemini-3.8-live` untested live (no 3.8 quota on Ember's keys, 2026-11-06), so 2.5 stays first |
| Latency review fixes | A GoAway soon after setup backs off like a quick loss, and Ember moves only once the connection has its own resume handle; lost connections back off up to 4.8 s and never reset the audio devices; recalled memories are looked up from the person's whole turn once its reply has played, and a resumed connection keeps what it was given; a quota close retries the same Live model; no notes without `turn_complete` on Gemini 3 until checked; `[MIC STATUS]` printed by the queue monitor; `evals/live_eval.py` `note` step and `memory-note-then-speech` scenario; Google Search mentioned only when on | Offline gate passed; live pending: the memory-note scenario (2.5 now, 3.8 once it has quota), reconnect and GoAway timing, the Orange Pi 5 Max A/B |
| Reply in the person's language | James's `[LANGUAGE]` section in `features/persona.py` (also carried by Nate's PR #1, whose Orange Pi simulations tested it), plus live-tested follow-ups: judge the words, not the accent or transcript script; never say a tag; the pause words "Hey Ember" and "stop" in English. Ember's name in Hindi (Devanagari and Urdu script) for the opt-in address gate and "Ember disengage" (`addressing.py`, `engage.py`); `evals/live_eval.py` language scenarios, with reply checks that ignore the transcript's leading `[TAG]` | Offline gate passed; live eval 2026-11-07 (Gemini 2.5, 3 runs each): Hindi, Hindi-English weather, English pause words in Hindi and the Hindi name 3/3, other language scenarios 2/3, Hindi fall check-in 0/3 (two failed only on the transcript tag, since fixed in the eval); Hindi on the robot and an Urdu-script "disengage" pending |
| RK3588 core layout | Ember on the four A76s (`BAYMAX_CPU_PINNING=auto`, RK3588 only); 1 ms GIL switch interval (`BAYMAX_GIL_SWITCH_MS`); pose process on the fast cores at nice 5, face process on the slow cores at nice 10 (`BAYMAX_VISION_CPUS`); single-threaded OpenCV/OpenBLAS/tokenizers pools; speaker stream with 512-frame callbacks and low latency (`BAYMAX_OUTPUT_BLOCKSIZE` 64-4096, `BAYMAX_OUTPUT_LATENCY` up to 0.5 s; `.env` wins). Nothing changes on the Orange Pi 4 Pro (unpinned by default), x86, macOS or Windows | Offline gate passed; speaker measured on the Orange Pi 5 Max (output latency about 96 ms to about 42 ms, no underflows in three 3.5-minute runs); the mic callback's p99 with the 1 ms interval pending |
| Final review fixes | A retry that never reached Gemini (DNS, unreachable, refused, open timeout) keeps the resume handle; DNS and "network unreachable" back off without the audio reset; Ember's name in Urdu script; the `[MEMORY CONTEXT]` note and `[WHEN TO SPEAK]` say a memory note is background that needs no reply; the robots' source check parses only the repository's own files (not the 5 Max's NPU converter venvs); `tests/run.py --changed` selects the suites using `live_models.py`, `addressing.py`, `vision_process.py`, `persona.py` and the live eval; the SD-card guide clones `main` | Offline gate passed; live pending: `memory-note-then-speech` with the new wording, reconnect during a Wi-Fi drop |

## Verification

- `Baymax-main/check_release.py` reports separate core/toolkit results.
- From the root, `node test/monitor-chat.cjs` checks chat updates.
- Flash builder tests are separate; an image build is not a fresh-card boot.
- Desktop CI has environment/platform failures: Linux PortAudio server
  initialization and Windows file-locking/symlink privileges. Do not label it green.
- Actual board checks, software-lead review and release promotion remain separate.
