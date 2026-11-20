# Person recognition: faces and voices

Ember knows the people it has been introduced to: by face (camera) and by voice (mic).
This is PR #8 (people awareness, face recognition, Joe Kojima) and PR #36 (voice
recognition, sjylee) from JoeKojima/Baymax, ported to the Orange Pi build so that it
costs the conversation nothing.

## How it runs

| | Where | Cost to the conversation |
|---|---|---|
| Face models (YuNet detection, SFace 128-d embeddings; OpenCV DNN) | `face_process.FaceChild`, a child process on the slow cores (cpu0-3 on the RK3588), nice 10, one OpenCV thread | none: the capture loop only stores a frame reference (`frame_observers`) |
| Face tracking, voting, learning, the people file | `faces.FaceIdentifier` in Ember's process, 2 frames a second, its own thread | small numpy work; a frame crosses the pipe once per cycle (4.1 ms round trip on a Mac vs 3.9 ms in process) |
| Voice models (Silero VAD, ECAPA-TDNN 192-d embeddings; ONNX Runtime, 3 threads) | `voice_process.VoiceChild`, a child process on the slow cores, nice 10 | the mic callback appends each 20 ms block to a bounded deque (~34 µs on a Mac); a feeder thread ships batches every 0.25 s |
| Telling Gemini | tools (`who_is_here`, `identify_speaker`) answered from the latest results without waiting on a model; spoken prompts (`[ARRIVED]`, `[NEW PERSON]`, `[VOICE LEARNED]`) only when Ember is free to talk | no mid-turn notes |

Why not as the PRs did it: both ran their models in Ember's own process (a face thread,
a voice thread with PyTorch), where inference competes for the GIL with the mic callback
and the Gemini connection (the reason pose moved to a child process), and both sent
notes while people were still talking (`[SELF]`, `[MEMORY CONTEXT]`, `[SPEAKER]` with
`turn_complete=False`). Gemini 2.5 ignores such notes (0 of 10 in the 11-07 live eval) and
on the Orange Pi they arrived while a reply was owed. Here Gemini asks with a tool, and
spoken prompts wait for `everyday.available_to_speak()` (not speaking, thinking or
calling a tool; nobody heard for 3 s), at most one per 15 s.

A child that dies or hangs (the vision watchdog: 5 s a face call, 20 s a voice call) is
started again after 60 s (face) or 30 s (voice); Ember carries on without it meanwhile.

## No PyTorch

The voice models are PR #36's own, on ONNX Runtime (already installed for memory):

- `convert_voice_model.py` (run on a computer with PyTorch, not on the robot) exports
  SpeechBrain's `spkrec-ecapa-voxceleb` with its mel filterbank, log/top-dB step and
  per-utterance mean normalisation inside, and copies Silero VAD's ONNX file. It checks
  the result against SpeechBrain's `encode_batch` at 1-15 s; 2026-11-13: cosine
  1.000000 at every length, so PR #36's thresholds (sure >= 0.40, maybe >= 0.25,
  calibrated on SpeechBrain) hold unchanged. The robot computes only the STFT in numpy
  (`voices.power_spectrogram`). Our Silero port matched the package frame for frame.
- 8-bit quantisation was tried and dropped: slower on ARM with ONNX Runtime's
  ConvInteger, and it moved scores by up to 0.025.
- Identification uses at most `MAX_ID_SECONDS` (4 s) of an utterance, bounding the
  work on the slow cores; PR #36 calibrated on 3-4 s clips.
- Measured on the Orange Pi 5 Max's slow cores (cpu0-3, nice 10), 2026-11-13: ECAPA 3 s
  of speech 2.73 s on 1 thread, 1.35 s on 2, 0.93 s on 3 (used), 0.73 s on 4; Silero VAD
  1.4 ms per 32 ms frame (~4% of one core while listening); YuNet 55 ms per frame at
  half scale (2 frames a second: ~11% of one core); SFace 234 ms per face, only for a
  new face or a recheck every 3 s.

## Models

