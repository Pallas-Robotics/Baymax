# MVP (unreleased)

- Language follow-up: remove the sticky ongoing-language preference after owner
  testing found Spanish input still received Korean replies. Select the language
  again each turn, including the reply to a switch request itself. Add direct
  Korean-to-Spanish synthetic text and speech regressions after a Korean request.

- Voice test follow-up: enforce next-reply language selection with explicit
  language overrides; require the voice-volume tool before confirming a change.
  Recognize an "Ember engage" pair split across final recognition results, and
  re-engage with an unavailable notice if the wake worker fails or takes over
  five seconds to load. Add native-recognizer and synthetic Gemini test cases.
  Bench installations must pin their PR source in the updater to prevent a
  reboot from silently returning to a build without volume support.
  Adapt Joe Kojima's d2cca1f 128-token compatibility setting when Google Search
  and function tools are enabled with zero thinking; a live volume call
  reproduced error 1011 at zero, and repeated calls passed at 128. Search stays
  available; existing nonzero budgets and search-disabled settings are retained.

- OPI4 candidate: adapt Joe Kojima's network retry/resumption changes
  (30c7d7b, 7f21fab, c3deba5) to the existing audio lifecycle, with bounded
  0.3–4.8 second transport retries and nonblocking device-reset delays. Port
  his language wording (51f15f1, 2d50ced) and weather/profile work (ee940fa).
  Profile changes stay pending after speech begins during lookup, send failure,
  cancellation or concurrent edits. No model/device configuration or release
  channel is changed. See docs/features/JOE_OPI4_PORT.md for attribution,
  external requests and validation boundaries.

- Joe Kojima: keep weather/location caches warm and defer family profile notes until Ember is quiet (source ee940fa9f7f29680fe012ab363122df1197b3a29). Background refresh requests and tests are documented in the toolkit README.

- Integrate the tested language-adaptation instruction into the shared Ember
  persona: follow the person's language, switches and mixed-language speech,
  while keeping internal English messages in the conversation's language.
  Preserve the rest of the persona/model settings and the measured limitations.

- Record real Orange Pi language simulations: 80/81 automated response runs
  passed across 240 turns; retain the Polish switching failure and synthetic
  speech/ASR failures explicitly. Add automatic pass counts and reject/recheck
  punctuation-only transcripts. Report the test-only prompt patch and measured
  CPU/RAM/temperature/NPU evidence; add A733 viplite load sampling support.

- Add multilingual simulation scenarios and a sourced 99-entry language candidate
  catalog. Reuse the real-config Gemini evaluator for native turns, follow-ups,
  language switching and Hindi-English mixing; optionally save synthetic reply
  WAVs for review. Compile per-model/config statuses and only list audio languages
  validated by repeated live runs, checks and exact-evidence human review. Add
  offline regression coverage; API keys/raw audio stay local and no universal
  language-support claim is made.

- Add a shared, read-only Linux feature performance sampler for CPU, runtime/
  descendant RSS, system RAM, exposed NPU/thermal sensors and existing speech
  latency. Compare baseline/active/combined workloads, summarize mean/p95/peak
  and gate explicit budgets/added p95 latency. Include sampler regression checks
  in feature selections and the existing release gate; require performance
  evidence for future feature PRs. Missing telemetry is reported, never zero.

- Document OPi4 V1 with an organized feature/source/validation log and an optional
  Graphify map/viewer. Keep board-specific evidence and pending live checks explicit.
  Add `devops/EDITING_PROTOCOL.md`, a concise repository README and feature-owned
  suite definitions under root `tests/`. `tests/run.py` supports full, feature and
  changed-path validation; shared or unknown changes select all suites. Existing
  CI/robot checks also include overall syntax, ownership and chat UI checks.

- Add local speech detection and per-turn latency to the chat debugger. User
  bubbles show speech/transcript timing and live reply waits; Ember bubbles show
  reply arrival, scheduled speaker playback and response delays. Keep toolkit
  start/end in one updating centered bubble and collapse the timing history.
  Track output underruns, preserve interruption/reconnect cleanup, and support
  a device override for speech-end sensitivity. Diagnostics use existing audio
  streams, keep bounded local metadata, and omit ambiguous speech matches.

