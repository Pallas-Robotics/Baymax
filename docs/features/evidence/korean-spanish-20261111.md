# Korean-to-Spanish follow-up — 2026-11-11

Owner speech on PR revision `2d02466` was transcribed as Spanish while Ember
continued answering in Korean, including a Korean acknowledgement of a request
to speak Spanish. This is a failed physical language-switch test. No private
transcripts or recordings are included here.

The earlier synthetic scenario asked in English for an explicit Spanish reply;
it did not cover ordinary Spanish immediately after Korean. The persona also
contained an ongoing-preference clause conflicting with latest-turn selection.
The follow-up removes it and explicitly re-evaluates each turn, including the
reply to a switch request itself. No model, audio, toolkit, app, or capture
configuration changes.

`evals/voice_controls.json` adds `korean-to-spanish-text` and
`korean-to-spanish-audio`. Each runs English, an explicit Korean reply request,
ordinary Korean, an ordinary Spanish question without a switch request, a
Spanish switch request, then ordinary English. Synthetic questions concern
colors, not the owner's conversation. Audio uses installed espeak-ng voices
en/ko/es. The real MVP Gemini configuration is used with stub tools and isolated
synthetic data. No microphone/camera is opened; only these generated inputs and
the normal persona/tool schema are sent to the existing Gemini provider.

## Provider comparison

Same Gemini model/configuration, two fresh sessions per scenario and policy:

| Policy | Text | Generated speech | Median first audio, text / speech |
|---|---|---|---|
| PR `2d02466` | 2/2 | 1/2 | 1.08 s / 1.96 s |
| Revised language block | 2/2 | 2/2 | 1.08 s / 1.92 s |

The baseline speech failure was a misunderstood color question (a Spanish answer
about honey instead of grass), not a reproduced Korean reply. Both baseline
and revised synthetic runs switched out of Korean; this comparison does not
establish that the preference clause caused the owner's failure. It removes a
real policy conflict and closes the missing direct-transition test coverage.
Reviewed revised output transcripts answer the first Spanish question in Spanish
in all four sessions, with no Korean characters. Some synthetic audio input
transcripts were incomplete or punctuation-only; this does not certify ASR or
pronunciation. No reply recordings were retained or audibly reviewed.

## Offline validation

Tested source tree: `77210ac90bb40867e398abcbda903f0577b0fcb9`.
Public archive SHA256:
`1578bbf9c5217c2bfe9f4bbcd04da97528267c5a1b4509cc4d76da6e34535429`.
The comparison substitutes only the exact revised persona block into the current
PR's effective session. These application/test blobs are included in this tree.
Subsequent documentation updates do not change them.

- OPI4 `python check_release.py`: 594 core tests, 252 toolkit tests, no failures
  or errors; five core skips; exit 0.
- OPI4 `python tests/run.py`: 888 tests, 16 passing suites, five skips; exit 0.
  Person recognition remains an explicitly reserved namespace with zero tests.
- Skips remain ChromaDB integration, two unselected real-memory-model checks,
  converted pose assets, and Node unavailable for the Linux chat UI check.
- Windows targeted runner: languages 15, overall 17 and performance 9 passed;
  no skips, including the Node chat UI check.
- Graph regenerated; graph suite passed as part of the OPI4 root gate.

Owner voice retest remains required; synthetic results alone do not certify
physical language switching or release. PR stays draft and unmerged; the
previously recorded Windows/headless desktop CI failures remain review issues.
