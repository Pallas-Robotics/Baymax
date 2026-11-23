# Voice control follow up evidence

2026-11-11. Tested source tree d36a43e703fb6aa8988da7b4bd8e0755334a986b.
Linux 6.6.98-vendor-sun60iw2 on the OPI4, Python 3.12.15.

## Findings and scope

The board was running its older e5a4bd7 release after a restart, rather than the PR checkout. That build has no voice-volume integration. Its local wake recognizer was waiting for its first model download after disengage. The microphone capture remained active; only audio sent to Gemini was withheld.

New regression tests first failed for an unbounded startup wait, a pair split across final recognizer results and an unreported decoder failure. They pass with the fixes. Tool dispatch, persistence and different PCM amplitudes at voice levels 4 and 6 are also tested together. Existing confidence thresholds and audio rates are unchanged.

## Native recognizer replay

Four Windows SAPI generated 16 kHz mono PCM files were replayed through the OPI4's real Vosk model and WakeListener path. No microphone or speaker stream was opened.

| Synthetic phrase | Expected | Actual |
|---|---|---|
| Ember engage | Wake | PASS |
| Ember then one second silence then engage | Wake across final results | PASS |
| Ember disengage | No wake | PASS |
| Ember alone | No wake | PASS |

The wake implementation blobs are unchanged between the native-probe tree 8ce10be93925e1f4b65139864cc4a2e91269c266 and the final tested tree above. This is digital recognizer evidence, not room-acoustic validation.

## Provider compatibility

The initial volume scenario with Google Search and zero thinking closed with API error 1011 before a function call arrived. Both repeated runs passed with search off. Both repeated runs also passed with search retained and Joe Kojima's 128-token compatibility setting. The latter is the adapted fix; explicit nonzero budgets are preserved. Source: https://github.com/Pallas-Robotics/Baymax/commit/d2cca1f579271dbcd01b108d09cef7d4e967f7b1

All five initial language selections were correct; one 26-word reply exceeded a 25-word test limit. The explicit-override case now allows 40 words so that its assertion checks the requested language without an unrelated one-word boundary failure. Final runs are below.

## Final live synthetic evaluation

Only scripted public text prompts and stub tool responses were sent to the existing Gemini model. No real conversation history, app profile, microphone, camera or speaker was used. The real robot's voice tests remain pending owner repetition.
# Ember live evaluation (Gemini in the loop, no robot)

Generated: 2026-11-11 00:33:34 PST

Synthetic speech and pictures only. This is Gemini-behaviour evidence; the robot's microphone, echo cancellation, speaker, camera and eyes are NOT exercised.

| Scenario | Passed | Median first audio | Notes |
|---|---|---|---|
| voice-volume-command | 2/2 | 1.87s |  |
| immediate-language-switching | 2/2 | 1.98s |  |

## voice-volume-command
- Run 1 ok: said 'Ember, set your voice volume to 4.' | heard '' | tools set_voice_volume | reply 'Okay. Is this better?' | first audio 1.34s
- Run 1 ok: said 'What is your voice volume now?' | heard '' | tools set_voice_volume | reply 'My voice volume is currently at 4 out of 10.' | first audio 1.55s
- Run 2 ok: said 'Ember, set your voice volume to 4.' | heard '' | tools set_voice_volume | reply 'Okay. Is this better?' | first audio 2.18s
- Run 2 ok: said 'What is your voice volume now?' | heard '' | tools set_voice_volume | reply 'My voice volume is currently at 4 out of 10.' | first audio 2.22s