| Model | File | Source | Size |
|---|---|---|---|
| YuNet | `.models/faces/face_detection_yunet_2023mar.onnx` | OpenCV Zoo, downloaded at first start, SHA-256 checked | ~0.2 MB |
| SFace | `.models/faces/face_recognition_sface_2021dec.onnx` | OpenCV Zoo, the same | ~39 MB |
| AuraFace (Orange Pi NPU) | `.models/faces_npu/auraface_fp16.rknn` | `convert_face_rknn.py` on the board, from fal/AuraFace-v1 `glintr100.onnx` (Apache-2.0, sha256 `a7933ea5…`) | ~250 MB ONNX |
| ECAPA-TDNN | `.models/voice/ecapa_voxceleb.onnx` | `convert_voice_model.py`, then copied to the robot | 83 MB (sha256 `9df89cb3…`) |
| Silero VAD | `.models/voice/silero_vad.onnx` | the same | 2.3 MB |

Voice recognition stays off (logged once) until `.models/voice/` has both files.

## Changes from the PRs after testing on the board (2026-11-13)

- **Voices are learned from conversation.** When exactly one face is in view and it is
  someone Ember knows, their finished utterances of 2 s or more are offered as voice
  samples. Anyone in the room may be the one talking, so a clip is kept only if it
  clearly sounds like them: a first voice needs three clips (among their last eight)
  that agree with each other by 0.45 (`voices.consistent_core`), and a later clip must
  score 0.38 against the stored voice (`ADOPT_MATCH`). A voice already recognised "sure"
  is sampled at most once a minute. On the board's USB mic, clips of one speaker agreed
  by 0.43-0.46 and other people's clips scored 0.08-0.28 against them. A first version
  used PR #36's 0.25 check: during a group conversation Joe's voice took in other
  people's clips (pairwise similarity down to -0.03) and then matched nobody well.
  Voices saved before these rules are rechecked once at start (`recheck_voices`): only
  the clips of one speaker are kept, or the voice is removed to be learned again.
  PR #36 only learned voices through `remember_voice`, which never happened in
  practice. `BAYMAX_VOICE_LEARNING=0` restores remember_voice-only.
- **Voice and face together, either one enough** (`people.identity_answer`).
  `identify_speaker` and `who_is_here` answer who is talking from both:
  - a sure voice names the speaker;
  - the only face in view, recognised, names them when the voice can't (not heard
    yet, unknown, or a weak "might be" someone else);
  - a voice and a face that only "might be" the same person agree, so it's sure;
  - with no face in view, the one person recognised by face or voice in the last
    `RECENT_S` (120 s), nobody else since, is "probably" still them.

  On ember-001 Ember greeted Joe by face, then answered "who is this?" with "I haven't
  heard enough of your voice yet", because only the voice was asked.
  - A sure voice also names a "maybe" face (`faces.confirm_by_voice`).
  - A named face that turns away keeps its name (`faces.HANDOFF_SECONDS`).
- **One yes covers face and voice.** The prompt asks to remember "them", not their
  face. `remember_person` tells Gemini their voice is learned on its own as they talk.
  `remember_voice` for someone not yet remembered, alone in view, remembers their face
  first.
- **Memories don't name "the user".** Memory lines saved by the summariser assume one
  user; "The user's name is Nadi" made Gemini call everyone Nadi. With recognition on,
  such lines are left out of the prompt, recalled memories and new summaries, and the
  summariser and the memory block say that several people may be talking.
- **Already known.** `remember_person` for someone recognised in view answers
  `already_known` (it used to refuse with a message about someone who had said no).
- **Placeholder names.** Gemini saved a face as "User" while asking the person's name.
  `remember_person`/`remember_voice` now refuse placeholder names, and a face in view
  stored under one is renamed when the person gives their name.