- **Ember for Orange Pi: one repository for the Orange Pi 4 Pro and the Orange Pi 5
  Max.** Made from JoeKojima/Baymax PR #29 (`opi4-demo`), PR #22 (the SD-image builder),
  the Orange Pi 5 Max branches `orangepi5-npu` + `memory-incremental-saves`,
  `orangepi-features` (opi4-demo with Svanik's PRs #23-#28 and #30 and the voice
  volume control: the entries below from "Let the person change how loud" to
  "Weather") and one commit of `reconnect-any-disconnect` (6afe802: the entries
  from "Reconnect after any disconnect" to its "Tested offline only"; not its
  LattePanda parent b5d41de). It deliberately diverges from Baymax's
  `startup-greeting` (LattePanda) branch; shared fixes are ported by hand. One
  dependency file, `requirements-orange-pi.txt`, for both boards (memory included;
  rknn-toolkit-lite2 installs on the 4 Pro but is never imported there). Offline
  only: `check_release.py` passes on macOS (core 541 with 4 skipped, toolkit 241)
  and OPI4-Flash's tests pass (39, 9 Linux-only skipped). Nothing has run on either
  board in this combination yet.
  - Pose stays in its own process (opi4-demo's fix for minute-late replies and mic
    overflows on the 4 Pro) and the Orange Pi 5's backend now runs inside that child
    (`features.pose.backend.ChildPose`): with `camera.pose_model` lite (default) or
    full, a set-up RK3588 runs the checked full model on the NPU, otherwise MediaPipe
    runs the chosen .task on the CPU; heavy always uses the CPU. A pose process that
    cannot start (e.g. librknnrt crashing it during NPU setup) or dies later is started
    again once, on the CPU; video continues. The vision process reports
    the model's device with every frame, so the fall toolkit shows NPU/CPU correctly.
    A pose process that hangs without exiting is not detected yet.
  - Fast-core pinning: `BAYMAX_CPU_PINNING=auto` (default) pins only on an RK3588,
    where it was measured; `on` pins on any ARM big.LITTLE board (to try the 4 Pro's
    two A76s, which would also carry the pose/face processes); `off` never pins.
  - Kept from the 5 Max work: the live view drawn only while watched (replaces
    opi4-demo's viewer counter, with its tests), the 1-frame V4L2 buffer on ARM, the
    fall toolkit's shared pose, the per-platform MVP updater, memory saved during
    uptime. Kept from opi4-demo: lite model at 10 fps with 6 frames of fall history,
    conversation/eyes/engage work, the 4 Pro Wi-Fi hotspot. The pose tools
    (`validate.py`, `benchmark.py`) now use 10 fps and history 6; VALIDATION.md's
    numbers are marked as measured before the merge (in-process, full model, 15 fps).
  - OPI4-Flash follows a branch of this repository (default `release`;
    `https://github.com/Pallas-Robotics/Baymax.git`, the Orange Pi fork) instead of
    Baymax PR #20, installs packages only from `requirements-orange-pi.txt` (its
    separate v10 list is gone; the import check adds chromadb, onnxruntime,
    tokenizers), and keeps memory, reminders and logs in `~/ember-deployment/data`
    and downloaded models in `~/ember-deployment/shared` across updates on cards
    built by this builder (a card from an older image keeps its own updater, which
    does neither: reflash it, or move the data by hand as the OPI4-Flash README
    says). The Debian prerequisites install git, which the updater needs on a fresh
    5 Max.
  - With the features and the reconnect fix: both fall alerts pause playback and
    retry a failed send, and a retry keeps the note that playback was paused (it was
    dropped: the first try had already paused it). The voice volume and the daily
    check-ins keep their state (`voice_volume.json`, `toolkit_checkins.json`) in
    `BAYMAX_DATA_DIR`, which SD-card updates keep. The offline checks also run on
    pushes to `release`, the branch both boards' updaters follow. With the Orange Pi
    pins (NumPy 1.26.4, OpenCV 4.10, MediaPipe 0.10.18, ChromaDB 1.5.8) first on the
    path, `check_release.py` also passes on macOS (core 541 with 3 skipped, toolkit
    241); google-genai, miniaudio and other packages that environment lacked came
    from the desktop one, so this only approximates CI's Linux ARM64 job.
  - Fixed after reviewing the combination:
    - A message of Ember's own that a disconnect cut off (a fall check-in, a family
      message) was lost for good if its resend on the new connection failed too,
      although the code said the next connection would send it (6afe802 had this;
      here a fall check-in's paused radio also stayed paused, unexplained). It now
      stays first in line for the connection after.
    - `check_release.py` now ignores the machine's own settings while the checks
      run (`BAYMAX_*`, `EMBER_*` and API keys leave the environment; its `.env`
      reads as empty). A 5 Max with `BAYMAX_AUTO_UPDATE=1` and a setting
      `.env.example` documents (e.g. `BAYMAX_VOICE_VOLUME_TOOL=0`, a check-in
      window, `EMBER_DISCOVERY_ENABLED=0`) failed tests written for the defaults once
      `baymax_cloud` loaded that `.env` on import, so its updater rolled back every
      release (with such a `.env` the old gate failed 13 tests here; it now passes).
    - OPI4-Flash images were always set to `America/Los_Angeles`, unmentioned, so on
      a card in another time zone the daily check-ins, reminders and greeting ran on
      Pacific time (the morning question at 11:00-14:00 in Philadelphia). The image
      settings now take an optional `timezone` (default unchanged; checked against
      the image's zone list, written to `dietpi.txt`, `/etc/timezone` and
      `/etc/localtime`), and the docs say how to check and change it on a running
      board.
    - Audiobook chapters and Internet Archive recordings were cached in `/tmp` (RAM
      on both boards) and never deleted, so days of listening pinned hundreds of MB
      next to Ember until a reboot. The cache now keeps only the chapter or track
      being played (a long single-file recording is still held whole while it
      plays).
  - Pending on the boards: CPU summed over Ember and its pose/face processes (NPU vs
    `BAYMAX_POSE_BACKEND=cpu`, with and without the fall toolkit), pose round-trip
    time, mic overflows and reply latency over 10 minutes, killing the pose process,
    the live view, frame age with the 1-frame buffer, `validate.py --fps 10` fall
    agreement on the 5 Max; `cpu_capacity` and pinning, memory download and survival
    across `ember-update`, image build size/time and an emulated `check_release` on the
    4 Pro; on both, radio and recordings on the board's speaker and their pause for a
    fall check-in or due reminder, the voice volume, "look closely" with the board's
    camera, daily check-ins, and reconnecting after a Wi-Fi drop; on a 4 Pro card
    built with another `timezone`, that the zone survives DietPi's first boot.

- Reconnect after any disconnect. The send and receive loops only reconnected when
  the error text contained 1000, 1006, 1011, CANCELLED or closed. Gemini closing
  with any other code (for example 1001, 1007, 1008 or 1013) left Ember retrying a
  dead connection, deaf and silent, until the service restarted. A failed mic send
  now ends the session at once; the receive loop ends it on any closed connection,
  decided by exception type (`connection_lost`), or after 3 errors in a row with no
  message between them. A one-off error handling a single message still keeps the
  session. The partial transcript is still posted when the send side notices first.
- Recover from a network that goes silent, with no close and no reset (a Wi-Fi
  drop). A mic send then waited, with the websocket's keepalive stuck behind it,
  until the kernel gave up, so Ember stayed deaf about 15 minutes (the Linux default;
  not measured). A send stuck for 5 s now counts as failed
  (`MIC_SEND_TIMEOUT_S`), and the socket is aborted when a session ends. In a
  simulated freeze Ember was reconnected about 15 s later, plus the 5 s audio reset
  a robot adds.
- Clear leftover reply audio on reconnect (`_abandon_reply`). Queued audio survived
  the reconnect and played about 7 s later with the mic muted, so Ember finished an
  old sentence out of nowhere and couldn't hear anyone meanwhile. A reply in progress
  when the connection goes (Gemini resets it about every 10 minutes) now stops there;
  the conversation still resumes. Ember's own messages (a fall check-in, a family
  message, a reminder) that reached Gemini are sent again on the next connection if
  their reply was still playing, or hadn't come yet within 30 s of the message
  (`AddressGate.abandon`), unless the person has spoken since. One already heard isn't
  repeated. A message that never reached Gemini is left to its sender's own retry, so
  nothing is sent twice; the startup greeting now counts as done once it's sent, and
  both fall alerts (`fall_alert_monitor`, the fall toolkit) now retry a failed send
  themselves. On this Orange Pi line a retry still tells Gemini that the radio,
  recording or book was paused for it: the first try paused playback, so the retry's
  own `hold_for_answer()` had nothing to say and the note was dropped. Limit: a message with no reply that is noticed lost after more than 30 s
  isn't resent (for example a silent Wi-Fi drop during an audiobook, when the mic sends
  only silence and the keepalive notices it up to about 45 s later). An audiobook isn't
  cleared: it goes quiet during the reconnect (at least 7 s) and resumes where it
  stopped.
- A stop after a reconnect now reaches the shutdown summary. On Python 3.11/3.12 a
  failed TaskGroup leaves `run()`'s cancel count up, so a later Ctrl+C or Stop ended
  `asyncio.run` with CancelledError instead of KeyboardInterrupt. That skipped the
  summary, so the session's memories were lost, and exited with an error.
- Tested offline only: the real send and receive loops on the real SDK session over
  a local websocket, closed with each close code or left unread, and the real `run()`
  with a cut-off fall check-in. Not yet tested against a real Gemini close or on a robot.

- Let the person change how loud Ember speaks (`features/conversational_ability/volume.py`).
  "Ember, speak louder", "softer" or "set your volume to 8" call a new
  `set_voice_volume` tool, and the confirmation already plays at the new level. The
  scale is 1-10; 7 is the original loudness and leaves the audio unchanged, so nothing
  changes until someone asks. Above 7 a soft limiter avoids harsh clipping (up to
  about +6 dB); the lowest level is 1, never mute, so a fall check-in can't be silenced.
  Only Ember's voice is scaled, before audiobooks are mixed in and before the echo
  canceller's reference; audiobooks keep their own volume. The level is saved in
  `voice_volume.json` and survives restarts; `BAYMAX_VOICE_VOLUME` sets the starting
  level and `BAYMAX_VOICE_VOLUME_TOOL=0` stops offering the tool. Offline tests cover
  levels, saving, gain, limiting, ramping and the speaker callback order; not yet tried
  on a robot's speaker or with real Gemini tool calls.

- Offline checks run on every pull request: `.github/workflows/offline-checks.yml`
  (Natey's `docs/ci/desktop-offline.yml.example`, enabled and extended to
  Linux) runs `check_release.py` on Ubuntu, Windows and macOS, adds the result
  to the run summary and uploads the report. No secrets, devices or providers.
  On this Orange Pi line a fourth job installs the robots'
  `requirements-orange-pi.txt` (NumPy 1.26, OpenCV 4.10, MediaPipe 0.10.18) on
  GitHub's Linux ARM64 runner (`ubuntu-24.04-arm`); the others use the desktop
  pins, so code needing NumPy 2 or OpenCV 4.13 passed CI and broke on the OPI4.
  `requirements-pc.txt` skips Vosk on macOS, which has no wheel, so the Mac
  install no longer fails. A Mac then has no offline wake words: no pause words
  during audiobooks, radio or recordings, and on this line "Ember disengage" is
  refused with a spoken note. A green run is offline evidence only.

- Gemini-in-the-loop evaluation without a robot: `evals/live_eval.py` builds
  the MVP's real Live config from the runtime `run_pc.py` launches
  (`realtime_gemini_10.py` here, with its `device.toml` prompt sections and
  proactive audio; v8 for older checkouts) and the launchers,
  plays scripted text-to-speech turns, picture cards and text notes into fresh
  sessions, stubs tool results and checks transcripts, tool calls, replies and
  first-audio latency over repeated runs. Feature scenarios (brain games, look
  closely, radio, disengage) skip on branches without that feature; `--root`
  tests another PR's checkout. Dry-run and confirmation before anything is sent.
  Offline tests: `tests/test_live_eval.py`. Live run pending (needs a key).

- Radio and recordings: a `listen` tool plays live internet radio stations
  (Radio Browser; MP3, Ogg and FLAC streams decoded incrementally by miniaudio,
  with AAC and HLS skipped) and openly licensed Internet Archive recordings
  (public domain, CC0, CC BY, CC BY-SA only). Both go through the audiobook
  player, which now plays one program at a time (book, station or recording):
  mixed into Ember's speaker, held while Ember speaks, mic withheld with the
  "Hey Ember"/"stop" wake words. Streams reconnect on drops, and resume
  reconnects to live radio. Stream URLs must resolve to public addresses.
  Playback pauses for a check-in after 30 min (`BAYMAX_LISTEN_CHECK_MIN`), and
  `BAYMAX_LISTEN_ENABLED=0` disables it. Audiobook download failures now tell
  Ember the book stopped instead of "finished". Pause, check-in and station
  notes now follow Gemini's ~10-minute reconnects to the newest session (they
  went to the closed one, so the 30-minute question was never asked; book
  chapter notes had the same gap). A possible-fall check-in (built-in v10 or
  toolkit) or a due reminder now pauses whatever plays first, since playback
  withholds the mic: the answer used to be dropped, and reminders waited up to
  30 min (with `BAYMAX_LISTEN_CHECK_MIN=0`, until the radio was stopped).
  Live robot check pending.

