# Ember features

Core feature systems and callable toolkits are siblings here:

`persona.py` supplies Ember's spoken identity and social guidance through the
existing session config hook, with toolkits enabled or disabled. Replies normally
make one brief conversational move, follow a specific detail with a question or
observation when appropriate, and leave space for the person. Personality stays
present without routine praise, unsolicited wellness reminders or invented topics.
The prompt is sent to Gemini with session instructions; it adds no provider,
device stream or setting. `tests/test_toolkit_eyes.py` checks persona injection;
live conversation still needs evaluation for brevity, relevance and turn balance.

Its `[LANGUAGE]` section has Ember answer in the language the person speaks (a
switch is followed on the next reply, a Hindi-English mix gets the same mix) and
answer its own English notes ([FALL ALERT], reminders) in that language. The
native-audio model picks the language itself: no `language_code` or transcription
language is set, so none must be added. The offline pause words stay English
("Hey Ember", "stop"). Still English: the startup hello before anyone speaks,
the eye cues read from the reply text and the saved memory lines (the memory
embedder is English-only). `tests/test_live_eval.py` checks that the section
reaches the session config; `evals/scenarios.json` has the live `language-*` runs.

```text
features/
  memory_palace/       Connected memories, interests, relationships and goals
  person_recognition/ Recognized people and confirmed identity context
  toolkits/           Callable information sources and other bounded actions
  conversational_ability/ Responsive listening, audio cleanup and turn-taking
  eyes/                Normal blinking and delayed thinking on MAX7219 matrices
  pose/                Camera pose: MediaPipe on the CPU, or the RK3588 NPU when ready
```

The runtime coordinates these systems. Person recognition supplies identity context;
the memory palace maintains the connected understanding of that person; toolkits
retrieve information or perform supported actions. A personal news feed is one
consumer of that understanding, alongside conversation starters and follow-ups.
Memory and recognition are core feature systems, not toolkits.

`toolkits/` contains the existing implementation used by desktop v8 and robot v10.
Its [README](toolkits/README.md) and [public experiment guide](toolkits/EXPERIMENTAL.md)
describe providers, data flows, enablement and tests. Imports use
`features.toolkits`; desktop Setup/Monitor and clickables keep the same entry points.

`memory_palace/` and `person_recognition/` reserve integration boundaries; this PR adds no memory graph, recognition engine, identity prompt or changes to recalled-memory wording. Those behaviors are scoped to PR #13 and the existing people-awareness work in PR #8. Do not create another identity store or treat feed profile IDs as recognized people.

Feature package imports must not start providers, open devices or run background
workers. Runtime startup owns lifecycle and enablement; each implemented feature
needs documented inputs, outputs, data flows and tests. Runtime data stays under
the existing `BAYMAX_DATA_DIR`, outside the source tree in Docker.

[Conversational ability](conversational_ability/README.md) is implemented independently
of people recognition and memory. Memory Palace is reviewed in PR #13.

[LED eyes](eyes/README.md) are started by the MVP launcher: normal blinking
while listening/speaking, and the supplied thinking dots only after a wait
exceeds two seconds. Serial hardware is optional and failures are non-fatal.

[Pose](pose/README.md) chooses the camera thread's pose backend, inside its pose child
process (`vision_process.py`): MediaPipe on the CPU as before, or the full model on
the Orange Pi 5's NPU once it is converted and passes
the converter's on-board check (which verifies the conversion; the NPU's fall decisions
are close to MediaPipe's but not identical, see its VALIDATION.md). It never opens the
camera itself. In v10 the fall toolkit reuses this pose instead of running its own.
