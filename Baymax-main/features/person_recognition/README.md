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
| Voice models (Silero VAD, ECAPA-TDNN 192-d embeddings; ONNX Runtime, 2 threads) | `voice_process.VoiceChild`, a child process on the slow cores, nice 10 | the mic callback appends each 20 ms block to a bounded deque (~34 µs on a Mac); a feeder thread ships batches every 0.25 s |
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
- Identification uses at most `MAX_ID_SECONDS` (5 s) of an utterance, bounding the
  work on the slow cores; PR #36 calibrated on 3-4 s clips. Speed on an M-series Mac,
  2 threads: about 45 ms per second of speech.

## Models

| Model | File | Source | Size |
|---|---|---|---|
| YuNet | `.models/faces/face_detection_yunet_2023mar.onnx` | OpenCV Zoo, downloaded at first start, SHA-256 checked | ~0.2 MB |
| SFace | `.models/faces/face_recognition_sface_2021dec.onnx` | OpenCV Zoo, the same | ~39 MB |
| ECAPA-TDNN | `.models/voice/ecapa_voxceleb.onnx` | `convert_voice_model.py`, then copied to the robot | 83 MB (sha256 `9df89cb3…`) |
| Silero VAD | `.models/voice/silero_vad.onnx` | the same | 2.3 MB |

Voice recognition stays off (logged once) until `.models/voice/` has both files.

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

## Not ported yet

PR #8's per-person conversation memory (`person_memory.py`: memories tagged with who was
there, recalled on arrival, rewritten when someone is forgotten), appearance notes,
the dashboard's People page and photo enrolment, and `ember_self`/`ember_identity.md`
(which would replace the base persona; AGENTS.md keeps it).

## Tests

`tests/test_person_recognition_faces.py` (PR #8's 88 tests), `..._voice_id.py` and
`..._voice_live.py` (PR #36's 17 and 25), `..._runtime.py` (children, the mic feed,
tools, spoken prompts, registration: 23). Suite: `tests/features/person_recognition`.
Hardware checks pending: on the Orange Pi 5 Max, CPU per core with both children
running, conversation latency unchanged, a face learned and recognised, a voice learned
and recognised, `[ARRIVED]` spoken only between turns. The board's camera frames were
nearly black at the last check (mean 5.6/255): faces need the exposure fixed first.
