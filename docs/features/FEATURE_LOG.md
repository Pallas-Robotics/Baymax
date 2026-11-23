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

## Joe's OPI4 integration candidate

Voice follow-up work adds immediate language overrides, bounded wake-listener
startup and split-result wake phrases. See the candidate record for the
distinction between source tests, a PR bench installation and owner voice tests.
The language follow-up removes a conflicting ongoing-preference rule and adds
direct Korean-to-Spanish text/audio cases; owner speech remains a separate check.

The focused candidate builds on our original `opi4-chat-timing` revision
`54ee70e`. It ports Joe Kojima's language, weather-cache, quiet-profile and
network-retry changes, with OPI4-specific profile retry handling. See
[scope, attribution and validation](JOE_OPI4_PORT.md). Offline software testing
is separate from spoken, chamber, performance and physical evidence. No live
installation or release-channel promotion is part of preparing this candidate.

## Core features

Paths below are relative to `Baymax-main/`.

| Feature | Organized source | Behavior and limits | Tests / evidence |
|---|---|---|---|
| Startup | `run_mvp.py`, `run_pc.py`, `device_config.py` | Saved-device readiness, single instance, eye lifecycle and v10 startup | `tests/test_mvp.py` |
| Conversation | `features/persona.py`, `features/conversational_ability/`, `realtime_gemini_10.py` | Gemini conversation, language adaptation/switches/mixed speech, audio cleanup, turn-taking, wake/disengage and volume; remote processing requires connectivity | `tests/test_conversational_ability.py`, `tests/test_address_gate.py`, `tests/test_session.py`, `tests/test_voice_volume.py`, `tests/test_language_eval.py` |
| Speech and latency | `features/conversational_ability/timing.py`, `runtime_monitor.py`, `static/emberdevUI.html` | Independent local speech indicator, updating chat timings, reply wait, output-underrun counter; speech/DAC times are estimates | `tests/test_conversation_timing.py`, `tests/test_runtime_monitor.py`; repository-root `test/monitor-chat.cjs` |
| Memories | Memory hooks in `realtime_gemini_10.py`, `semantic_embedder.py` | Existing retrieval and incremental persistence; settings control enablement, models download separately | `tests/test_memory_saving.py`, `tests/test_semantic_embedder.py` |
| Person recognition boundary | `features/person_recognition/`; face-presence handling in `realtime_gemini_10.py` | Namespace reserved; face presence is not verified personal identity. This folder does not implement an identity engine | Person-recognition README and camera/pose tests |
| Pose and fall processing | `features/pose/`, `features/vision_process.py`, `fall_detection/` | Child-process inference and CPU fallback; RK3588 NPU requires conversion/checks. Enabled detection does not establish accuracy | `tests/test_v10_runtime.py`, `features/pose/VALIDATION.md` |
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

## Verification

- `Baymax-main/check_release.py` reports separate core/toolkit results.
- From the root, `node test/monitor-chat.cjs` checks chat updates.
- Flash builder tests are separate; an image build is not a fresh-card boot.
- Desktop CI has environment/platform failures: Linux PortAudio server
  initialization and Windows file-locking/symlink privileges. Do not label it green.
- Actual board checks, software-lead review and release promotion remain separate.
