# Joe OPI4 candidate: isolated software evidence

Source tree: 479180f4e566fec9b02f92f661e133ec40c40a91. No live release, device preference, credential or service was changed by these tests.

## Application gate

# Ember review checklist

Commit: unavailable
Generated (UTC): 2026-11-10T11:14:46.593189+00:00
Platform: Linux 6.6.98-vendor-sun60iw2; Python 3.12.15

## Automated offline results

- core: PASS; 587 tests, 0 failures, 0 errors, 5 skipped.
- toolkit: PASS; 251 tests, 0 failures, 0 errors, 0 skipped.

Offline checks use simulated devices/providers. PASS is not live validation or release approval.
Skipped tests must be explained in the PR. Local uncommitted changes are not identified by the commit ID.

## Core: live checks (pending until a person records evidence)
- [ ] Fresh install, configure devices/key, save and reopen successfully.
- [ ] Test actual microphone, speaker and camera in devUI.
- [ ] Verify a rejected key stops startup before capture; restore a working key.
- [ ] Start Ember, confirm key/model verification, say hello and receive audible Gemini speech.
- [ ] Have a multi-turn conversation and interrupt a reply; record behavior.
- [ ] Verify live transcripts, listening and reply timing in the monitor.
- [ ] Stop and restart; confirm capture/audio stop and devices are released.
- [ ] Disable toolkit; confirm core conversation still works.

## Toolkit: live and integration checks (pending)
- [ ] Ask location; verify approximate city/region or a clear failure.
- [ ] Correct location with city/region/country; resolve ambiguity before acceptance.
- [ ] Ask weather; compare spoken result, units and time with provider data.
- [ ] Ask weather for another city; confirm saved user location stays unchanged.
- [ ] Verify one toolkit start/end message per call, then continue conversation.
- [ ] Record slow/unavailable provider behavior and confirm conversation recovery.
- [ ] If fall detection is enabled: verify disabled/loading/running/error states, actual FPS, safe positive/negative clips, camera loss, check-in and concurrent conversation on the target device.
- [ ] For each added/changed toolkit, record normal, invalid-input and failure cases,
      core integration, disabled behavior, dependencies and external data flows.

## Every feature: performance evidence (pending)
- [ ] Follow devops/performance/README.md: baseline/off, active/on and combined load on the target board.
- [ ] Record CPU/RAM peaks, NPU telemetry or justified N/A, temperature and recognition/reply/playback p95.
- [ ] Check explicit budgets and added p95 latency; record completed/missed turns, sensor gaps and repeat runs.

## Evidence and lead review
- Tester, date, OS, devices and runtime/deployed baseline:
- Core live result and evidence:
- Each toolkit result and evidence:
- Untested platforms, failures and justified N/A items:
- James's review/approval: PENDING (not granted by this command).

Do not paste API keys, private transcripts or recordings into a public PR.


## Repository suites

# Repository validation

Generated UTC: 2026-11-10T11:17:18.844566+00:00

| Suite | Result | Tests | Failures | Errors | Skipped |
|---|---|---:|---:|---:|---:|
| conversational_ability | PASS | 158 | 0 | 0 | 0 |
| device_settings | PASS | 59 | 0 | 0 | 0 |
| everyday | PASS | 42 | 0 | 0 | 0 |
| eyes | PASS | 60 | 0 | 0 | 0 |
| family | PASS | 12 | 0 | 0 | 0 |
| graphify | PASS | 3 | 0 | 0 | 0 |
| information | PASS | 90 | 0 | 0 | 0 |
| languages | PASS | 13 | 0 | 0 | 0 |
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
