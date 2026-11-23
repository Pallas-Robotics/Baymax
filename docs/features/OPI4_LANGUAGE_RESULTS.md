# Orange Pi language simulation results

Test date: 2026-11-06 (America/Los_Angeles). Hardware: Orange Pi 4 Pro, Linux ARM64, 8 CPU cores, 3,903.7 MiB usable RAM.

## Configuration and scope

- Public source base: `7eca611`; the reviewed language instruction from upstream commit `1779a2cd86` was added **only to the temporary test copy**.
- Gemini model: `gemini-2.5-flash-native-audio-preview-12-2025`.
- Configuration SHA256: `4637e7e94b804aa474c1d57f73dda9120fa35e86345f14e43dc7ae2f7fb417e8`.
- The installed Ember app was left running and listening. No runtime deployment, restart, microphone/camera capture stream or physical test-speaker output was added.
- Local synthetic speech uses Debian espeak-ng, installed for these tests. All Gemini calls use synthetic content and stub function-tool results. The actual prompt/config retains its built-in model capabilities.
- The complete matrix uses text input and receives actual generated speech/output transcripts. It tests response content and switching, not microphone recognition or acoustic pronunciation.
- Completed serial checkpoints were reused only with the exact same configuration; remaining work used two concurrent test sessions alongside the normal Ember service.

The same tested instruction is now integrated into `Baymax-main/features/persona.py` on the review branch. The measurements above retain their original temporary-copy provenance; this Git update was not deployed to the now-unplugged robot.

## Repeated response-language matrix

27 scenarios × 3 runs; 240 turns. **80/81 complete runs passed scripted checks; 26/27 scenarios passed all three runs.**
The cases cover 26 language/script/region entries plus Hindi-English mixing. A keyword match is not human language/pronunciation review; no fully validated audio-language claim is made.

| Scenario | Code | Automatic runs | Median first audio (s) | p95 (s) |
|---|---|---|---:|---:|
| English | en | 3/3 | 1.06 | 2.78 |
| Spanish | es | 3/3 | 1.74 | 2.21 |
| French | fr | 3/3 | 1.67 | 2.6 |
| German | de | 3/3 | 1.97 | 2.72 |
| Italian | it | 3/3 | 1.72 | 2.32 |
| Portuguese (Brazil) | pt-BR | 3/3 | 1.8 | 3.15 |
| Portuguese (Portugal) | pt-PT | 3/3 | 1.21 | 2.76 |
| Hindi | hi | 3/3 | 1.38 | 1.99 |
| Japanese | ja | 3/3 | 1.25 | 2.12 |
| Korean | ko | 3/3 | 1.76 | 2.05 |
| Chinese (Simplified) | zh-Hans | 3/3 | 1.25 | 2.8 |
| Chinese (Traditional) | zh-Hant | 3/3 | 1.11 | 1.83 |
| Arabic | ar | 3/3 | 1.17 | 2.79 |
| Russian | ru | 3/3 | 1.85 | 2.96 |
| Bengali | bn | 3/3 | 1.23 | 2.47 |
| Dutch | nl | 3/3 | 1.99 | 2.45 |
| Indonesian | id | 3/3 | 1.94 | 4.58 |
| Marathi | mr | 3/3 | 1.34 | 2.62 |
| Polish | pl | 2/3 | 1.25 | 2.13 |
| Romanian | ro | 3/3 | 1.47 | 2.05 |
| Tamil | ta | 3/3 | 1.79 | 2.42 |
| Telugu | te | 3/3 | 1.1 | 1.8 |
| Thai | th | 3/3 | 1.2 | 2.43 |
| Turkish | tr | 3/3 | 1.46 | 2.45 |
| Ukrainian | uk | 3/3 | 1.16 | 1.91 |
| Vietnamese | vi | 3/3 | 1.4 | 3.96 |
| Hindi-English mixed | hi-en-mixed | 3/3 | 1.44 | 2.64 |

Failed/flaky scenarios: pl.
The observed Polish failure kept responding in Polish after the user switched to an English banana-color question; its other two runs passed. Do not retry away or conceal that failure.

Overall first-audio latency: median **1.48 s**, p95 **2.62 s**, max **4.58 s**, 240 observed turns.
For this text-input matrix, timing starts when the text turn is sent and ends at receipt of the first reply-audio chunk. It does not measure Gemini internals, microphone speech end, DAC playback or acoustic output.
No matched single-session baseline was collected for every language/workload, so added latency caused by concurrency or a particular feature is not established by this run.

## Synthetic speech and transcription pilots

- One three-turn audio run each exercised English, Spanish and Hindi with the staged language instruction. These did not establish complete speech-path passes.
- Spanish replies were in Spanish and switched back to English, but native input transcripts were punctuation-only. A separate trace with a six-second settling window still produced only punctuation: it arrived about 0.31 s after speech end; first reply audio arrived after 2.15 s. This observation was not merely a late debugger render.
- One English input transcript was empty even though a sensible reply arrived. Other English input transcripts were incomplete.
- Hindi synthetic speech led to a weather-tool call and incorrect color answers, while the Hindi text comparison passed. This separates response-language behavior from synthetic-voice/input-recognition quality; it does not prove real Hindi speakers are unsupported.
- Punctuation-only ASR now fails the shared test, including when older saved results are recompiled. Local transcript-based address gating was not exercised and needs its own language checks.

## Resource measurements

The table compares a 15-second baseline with a separate 30-second read-only sample during two concurrent language test sessions. Evaluation/monitoring overhead is included; this is an observed comparison, not a controlled estimate of production feature cost.

| Metric | Baseline mean / peak | Concurrent tests mean / peak |
|---|---:|---:|
| System CPU % (0–100 across 8 cores) | 22.69 / 24.97 | 25.8 / 27.97 |
| Existing Ember process-tree RSS MiB | 433.39 / 433.86 | 435.04 / 435.7 |
| System RAM used MiB | 613.25 / 623.26 | 847.75 / 851.57 |
| Hottest exposed thermal zone °C | 41.49 / 45.57 | 43.51 / 46.25 |
| NPU core load % | Unavailable to normal user | 0.0 / 0.0 |

The A733 vipcore driver/device and viplite debug telemetry were present. NPU usage was read with existing sudo authorization, without enabling profiling or changing permissions; load was 0% throughout the 30-second sample and the driver memory profile showed no allocations at the inspection. Cloud Gemini conversation does not establish local NPU inference functionality. The shared sampler now recognizes viplite/core_loading as well as Rockchip telemetry.
Process-tree RSS sums descendants and can double-count shared pages; it is not PSS.

## Validation limits and artifacts

- On-Pi offline checks: 12 multilingual evaluator tests and 16 existing evaluator tests passed before live testing. Afterwards the updated 12 multilingual, 16 existing evaluator and 9 performance tests all passed on the Pi. Focused desktop checks also cover punctuation rejection, stored-result rechecking, automatic pass counts and A733 load parsing.
- Full 99-entry candidate/validation matrix: [LANGUAGES.md](LANGUAGES.md). Unscripted/unrun candidates remain labeled; no language is promoted into the validated-audio list without repeated audio runs and exact-evidence review.
- Raw synthetic transcripts, output WAVs, ASR trace and resource JSON stay under the ignored local `.local-review/opi-languages/` directory; no keys, private runtime records or raw dialogue are published here.
- Still pending: competent language/audio review, real-voice microphone/speaker trials, local address-gate tests, longer/noisy conversations, and the combined release hardware checklist. No universal Orange Pi compatibility or release approval is implied.
