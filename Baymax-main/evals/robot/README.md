# Robot test rig (Layer 2): a test robot that tests itself against a PR

Layer 0 (`.github/workflows/offline-checks.yml`) runs `check_release.py` on every PR.
Layer 1 (`evals/live_eval.py`) plays synthetic turns straight into Ember's Gemini
session. Neither touches the robot's microphone, echo cancellation, speaker, camera,
eyes, CPU or wake words. Layer 2 does, without a person in the room:

- A dedicated **test robot** runs the PR's **real runtime** (`run_mvp.py` → `run_pc.py` →
  the `realtime_gemini_N.py` that `run_pc.py` names: `realtime_gemini_10.py` on main) with
  its real devices. `run_mvp.py` is what the Orange Pi robots autostart.
- A **second USB speaker** about 2 m away plays the scripted "person" (synthetic speech)
  and noise (synthetic TV babble, music, pink noise). Ember's echo cancellation only
  removes Ember's own output, so the robot hears this like a person in the room.
- A **tablet** in front of the camera shows synthetic cards from a page the rig serves.
- The rig watches only what the runtime already writes: `.runtime-status.json`
  (`runtime_monitor.py`: states, events, transcripts, toolkit activity, and v10's
  `conversation_timing` rows with the runtime's own speech-end and playback times), the
  stdout log (`[USER]`, `[GEMINI TXT]`, `[ENGAGE]`, `[WAKE]`, `[EYES]`, `[MIC LEVEL]` ...),
  the agent harness trace when the runtime writes one (`EMBER_TRACE`, below), and CPU,
  memory, temperature and NPU load of the process tree (through
  `devops/performance/check.py`'s sampler on Linux).
- It checks each scenario, writes a Markdown and JSON report, and the workflow posts
  the report on the PR.

```bash
python evals/robot/rig.py --root ../pr/Baymax-main --dry-run   # plan and gating only; opens nothing
python evals/robot/rig.py --root ../pr/Baymax-main --yes       # test robot only
python evals/robot/rig.py --calibrate --spl 60.5               # level calibration (below)
python evals/robot/rig.py --root ../pr/Baymax-main --cleanup   # stop a leftover runtime, delete recordings
```

## How this relates to what Pallas already has

| Existing | What it does | Relationship |
|---|---|---|
| Robot gate: `devops/mvp/update.py` (and the image's `ember-update`, `devops/opi4-flash`) | On a production robot, runs `check_release.py` before switching to a new release and rolls back unless `.runtime-status.json` reaches `listening` | Production safety net with no stimulus. The rig runs on a separate **test** robot, before a release, and drives the runtime. Both use the same readiness signal (`listening`); the rig measures it as `ready_within_s`. The rig never installs, moves or rolls back a release, and the test robot must not run the updater. |
| `device_test.py` (devUI's device tests) | A person checks one microphone, speaker or camera works | The rig does not repeat device tests. Use devUI's tests once when setting up the test robot (and to find the person speaker), then calibrate (below). |
| `devops/performance/check.py` | Read-only CPU/RAM/NPU/temperature/latency sampler; a person drives the conversation | The rig **reuses its sampler** for CPU, memory, temperature and NPU (Linux), and reads the same `conversation_timing` rows for reply latency. The rig adds the repeatable synthetic workload the performance protocol asks for. Budgets in `scenarios.json` are placeholders until a baseline run. |
| `evals/live_eval.py` (Layer 1) | Gemini in the loop, no devices | The rig reuses its scenario format, expectation checks, speech synthesis, cards and session-config reading. |
| `check_release.py` (Layer 0) | Offline gate | The workflow runs it on the robot before the rig. |

**Harness trace.** The rig reads the agent harness event trace when the branch's
runtime writes one. The format is **schema version 1** from Pallas-Robotics/Baymax#10
(`features/harness/README.md`).

- **How it gets one.** The rig sets `EMBER_TRACE` to a file in the run's private data
  folder. It does not set `EMBER_TRACE_TEXT`, so the trace has no transcript text or
  tool argument values; transcripts still come from the status file.
- **What it uses:**
  - `state` events at the runtime's `wall` time, which is exact. Without the trace,
    states are seen on the next status poll.
  - `audio_received`, the first reply audio from Gemini. It is a latency source after
    v10's `conversation_timing` playback time, and before the polled "speaking" state.
  - connection, interruption, turn and tool outcomes, as `trace: <kind> …` events that
    `event` expectations can match.
- **Fallback.** A runtime without the trace (it ignores `EMBER_TRACE`), or a
  `trace_start` with another schema version, leaves the status file and log parsing
  as the only sources.

## Design: a self-hosted runner on the test robot, composed with the image updater

**Chosen: option (a).** `.github/workflows/robot-test.yml` runs on a GitHub Actions runner
registered on the test robot (`runs-on: [self-hosted, ember-test-robot]`). It is triggered
when someone adds the `robot-test` label to a same-repository PR.

Why (a) rather than (b), driving the image's boot updater (`ember-update`, `devops/opi4-flash`):

- **Per-PR and on demand.** `ember-update` follows one configured branch and updates at
  boot. Switching PRs means rewriting device settings and rebooting. A labelled run
  checks out exactly the labelled commit (`head.sha`, so a later push cannot slip in),
  and the result lands back on that PR.
- **Built-in plumbing.** Actions provides queueing (one runner, so one job at a time), timeouts,
  cancellation, secrets scoped to an environment with required reviewers, and a place
  to post results. With (b) we would have to build all of that.
- **The installed release is untouched.** The PR runs from the runner's workspace.
  `~/ember-deployment/current` is never re-pointed, and no systemd unit is changed.

How it composes with the image (its files are only read, never modified):

- The `devops/opi4-flash` image provisions the test robot: the OS, packages, AnyDesk, device detection, and
  an installed release with a ready `.venv`.
- `prepare_checkout.sh` reuses that `.venv` when the PR's `requirements*.txt` and
  `devops/clickables/bootstrap.py` match the installed release. This is the same
  shortcut `ember-update` takes for code-only updates. Otherwise it installs the way
  `ember-update` does, including the v10 native packages when the PR runs
  `realtime_gemini_10.py`.
- The rig's `base_env` points at the installed release's `.env`. The robot's saved
  microphone, speaker and camera choices (from Ember Setup and `ember-hardware.py`) are
  reused, and any key in that file is dropped.
- On the test robot, `ember.service` must not hold the microphone. Keep **no
  `GOOGLE_API_KEY`** in `~/ember-deployment/current/Baymax-main/.env`: `ember-run` waits
  for a key and never starts capture. Also create `~/.config/ember/no-autostart`, which
  `ember-update` honours. The rig refuses to start while any other Ember runtime is
  running (it checks every process, not just its own checkout). It never stops a
  service itself.
- Do not put the Docker nightly updater (`deploy/provision/`) on the test robot. That
  unit runs the runtime all the time.

## Scenarios (`scenarios.json`)

These are the same format as Layer 1. `heard`, `reply`, `not_reply`, `tools`,
`not_tools`, `no_tools`, `max_words` and `max_latency_s` are checked by
`live_eval.check`. Synthesis (`live_eval.synthesize`), cards (`live_eval.card`) and
config building (`live_eval.core_session`, `launcher_defaults`, `missing_requirements`)
are reused too.

| Scenario | What only the robot can show |
|---|---|
| `conversation-latency` | Two turns through the real mic and AEC; first audio within 5 s of the person's line ending (this includes Gemini's end-of-turn wait); states thinking → speaking → listening |
| `barge-in` | Talking over a long reply interrupts it (skipped while `BAYMAX_INTERRUPTION_ENABLED=0`, the MVP default) |
| `echo-own-voice` | No echo flag, no user transcript while Ember speaks, then 15 s of silence |
| `disengage-tv-engage` | "Ember, disengage", then 2 min of synthetic TV with near-miss words ("members engaged", "embers"). Zero false wakes, nothing reaches Gemini. "Ember engage" works at 1 m and at 3 m-equivalent (−9.5 dB) |
| `reminder-reengages` | A reminder that comes due while disengaged engages Ember again and is announced |
| `look-closely-card` | a label on the tablet, read by the real camera |
| `radio-stop` / `audiobook-stop` | "Stop" / "Ember stop" over playback from Ember's own speaker pauses it through the offline wake words (needs internet) |
| `eye-states` | `[EYES] Ready` at start, no lost sensors, and the state sequence that drives the eyes |
| `cpu-ceiling` | Mean and peak CPU (% of all cores) and resident memory of the runtime during a turn with a card |
| `restart-engaged` | A restart after "Ember disengage" comes back engaged |
| `restart-recovers` | Stop and start again; connected within 150 s, and it answers |
| `fall-clip-checkin` (timeline, safety gate) | A clip of a person falling plays fullscreen on the TV; `[FALL] Injecting fall alert`, then Ember checks on the person. The reference mic times Ember's first words from the fall moment in the clip. Needs `fall_detection.enabled` and `camera.pose_tracking` in the robot's `device.toml` |
| `fall-clip-control` (timeline, safety gate) | A person sits down normally: no fall alert, no check-in (false-positive control) |
| `barge-in-timed` (timeline) | The person speaks 800 ms after Ember starts a long reply; the reply is interrupted; the reference mic reports how long Ember had really been speaking |
| `noisy-room` (timeline) | Questions over synthetic TV and music; heard and answered, reference-mic latency within 6 s |
| `network-drop` (timeline, safety gate) | The robot loses its network for 45 s (fault hook): it reconnects without a restart and answers afterwards |
| `dim-light-conversation` (timeline) | The smart bulb goes to 5%: camera and conversation keep working (robustness only; see the screen limits) |
| `boot-to-ready` (timeline, safety gate) | Smart-plug power cycle to listening within 180 s. Needs an external controller; the on-robot rig always skips it |

Step kinds:

- `say`: synthetic speech on the person speaker. Optional `distance_m` or `level_db`,
  `voice`, `timeout_s`, `observe_s`.
- `noise`: `tv`, `music` or `pink`, for `seconds`, at a `level_db`.
- `show` (card lines), `show_image`, `clear_display`.
- `wait` (seconds).
- `restart`.

Any step can take `wait_for` with `state`, `log` or `event`, plus `timeout_s` and
`after_s`.

Robot-only expectations:

- `silent`, `max_false_wakes`, `no_user_speech`
- `log` / `not_log` / `startup_log` / `event` / `not_event` (regex)
- `states` (ordered), `engaged`, `no_echo`
- `max_cpu_percent` / `max_cpu_peak_percent` / `max_rss_mb`
- `ready_within_s`

### Timelines

A scenario may have a `timeline` instead of `steps` (`timeline.py` documents the
format): cues on one rig clock that can overlap, such as noise under speech, a clip
while a fault is active, or a barge-in.

- **Timing.** `at` seconds, or `after` another cue's start or end, or `after` the
  runtime reports a state or log line (for example, "800 ms after Ember starts
  speaking").
- **Exact audio spacing.** Fixed-time audio cues are mixed into one track before the
  run and played as one buffer, so their spacing is exact to the sample. A relative
  audio cue is mixed in when it fires, and playback continues from the current
  position.
- **Cue kinds:** `say`, `play` (a fixture WAV), `noise`, `show`, `show_image`,
  `show_video` (a fixture clip, with `marks` such as the fall moment), `clear_display`,
  `light` (percent), `fault` (name and seconds; always restored), `power`
  (`cycle`) and `mark`.
- **Checks and measurements.** `checks` apply step expectations to a window after a
  cue. `measure` asks the reference microphone for `latency` (end of a line to
  Ember's first sound), `speech_after` (a cue or clip mark to Ember's first sound)
  or `barge_offset`.
- **Cue log.** Every cue's scheduled and actual time, and its lateness, go into the
  report's JSON. A display, light or fault cue more than 150 ms late, a display
  that does not confirm a card or clip within 3 s, or clipped audio is a **rig
  fault**.
- **Deterministic stimuli.** Noise, TV gaps and the screen-check code come from the
  trial's seed, and the trial records a SHA-256 of exactly what was played.

### Fixtures

Clips and recordings the rig cannot generate live outside Git, in `fixtures_dir`.
For example, `fall-forward-01.mp4` and `sit-down-01.mp4` are recordings of
consenting actors, or licensed footage, never residents. After adding files, run
`python evals/robot/fixtures.py lock <dir>`, which writes `manifest.json` with each
file's SHA-256 and size.

Before any device opens, every fixture a planned scenario uses must match the
manifest, and must also match a `sha256` pinned on the cue in `scenarios.json` if
there is one. Otherwise the run is a rig fault and nothing starts. The report
records the manifest's own hash.

**Requirements gating.** As in Layer 1's `requires`, a scenario is skipped when the
branch lacks its feature. The checks are:

- `tools`, `any_tools` and `prompt`, from the branch's own session config. `probe.py` runs
  in the branch's interpreter and builds the config from the core that the branch's
  `run_pc.py` names.
- `files` that must exist.
- `env`: the effective value from the rig's `.env` plus the branch's launcher defaults,
  with `env_defaults` for the code default.
- `display`.
- `device`: per-robot settings from the branch's `device_config.py`, read by the
  probe (`/etc/ember/device.toml`). Fall detection in v10 is enabled there, not in
  `.env`.
- Rig hardware: `video`, `light`, `faults` (names), `power` (an external controller)
  and `reference_mic`. Scenario `modes` (`physical`, `virtual`) excludes scenarios that
  mean nothing in virtual-device mode.

If the branch's config can't be read (for example, a v10 core the AST reader doesn't
understand), the scenarios that depend on it are skipped and the reason is reported.
The rig never guesses.

**Repeats and statistics.**

- `--repeat N` runs every scenario N times, in a seeded random order each round.
  `--seed` reproduces a run; the seed is printed and reported.
- **Pass rates** per scenario come with a Wilson 95% interval.
- **Failure classes:**
  - *product*: an expectation failed, or the runtime crashed or never came up. A
    startup failure counts here, so a PR that breaks startup fails.
  - *flaky*: the same scenario also passed in this run.
  - *rig fault*: calibration or the screen check failed, a fixture changed, a device
    was missing, a cue was late, or the reference mic heard nothing while the
    runtime reported speech.

  Rig faults are not runs of the product and are left out of pass rates.
- **Release gates.** `--release` judges by the config's `gates`. For example, safety
  scenarios need at least 20 valid runs and 95% passing (19/20); latency scenarios
  need a Wilson lower bound of at least 0.8. An ordinary run requires every valid run
  to pass.
- **Verdict:** PASS, FAIL, RIG FAULT (nothing concluded) or INCOMPLETE (too few runs
  for a gate).

**Screen check.** Before a session that uses the display, the rig shows a card with
a seeded four-digit code. It then asks Ember, through the person speaker, to read
the code. Ember answering with the code (in digits or words) proves the screen is
placed, lit and in focus for this camera. Otherwise the run is a rig fault, so a
misplaced TV is never blamed on a PR.

**Runs.** One runtime serves the suite. It is restarted before any scenario that follows
a failure or error, and when a scenario sets `fresh_runtime`. Conversation context
carries over between passing scenarios. Limits apply at every level: per step
(`timeout_s`), per scenario (`timeout_s`, default 600 s), for the suite
(`--max-minutes`), and in the workflow. The runtime is always stopped at the end:
first through the monitor's stop file (a clean shutdown), then SIGTERM, then SIGKILL.

## Physical setup

**Shopping list** (one test robot; rough 2026 US prices):

| Item | Why | Rough price |
|---|---|---|
| Second complete robot like production: Orange Pi board, the same USB microphone, speaker and camera, eyes | The device under test. Use the `devops/opi4-flash` image, and do **not** run `devops/mvp/install_autostart.py` or enable `BAYMAX_AUTO_UPDATE` on it: the rig starts the runtime itself | (production BOM) |
| Person speaker: small powered USB speaker that is its own sound card, with a different name from Ember's speaker (not another Jieli "UACDemo"; the rig refuses one that matches) | The scripted person, about 2 m away | $25–40 |
| TV or monitor, 55–65", on a stand or wall mount | Full-size people in fall and video clips at a realistic distance | $300–600 |
| or a 7–10" tablet or small monitor | Cards and the screen check only; too small for fall clips | $60–150 |
| Reference USB microphone (omni measurement mic such as a miniDSP UMIK-1 or Dayton iMM-6/UMM-6, or any decent USB mic) on a small stand | Measures Ember's real audio output: reply latency at about 10 ms | $20–110 |
| Dimmable smart bulb with a local API (TP-Link Kasa/Tapo, WiZ, or a Zigbee bulb with a hub) | `light` cues | $10–25 |
| Smart plug with a local API (optional; Kasa/Shelly) | `boot-to-ready`, from an external controller only | $10–25 |
| SPL meter, Class 2, A-weighted, slow | Calibration only | $30–60 |
| Mic stand or tripod, tape measure, powered USB hub, ethernet cable | Placement; wired network makes `network-drop` controllable | $30–60 |

Also needed: a room or closet that can be closed and kept empty during runs.

**Rough total:** about $450–900 on top of the robot (or $150–400 with a tablet
instead of the TV, without fall clips).

**Placement:**

- Put the person speaker **2 m** from the robot's microphone, at seated head height
  (about 1.1–1.2 m), pointed at the robot. Keep it at least 1 m from Ember's own speaker
  and not directly behind the robot.
- Put the tablet 40–60 cm in front of the camera so a card fills about two-thirds of the
  frame. Or put the TV 2–3 m away so a person in a clip appears life-size and whole,
  floor included. Check the framing once on devUI's camera test (`device_test.py`).
- Open `http://<robot>:8790/` on the screen in kiosk mode. On Chromium:
  `--kiosk --autoplay-policy=no-user-gesture-required`. Turn off sleep, notifications
  and auto-brightness. Clips play muted, because the sound comes from the person
  speaker on the rig clock.
- Put the reference mic 30–50 cm in front of the robot's speaker, at its height and
  not touching the robot (vibration). It must not be Ember's microphone; the rig
  refuses a `reference_mic` command naming it.
- **Limits of a screen.** A screen is flat and emits its own light. A clip on a TV
  tests the camera → pose → fall → check-in path. It does not test depth, real
  lighting or occlusion. Dimming the room while a screen is on is **not** a valid
  dark-room test, because the screen lights the scene. Use a printed card or a
  person for that. Any radar or mmWave presence sensor (none on the current robots)
  cannot see people on a screen.
- Keep the room as it is. Soft furnishing helps. Moving the speaker or changing its
  volume invalidates the calibration, and the next run detects that.

**Levels.** Level 0 dB is a person talking normally at 1 m: **60 dB(A) at the robot's
microphone** (`target_spl_dba`).

- `distance_m: 3` plays at −9.5 dB (inverse square law, about 50.5 dB(A)).
- The TV scenario plays at −3 dB (about 57 dB(A), TV at a normal listening level).
- Change `gain_db` or the speaker's hardware volume, never one of these per-scenario
  levels, to calibrate.

**Calibration** (once, and after moving anything):

1. Hold the SPL meter at the robot's microphone, pointed at the person speaker.
2. Run `python evals/robot/rig.py --root <installed or any checkout> --calibrate`. It
   starts the runtime (so it connects to Gemini and sends the pink noise, which is not
   speech) and plays 12 s of pink noise at level 0.
3. Enter the meter's reading. If it is more than 1 dB from 60, the rig prints the
   `gain_db` to set in `/etc/ember/robot-rig.json`. Repeat until it is within 1 dB.
4. The rig saves `calibration_file`: the gain, the SPL, and the robot's own mic level
   (median of the runtime's `[MIC LEVEL]` lines, in dBFS).

Every suite run starts with a 6 s calibration check. If the robot's mic hears the rig
more than 4 dB away from the saved level, the run stops with a **rig problem** (speaker
moved, volume changed or mic fault), so a PR is not blamed for it. Without a
calibration file the run proceeds and the report says "not calibrated".

## Reference microphone (outside measurement)

`reference_mic.command` records the room for each scenario. `measure.py` finds each
line the rig played in that recording and measures from the end of the line to
Ember's first sound.

- **Method.** The line is located by GCC-PHAT cross-correlation with the exact
  samples played; Ember's start is found by onset detection on 10 ms frames, refined
  to 1 ms.
- **Precision.** About 10 ms. Synthetic tests with noise and reverberation recover
  known delays within ±20 ms.
- **No clock alignment needed.** The person and Ember are in the same recording.
- **Next to the runtime's own numbers.** The report also shows the runtime's
  playback time (v10 `conversation_timing`) and the state-based estimate, labelled
  by source.
- **Recordings are not kept.** The recording is in a private 0700 temporary folder
  and is deleted right after the analysis. Reports keep only numbers.
- **Speech-to-text is a hook only.** `measure.transcribe(samples, rate, command)` is
  for a local command such as Vosk with a downloaded model. None is configured or
  bundled.

## Faults

`faults.<name>` has `start` and `stop` commands. A timeline `fault` cue runs
`start`, then always runs `stop` after `seconds`, even if the scenario errors. For
`network_drop`, use a helper that **restores itself**, so a crashed rig cannot leave
the robot offline. For example, `/usr/local/sbin/ember-rig-netdrop`, with a sudoers
line for the runner user that allows only this script:

```bash
#!/bin/sh
# ember-rig-netdrop on SECONDS | off: drop all traffic except loopback and the LAN, then
# restore it automatically after SECONDS.
set -e
case "$1" in
  on)  nft add table inet ember_rig_drop
       nft add chain inet ember_rig_drop out '{ type filter hook output priority 0; policy accept; }'
       nft add rule inet ember_rig_drop out oif lo accept
       nft add rule inet ember_rig_drop out ip daddr 192.168.0.0/16 accept
       nft add rule inet ember_rig_drop out drop
       ( sleep "${2:-60}"; nft delete table inet ember_rig_drop 2>/dev/null ) >/dev/null 2>&1 & ;;
  off) nft delete table inet ember_rig_drop 2>/dev/null || true ;;
esac
```

The Actions runner loses GitHub for the length of the drop. Keep it short (it reconnects).

## Virtual-device mode

`--virtual` (with a `virtual` section in the rig config) runs the real runtime on the
test robot without the person speaker or screen, so it can run many more repeats.
The devices are replaced as follows (`virtual.py`):

| Real device | Virtual replacement |
|---|---|
| Microphone | ALSA loopback card `EmberRigMic` (`snd-aloop`). The rig plays the person into device 0; the runtime records device 1 |
| Ember's speaker | Loopback card `EmberRigOut`. The runtime plays into device 0; the rig records device 1, an exact digital reference |
| Camera | `/dev/video42` (`v4l2loopback`, "EmberRigCam"), fed by `ffmpeg` with the card, clip or black. The display "confirms" on ffmpeg's first frame |

The runtime's device code, echo canceller, Gemini session and toolkits are the real
ones. The rig finds the loopback devices in the branch's interpreter and gives them
to the runtime (`BAYMAX_MIC_*`, `BAYMAX_SPEAKER_*`, `BAYMAX_CAMERA_DEVICE`).
Calibration is skipped, since levels are digital. Acoustic scenarios (echo, wake
words over Ember's own playback) are skipped with `modes: ["physical"]`, and light is
not available.

Setup, once, on the test robot. The rig never edits `/etc/modules-load.d`, systemd or
startup:

1. `sudo apt install v4l2loopback-dkms ffmpeg alsa-utils`.
2. Either load the modules yourself before a run:
   `sudo modprobe snd-aloop enable=1,1 index=10,11 id=EmberRigMic,EmberRigOut pcm_substreams=1,1`
   and `sudo modprobe v4l2loopback devices=1 video_nr=42 card_label=EmberRigCam exclusive_caps=1`.
   Or set `"virtual": {"load_modules": true}` and allow the runner user
   `sudo -n /usr/sbin/modprobe` for these modules only. The rig then loads them and
   unloads only what it loaded.
3. Keep PipeWire/WirePlumber off the loopback cards: a user rule that disables the
   `alsa_card.*Loopback*` devices, in `~/.config/wireplumber/`. Otherwise it may hold
   them open.
4. `python evals/robot/rig.py --root <checkout> --virtual --dry-run` prints every
   command it will run.

Config: `"virtual": {}` uses the defaults (cards 10 and 11, `/dev/video42`, 1280×720
at 15 fps, `sudo -n`). Override `mic_card`, `out_card`, `camera` or `sudo` there.
None of this is loaded unless the config has the section **and** `--virtual` is
given.

**Limits.** Timing between the rig's playback and its reference recording depends on
`aplay`/`arecord` starting, about ±30 ms, unlike the single-recording physical
measurement. The camera sees perfect frames, which is not optics. Nothing acoustic is
tested.

## What each layer can and cannot prove

| Layer | Can prove | Cannot prove |
|---|---|---|
| 0: `check_release.py` | Code paths, config, tool routing and failure handling with fakes | Anything about Gemini, devices or timing |
| 1: `evals/live_eval.py` | Gemini's behaviour on the real prompt and tools: transcripts, tool calls, reply content and latency (no audio devices) | Microphone, echo cancellation, speaker, camera, wake words, eyes, CPU |
| 2, virtual devices | The real runtime and device code end to end, many repeats; pass rates; pose and fall from clips; network and fault recovery | Acoustics (echo, distance, room noise as heard), optics, lighting, the eyes |
| 2, physical rig | Real microphone, echo cancellation, speaker, camera and CPU; calibrated levels; fall clips on a TV; outside-measured latency | Real people and voices, depth, real lighting on a person, what the eyes look like, long-term reliability, radar |
| Human checks (TESTING_PROTOCOL.md §2–3) | Real voices, appearance, comfort, judgement | (release approval is the software lead's) |

**The chamber.** The Baymax-pi4 repository planned the same idea: a closed "chamber"
where synthetic people and scenes are played to a real robot and measured from
outside. See the archived branch `natey/offline-fall-checkin`,
`features/experimental/tested_in_sim/README.md`. This rig is meant to be that
chamber.

## Runner registration (one-time, by a repository admin)

> **Pallas-Robotics/Baymax is a public repository.** GitHub advises against self-hosted runners on public repositories, because PR code runs on the machine. This workflow only runs same-repository PRs labelled by someone with write access, and inside an environment that should require the software lead's approval. Before registering the runner, also:
> - set Settings → Actions → "Require approval for all external contributors";
> - give the test robot no other credentials, family data or network access beyond Gemini and GitHub;
> - re-image it with the `devops/opi4-flash` card if a run ever looks wrong.
>
> If that's not acceptable, keep the rig and trigger it by hand on the robot (`rig.py --root <checkout>`) instead of registering a runner.


These steps add a new service for the runner. They do not change Ember's own systemd
units. The software lead approves them.

1. On the test robot:
   - `sudo mkdir -p /etc/ember && sudo touch /etc/ember/test-robot` (the marker).
   - Copy `rig.example.json` to `/etc/ember/robot-rig.json` and set
     `person_speaker.name` (see `python -m sounddevice`), or set
     `person_speaker.command` (`["pw-play", "--target", "<sink>", "{wav}"]` or
     `["aplay", "-D", "plughw:CARD=<card>", "{wav}"]`) if PipeWire hides the device
     from PortAudio.
   - Install `espeak-ng`, or set `tts_command` to a better voice such as Piper:
     `["piper", "--model", "/opt/piper/en_US-lessac-medium.onnx", "--output_file", "{wav}"]`.
     The text arrives on stdin.
   - Keep no key in the installed release's `.env`, and `touch ~/.config/ember/no-autostart`.
2. In GitHub, go to Settings → Actions → Runners → **New self-hosted runner**: Linux,
   with the test robot's architecture (**ARM64** for an Orange Pi like the production
   robots; x64 only if the test robot is an x86 board such as a LattePanda, which then
   does not represent the Orange Pi units). On the robot, as the `ember` user (it owns the PipeWire session and the
   audio devices), run:
   `./config.sh --url https://github.com/Pallas-Robotics/Baymax --token <token> --name ember-test-1 --labels ember-test-robot --unattended`
   then `sudo ./svc.sh install ember && sudo ./svc.sh start`.
3. Under Settings → Environments, create **`ember-test-robot`**:
   - Add the secret `GEMINI_TEST_KEY`. Use a key from a separate Google Cloud project
     with a budget alert and a low quota limit, not the production key.
   - Add the software lead as a **required reviewer**, so every run waits for approval
     before the key is released.
   - Allow deployments only from branches of this repository.
4. Create the **`robot-test`** label. Labelling a PR queues a run. To run again, remove
   the label and add it again.
5. Register **only one runner** with the `ember-test-robot` label per robot. The queue
   below depends on it.
6. Under Settings → Actions, keep "Require approval for fork pull request workflows".
   The job's `if:` and the guard step refuse forks anyway.

## Submitting a run and the queue

Anyone with write access can put a branch on the robot, like submitting a job to a
cluster:

- **A PR:** add the `robot-test` label. The report is posted on the PR.
- **Any branch, no PR needed:** Actions → **Robot test** → **Run workflow**. Enter the
  branch, optionally a comma-separated scenario list (`radio-stop,eye-states`), and
  optionally a PR number to post the report on. The report is always in the run summary.

What "your environment" means: the code on your branch, the scenarios you pick, and
the robot's own device settings. Not an uploaded folder or image. Only branches of
this repository can run, because the code gets a live microphone, camera and Gemini key.

**How the queue behaves:**

- The robot runs one job at a time. The others wait (shown as "Queued" in Actions),
  roughly in the order submitted, for up to 24 hours.
- **Nobody can bump anyone else.** Each PR, and each person's branch, has its own
  concurrency group. Submitting again replaces only *your own* waiting run for that
  PR or branch, never someone else's, and never a run in progress. (A single shared
  group would not work: GitHub keeps one waiting run per group and a newer submission
  cancels it.)
- With the environment's required reviewer, each job also waits for the software
  lead's approval before it starts.
- **Cancelling:** GitHub lets anyone with write access cancel any run. Cancel only your
  own; the cleanup step stops the runtime and deletes recordings either way.
- **Not provided:** a queue position, per-person limits or reservations. If the robot
  gets busy enough to need those, the next step is a small job service that the robot
  polls (a lease with a heartbeat, first in first out with per-person limits, cancel by
  submitter or admin only). That would be new infrastructure, so it needs the software
  lead's approval.

**Who can trigger a run** (enforced by the workflow and `safety.py`):

- The event must be a `labeled` event with `robot-test`, or a "Run workflow" submission.
- A submission must name a plain branch of this repository (no `refs/...`, which could
  point at a fork's PR head), and a valid scenario list and PR number.
- The PR's head repository must be this repository. Forks never run.
- The PR must be open, and its author must be OWNER, MEMBER or COLLABORATOR.
- The labeller or submitter must have write, maintain or admin permission (a read-only API lookup).
- The `/etc/ember/test-robot` marker must exist.

The rig code comes from the default branch, so a PR can't weaken its own checks. Until
this layer is merged, the PR's copy is used, with a warning. The PR's code still runs
with the test key and can see the robot's devices, which is why the key is spending
capped and runs are limited to collaborators.

## Privacy and data flows

- **Sent to Google Gemini Live (by the runtime, as in production):**
  - microphone audio of the room, which should contain only the rig's synthetic speech,
    synthetic noise and Ember's own voice;
  - camera frames, which should show only the tablet's synthetic cards;
  - the MVP prompt and tools.

  The rig itself sends nothing to Gemini. **The room must be empty of people and
  private material.** The rig prints this before every run and asks for confirmation
  unless `--yes` is given (the workflow passes it; the operator keeps the room
  dedicated).
- **Real toolkit calls happen.** The runtime is real, so weather, radio and LibriVox
  providers are contacted as in production.
- **Recordings.** The v8/v9/v10 runtimes always write raw microphone audio to
  `day_utterance/seg_*.wav`. There is no switch to turn this off. `BAYMAX_AUDIO_RETENTION_DAYS<=0`
  *disables pruning* and keeps everything, so do not set it to 0.
  - The rig sets `BAYMAX_AUDIO_RETENTION_DAYS=0.01` and `BAYMAX_VOICE_ANALYSIS=0`.
  - It gives the runtime a private, empty data folder (`BAYMAX_DATA_DIR`, a 0700
    `ember-rig-data-*` temporary folder). v10 writes its audio segments, session audio,
    transcripts and **memory store** (`chroma_store`) there, so the run neither reads
    nor adds to the robot's memories, and the folder is deleted at the end (the
    harness trace, if any, is in it too).
  - After every run it also deletes `day_utterance/seg_*.wav`, `session_*.wav`,
    `od_session_*.wav` and `transcript_*.txt` written in the checkout since the run
    started (a runtime that ignores `BAYMAX_DATA_DIR`), and reports the count. Any
    file left behind turns the report into a rig problem.
  - The workflow's always-run cleanup step repeats this, including leftover data
    folders, if the job is cancelled.
- **The key.**
  - `GEMINI_TEST_KEY` goes into a 0600 file in a private temporary folder. The branch's
    `.env` only names that file (`GOOGLE_API_KEY_FILE`).
  - Both are removed at the end, and an existing `.env` is restored.
  - The runtime process gets no GitHub or Actions tokens.
  - The report is posted by a separate step, so the PR's code never runs alongside
    `GITHUB_TOKEN`.
- **Reports.**
  - `robot-test.md` and `.json` contain the scripted lines, what the runtime transcribed
    of them, tools, states, latencies and CPU.
  - Log excerpts in the report leave out transcript lines.
  - The full runtime log (`runtime.log`) stays on the robot, in the job's temporary
    folder (deleted with the job) or `.local-review/robot/` (gitignored), and is never
    posted.
  - No audio or images are kept. Reference-mic recordings are deleted after their
    analysis; the report keeps only latencies and counts.

## Inputs, outputs, dependencies

- **Inputs:**
  - the branch checkout (`--root`);
  - `/etc/ember/robot-rig.json` (see `rig.example.json`), the calibration file and
    `scenarios.json`;
  - `GEMINI_TEST_KEY`;
  - for posting, `GITHUB_TOKEN` and `--pr`.
- **Outputs:** `<report-dir>/robot-test.md` and `.json`, and `runtime.log`. With
  `--post-comment` (rig) or `report.py --post` (workflow), one PR comment. Exit codes:
  0 pass, 1 fail or not confirmed, 2 refused.
- **Dependencies:**
  - the branch's own `.venv` (numpy, opencv, sounddevice, python-dotenv, google-genai
    for the probe);
  - `espeak-ng` or a `tts_command`;
  - `ps`, and psutil (optional: `/proc` is used otherwise).
  - `safety.py` and `report.py` use only the standard library, so the workflow runs
    them with the system `python3`.
- **Enabling and disabling:**
  - Nothing in the runtime imports the rig.
  - To enable, register the runner and create the label and environment.
  - To disable, take the runner offline, remove the label, or delete
    `/etc/ember/test-robot`; the rig then refuses.

## Tests

`tests/test_robot_rig_av.py` covers the audio/video upgrade:

- the timeline scheduler: exact track mixing, relative and state-anchored cues,
  barge-in restarts, faults always restored, display confirmations, seeds;
- fixture checksums;
- latency estimation on synthetic signals: known delays recovered within ±20 ms with
  noise and reverberation, at 48 and 16 kHz;
- Wilson intervals, gates, verdicts and failure classes;
- virtual-device command construction and module handling with fakes;
- the HTTP display's video handshake on localhost;
- the rig end to end with a fake reference microphone, the screen check, seeded
  repeats, capability gating, and `rig.main` with fixtures, release gates and
  virtual mode.

No device is opened and nothing is played.

`tests/test_robot_rig.py` (core section of `check_release.py`; runtime suite in
`tests/features/runtime/suite.json`, so `python tests/run.py --feature runtime` runs it)
uses fakes from `fakes.py`:

- a clock that advances on sleep;
- a speaker that records what it plays;
- a display;
- a CPU sampler;
- a scripted `FakeRobot` that writes `.runtime-status.json` (with v10's
  `conversation_timing` rows, optionally a harness trace) and a log in the runtime's
  formats.

Covered:

- **Scenario file:** validation, and coverage of the hardware checks.
- **Gating:** including a real probe of this branch, where `barge-in` is skipped
  because the MVP turns interruptions off.
- **Ideal robot:** every scenario in `scenarios.json` passes against one.
- **Failure cases:**
  - a false wake during TV, and audio reaching Gemini while disengaged;
  - echo, slow first audio, and a reminder that never re-engages;
  - a runtime crash (error, then a fresh runtime for the next scenario);
  - a runtime that never connects, and the CPU or memory ceiling;
  - a sticky disengage after restart, and the suite time limit.
- **Calibration:** pass, drift detection, and saving only when on target.
- **Timing sources:** latency from the runtime's playback time, the trace's reply
  audio, or the speaking state. #10's documented schema-v1 example is read exactly
  (wall times, tool outcomes, `audio_received`). Partial and malformed lines are
  handled, and another schema version falls back.
- **Observer:** event deduplication, ignoring the previous session, partial log lines,
  engagement reset on restart.
- **Safety:**
  - the marker, the trigger guard (forks, labels, permissions) and the key and `.env`
    lifecycle;
  - deletion of only this run's recordings;
  - token stripping from the runtime environment.
- **Report rendering and posting:** only with the flag.
- **`rig.main` end to end** with fake hardware: refusal without the marker or
  confirmation, dry run, missing key, cleanup.
- **Real hardware classes with stand-ins:**
  - the speaker chosen by name, which refuses Ember's own;
  - the HTTP display on localhost;
  - finding other runtimes;
  - stopping a dummy runtime through the stop file, and with SIGTERM;
  - the CPU sampler.

No microphone, camera or speaker is opened, and nothing contacts Gemini or GitHub.

## What still needs a human

- **Fixtures.** Record or license the fall and sit-down clips, then lock them.
- **First hardware run of the AV parts.** Check the kiosk page's timestamps against
  the reference mic. Check that a fall clip on a TV triggers MediaPipe at that
  distance. Tune `network-drop` and the latency limits, and check the
  snd-aloop/v4l2loopback setup with PipeWire on the Orange Pi image.

- **The trace on hardware.** Check that the runtime's real trace (once #10 merges)
  matches the schema-v1 example the reader is tested against.

- **Setup and approval:**
  - buying and placing the hardware, calibrating, registering the runner;
  - approving each run (environment reviewer);
  - tuning the CPU and memory ceilings from the first baseline run (the current numbers
    are placeholders).
- **Things the rig cannot see:**
  - **What the eyes look like.** The telemetry records eye *expressions* (on some
    branches) but not the displayed mode, so `eye-states` checks startup, sensors and
    the driving states only. Look at the eyes, or add a second camera later.
  - **How Ember sounds** (volume, distortion) to a listener, and fall detection.
- **Real voices.** Synthetic speech differs from people. Layer 1 found that Gemini
  transcribed synthetic "Ember, disengage." as ".". A robotic espeak voice through a
  speaker may fail the same way, so use a natural `tts_command` voice. Check any failure
  of a transcript-matched command with a real voice before blaming the PR.
- **Judging failures.** A run is evidence for the reviewer. Gemini is
  non-deterministic, so one pass or fail needs judgement. Re-run if in doubt.
- **First live use.** None of the hardware path has run on a robot yet. The workflow,
  `prepare_checkout.sh`, sounddevice and command playback, the HTTP display on a tablet,
  `[MIC LEVEL]` calibration, the regexes for real `[WAKE]`, `[ENGAGE]`, radio and
  audiobook output, and stopping a real `run_mvp.py` all need a supervised first run.