- **Greeting on return.** Known people are greeted again after
  `BAYMAX_PEOPLE_GREET_AFTER_S` (120 s) away instead of PR #8's 20 minutes, by face or by
  voice (a "sure" voice of someone not seen or heard for that long), once per return.
  "Last seen" is saved every minute (PR #8: 10), so restarts don't lose it.
- Gemini is told to call `who_is_here` before asking anyone's name or permission, and
  that names from its own recognition may be used.
- Logs: one `[FACE] track` line per face whose identity changes (score included), one
  `[VOICE]` line per identification, and each people tool call with its result.

## Who is talking, and to whom

- **Session voices** (`speakers.py`, in the voice child). Every utterance of 1 s or more
  gets a session voice: the one it sounds most like among those heard in the last 10
  minutes (`SAME_SPEAKER` 0.32 against its average, 0.25 against a voice heard only once),
  or a new one, "voice 1", "voice 2", .... Two voices whose averages agree by 0.50 are
  merged (one person whose first utterances were unlike each other). Nothing is stored.
  In a simulation matching the board's numbers, two people taking turns were two clean
  voices after eight turns 85% of the time and never merged into one.
- **Recognition from several utterances.** One utterance on this mic scores only
  0.25-0.55 against the speaker's own stored voice. When a session voice's average is
  "sure" for a stored voice, its utterances count as that person's (`by: several
  utterances` in the log), even the ones that alone were "might be".
- **Turn taking** (`conversation.py`, in Ember's process). Each finished utterance (also
  the short ones) is a turn with its session voice and whether the speaker faced the
  camera (their face, or the only face in view; YuNet's eyes and nose:
  `Detection.facing`). People are **talking to each other** when, since Ember last
  spoke, two established voices (heard twice, or recognised) have taken turns, two
  people are evidently here (two faces in view in the last 30 s, or two known voices),
  and the latest speaker isn't facing Ember. One person alone never counts, even if their
  voice is split.
- **What Ember does about it.** The reply gate (`addressing.AddressGate`, `side`) holds
  Gemini's reply and drops it unless someone says "Ember" (or it answers one of Ember's
  own messages); a held reply plays if the speaker turns out to be facing Ember. Ember
  starts no conversation of its own ([ARRIVED], [NEW PERSON]) meanwhile. It ends when
  Ember speaks again or someone turns to it. `who_is_here` reports how many people have
  been talking and whether they are talking to each other. Logs: `[GROUP]` when this
  changes, `[ADDRESS] Not spoken to (people talking to each other)` for a dropped reply.
  `BAYMAX_GROUP_AWARE=0` turns it off; it is also off with
  `conversation.only_when_addressed = false`.

## Faces: fewer misses, less forgetting

- **New angles are learned.** A face confirmed on its track (two clear matches) that
  then scores 0.40-0.55 against the same person (turned, other light) is stored as a
  sample, at most every 3 minutes per person (PR #8: only 0.55-0.85, every 15 minutes).
- **No "who are you?" for a half-recognised face.** A face that scores 0.30 or more
  against someone known isn't introduced as new: it waits for a clearer look (board:
  Joe turned away, 0.33-0.38, was asked his name).
- **Backups.** Before a change, `ember_people.json` is copied to
  `ember_people.json.backup-<time>` at most every 6 hours; the last 4 are kept.

## A stronger face model (SFace + AuraFace on the NPU)

On the Orange Pi 5 Max, each face is embedded twice: SFace on the CPU as before, and
AuraFace (an ArcFace ResNet100, 512-d, Apache-2.0) on NPU core 1. The two are joined into
one 640-d vector (`face_npu.fuse`) whose similarity is 0.39 x SFace's + 0.61 x AuraFace's.

Why both: AuraFace alone is no better than SFace, but they make different mistakes
(different-person scores correlate only 0.37). LFW, 6000 pairs (3000 same person), on
a Mac, with faces as LFW has them (median 96 px wide) and shrunk to 43 px (Ember at
2-3 m):

| Same people recognised at the "recognised" bar | SFace | AuraFace | SFace + AuraFace |
|---|---|---|---|
| Full-size faces, no false match | 97.3% | 96.9% | 98.7% |
| Small faces, no false match | 97.5% | 95.5% | 97.7% |
| Full-size, 1 in 1000 false | 98.7% | 98.3% | 99.1% |
| Small, 1 in 1000 false | 98.1% | 98.5% | 99.0% |
| Full-size, at Ember's MATCH 0.50 (fused: 0.395) | 92.3% | | 96.3% |
| Small, at Ember's MATCH 0.50 (fused: 0.395) | 89.5% | | 95.5% |
| Turned faces (yaw > 0.25), at MATCH: full-size / small | 91.2% / 86.5% | | 96.0% / 92.9% |

At Ember's own thresholds the combination misses about half as many people with no more
wrong names (no different-person pair reaches the fused MATCH or 0.45-equivalent in
either set; at MAYBE, 2 of 3000 small-face pairs, "might be", which Ember asks about).
Thresholds are SFace's mapped onto the fused scale with one formula
(`faces._sface_to_fused`: the same share of different people above each), except the
near-duplicate end of `AUTO_LEARN_RANGE` (0.82, same share of same-person pairs).

- **Nobody is forgotten in the switch.** People saved with SFace alone are still matched
  by the SFace part of each fused embedding (`PeopleStore.match_all`); once recognised
  on a confirmed track, three fused samples are stored for them, 2 s apart (log:
  `[FACE] Learning Joe's face for sface+auraface: 1 of 3 samples`). SFace samples are
  kept. With SFace alone again (NPU model removed or failing), people met meanwhile are
  matched by the SFace part of their fused samples.
- **Only after a check on the board.** `convert_face_rknn.py all --images <dir of face
  photos>` (in pose's `venv-rknn`) downloads the ONNX (SHA-256 checked), builds an FP16
  RKNN with the normalisation built in, and compares NPU and ONNX embeddings on real
  faces (cosine >= 0.99 each). Only then does `manifest.json` say `"check": "passed"`,
  and only then (Linux aarch64) is it used; otherwise SFace alone, as before.
- If the NPU model won't load at start, Ember logs it and uses SFace alone at once.
- InsightFace's own ArcFace models score higher but are licensed for non-commercial
  research only; EdgeFace's weights likewise. AuraFace was trained on data its authors
  describe as commercially usable.
- Mac reference speed: SFace 18-30 ms, AuraFace 307-700 ms per face on CPU. NPU time
  on the board: to be measured by `convert_face_rknn.py check`.

## Data, consent and forgetting

- `ember_people.json` in `BAYMAX_DATA_DIR`: per person an id, name, optional
  relationship, up to 20 face embeddings and up to 20 voice embeddings (numbers, never
  photos or recordings), times seen. PR #8's `PeopleStore`: file-locked writes, an
  unreadable file is set aside, not overwritten.
- Nobody is remembered without saying yes: `remember_person` and `remember_voice` are
  only for after a clear yes; a first voice is only taken for someone known by face and
  in view (nobody can file their voice under someone else's name).
- `forget_person` removes the person's face and voice. Face crops for arrivals are made
  in Ember's process and never stored.
- Nothing here sends anything anywhere: models run on the robot. Gemini receives only
  the bracketed prompts and tool answers (names, "might be", "unknown").

## Switches

| Variable | Default | |
|---|---|---|
| `BAYMAX_PEOPLE_RECOGNITION` | `0`; `run_mvp.py` sets `1` | faces (and voices when the models are present) |
| `BAYMAX_VOICE_RECOGNITION` | `1` | `0`: faces only |
| `BAYMAX_PEOPLE_GREETINGS` | `1` | `0`: no `[ARRIVED]`/`[NEW PERSON]` conversations started by Ember |
| `BAYMAX_GROUP_AWARE` | `1` | `0`: replies aren't held while people talk to each other |
| `BAYMAX_FACE_NPU` | `1` | `0`: SFace alone even with the NPU model converted |
| `BAYMAX_FACE_NPU_CORE` | `1` | NPU core for AuraFace (`0`, `1`, `2`, `auto`; pose uses 0) |
| `BAYMAX_FACE_NPU_DIR` | `.models/faces_npu` | where the converted model and manifest are |

## Not ported yet

PR #8's per-person conversation memory (`person_memory.py`: memories tagged with who was
there, recalled on arrival, rewritten when someone is forgotten), appearance notes,
the dashboard's People page and photo enrolment, and `ember_self`/`ember_identity.md`
(which would replace the base persona; AGENTS.md keeps it).

## Tests

`tests/test_person_recognition_faces.py` (PR #8's 88 tests), `..._voice_id.py` and
`..._voice_live.py` (PR #36's 17 and 25), `..._runtime.py` (children, the mic feed,
tools, spoken prompts, registration), `..._conversation.py` (session voices, turn
taking, the reply gate's hold, facing, backups), `..._face_npu.py` (alignment, fusion,
fused thresholds, moving people saved with SFace over, falling back). Suite: `tests/features/person_recognition`.
Hardware checks pending: on the Orange Pi 5 Max, CPU per core with both children
running, conversation latency unchanged, a face learned and recognised, a voice learned
and recognised, `[ARRIVED]` spoken only between turns. The board's camera frames were
nearly black at the last check (mean 5.6/255): faces need the exposure fixed first.