- Brain games, played only when someone asks: word association, twenty
  questions either way, categories, remembering a list, story recall, riddles
  and word puzzles. A local `brain_game` tool supplies fresh rounds from word
  lists (no repeats until a list is used up) and keeps the score; nothing leaves
  the robot. The prompt keeps it fun and encouraging and never treats it as a
  memory test. On with Discovery (`EMBER_DISCOVERY_ENABLED=0` removes it). Live
  check pending.

- "Look closely": the new `look_closely` tool sends Gemini one sharper still
  (full 640x480 capture, JPEG quality 90, instead of the 320x240 quality-50
  frames every 3 s) when someone asks Ember to read or identify something. The
  still comes from the existing camera frames on the existing video input. A
  local blur/darkness check lets Ember ask them to hold it closer or steadier,
  or to turn on a light. It never gives dosing advice from medicine labels.
  `BAYMAX_LOOK_CLOSELY=0` disables it. Live robot check pending.

- Gentle daily check-ins (`features/toolkits/checkins.py`): Ember asks how
  someone slept in the morning, about water in the afternoon and about their
  day in the evening.
  - At most once per window per day, with no repeat if unanswered.
  - Never within 20 minutes of starting, never as the startup hello, and only
    when it is quiet and the person spoke to Ember in the last 3 hours. Ember's
    own speech doesn't count: after an unattended reboot the startup hello made
    the morning check-in ask an empty room how they slept.
  - Not while Ember is busy, a book plays or Ember is disengaged ("Ember
    disengage"), since the person could not answer.
  - Spoken only; answers are not saved. "Done today" survives restarts in a
    gitignored `toolkit_checkins.json`.
  - `BAYMAX_CHECKINS=0` disables it; windows are configurable.
  - Live robot check pending.

- Weather: asked "what's the weather like?", Gemini often called only
  `get_location`, then asked "would you like me to check the weather?" or
  made the weather up (live eval: 5/10 on MVP, one invented "12 °C, clear").
  `get_weather` already finds the location itself, so the toolkit prompt and
  tool descriptions now say to call it directly, never to state conditions
  that didn't come from it, and to answer with the approximate place instead
  of asking first. `get_location`'s result carries a note that it has no
  weather. Live eval after: 20/20 (10 with each stub), always `get_weather`
  only, median first audio 2.3 s. Base prompt, models and audio unchanged;
  robot check pending.

- Split device demos: this branch is **OPI4 Demo** (`opi4-demo`). Both demos
  retain identical shared v10 runtime/features; only device setup and the
  Orange Pi Wi-Fi provisioning differ. Add missing ARM audio cleanup,
  eyes and audiobook dependencies, with profile regression coverage.
  See `docs/DEMO_DEVICES.md`; physical validation remains pending.

- Run MediaPipe pose and face inference in a child process
  (`features/vision_process.py`). In Ember's main process it held Python's GIL,
  so the mic callback and Gemini connection waited behind each frame: on the
  Orange Pi 4 Pro replies came up to a minute late and the mic overflowed about
  every 7 s with pose tracking on (no delay with the camera off). Fall detection,
  eye contact and video are unchanged; only frames and landmark tuples cross the
  local pipes. A child-process test covers round trips, errors and load failures;
  the Orange Pi latency and overflow re-check is pending.

- Add an Orange Pi 4 Pro Wi-Fi setup hotspot (`deploy/host/ember_wifi_setup.py`
  and `ember-wifi-setup.service`). After 90 seconds without any network, the
  board's single radio becomes a password-protected `Ember-Setup-XXXX` hotspot
  with a phone page for picking a network; the board saves it alongside existing
  networks and reconnects, reopening the hotspot if that fails. Needs `hostapd`,
  `dnsmasq-base` and `iw` and a per-robot password in `/etc/ember/wifi-setup.json`.
  The x86 robot's NetworkManager portal is unchanged. Offline tests cover saving,
  scanning and retry; the hardware test remains pending.

- Cut v10 camera CPU use. Pose tracking now defaults to MediaPipe's lite model
  (`camera.pose_model` in device.toml: lite, full or heavy) at 10 fps instead
  of full at 15, with velocity history kept at about 0.5 s. Stream overlays are
  drawn only while someone watches `/stream`, and eye-contact face checks drop to
  1 fps when no face is in view. Fall detection with the lite model and on-board
  CPU/thermal profiling on the Orange Pi remain pending.

- Add OPI4-Flash: a Linux-hosted Orange Pi 4 Pro SD-image builder and first-boot
  instructions with configurable PR/Wi-Fi/Gemini key, generated per-image access
  credentials, checked assets, headless desktop/AnyDesk/status services and staged
  startup updates. Includes offline configuration, updater, hardware and checksum
  tests. A fresh-card boot of the reusable package remains pending.

- OPI4-Flash's board updater reuses the installed Python environment when a new
  commit leaves `Baymax-main/requirements*` and the bootstrap unchanged, so a
  code-only update takes seconds instead of reinstalling (and on ARM compiling)
  every package. Changed dependencies still get a fresh environment. Release
  folders must be kept, since a shared environment lives in an older one.

- OPI4-Flash accepts an optional private `access_password` setting that becomes
  both the Linux and AnyDesk password on every card built with it; without it,
  each card still gets separate random passwords.

- OPI4-Flash downloads prebuilt ARM64 wheels for praat-parselmouth 0.4.7 and
  miniaudio 1.71 from the `opi4-wheels-1` release (checked against `assets.json`),
  so a new PC no longer compiles them under emulation. Changing wheels no longer
  rebuilds the package base. The builder's own tests now run from its source
  folder. A fresh-PC build using the release wheels is pending.

- OPI4-Flash now preinstalls the Debian packages, AnyDesk and the selected
  release's Python environment while building (ARM64 chroot via
  qemu-user-static), so first boot no longer downloads them. The package base
  image and built wheels are cached between cards. Fix a builder crash
  (`timer_image_directory`) from the permissions fix. A full emulated build and
  a boot of the preinstalled image remain pending.

- Make the MVP turn-end silence default and invalid-value fallback 400 ms,
  matching the demo timing preference. Saved valid timing overrides still win.
  Document and test that built-in v10 pose tracking and fall detection default
  on; explicit per-device off settings remain supported. This does not turn on
  the separate optional CPU fall toolkit or configure caregiver notifications.

- Fix empty-playback monitor heartbeats continually resetting eye inactivity.
  Count playback completion once per finished reply, allowing sleep after 60
  quiet seconds while preserving wake-on-conversation behavior. Live verification
  on the matrices remains pending.

- Broaden happy, success, excited and confused eye cues. Let automatic expressions
  follow successive streamed sentences instead of locking the first question
  mark for a whole reply. Avoid repeated identical updates; preserve explicit
  requests, medical concern and controller safety priorities. Updates follow
  transcript timing; real-device speech alignment remains pending.

- Recognize change/set/switch/turn commands targeting eyes, face or expression,
  including the transcribed "too" in place of "to". Preserve normal expression
  playback/reset behavior. Physical LED verification remains pending.

- Decide whether speech warrants a reply before composing one. Wait silently
  through unfinished phrases, hesitation and listening acknowledgments; preserve
  complete short answers, corrections, requests and urgent safety responses.
  Clarify completed unclear thoughts rather than interrupting thoughts in progress.
  Uses existing proactive-audio behavior; live restraint remains unverified.

- Separate consecutive queued v10 replies with a 180 ms pause so their sentences
  do not run together. Preserve continuous chunks within a reply, immediate
  playback after an empty queue, and interruption flushing. Live audio transition
  verification remains pending.

- Make startup scene comments optional and grounded in a concrete visible detail.
  Otherwise just greet the person; do not invent a peaceful atmosphere or ask
  them to explain an unclear view. Separate calm delivery from scene and mood.
  Live greeting variation remains pending.

- Allow occasional topic-driven discovery through the existing Google Search
  session tool: share one relevant sourced finding with a personal reaction or
  invitation to discuss, without waiting for an explicit search request. Keep
  private disclosures out of queries, respect storytelling and venting, and never
  invent sources or previous familiarity. Live quality and latency remain pending.