## immediate-language-switching
- Run 1 ok: said 'Ember, what color is a ripe banana? Answer briefly.' | heard '' | tools none | reply 'A ripe banana is typically yellow.' | first audio 1.02s
- Run 1 ok: said 'Ember, ¿de qué color es el cielo despejado?' | heard '' | tools none | reply 'El cielo despejado es de un azul brillante.' | first audio 3.2s
- Run 1 ok: said '엠버, 잘 익은 바나나는 무슨 색인가요?' | heard '' | tools none | reply '잘 익은 바나나는 주로 노란색이에요.' | first audio 1.72s
- Run 1 ok: said 'Ember, what color is fresh snow? Answer in Spanish.' | heard '' | tools none | reply 'La nieve fresca es de color blanco.' | first audio 1.68s
- Run 1 ok: said 'Ember, what color is fresh grass?' | heard '' | tools none | reply 'Fresh grass is usually a vibrant green.' | first audio 0.94s
- Run 2 ok: said 'Ember, what color is a ripe banana? Answer briefly.' | heard '' | tools none | reply 'A ripe banana is yellow.' | first audio 2.61s
- Run 2 ok: said 'Ember, ¿de qué color es el cielo despejado?' | heard '' | tools none | reply 'El cielo despejado es azul.' | first audio 3.11s
- Run 2 ok: said '엠버, 잘 익은 바나나는 무슨 색인가요?' | heard '' | tools none | reply '잘 익은 바나나는 노란색이에요. 때로는 갈색 반점이 생기기도 하죠.' | first audio 2.15s
- Run 2 ok: said 'Ember, what color is fresh snow? Answer in Spanish.' | heard '' | tools none | reply 'La nieve fresca suele ser de un blanco deslumbrante, aunque puede cambiar según la luz y el cielo.' | first audio 2.06s
- Run 2 ok: said 'Ember, what color is fresh grass?' | heard '' | tools none | reply 'Fresh grass is usually a vibrant green, full of life.' | first audio 1.89s


## Final application gate

# Ember review checklist

Commit: unavailable
Generated (UTC): 2026-11-11T08:32:41.025495+00:00
Platform: Linux 6.6.98-vendor-sun60iw2; Python 3.12.15

## Automated offline results

- core: PASS; 593 tests, 0 failures, 0 errors, 5 skipped.
- toolkit: PASS; 252 tests, 0 failures, 0 errors, 0 skipped.

Offline checks use simulated devices/providers. PASS is not live validation or release approval.
Skipped tests must be explained in the PR. Local uncommitted changes are not identified by the commit ID.

## Final repository suites

# Repository validation

Generated UTC: 2026-11-11T08:33:59.605493+00:00

| Suite | Result | Tests | Failures | Errors | Skipped |
|---|---|---:|---:|---:|---:|
| conversational_ability | PASS | 163 | 0 | 0 | 0 |
| device_settings | PASS | 59 | 0 | 0 | 0 |
| everyday | PASS | 42 | 0 | 0 | 0 |
| eyes | PASS | 60 | 0 | 0 | 0 |
| family | PASS | 12 | 0 | 0 | 0 |
| graphify | PASS | 3 | 0 | 0 | 0 |
| information | PASS | 91 | 0 | 0 | 0 |
| languages | PASS | 14 | 0 | 0 | 0 |
| media | PASS | 54 | 0 | 0 | 0 |
| memory_palace | PASS | 61 | 0 | 0 | 3 |
| overall | PASS | 17 | 0 | 0 | 1 |
| performance | PASS | 9 | 0 | 0 | 0 |
| person_recognition | N/A | 0 | 0 | 0 | 0 |
| pose | PASS | 107 | 0 | 0 | 1 |
| runtime | PASS | 71 | 0 | 0 | 0 |
| sd_card_flash | PASS | 39 | 0 | 0 | 0 |
| startup | PASS | 85 | 0 | 0 | 0 |

Offline results do not establish physical board compatibility or release approval.
Explain skips and run the live checklist before deployment. Detailed logs: .local-review/validation/.


## Remaining checks

Owner speech, actual playback volume, engage recovery in the room and immediate language changes must be repeated on the fixed PR build. Other feature trials stay deferred. James's app is untouched. Windows and headless desktop CI limitations remain separate review issues; this is not release approval.