- Strengthen conversation guidance against praise-and-question loops: contribute
  a grounded thought, avoid invented categories and redundant confirmation,
  recognize sustained questioning, and reserve closing wishes for real endings.
  Brief confirmations should not trigger a farewell. Live evaluation is pending.

- Refine spoken conversation guidance around intent, meaningful follow-ups,
  context-aware turn balance, purposeful questions, emotional responsiveness,
  boundaries, clarification and correction. Keep personality and direct answers;
  avoid scripted scenarios, question quotas and forcing every exchange into a
  task. Live conversational quality remains pending evaluation.

- Remove scenario examples and sample replies from conversation guidance;
  retain the general rules for engagement and sharing the floor.

- Follow newly shared uncertainty or difficulty rather than treating an answer
  as a farewell. Replace generic reassurance and closing wishes with a specific
  question or concrete observation, while still respecting pauses and endings.
  Live conversational evaluation remains pending.

- Balance Ember's conversation around sharing the floor: usually one short
  sentence, one relevant question or observation when there is room to elaborate,
  and brief personality without generic praise or changing the subject to itself.
  Clarify unclear details instead of inventing a topic; avoid routine wellness
  reminders triggered only by late hours. Offline configuration checks do not
  establish live conversational quality; spoken evaluation remains pending.

- Give Ember an explicit point of view instead of relying on a weak “have
  opinions” instruction. It stays receptive but does not automatically agree or
  mirror; it can make a clear claim, give a brief reason, disagree gently and
  change its mind when persuaded. Its grounding values are dignity and
  independence in care, patient attention, dependable understandable technology
  and the continuing relevance of classical literature. These are conversational
  starting points rather than slogans. Medical limits, uncertainty and the ban
  on invented expertise or lived experience remain unchanged. Live personality
  consistency remains pending.

- Replace timer-driven chatter with event-driven silence by default: the old
  silence/face-presence random initiation requires
  `BAYMAX_RANDOM_INITIATION=1`. Gemini's existing proactive audio, conversation
  context and visual cues still judge when speech is directed at Ember; facing
  or looking toward Ember while speaking is supporting evidence, not proof.
  Default nap eyes now appear after 60 quiet seconds instead of 10. A sustained
  scene-change trigger is not added yet; live mixed-room and gaze behavior remain
  pending.

- Give Ember grounded personal interests: it is studying nursing curriculum in
  hopes of becoming a personal caregiver, while remaining explicitly a learner
  rather than a medical professional; it likes sleeping to recharge, classical
  literature and hunting for software bugs online. Clear pain or medical-
  emergency language now forces surprised/exclamation eyes for the next reply,
  above explicit requests and sad reply cues. This is a local visual reaction;
  medical wording, diagnosis limits and alert behavior are unchanged. Live
  personality naturalness and matrix timing remain pending.

- Prevent Ember from interrupting its own replies: MVP no longer opts into
  speech-triggered interruption by default, so the existing reply-time mic mute
  and 250 ms echo tail protect playback. `BAYMAX_INTERRUPTION_ENABLED=1` remains
  an explicit barge-in option after live echo-cancellation validation. Restore
  the startup statement even when ambient noise is transcribed during its initial
  camera wait, and retry it after a failed session send.

- Keep eye expressions local by default. A live blocking `set_eye_expression`
  call occurred after reply generation and made Gemini repeat the same sentence;
  two startup attempts also ended in connection recovery immediately after their
  prompts. Restore the phrase-based one-expression-per-reply path so ordinary
  replies add no model tool round trip. Treat yes/no as well
  as wh-questions near the reply opening as curious, while concern such as “Oh no”
  retains sad eyes. Tell Gemini that its natural opening wording guides the local
  animation, giving it conversational control without hidden metadata or a tool.

- Disable the strict local name/20-second reply gate by default: it suppressed
  legitimate "Tell me about yourself" requests after a conversational pause.
  Existing Gemini proactive audio/instructions still judge whether speech is
  directed at Ember; direct questions do not need its name. No extra classifier.
  `BAYMAX_ADDRESS_GATE=1` restores the old gate. Live background-conversation
  and noise behavior remains pending; model judgment is not guaranteed.

- Turn-taking: MVP waits for 600 ms of silence instead of 500 ms (raw fallback
  previously used 100 ms); speech-triggered interruption remains opt-in.
  Saved settings still win; `BAYMAX_TURN_END_SILENCE_MS` accepts 300-2000 ms.
  v10 cancels queued reply audio before handling output on an interruption
  message, while retaining co-delivered user transcription. Share the speaking
  flag with the monitor/initiation logic instead of a receive-loop local. Added regressions
  for pause validation, overrides and cancellation ordering. Physical echo,
  thinking pauses and spliced-response reproduction remain pending.

- Slightly slower speech via the persona, with brief natural pauses instead of
  drawn-out words. Broaden automatic eye cues for ordinary conversation; default
  open eyes retain only the original blink. Fix missed cues in
  large transcription chunks and curly apostrophes. Explicit requests and
  alert/status priority remain; no extra model call or capture. Physical voice
  speed, matrix timing and perceived naturalness remain pending.

- Omit timezone names and UTC offsets from the spoken startup greeting; retain
  device-local clock context so the time and daypart remain accurate.

- Keep ordinary conversation on the person's topic: remove task/capability
  suggestions from the startup greeting and instruct the persona to offer
  reminders, lists or other services only when they address an expressed need.
  Requested tools and existing reminder announcements are unchanged. Live
  conversation validation pending.

- Startup greeting: supply the actual device-local time, timezone and UTC offset
  instead of asking Ember to act as if waking up. Request a friendly two- or
  three-sentence hello, a grounded camera observation when available, and one
  question or offer to help. No humming or invented places; an unclear view
  prompts a question instead. Wait four seconds for the existing video sender.
  Offline: 267 core + 156 toolkit tests pass on Windows/Python 3.12.15,
  including local clock/daypart and session regressions. Live voice/camera and
  device timezone validation pending. No extra capture or provider calls.

- Memories are saved while Ember runs, not only at a clean stop (v10, wherever memory
  is installed: the Orange Pi, the Docker robot image):
  - A background thread summarises the conversation since the last save with
    `gemini-3.5-flash` after 20 user turns, 8,000 characters (e.g. people talking among
    themselves) or 3 minutes with nobody talking. It stores the lines in ChromaDB and
    drops what was saved from RAM, keeping the last 4 utterances (up to 600 characters
    each) as context for the next summary. A crash or power cut now loses only the
    conversation since the last save; before, it lost everything since the last clean
    stop, and a robot that ran for days kept every transcribed fragment in RAM. A failed
    summary or store keeps the text and retries after 5 minutes; past 60,000 unsaved
    characters the oldest text is dropped.
  - The stop summarises only what is left and must finish within
    `BAYMAX_MEMORY_STOP_SECONDS` of the stop beginning: run_mvp.py sets 10, since its
    service has 15 s (`TimeoutStopSec`) and the eyes take up to 3 s to close after Ember;
    the default elsewhere is 60 (Docker allows 120 s, the robot image's service 150 s).
    The clock starts when the session is cancelled. The stop waits for a save in
    progress, then gives Gemini the time left. Each summary call runs on a daemon thread
    with the same HTTP timeout (20 s during uptime). Once stopping, no new save starts,
    and the 2 s pause before reconnecting is skipped (nothing reconnects). Before, a long
    session's summary and embedding could outlast the 15 s, and systemd killed Ember
    with that session's memories unsaved.
  - The transcript sent for a summary is in order, one line per utterance. Before, the
    user's and Gemini's fragments were paired by position (user fragment n next to
    Gemini fragment n), not by time.
  - Gemini is asked for a summary only when the memory store can save it. On the
    desktop profiles (no memory packages) a stop no longer sends the transcript to Gemini
    for lines that could not be stored, and no transcript file is written there.
  - The text behind saved memories is appended to one `transcript_<start time>.txt` per
    run (it was written once, at the stop). Memory ids gain a random part
    (`memory_<timestamp>_<8 hex>_<line>`) so two saves in one second can't overwrite
    each other; the metadata (`source`, `timestamp`, `line_index`) is unchanged.
  - The system instruction lists the 50 newest memories (sorted on `timestamp`), oldest
    first. Chroma's `get(limit=50)` returns the first 50 ever added, so once there were
    more than 50, new memories never reached it. The initiation hint's random pick also
    comes from the newest 50. Sorting reads every memory's metadata at each connect:
    measured on a Mac, 31-40 ms with 2,000 memories and 275-452 ms with 20,000.
    Not measured on the Orange Pi.
  - The initiation hint's memory lookup runs on a worker thread, not the event loop. If
    the user starts talking during it, no opener is sent.
  - `[QUEUES]` counts unsaved utterances, and the stop prints the conversation since the
    last save. Voice analysis still gets the user's fragments of the run, kept only while
    it is on (the MVP launcher turns it off) and now capped at the last 50,000 (a few
    days of talk; its audio is kept for a day) instead of growing without limit on the
    robot image and Docker, which run it.
  - v8 and v9 are unchanged.

- Orange Pi 5 work that changes every platform (Windows, macOS, Linux including the
  LattePanda MVP and Docker robot paths), v10 camera thread and updater:
  - The fall toolkit (Setup's "experimental fall detection") reuses the core camera
    thread's pose result (`shared_pose`) instead of loading a second pose model, so
    enabling it no longer doubles pose CPU. With `camera.pose_tracking` on (the default)
    it loads and downloads no model of its own and gets no frames, only the core's
    landmarks; Monitor shows `Running on CPU (core pose)` (`NPU (core pose)` on a set-up
    Orange Pi) and the core's inference time. With pose tracking off, and in v8, it runs
    its own checksummed CPU model as before. Setup's checkbox now reads "experimental
    fall detection (CPU, or NPU on a set-up Orange Pi)".
  - The dashboard's annotated picture (7-20 ms a frame) is drawn only while someone
    watches the live view; a new viewer gets the latest frame drawn at once, or the
    camera's last one if it stopped. A frame already streamed is re-sent, not re-encoded.
  - The MVP updater (`devops/mvp/update.py`) installs the same package list as Setup on
    each platform (it always used requirements-pc.txt): requirements-orange-pi.txt on
    Linux ARM64, requirements-macos-intel.txt on Intel Macs. A pip or check_release run
    that times out (or a missing venv python) now rolls back like a failed one, and an
    update stopped after the switch (systemd's 20-minute start limit, power loss) is
    rolled back on the next start; both used to leave the robot on the new, unchecked
    commit. The update is recorded before the checkout, and a rollback records the bad
    commit before switching back: a rollback stopped before switching back or during its
    own package reinstall (a 15-minute pip timeout leaves little of the start limit) is
    finished on the next start, and that commit is never applied again. Still not
    handled, as before: a power cut during the git checkout itself (the update's or the
    rollback's) can leave the tree part old, part new, or a stale `.git/index.lock`;
    updates can then stop, with Ember starting on that unchecked mix (or on the failed
    commit) until it is fixed by hand (`git checkout --force HEAD` after removing the lock, then
    `git clean`; see devops/mvp/README.md). Follow-up: a forced checkout and removing the
    lock in the updater, once tested on a robot. An Orange Pi updating from an older
    version is switched by the old updater, which watched only requirements-pc.txt:
    install requirements-orange-pi.txt once by hand (or rerun Setup).
  - Memory's embedder (`semantic_embedder.py`) runs MiniLM with ONNX Runtime and the
    `tokenizers` library instead of optimum, transformers and torch. Same tokens, vectors
    within 3e-7 of before (cosine >= 0.9999998) and the same top-3 recall on 100 test
    queries, so existing `chroma_store`s keep working. ONNX Runtime gets two threads that
    sleep between calls (it used one per core, spinning, and set their core affinity
    itself). Where memory's packages aren't installed (the desktop profiles) memory
    stays off, as before. Where they are and `all-MiniLM-L6-v2-onnx/` is missing, Ember
    downloads it in the background on start (see the Orange Pi entry) instead of
    turning memory off. `deploy/make_lock.py` now also reads convert_model.py, so a
    regenerated Docker lock keeps optimum and torch for the image build's conversion,
    and counts `features/` as Ember's own code (it stopped with "no installed
    distribution provides module 'features'").

- Orange Pi 5 (RK3588) only (Linux on 64-bit ARM), on any kernel:
  - Ember runs on the four fast Cortex-A76 cores (cpu4-7), found from sysfs at start
    (`pc_hardware.use_performance_cpus`, called first thing by run_mvp.py and run_pc.py,
    before the eyes, network-watch and numpy threads start; it also moves any thread
    that already exists).
    MediaPipe runs pose on one thread on Linux: measured on the board, 43 ms per frame
    on an A76 and 215-247 ms on an A55. Linux on 64-bit ARM with fast and slow cores
    only: no effect on Windows, macOS, x86 Linux (including PCs with Intel/AMD hybrid
    P/E-core CPUs, and the LattePanda) or ARM boards whose cores are alike, and never
    onto a single core. `BAYMAX_CPU_PINNING=off` turns it off.
  - The camera keeps only its newest frame (V4L2 buffer of 1; Linux ARM64 with the pc
    profile, which the Orange Pi runs), so pose and fall detection see the present
    instead of a frame up to ~0.2 s old (an estimate from OpenCV's four-frame queue).
    Not measured yet, on the Orange Pi either: no camera run there so far. x86 Linux
    cameras (the LattePanda MVP, which also runs the pc profile, and the Docker image)
    keep OpenCV's default queue.
  - `requirements-orange-pi.txt` gains the MVP packages it was missing (pywebrtc-audio,
    pyfirmata2, miniaudio, vosk): audio cleanup is on by default, so the runtime stopped
    at import on the Pi.
  - Memory is on: `requirements-orange-pi.txt` adds chromadb 1.5.8, onnxruntime 1.24.4
    and tokenizers 0.22.2 (no torch), with ChromaDB's OpenTelemetry held at 1.27.0, the
    last release that accepts MediaPipe 0.10.18's protobuf < 5. On its first start with
    memory retrieval on, Ember downloads all-MiniLM-L6-v2's own ONNX export and tokenizer
    from Hugging Face in the background (about 90 MB, revision 1110a243) into
    `all-MiniLM-L6-v2-onnx/`. Ember starts and talks meanwhile, and memory turns on when
    it finishes. Each file is checked by size and SHA-256 and synced to disk before it
    is renamed into place, model.onnx last; partial files are cleaned up after 10
    minutes. `python semantic_embedder.py --download` fetches it beforehand (needed
    once when memory retrieval is off, for session summaries). Measured on the Orange
    Pi 5 Max with 500 memories: a lookup takes 34 ms of CPU and is ready in 19 ms on two
    fast cores (about 140 ms on a slow core); in the full-system test Ember went from
    42.7% to 42.8% of one core (NPU pose) and from 91.8% to 92.0% (CPU pose), with one
    lookup per user turn and none timed out; RAM about 170-230 MB more (the torch-based
    setup measured 410-500 MB on a Mac). Not yet run in a live conversation.
  - `requirements-orange-pi.txt` also installs rknn-toolkit-lite2 2.3.2 (aarch64,
    Python <= 3.12), the NPU's Python side, which was a manual step: the same version
    as the converter's default, which the accuracy check records. It is imported only
    on an RK3588. `librknnrt.so` is still installed by hand (needs sudo).

- Pose can run on the Orange Pi 5 Max's NPU (RK3588), with MediaPipe on the CPU
  everywhere else. New `features/pose/`: MediaPipe Pose Landmarker's VIDEO-mode graph
  (detector, ROI tracking, heatmap refinement, smoothing) ported to numpy + OpenCV,
  running the two networks from the same `pose_landmarker_full.task` either on the NPU
  (RKNN FP16, via rknn-toolkit-lite2) or ONNX Runtime. The camera thread asks
  `create_pose_backend()`: the NPU only on Linux aarch64 + `rockchip,rk3588` +
  rknnlite + converted models whose SHA-256s and on-board accuracy check (run with the
  same rknn-toolkit-lite2 version, and the same librknnrt/driver version as now) are in
  `manifest.json` + a working `init_runtime`; otherwise, and on any error while checking
  (unreadable files, malformed manifest), MediaPipe with the same options and calls as
  before. The port uses the runtime's MediaPipe confidence settings. The choice and the
  reason are logged once (`[POSE] Pose backend: ...`), with a warning if the runtime's
  .task weights changed since conversion; `BAYMAX_POSE_BACKEND=cpu|npu|onnx` overrides
  (`mediapipe` = `cpu`; any other value is MediaPipe with a warning, never the NPU;
  `npu` also runs on a changed librknnrt/driver, with a warning). An NPU error mid-run,
  including NaN/inf outputs, switches to MediaPipe and logs the traceback once. The
  fall toolkit shows `Running on NPU (core pose)`, and `Running on CPU (core pose)`
  after such a fallback.
  Board-side `features/pose/convert_rknn.py` (+ `setup_converter.sh`) derives ONNX
  from the .task's TFLite (densify + tf2onnx, checked against TFLite), builds RKNN with
  rknn-toolkit2 2.3.2 or 2.5.0 and checks NPU vs ONNX: raw landmarks 0-34 (incl. the
  two that steer tracking), visibility of 0-32 and the pose flag, then the whole
  pipeline with each engine tracking on its own over still frames and a 72-frame
  tracked sequence (person leaves and returns, falls, lies still): positions, and
  which landmarks are visible (>= 0.5, what fall detection and face presence use),
  with minimum frame counts so a check that compared nothing fails. This gate checks
  the conversion, not behaviour on real footage. A changed or truncated runtime .task
  makes the export download the pinned one. `benchmark.py` prints CPU vs NPU ms and
  process CPU per frame (all threads, while each frame's pose runs), back to back or
  paced like the camera (`--fps 15`, `--empty`), pinned to the fast cores before numpy
  loads, as Ember is.
  Results (features/pose/VALIDATION.md):
  - The port on ONNX Runtime (FP32), macOS, 8 licensed clips + synthetic falls (8,075
    frames at 15 fps): agrees with MediaPipe as closely as MediaPipe agrees with itself
    under +-1 grey level of noise, with the same fall decisions (2 real, 11 synthetic).
  - The NPU (FP16) on an Orange Pi 5 Max (Armbian Debian 13, vendor kernel 6.1, RKNPU
    0.9.8, 2.3.2 toolkits) against MediaPipe 0.10.18, 17 licensed clips + synthetic
    falls: on real clips closer than the noise baseline (detection 98.1% vs 95.5%), but
    it confirmed 1 of MediaPipe's 2 real-clip falls and 10 of its 11 synthetic ones, and
    on the synthetic falls it is below the baseline (detection 96.9% vs 99.0%,
    fall-pending 98.4% vs 99.1%). The real-clip difference is judo_soft, a two-person
    throw then get-up: same onset, then the NPU lost the person for a frame, re-found
    the standing partner and dropped the pending fall. The synthetic one is
    kettlebell_squat at 25 s, a slow lie-down labelled "no fall" that MediaPipe flagged.
    The converter's check passed on that board; `check --video` on 8 hard real clips
    fails its limits, which are set for the synthetic sequence.
  - Speed on that board: 10.5-11.9 ms instead of 36-44 ms per frame back to back
    (benchmark.py, per-clip medians over the 17 clips; all speed and CPU figures here
    predate benchmark.py loading numpy after the pinning, not re-run). Paced at the
    camera's 15 fps (`benchmark.py --fps 15`) a frame takes longer, as the clocks drop
    between frames:
    median 16.2 ms (p90 23.8) with a person in view and 28.5 ms with nobody in view
    (`--empty`: the detector then runs every frame), against 43.7 / 39.6 ms for
    MediaPipe. Process CPU at 15 fps: 12.4% instead of 76.7% of a core with a person in
    view, 19.7% instead of 70.5% with nobody in view (an earlier paced script that
    counted the whole run gave 13% / 76%).
  Stop Ember before the check or the benchmark. `setup_converter.sh` now always installs
  setuptools<80.1 into the converter venv (rknn-toolkit2 2.3.2 imports pkg_resources,
  which a fresh Python 3.12 venv lacks; found on the board).

- Tested offline only. `check_release.py` passes on macOS at this commit (core 356
  tests, 1 skipped: the parity test, no onnxruntime; toolkit 158), also with
  `BAYMAX_CPU_PINNING=off` exported or in `.env`, and passed on the Orange Pi 5 Max at
  commit 61c410e (core 352 tests, 15 skipped, 0 failures; toolkit 158, 0 skipped). The
  board's 15 skipped are, by the counts, the 14 git-based updater tests (no git on the
  board) and the off-RK3588 default test, so no updater test has run on Linux aarch64
  yet (to confirm with a verbose run next time). Windows was checked only by emulating
  its paths and file handling on macOS. The NPU tools (converter, check, validate,
  benchmark back to back and paced) ran on the board with Ember stopped. Pending:
  `check_release.py` on real Windows; Ember running live on the Orange Pi (not started
  there yet, CPU or NPU: audio, camera, a conversation, the live view, the fall
  toolkit's status, fast-core pinning under ember-mvp.service, and that the runtime
  gets past import with the added `requirements-orange-pi.txt` packages; with the NPU:
  the camera thread, `[POSE] Pose backend: NPU`, Monitor `Running on NPU (core pose)`,
  a mid-run fallback, a conversation alongside); the camera loop with the 1-frame
  buffer on the Orange Pi's camera (loop FPS and frame age: with Setup's fall-detection
  checkbox on, the Monitor's FPS should stay near 15, or read the FPS overlay in the
  live view); thermals over a long run; fall accuracy at home (real rooms, older
  adults, two people in view); the live view (open, close, reopen, two viewers) and
  the fall toolkit's status and inference time on Windows, macOS, the LattePanda and
  the Orange Pi; the updater tests on Linux aarch64 (install git on the board); the
  updater's rollback on a robot, and handling a power cut during its checkout (above).

- Eyes sleep 60 s after the conversation goes quiet (was originally 180 s), counted from
  when Ember's voice finishes playing (`reply_playback_done` now resets the
  timer). Speech wakes them to normal blinking. `BAYMAX_EYES_SLEEP_AFTER_S`
  still overrides.

- Ember answers questions about what the family wrote: asked "do you know my
  name?" with "Call the person James" in its profile, it said it didn't know,
  because the profile said "don't recite it". The header now says to answer
  such questions directly, but still not to read the profile out unprompted.

- Replies to family messages are confirmed out loud: after `send_reply`
  succeeds, Ember says "Got it, message sent." (instruction plus a `say` field
  in the tool result); on an error it says the reply didn't go through.

- Stopping an audiobook no longer needs shouting up close. On ember-002 only 6
  of 40 wake-word utterances paused the book. Pairs now count in either order
  ("Ember, hey"), with hey/okay + stop/pause, at confidence 0.8 (was 0.9), and
  across Vosk splitting "hey | Ember" up to 3 s apart: 9 of the same 40 logged
  attempts now wake. Any wake word heard alone also lowers the book to 30% for
  4 s, so the rest of the phrase is heard over it (needs a live check; narration
  that sounds like a wake word causes the same brief dip). Lone "ember" still
  never wakes.

- Ember app link (needs `BAYMAX_DEVICE_KEY`; Company-Website `messages-reminders`
  branch): every ~15 s the robot reads its inbox and syncs back. Messages from
  the dashboard are read out at the next quiet moment and Ember offers to
  reply (a reply is read back and sent only after the person confirms).
  Reminders made on the dashboard, and cancellations, go into the everyday
  reminder store. Notes about the person are summarised with
  `gemini-3.5-flash` into `[ABOUT THE PERSON]` in Ember's instructions (and
  sent mid-conversation when they change); the summary goes back to the app.
  Transcripts are never sent. See `features/toolkits/family/README.md`.

- Reminders repeat like Apple Reminders' custom repeat (hourly to yearly,
  every N, weekdays, days of the month, "the second Tuesday", end date), by
  voice or from the app. Ember asks whether each one was done and logs done /
  not done / no answer (asking once more after 10 minutes); the history is
  kept and shown on the app, with who set each reminder. Offline tests:
  `test_toolkit_reminder_repeats.py`, `test_toolkit_family.py`; also run
  against the site's real Worker locally. Live robot check pending.

- Audiobooks play 25% louder: narration was quiet on ember-002 even at full
  book volume. New `BAYMAX_BOOK_BOOST` (default 1.25) multiplies the book
  volume, and samples are clipped so loud passages cannot wrap into noise.
  The spoken "louder/quieter" 0-100 % control is unchanged (100 % now means
  the boosted level). `BAYMAX_BOOK_BOOST=1` restores the recording's level.

- Session memory summaries on v10 use `gemini-3.5-flash`. Google now answers
  `gemini-2.5-flash` with 404 ("no longer available to new users"), so every
  shutdown summary failed and nothing was saved. 3.5 Flash answered a test
  prompt in about 1 s; 3.8 Flash (Google's suggestion) was returning 503 "high
  demand", and the summary runs once at shutdown without a retry. The live
  conversation model is unchanged. Live shutdown summary check pending.

- Only when addressed, enforced on the robot: proactive audio and the prompt
  only asked Gemini to stay quiet, and on ember-002 it still answered
  `<noise>` and an overheard phone call. v10 now plays a reply only when the
  person said "Ember", spoke within 20 s of Ember's last reply
  (`BAYMAX_FOLLOW_UP_SECONDS`), or it answers Ember's own alert/reminder/
  greeting message; otherwise it is dropped and logged as `[ADDRESS]`. Uses
  Gemini's existing transcription only. `BAYMAX_ADDRESS_GATE=0` backs it out.
  The name also counts when transcription splits it ("A mber", "E mperor"):
  the first robot run dropped "Hey, E mperor, what's the weather".
  Offline tests pass (tests/test_address_gate.py); live robot check pending.

- "Ember disengage" / "Ember engage": voice sleep and wake. After "Ember
  disengage", Ember says a short goodbye, shows sleep eyes and Gemini hears only
  silence. The echo-cancelled mic goes to the offline Vosk listener, which
  answers only to "Ember engage" ("Ember" alone does nothing). While engaged no
  wake word is needed. Due reminders and possible-fall check-ins engage Ember
  again; greetings stay quiet. If audio cleanup is off or Vosk is unavailable,
  disengage is refused or undone, so Ember is never left deaf.
  `BAYMAX_ENGAGE_WORDS=0` disables it. The wake-word listener now takes its own
  rules and shares one loaded model. Live robot check pending.

- Long conversations: add Gemini Live context window compression (sliding
  window at 32k tokens) and session resumption. Without them Google ends
  audio+video sessions after 2 minutes and resets connections about every 10,
  and Ember reconnected as a brand-new conversation with a growing, slower
  context. The receive loop stores each resume handle (parity test strips the
  hook); a handle that fails once or is near Google's 2 h expiry is dropped.
  Full config validated against google-genai 1.62; live robot check pending
  (BAYMAX_CONTEXT_COMPRESSION=0 / BAYMAX_SESSION_RESUMPTION=0 to back out).

- The local "hmm" while Gemini thinks is off by default on the MVP (testers
  found the fixed sound unsettling); `BAYMAX_WAIT_CUE_ENABLED=1` restores it.

- Robot test follow-ups: `public_poem` takes a title (PoetryDB title search,
  narrowed by author when given; live check returns The Raven even for "Edgar
  Allen Poe"), and Ember quotes requested lines instead of paraphrasing.
  TheMealDB (~300 recipes) has no peach cobbler, so when a recipe is missing
  Ember offers a simple version from its own knowledge and says so.

- Robot test: asked for "mystery thrillers", Gemini searched TVmaze by genre
  and gave up when it failed. TVmaze search is title-only and currently
  returns 502 for some queries ("mystery thriller", "crime drama", even "the
  crown"; "the office" works). `public_shows` now says title lookup only, and
  the prompt says to recommend from Gemini's own knowledge or Google Search
  and keep helping when a source fails.

- Discovery tools (trivia, TVmaze shows, PoetryDB, radio station lookup,
  TheMealDB recipes, Wikipedia, Open Library) are on by default via
  `EMBER_DISCOVERY_ENABLED` (default 1), independent of the opt-in public
  toolkit so a Setup save with it off does not remove them. Feeds, extra
  weather/air quality, reference and events stay opt-in. 226 core + 126
  toolkit offline tests pass; live provider calls on the robot pending.

- Fix Ember going deaf after connecting: the everyday reminders session task
  awaited `shutdown_event.wait()`, but the runtime passes a threading.Event,
  so it blocked the event loop forever (py-spy on ember-002: MainThread in
  everyday/tools.py:100; mic captured but never sent, no startup hello). It
  now sleeps with asyncio; the hook documents the contract, and a test runs
  every registered session task with a threading.Event and checks the loop
  keeps ticking (it hangs without the fix).
- Eyes stay on the confused spinner after Gemini connects until mic audio is
  actually being sent (`mic_live` from the audio cleanup, at most 1/s), and go
  back to it if mic audio stalls (`mic_stalled`); the waiting cue ignores both.
  `BAYMAX_EYES_WAIT_FOR_MIC=0` restores connect = ready. 226 core + 125 toolkit
  offline tests pass; robot check pending.

- Ember's systemd unit stops within 15 s (`TimeoutStopSec`, was 180 s) so a
  slow or hung exit no longer holds reboots for minutes; systemd then kills it
  and the eyes' ExecStopPost still runs. Existing robots rerun
  `python3 devops/mvp/install_autostart.py` once to apply.

- Eye-contact greetings are off by default (`BAYMAX_PRESENCE_GREETING=1`
  enables). On ember-002 (i5-8200Y, pose fall detection also on) the mic
  stopped reaching Gemini for 144 s starting 2 s after the face model loaded,
  and the startup hello never ran. On a desktop the face model at 81 Hz did
  not delay the event loop, so robot CPU load is the likely cause; pending a
  robot check with fall detection on/off. The startup hello stays on.

- Double replies were still intermittent: our runtime records each Gemini
  transcription event once, so Gemini itself generated the reply twice, each
  time around a mid-reply `set_eye_expression` call. Gemini no longer gets the
  eye tool by default; `features/eyes/auto.py` picks one expression per reply
  locally from explicit requests ("make a sad face") or clear cues in Ember's
  reply opening ("I'm so sorry"), via a new monitor `transcript_observer`.
  `BAYMAX_EYE_TOOL=1` restores the tool. Eye contact is logged to the
  dashboard as "Eye contact started/ended" (with the greeting decision), with
  a 1 s grace so blinks do not split a look. 224 core + 104 toolkit offline
  tests pass; robot check of doubles/expressions pending.

- Add persistent local timers, one-time reminders and shared household lists.
  Confirm tool saves, clarify absolute-time offsets, and announce overdue reminders
  through the existing Gemini session. Disable with EMBER_EVERYDAY_ENABLED=0.
  Spoken delivery requires Ember online; live voice/device checks remain pending.

- Add five opt-in Discovery tools: cached trivia batches, TVmaze show/cast/schedule
  lookup, PoetryDB excerpts, radio station discovery, and
  TheMealDB recipes. Reuse bounded HTTPS/cache and Gemini tool routing; no new
  dependencies or capture. Radio playback and live hardware checks remain pending.

- Robot timeline showed the double reply again inside one turn, right after a
  mid-reply `set_eye_expression('normal')`: remove `normal` from the tool enum
  (eyes reset on their own) and tell Gemini never to call it for neutral
  replies. Misspelled `BAYMAX_VOICE` ("Archernar") now maps to the closest
  voice, unknown names to Autonoe, instead of silently Fenrir. The startup
  hello waits 3 s, skips if someone is already talking, and is retried once if
  the session drops right after it (it did on ember-002, so no hello was heard).

- Ember starts conversations: one calm hello at startup and a greeting on new
  eye contact (MediaPipe Face Landmarker on existing camera frames via new
  `fall_detection.frame_observers`/`session_tasks` hooks; quiet 120 s, cooldown
  10 min, never during books/replies). Real-model check on a frontal portrait:
  eye contact detected at 202/142/101 px face widths, none below ~80 px
  (~1-1.5 m); ~8 ms/frame desktop CPU. Persona gains a [YOUR VOICE] section
  (calm, unhurried, not cheerful by default) since Autonoe still sounded
  upbeat. 220 core + 102 toolkit offline tests pass. Robot checks pending:
  real eye contact/false greetings, CPU on the robot, voice tone.

- Audiobooks play at full volume by default (`BAYMAX_BOOK_VOLUME`, 0-1) and
  the tool gains `volume` (0-100 %) so people can say "louder"/"quieter";
  status reports the volume. Persona asks for a calm, warm resting voice
  (Autonoe sounded too energetic) that brightens or softens with their mood.

- Ember's voice defaults to Autonoe (`persona.DEFAULT_VOICE`) when
  `BAYMAX_VOICE` is unset; `BAYMAX_VOICE` still overrides it.

- Google Search grounding: the toolkit adds Gemini's built-in `google_search`
  tool plus a short prompt (use it for uncertain/current facts and book
  critique, cite sources briefly, results are data). Gemini runs the search;
  only the query goes to Google. `BAYMAX_GOOGLE_SEARCH=0` (or disabling the
  toolkit) removes it; the dashboard capability reports it. Offline config
  tests pass; live check pending on the robot (no Gemini key on the dev PC).

- Audiobook book mode: while a LibriVox book plays the mic is withheld from
  Gemini (it answered the narrator as the person) and fed to an offline Vosk
  wake-word listener ("Hey Ember", "Ember stop/pause", "stop", "pause"). Books
  also pause after every chapter; Ember gets an `[AUDIOBOOK]` note with book,
  author and chapter so it can discuss it, and announces it is playing a
  recording. Falls back to the open mic without Vosk/model. Offline narration
  eval (A Tale of Two Cities ch. 1-2, ~22 min, leak 10/20 dB down): 0 false
  pauses; 15/16 phrases detected (missed "ember pause" at 10 dB down, its
  "pause" alone still works at 20 dB). Robot run with real speaker/mic pending.

- Integrate all supplied eye-expression patterns with an explicit Gemini
  `set_eye_expression` command: happy, sad, excited, confused spinner, surprised,
  curious, love, sleep bubbles and dead/X pose, plus normal blinking. Keep the
  existing >2s thinking dots automatic. Fall-alert dispatch forces surprise
  above model expressions/thinking; detection remains opt-in and local.
- Preserve expressive eyes through buffered reply playback; reset on new turns,
  interruption/reconnect/stop, playback drain or safety expiry. Validate command
  arguments and keep eye tools independent of public toolkit enablement.
- 180 core + 73 toolkit offline checks pass (253 total), including animation
  order, late fragments, fall priority, playback completion, invalid/disabled
  hardware and async tool response IDs. Physical eye/voice timing and fall
  check-ins on ember-002 remain pending.

- Default MVP to Gemini `NO_INTERRUPTION` while preserving healthy-DSP microphone
  delivery during replies, AEC and residual subtraction. Add private
  `BAYMAX_INTERRUPTION_ENABLED=1` override to restore interruptions; other
  launchers keep the earlier default. The setting also applies with enhancement
  disabled without changing that raw path's playback gating. Echo may still be
  recognized; no transcript is deleted or treated as guaranteed speaker identity.
- Offline tests cover the no-interruption config, unchanged audio/transcription
  settings, MVP default and saved override. Physical Gemini/LattePanda checks
  remain pending.

- Enable the opt-in MVP user service at user startup; add an independent,
  restarting Monitor service and desktop/Applications launcher. Open Monitor at
  desktop login when a physical display is connected; otherwise keep it local
  and available through the launcher. GNOME automatic login is still required
  for unattended boot. Setup uses a separate port to avoid Monitor conflicts.
- Bound Monitor requests to five seconds so stalled requests can retry. Offline
  startup/launcher tests pass; physical Debian login/boot and browser recovery
  still need validation on ember-002.

- Add conservative residual echo subtraction after WebRTC: search bounded recent
  speaker PCM for changing delays, require strong/stable correlation, subtract
  only 90% of the fitted component and retain unexplained user audio. Uncertain
  fits/strong double talk pass unchanged. No fixed mic/speaker geometry is used.
  Extra-layer failures retain WebRTC; local disable override is documented.
- Add debugger-only recent transcript prefix-overlap warnings. Retain all user
  text; no transcript-based gating, identity claim or extra provider/capture call.
- 169 core + 70 toolkit tests pass (239 total); ten new regressions cover delays,
  polarity, movement, quiet/strong double talk, reference bounds/staleness,
  failure isolation and diagnostic-only overlap/expiry. Native synthetic echo/
  double-talk probe still passes. Physical room behavior and CPU cost are pending.

- Fix residual robot speech being re-amplified by AGC after AEC: remove positive
  gain while speaker audio, its device buffer and a 250 ms echo tail are active.
  Healthy DSP still passes user speech during replies; failed DSP suppresses
  microphone input through the audible tail, even after queued playback drains.
  Raw recording and disabled enhancement behavior remain unchanged.
- Feed callback ADC/DAC timing into AEC delay hints and show echo protection /
  effective gain / delay in Monitor diagnostics. Apply shared v8/v10 hooks.
- 159 core + 70 toolkit offline tests pass (229 total). Native pinned-WebRTC
  synthetic probe: 11.0 -> 26.1 dB net echo reduction, with non-muted independent
  voiced double talk. Actual LattePanda echo, quiet interruptions, volume and
  timing still require live testing; no hardware recording or cloud call made.

- Lower Conversational Ability speech-start sensitivity from high to low after
  the ember-002 user reported overly sensitive interruptions in the MVP service.
  Keep echo cancellation, noise reduction, prompts/models and turn-end timing.
  Add local `BAYMAX_SPEECH_START_SENSITIVITY=high` override; invalid values warn
  and use low. Document the existing runtime debugging Monitor command.
- 154 core + 70 toolkit offline checks pass (224 total); real-device improvement
  and intentional-interruption tradeoff still need confirmation on ember-002.

- Fix the generated user service's invalid quoted WorkingDirectory path, which
  systemd rejected as non-absolute on ember-002. Keep ExecStart word quoting;
  add a regression assertion for the distinct path directive. Import only set
  desktop environment variables, avoiding the X11 WAYLAND_DISPLAY warning.

- Add one local synthetic “hmm” for response waits over 2s, at most once per wait.
  Uses the existing speaker callback/AEC render reference. Reply audio, cleaned
  near-end speech, turn end/interruption and reconnect cancel it. No new audio
  stream, prompt/model change or provider request. Requires healthy AEC; local
  `BAYMAX_WAIT_CUE_ENABLED=0` disables it. Preserve raw recording/disabled parity.
- Load saved feature overrides before starting eyes; this fixes local eye
  enable/port overrides being read before `.env` was loaded.
- Cue/installation update: 152 core + 70 toolkit offline tests pass (222 total),
  Windows/Python 3.12.15; no failures/errors/skips. Added LattePanda download and
  setup commands. Audible quality, echo/interruptions and boot testing remain pending.

- Integrate the supplied eyes under `features/eyes/`: hollow open/blinking eyes
  while speaking/listening; six-position thinking dot only after a wait exceeds
  two seconds from the latest user-transcription fragment. First reply audio,
  turn end/interruption and reconnect restore normal blinking. Brightness 3,
  left device 1/right device 0, no flips or servo. Serial writes run separately
  from Gemini; missing eyes are non-fatal. Linux MVP enables them by default.
- Eye integration gate: 139 core + 70 toolkit tests pass (209 total), zero
  failures/errors/skips, Windows/Python 3.12.15. Physical eyes/LattePanda live
  integration remain pending. Expression integration is described above.

- Combine #7, #11 and #12 into one PR against main. Defer #8 people awareness,
  #9 latency optimization and Memory Palace. Eye integration is described above.
- Add `run_mvp.py`: require explicit saved sensors once, then reuse configuration
  through the existing v8 key/model preflight and runtime. Add an OS-backed
  concurrent-launch lock and graceful SIGTERM handling.
- Add one-time Setup completion and opt-in Debian desktop autostart in
  `devops/mvp/`, using a dedicated user service and the private Python runtime.
- 124 core + 70 toolkit offline checks pass on Windows/Python 3.12.15. Actual
  Debian installation, devices, live conversation/toolkits and cold-boot tests
  remain pending. No robot deployment, production service change or merge.

# Conversational Ability (#12)

- Extract responsive audio into `features/conversational_ability/`, independent
  of Memory Palace, Chroma and face recognition (reviewed in #13).
- Integrate WebRTC AEC3, noise suppression, AGC2, short-answer detection and
  listening during replies into v8/v10; preserve models/rates/raw recording.
- Add a Setup feature toggle/noise level and microphone telemetry in Monitor.
- Include generic external-key-file loading for desktop startup.
- Combined Windows v10 live test was reported responsive by the user;
  standalone v8 integration still needs a separate live test.

# Update notes

## Unreleased — feature organization and toolkits (PR #11)

- Stack on PR #7 for all OS compatibility, setup, device and monitor infrastructure, including the Intel Mac profile. The PR review diff excludes that inherited foundation.
- Scope location/weather, optional CPU fall detection and opt-in public information/headline/feed toolkits to this branch. Existing camera frames, bounded provider calls, explicit preferences, monitoring, controls and disabled behavior are preserved.
- Organize features under sibling `memory_palace/`, `person_recognition/` and `toolkits/` namespaces; memory/person folders reserve integration boundaries. Prompt/identity/recall changes and Memory Palace implementation belong to PR #13, not this PR.
- Runtime modifications here are limited to feature import paths, toolkit dispatch/lifecycle and monitoring. Original v8 prompt/models/audio behavior remain covered by strict upstream parity checks; v9 matches main.
- All 169 combined offline checks pass on Windows/Python 3.12: 99 core and 70 toolkit, with zero failures/errors/skips. Actual Gemini/hardware, Mac/Linux/Orange Pi installs, fall accuracy/performance, Docker and live integration remain pending. Earlier provider smoke checks do not establish current provider availability or full conversation correctness.
