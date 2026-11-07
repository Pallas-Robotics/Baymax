# Conversational ability

Responsive turn-taking and local microphone processing, independent of memory or face recognition.
Integrated with desktop v8 and robot v10.

## Language adaptation

The shared [persona](../persona.py) instructs Gemini to reply in the person's
language, follow switches on the next reply, and match mixed-language speech.
English internal system messages should retain the conversation's language.
This uses the existing Gemini Live audio/text input and reply stream: no local
language classifier, extra capture stream, model change or separate provider
request is added. The existing remote-processing data flow is unchanged.

It is enabled through the normal MVP persona, with no separate language toggle.
An approved deployment/runtime restart picks up the prompt change. This guides
reply generation; it does not fix transcription or verify pronunciation.
The [Pi results](../../../docs/features/OPI4_LANGUAGE_RESULTS.md) record 80/81
automated runs passing and a Polish switching failure. See the
[multilingual test guide](../../evals/languages/README.md) for simulations and
`tests/test_language_eval.py` for offline configuration/report checks.

## Conversation timing (OPI4 v10 monitor)

The Monitor shows the last 20 turns (six visible) with estimated local speech
start/end, first/last Gemini transcript arrival, first reply text/audio arrival,
and the first nonzero reply sample scheduled for playback. Durations separate
speech-end to the transcript's last piece (its first piece streams in while the
person speaks), transcript to reply audio, and reply audio to playback.
The status section shows local speech activity before a transcript exists,
then "waiting for Gemini transcript" after detected speech ends. It refreshes
with the existing 200 ms telemetry / 250 ms UI polling. Stale detector data is
marked unavailable. Speaker callback underruns are counted separately to help
diagnose stuttering; the counter does not detect every possible audible glitch.
Speech/transcript timing updates inside the user's chat bubble; reply arrival,
speaker playback and response delays update inside Ember's bubble. While waiting,
the user's bubble shows elapsed waiting time. Full timing history is collapsed
below the conversation. Each toolkit invocation has one centered bubble that
updates from running to finished with start/end timestamps and elapsed duration;
separate repeated invocations retain their own bubbles.
The local detector observes existing cleaned audio (70% speech probability,
at least -45 dBFS, 60 ms onset and 400 ms silence); it does not gate audio or
change Gemini settings. Noise, echoes and pauses can split/miss detections.
Speech times are omitted if multiple local segments could match a transcript.
A reply belongs to the newest turn still waiting. Gemini answers a turn before the
next starts, so an older turn with no reply audio (proactive audio stays silent on
side talk and hesitations) is closed when a new one begins and never takes a later
reply. A transcript that arrives after the reply's first audio keeps that reply if
the person started speaking before it; speech over Ember's own message does not.
A pause that ends a local segment inside one Gemini turn extends the turn to its
last segment, whose end is the turn's speech end. Not checked live: a question
that makes Gemini call a tool while the person speaks during the lookup. If Gemini
said nothing before the call, the newer turn closes the asked one as unanswered
and the tool's answer is charged to the newer turn (diagnostics only).
Gemini's internal recognition time and actual acoustic output are not measured.
Playback uses callback DAC timing; a missing timestamp falls back to the output
latency estimate. Interruption clears unplayed timing marks. A reply is timed from
its first chunk with sound, counted in the speaker's byte stream. Timing never
touches audio: its work runs outside the lock the speaker callback waits for, and
an error in it on the mic, speaker or playback-queue path turns timing off for
the run (one log line) instead of muting the mic or stopping the speaker. The
Monitor's status line then says so, and the performance sampler reports
`timing_failed` (a latency check it was asked for fails).
No new recordings, dependencies, streams or external requests are added. Timing
metadata is local and bounded. Times are kept on the monotonic clock and become
wall-clock times when the monitor reads them, so a clock corrected after start
(NTP) moves them with the heartbeat. Offline tests: `test_conversation_timing.py`
(including replayed Live API orderings), `test_runtime_monitor.py`,
`test_v10_runtime.py`. Live confirmation requires restarting v10 and speaking;
include a tool question (the weather) with a few words during the lookup and
check which turn the reply is charged to.
From the repository root, `node test/monitor-chat.cjs` checks inline timing
updates and one stable toolkit bubble per invocation without opening devices.

The existing camera/audio streams and 48 kHz rates stay in the runtime. This
module opens no devices and makes no external requests.

## Why

The old v10 sender discarded every microphone packet while playback was buffered,
so spoken interruptions and quick yes/no answers during a reply never reached
Gemini. The desktop enhancement sends those packets with a WebRTC AEC3 echo
canceller using the actual speaker callback PCM as the reference. Background
noise suppression, high-pass filtering and AGC2 volume control run before Gemini
input. Raw recordings for existing voice analysis remain unchanged.

The opt-in session configuration uses low speech-start sensitivity, zero required
speech-start padding for brief words, and 500 ms end-of-turn silence outside MVP.
MVP defaults to 400 ms to leave a short pause before answering. There is no
local speech/noise gate dropping packets. This changes speech detection, not
speaker identity, transcription models or the conversation prompt.

The lower speech-start default reduces eagerness to interrupt the robot when
sound is detected; it is not a guarantee against echo or noise. Intentional
interruptions may require clearer speech. To restore the earlier setting, set
`BAYMAX_SPEECH_START_SENSITIVITY=high` in private `.env` and restart. The default
is `low`; invalid values warn and fall back to low. `BAYMAX_TURN_END_SILENCE_MS`
overrides turn-end silence (300-2000 ms; invalid input warns/falls back to 400).
The override also applies without DSP; other raw-path behavior is unchanged.

## Inputs, outputs and dependencies

V10 inserts 180 ms of silence between distinct replies only when the preceding
reply still has queued audio. Chunks within a reply stay contiguous; a reply
after an empty queue starts immediately. Interruption still clears pending audio,
including the gap. This uses the existing PCM buffer and adds no stream, provider
or dependency. Offline coverage: `test_address_gate.py` and `test_v10_runtime.py`.
Audible transitions and device buffering still need live verification.

`processing.py` takes mono int16 mic samples and the already-rendered speaker PCM.
It processes whole 10 ms frames (480 samples at 48 kHz). v10 captures 960-sample
(20 ms) mic blocks, two whole frames, so each block is cleaned and sent with no added
delay. It used 1024-sample blocks, which kept 10 ms of every block for the next
callback. The 10 ms saving is expected, not yet measured: it assumes the host delivers
960-frame periods. If the device keeps its own period (1024 frames on some PipeWire or
ALSA setups), PortAudio's buffer adapter holds a carry of its own; the Orange Pi 5 Max
A/B will show which. A block that isn't whole frames (v8 still uses 1024) starts a 10 ms
output carry once and keeps leftovers, so no tail is zero-padded or discarded on every
callback. Dropped stale audio (`BAYMAX_MAX_MIC_LAG_MS`) is counted in the block
length actually cleaned. Reference storage
is bounded to 250 ms and missing reference is silence. Capture/output stream
callback ADC/DAC timestamps provide the AEC delay hint, falling back to stream
latencies when callback timestamps are unavailable. The capture thread alone owns native DSP;
the output callback only appends to the reference buffer.

**Speaker stream.** `start_output_stream()` opens the speaker with the two settings read
once at start: `BAYMAX_OUTPUT_BLOCKSIZE` (frames per speaker callback, 64-4096, default
1024) and `BAYMAX_OUTPUT_LATENCY` (`low`, `high` or seconds up to 0.5; unset passes no
latency, so sounddevice's `high` applies). With Ember on the RK3588's fast cores
(`pc_hardware.SPEAKER_STREAM`, Orange Pi 5 / 5 Max by default) they default to 512 and
`low`, unless `.env` or the shell set them; everywhere else (the Orange Pi 4 Pro, x86,
macOS, Windows) the stream is opened as before. Measured on the Orange Pi 5 Max with its
USB speaker on PipeWire's default device: the old default stream reported 42.7 ms of
PortAudio latency and the audio cleanup measured about 96 ms of output latency; 512/`low`
reported 10.7 ms and about 42 ms, with no output underflows (the timing monitor's counter,
read every 2 s) in three 3.5-minute runs, one with MediaPipe pose on the CPU, and the mic
and speaker callbacks' timing unchanged. Values outside the ranges print one line and use
the default: a block longer than the echo reference's 250 ms (12000 frames) leaves AEC
without its reference, and an output latency past 0.5 s (AEC3's delay range, the cap on
`output_latency_ms`) would end the mute during replies while Ember is still audible.
When the stream isn't the 1024/default one, `[AUDIO] Speaker stream: ...` logs its reported
latency at each connection. Lower values can underrun (crackle) on a busy board. Both are
documented in `.env.example`; nothing opens a stream or sends anything anywhere. Tests:
`SpeakerStreamTests` in `tests/test_v10_runtime.py` and `tests/test_pc_hardware.py`.

Dependency: `pywebrtc-audio==0.2.0` (Apache-2.0), included in the isolated
`requirements-pc.txt` environment. Native processing is imported only when
enabled. It adds no provider, cloud data flow, saved speaker embeddings or capture
stream. Gemini receives the processed mic audio; the existing raw recording path
is preserved. Monitor telemetry contains levels/clipping/probability only.

## Controls and fallback

A 4th-order 200 Hz high-pass (`BAYMAX_FAN_FILTER_HZ`, `0` disables) runs first,
before AEC/noise suppression/AGC. On ember-002 the fan put 95% of its noise power
below 211 Hz (29 Hz rotation, 88/193 Hz tones); filtering a recording removed
12.4 dB of noise while 300-3400 Hz speech changes by under 1 dB. It needs
`scipy` (installed with `librosa`); if unavailable or failing, cleanup continues
without it.

MVP sets `BAYMAX_INTERRUPTION_ENABLED=1` by default so the person can reclaim
the turn. Healthy DSP keeps cleaned microphone audio flowing during replies;
Gemini's interruption notification flushes queued audio before any co-delivered
reply text/audio is processed. New user transcription on that message is retained.
The receive loop updates the shared speaking flag used by initiation and Monitor,
so those consumers see generation in progress rather than a shadowed local value.
Set `BAYMAX_INTERRUPTION_ENABLED=0` in private `.env` for non-interruptible replies.
Saved overrides take precedence. The no-interruption option also applies
with enhancement off, but that existing raw path still skips buffered-playback
mic packets; failed DSP retains its protective half-duplex fallback.

Interrupted audio already buffered in the OS/device cannot be recalled by clearing
the Python queue. Acoustic echo/false interruptions and pause timing need live
robot checks; this change cannot establish that they caused a particular splice.

With interruptions off, `BAYMAX_MUTE_MIC_DURING_REPLY` defaults to `1`: AEC keeps
adapting on the captured audio, but Gemini receives silence while the speaker,
its device buffer and the 250 ms echo tail are active, so leaked robot speech
cannot become a later user turn. Speech during a reply is not heard; set it to
`0` to restore full-duplex input. Without the mute, leaked echo
can still be recognized as input. Matching a transcript afterward cannot remove
audio already processed by Gemini. We retain transcript warnings rather than
delete phrases a person may intentionally repeat. Echo subtraction is an acoustic
estimate and cannot guarantee separation of overlapping voices.

`run_pc.py` enables `BAYMAX_AUDIO_ENHANCEMENT=1` by default; Setup exposes the feature toggle and noise level. Set it to `0`
in private `.env` for the previous raw/half-duplex behavior. Robot/direct v10
startup keeps enhancement off unless explicitly enabled. Noise suppression level
`BAYMAX_NOISE_SUPPRESSION_LEVEL` is 0–3 (default 1, moderate). Restart to apply.
AGC is limited to 18 dB adaptive gain while the speaker is quiet. Positive AGC
boost is removed from the cleaned PCM while non-silent speaker output, its
reported device-buffer delay and a 250 ms echo tail are active. This prevents
the gain stage from re-amplifying residual echo after AEC. Healthy DSP still
passes near-end speech; it does not mute all microphone input during replies.
Quiet interruptions may receive less gain while the robot speaks and need
physical-device testing. These settings can reduce noise, but do not
repair microphone clipping. If native processing fails, raw capture continues
with a visible half-duplex failure state. In this failure mode only, microphone
input to Gemini is zeroed while rendered audio and its tail are audible, even
when the Python playback queue has drained. Raw session recording is unchanged.

Monitor's Microphone card shows raw/clean RMS dBFS, raw clipping percentage, speech
probability, gain, and whether listening during replies is active. Probability
is an estimate, not proof of a particular speaker or transcription accuracy.
Monitor also shows **Speaker echo protection: active** and the AEC delay hint.
Reported `gain_db` is the effective applied gain, while `agc_gain_db` retains
the native processor's gain for diagnostics. These are processing-status
indicators, not measurements that certify successful physical cancellation.

### Residual echo and double talk

`echo.py` adds a conservative check after WebRTC processing and gain protection.
It retains at most 600 ms of the actual speaker PCM and 40 ms of cleaned input,
in memory only. An FFT-based search looks across recent speaker audio for a
delayed match, then refines the match at the full sample rate. This search is
repeated as sound paths change; no fixed speaker/microphone distance is assumed.

Only correlation >=0.85 on two compatible delay estimates enables subtraction.
The fitted linear speaker component is reduced by 90%; unexplained audio is
retained. Strong double talk or a poor match passes unchanged. Quiet independent
voice mixed with dominant echo retains its independent component rather than
being discarded by a whole-frame speech/noise gate. The model is not proof of
speaker identity and can be wrong: nonlinear/distorted echo, strong reverberation,
clipping, unusually long delays and correlated human audio remain limitations.
Room movement, quiet interruptions and CPU cost need physical target testing.

Monitor reports residual state, correlation, estimated relative delay and
unexplained fraction. These are signal-model estimates, not an emotion, user
identity or recognition-accuracy score. Missing/stale references are passed
through; a residual-detector error disables this extra layer while retaining
healthy WebRTC. Set `BAYMAX_RESIDUAL_ECHO_ENABLED=0` in private `.env` and restart
to disable this layer for comparison without disabling primary AEC.

The debugger also checks a user-transcript prefix against Ember transcripts
updated within eight seconds. At least three matched words, 20 matched characters
and 80% prefix overlap are required for a warning. This is diagnostic only:
Gemini may already have detected an interruption before text arrives. No user
text is discarded, rewritten or blocked, including when a person quotes Ember.
No extra model requests, capture streams, files or identity inference are added.

## Speaking only when addressed (`addressing.py`)

With `conversation.only_when_addressed` (default on), the existing Gemini
session's proactive audio and `[WHEN TO SPEAK]` instruction judge conversational
intent. Direct questions do not require the name; ordinary pauses do not end an
established conversation. Current conversation and visual cues provide context,
not speaker identity; facing or looking toward Ember while speaking strengthens
the evidence but is not treated as proof by itself. The old silence/face-presence
random initiation is off by default; `BAYMAX_RANDOM_INITIATION=1` restores it for
demo testing. No extra classification call, capture or local gaze model.

The strict local transcript gate is now **off by default**. It suppressed two
"Tell me about yourself" requests after the user paused longer than 20 seconds.
Set `BAYMAX_ADDRESS_GATE=1` to opt in; `0` (default) lets generated replies play.
This trades deterministic name gating for model judgment: Gemini may still
misjudge background speech or noise. Mixed conversations need live verification.

When the optional strict gate is enabled, a reply plays only if:

- the person said "Ember" (or a usual mishearing: Amber, Emba, Embar, Emmer,
  Emperor; also split by transcription as "A mber" or "E mperor"; Hindi in
  Devanagari: एम्बर, एंबर, एमबर, ऐम्बर, अम्बर, अंबर, also split as "एम् बर"; Hindi in
  Urdu script, as Gemini often writes it: ایمبر, امبر).
  अंबर (امبر) is also the Hindi word for sky, so it can occasionally let an overheard
  reply through, as "amber" can in English, or
- they started speaking within `BAYMAX_FOLLOW_UP_SECONDS` (default 20) of Ember's
  last played reply ending, counting audio still buffered; noise tags such as
  `<noise>` are not speech, or
- it answers one of Ember's own messages: any `send_client_content` with
  `turn_complete=True` (fall alert, reminders, startup/initiation, audiobook
  notes), within 30 s, or the reply after a tool call from an addressed turn.

Gemini sometimes ends a turn without replying and answers in the next one, so
speech from a turn with no reply (nothing played or held) still counts for the
next reply for 10 s after the person's last words (`CARRY_S`). Only the last
such turn's words are kept, so talk Gemini stays quiet for cannot pile up over
many turns. A played reply clears it.

The decision is made at the first reply audio. Transcription can trail the reply,
so an undecided reply's audio and transcript are held and released in order if
the name arrives before the turn ends; otherwise they are dropped and
`[ADDRESS] Not spoken to; dropped reply: ...` is logged. Dropped replies never
reach the speaker, Monitor, dashboard transcript or session summary.

Limits: Gemini is not told a reply was dropped, so it may refer to it later. Tool
calls still run for overheard speech (only their spoken answer is dropped). A
conversation must start with the name. Transcription errors on the name drop a
reply that should have played; extend `NAME` in `addressing.py` if one recurs.

Inputs: Gemini's input transcription, reply audio/transcript and the messages
Ember sends on the existing session. Outputs: played or dropped replies, a log
line. No device, model, file or network request is added. Disable with
`only_when_addressed = false` in `device.toml` (also turns off proactive audio)
or `BAYMAX_ADDRESS_GATE=0` in `.env` (keeps them). Tests:
`tests/test_address_gate.py` (fake clock; the 2026-10-21 robot transcript through v10's
real receive loop).

## Validation and limits

### Long sessions: compression and resumption (`session.py`)

Google limits a Live session without compression to 15 minutes of audio (2 with
video; Ember sends a frame every 3 s), resets each connection after about 10
minutes, and re-reads the whole growing history every turn. `session.py` adds
`context_window_compression` (sliding window: trims to half once the context
passes `BAYMAX_CONTEXT_TRIGGER_TOKENS`, default 32000) and `session_resumption`.
The receive loop hands every resume handle and GoAway warning to
`on_server_message`; the next connection's config resumes that conversation. A
handle that fails once (no new handle arrives after using it) or is older than
110 minutes is dropped, so a bad handle cannot block reconnecting. An attempt that
never reached Gemini (DNS, network unreachable, connection refused, the websocket's
open timeout: an `OSError` before setup) is not a try (`session.not_tried`), so a
Wi-Fi drop still going on at the first retry, 0.3 s later, keeps the conversation.
`BAYMAX_CONTEXT_COMPRESSION=0` / `BAYMAX_SESSION_RESUMPTION=0` disable each.
No new data leaves the robot: both are settings of the existing Gemini session.
Any disconnect ends the session so `run()` reconnects. A failed mic send ends it at
once, and a send stuck for 5 s counts as failed (`MIC_SEND_TIMEOUT_S`): on a network
that goes silent, with no close and no reset, a send would otherwise wait until the
kernel gives up on the connection (about 15 minutes by default on Linux), with the
websocket's keepalive stuck behind it. The receive loop ends the session on any
closed connection (`connection_lost`, by exception type, whatever the close code) or
after 3 errors in a row with no message between them; a one-off error handling a single
message keeps the session. The socket is aborted when a session ends, because leaving
`connect()` otherwise waits on the same silent network.

**Fast reconnect.** When every error that ended the session is a lost connection
(`connection_lost`: Google's reset about every 10 minutes, a dropped network, a stuck
send), another `OSError` (DNS or "network unreachable" from `connect()` while the Wi-Fi
is still down) or a GoAway handover, `run()` reconnects without touching the audio
devices. PortAudio's errors are not `OSError`s, so device errors still get the reset.
Before, every session end waited 2 s, then reset PortAudio with a 5 s `time.sleep` that
blocked the event loop and chose the devices again: at least 7 s with the mic closed. A
handover from a connection that lasted 60 s (`QUICK_SESSION_S`) reconnects at once;
otherwise `run()` waits 0.3 s (`RECONNECT_DELAY_S`), twice as long for each connection in
a row that ended within 60 s of starting, up to 4.8 s (`RECONNECT_MAX_DELAY_S`): 0.3, 0.6,
1.2, 2.4, 4.8, 4.8 s. A connection that lasted starts the count again. On Wi-Fi that drops
every 45 s, each reconnect waits at most 4.8 s and never resets the devices, which can't
help a network. Any other error (a device or PortAudio error) runs the full reset as
before, but awaited, with the PortAudio calls and the device query on a worker thread.
Those worker-thread PortAudio calls are untested on Windows (WASAPI sets up COM per
thread); the reset only runs after a device or unknown error.

**GoAway.** Gemini warns (GoAway) shortly before it resets a connection. Instead of
waiting to be cut off, possibly mid-sentence, `between_turns()` ends the session at the
next quiet moment: Gemini owes no reply and isn't producing one, nothing is queued for
the speaker, nobody has spoken for 1 s and none of Ember's own messages awaits its
answer. It waits for a resume handle that includes the last turn
(`session.handle_received_at`), at most 3 s after that turn or the GoAway
(`GO_AWAY_HANDLE_WAIT_S`), and `run()` reconnects with the newest handle. A handle from
before this connection was used to set it up, and `session.py` offers a handle only once,
so moving with it would start the conversation over: Ember waits until Gemini has sent
this connection a handle of its own (or closes it). A handover from a connection shorter
than 60 s counts as a quick failure, so GoAways soon after every setup (a backend
draining, say) back off instead of reconnecting about once a second. If no quiet moment
comes in time, Gemini's own close is a lost connection like any other.

The reply in progress is abandoned at every session end (`_abandon_reply`): its queued
audio is cleared, so it can't play after the reconnect with the mic muted. Ember's own
messages (a fall check-in, a family message, a reminder) that reached Gemini are sent
again on the next connection if their reply was still playing, or hadn't come yet and
the message is under 30 s old (`AddressGate.abandon`), unless the person has spoken
since; one already heard isn't repeated. If that resend fails too (the new connection
drops before it), the message stays first in line for the connection after
(`_send_cut_off_prompts`). A message that never reached Gemini is left to its sender's
own retry, so nothing is sent twice; both fall alerts now retry themselves. An
audiobook isn't cleared: it goes quiet during the reconnect and resumes where it
stopped.

Tests: `tests/test_session.py` (fake handles; full config validated by the SDK) and
`ReconnectTests` in `tests/test_v10_runtime.py` (the real send and receive loops on
the real SDK session over a local websocket, closed with each close code or left
unread; a cut-off fall check-in through the real `run()`, also when its resend fails;
the fast path, the backoff, the full reset off the event loop, the GoAway handover and
a retry that never reached Gemini keeping the resume handle through the real `run()`),
and the resend rules in `tests/test_address_gate.py`. Live
pending: the reconnect time and Google's GoAway timing on the robot.

### Gemini Live model and fallback (`live_models.py`)

Ember was built and tested on `gemini-2.5-flash-native-audio-preview-12-2025` (the
runtime's `MODEL`). Google's deprecations page lists it for shutdown as early as
2026-11-17 (the exact date is announced later), replaced by `gemini-3.8-live`.

- **Order.** `BAYMAX_LIVE_MODELS` lists the Live models to use, comma-separated, in order;
  by default `MODEL`, then `gemini-3.8-live`. A `models/` prefix is dropped; an entry that
  isn't a model name is skipped with a warning.
- **Moving on.** `run()` connects with the first model, and at once with the next when this
  API key can't use it: Google refuses to set up a fresh connection (close 1007 or 1008: the
  model is not found or not supported for Live, or rejects a setting; refusing a resumed
  one can be about its resume handle, so that one just reconnects fresh), or the connection
  ends before Gemini finished a turn on it with Google saying the model is gone ("is not
  found for API version", "not supported for bidiGenerateContent", "no longer available").
  A close frame carries at most 123 bytes of reason, so these words are matched where
  Google's messages begin. One `[SESSION] ... can't be used with this API key (...);
  switching to ...` line is logged and shown in the Monitor. The new model is kept until
  Ember restarts. The old model's resume handle is dropped (a handle belongs to the model
  that issued it), so the conversation starts fresh.
- **Quota closes don't move.** "1011 You exceeded your current quota" is also what a
  per-minute or concurrent-session limit says, and those pass in seconds: whatever its close
  code, `run()` backs off and retries the same model, as before the list. One such close on
  2.5 used to keep Ember on the untested 3.8 until it restarted. A key with no quota at all
  for the first model therefore stays on it, so keep 2.5 first until the project has 3.8
  quota.
- **No model works.** Once every model has failed like that in a row, `run()` waits as after
  any lost connection (0.3 s, doubling up to 4.8 s) and tries the next model again, so Ember
  comes back with whichever model returns. No fast spin.
- **Never** for a dropped network, Google's routine resets, "1011 internal error", or any
  error once Gemini has finished a turn on the connection: those reconnect with the same
  model, as before.
- **Startup.** `run_pc.py`'s key check (`gemini_preflight.py`) accepts the first model of the
  list that Google lists for the key, so a retired `MODEL` doesn't stop startup while the
  next is available. It can't check Live quota: that is only known when connecting.
- **Settings per model** (`adapt`, also used by `evals/live_eval.py --model`). Gemini 2.5
  gets the config unchanged. Gemini 3 and later: no `thinking_config` (Google: omit it,
  thinking is built in), and every function Ember declares without a behavior is sent as
  `BLOCKING`, as 2.5 runs them (3.x defaults to `NON_BLOCKING`). The opt-in eye tool's
  explicit `NON_BLOCKING` is kept.
- **Not adapted, to check live on 3.8:** proactive audio is always on there, so
  `only_when_addressed = false` can't turn it off; a turn includes every camera frame by
  default (Ember sends one every 3 s); voices and tools are untried.
- **No notes without `turn_complete` on Gemini 3 yet.** Client content sent with
  `turn_complete=False` (recalled memories from `between_turns()`, the family's profile
  update) makes the server wait for more input, and whether 3.x still answers the person's
  next words after one is untested. `live_models.takes_quiet_notes()` is false on Gemini 3
  and later, so there Ember neither looks up nor sends recalled memories, and a profile
  update waits for the next session's instructions; the 50 newest memories are still in
  every connection's prompt. Lift it once `python evals/live_eval.py --model
  gemini-3.8-live --only memory-note-then-speech` passes. One message can carry several parts: the receive loop
  plays and prints every part in order, and a model turn without parts no longer drops
  the rest of its message (its turn end).
- **Today (2026-11-06):** Ember's keys get "1011 You exceeded your current quota" on
  `gemini-3.8-live` (their projects have no 3.8 quota yet) while 2.5 works, so 3.8 has not
  run with Ember. Keep 2.5 first. Once the key's project has 3.8 quota and
  `python evals/live_eval.py --model gemini-3.8-live` passes, put it first:
  `BAYMAX_LIVE_MODELS=gemini-3.8-live,gemini-2.5-flash-native-audio-preview-12-2025`.
- **Data flows:** none new. The same session data goes to Google whichever model is used.

Tests: `tests/test_session.py` (the list; the real SDK's errors from a local websocket closed
as Google closes it; switching, keeping and each round; the setup message sent for each
model), `ReconnectTests` in `tests/test_v10_runtime.py` (through the real `run()`: a model
refused at setup, a model gone before the first turn, a quota close retrying the same model,
every model failing, ordinary errors, a model that answered), `tests/test_gemini_preflight.py`,
`tests/test_live_eval.py`, and the several-parts and no-parts messages in
`tests/test_address_gate.py`. Live pending: 3.8 itself.

### Local waiting acknowledgement (MVP)

Off by default (testers found the fixed "hmm" unsettling); set
`BAYMAX_WAIT_CUE_ENABLED=1` to turn it back on. When enabled, after more than
two seconds since the latest user-transcription fragment, one short local
“hmm” is played while waiting for the real response. It does not repeat during
that wait. This uses the same wait estimate as the eyes, not an acoustic
end-of-speech detector. A fragment arriving later postpones/cancels the cue.

The clip goes through the existing 48 kHz speaker callback and is included in
the AEC render reference. No second output stream or model call is introduced,
and nothing is programmatically inserted into transcripts or memory. Real
reply PCM takes precedence on the next output callback; detected near-end
speech from the cleaned microphone PCM, interruption/end, reconnect and stop
cancel the cue. Audio already buffered by the OS/device cannot be recalled.

Working echo cancellation is required; if it is unavailable or fails, the cue
is silent and ordinary conversation continues. Set `BAYMAX_WAIT_CUE_ENABLED=0`
in private `.env` to disable. Direct `run_pc.py` keeps it off unless enabled.
The bundled synthetic local voice differs from Gemini; see
[asset provenance](assets/README.md). Speaker volume, residual echo, speech
interruption and perceived timing require a live robot check.

`tests/test_waiting_cue.py` has 12 fake-clock/PCM checks, including the actual
v8 output and microphone callbacks, echo-reference delivery, priority/reset,
one-shot behavior, disabled mode and bad-asset fallback. The guarded hooks are
normalized by upstream parity tests; prompt, models, sample rates, raw recording
and disabled audio behavior remain covered.

### Ember's voice volume (`volume.py`)

The person can change how loud Ember speaks by asking: "Ember, speak louder",
"softer", "set your volume to 8", "how loud are you?". Gemini calls the
`set_voice_volume` tool (louder, quieter, default, status, or an exact level), and
Ember's short confirmation already plays at the new level.

- **Scale:** 1 to 10. Level 7 is the original loudness and leaves the audio byte for
  byte unchanged, so nothing changes until someone asks. Each step is clearly audible:
  level 1 is about -14 dB, level 10 about +6 dB.
- **Above 7:** gain with a soft limiter from 70% of full scale, so loud passages are
  rounded off instead of clipping harshly. Quiet speech gets the full gain.
- **Never muted:** the lowest level is 1, so a fall check-in can't be silenced by
  accident. A level change ramps across one speaker block, so there's no click.
- **What it changes:** Ember's voice only (Gemini's replies and the waiting cue), on the
  speaker callback, before audiobooks are mixed in and before the echo canceller's
  reference. Echo cancellation therefore keeps matching what is played. Audiobooks,
  radio and recordings keep their own volume (the `volume` action of the audiobook or
  `listen` tool). The system speaker level is not touched.
- **Saved:** `voice_volume.json` in `BAYMAX_DATA_DIR` (next to the code by default), so
  the level survives restarts. `BAYMAX_VOICE_VOLUME` (1-10) in `.env` sets the level
  before anyone has changed it; a saved level wins. An unreadable file or value falls
  back to 7; if saving fails the new level still lasts until restart.
- **Core, like the eye tool:** offered with the toolkit off too.
  `BAYMAX_VOICE_VOLUME_TOOL=0` stops offering it; a level already saved still applies.
- **Data flows:** none. Nothing is sent anywhere and no device is opened.

Tests: `tests/test_voice_volume.py` (levels, saving, the gain and limiter, the order in
the real speaker callback, and the tool offered and answered with the toolkit off).

### "Ember disengage" / "Ember engage" (`engage.py`)

Say "Ember disengage" and Ember gives a short goodbye, shows sleep eyes and stops
hearing the room. Saying "Ember", or anything else, gets no response. Say "Ember
engage" and it says a short hello and listens normally again. While engaged
there is no wake word: people talk to Ember without saying its name.

- **Disengage** is matched locally in Gemini's transcript of the user
  ("Ember, disengage.", "amber dis-engage"; in a Hindi conversation also in
  Devanagari, "एम्बर डिसएंगेज", "अंबर, डिस-एंगेज", using the address gate's Hindi
  spellings of the name) through `runtime_monitor.transcript_listeners`.
  Ember's own transcript never counts. The name also counts in Urdu script
  ("ایمبر، disengage"), but only with the word in Latin or Devanagari script: how
  Gemini writes "disengage" in Urdu script has not been seen live yet, so a fully
  Urdu-script transcript is not matched (Gemini still says its goodbye, but the mic
  stays open).
- **While disengaged**, the mic gate (first in `processing.input_gates`, ahead of
  the audiobook) sends Gemini silence, as during audiobooks. The same
  echo-cancelled mic audio goes only to an offline Vosk listener
  (`features/toolkits/wakeword.py`, vocabulary "ember", "engage", "disengage").
  It wakes only on a confident final "ember engage" pair. No new mic stream is
  opened, and the Vosk model is shared with the audiobook listener and loaded
  on first use. The camera's 3 s frames still go to Gemini unchanged.
- **Engage** sends Gemini an `[ENGAGED]` note through the live session so it says
  hello. `[ENGAGE AND DISENGAGE]` in the MVP persona prompt asks for the short
  goodbye and hello. The upstream v8 prompt is unchanged.
- **Ember still speaks first for safety and for things the person set up:** a
  due reminder or a possible-fall check-in engages Ember again so the person can
  answer, and pauses any audiobook, radio or recording, which withholds the mic
  too. Eye-contact and startup greetings stay quiet.
- **Lifetime:** the state survives Gemini reconnects but not a restart, which
  always starts engaged.
- **Fallbacks:** without audio cleanup (`BAYMAX_AUDIO_ENHANCEMENT=0`), there is
  no mic gate, so Ember says disengage is unavailable and keeps listening. If
  the Vosk library or model is missing, Ember engages again at once and says
  so, so it can never be left unable to hear. That is always the case on a Mac
  installed from `requirements-pc.txt`, which skips Vosk (no macOS wheel), so
  disengage is not available there. While the model is still loading, the mic
  stays withheld.
- **Disable:** `BAYMAX_ENGAGE_WORDS=0` (no prompt section, transcript matching ignored).
- **Data flows:** none new. While disengaged, less audio goes to Gemini.
- **PR #20 integration:** retains its reminder occurrences/follow-ups, audiobook
  ducking and split wake-phrase matching, 400 ms MVP default and default-off strict
  address gate. Both built-in v10 and optional toolkit fall check-ins re-engage
  Ember. A failed audio-cleanup processor also re-engages with an unavailable note;
  a new disengage request is refused until cleanup is healthy again. Camera frames
  and existing raw recordings are unchanged: this is not a privacy-off mode.
- **Tests:** `tests/test_engage.py` (transcript matching, gate and audio cleanup,
  wake rules, missing model, no cleanup, disabled, prompt, eyes, live-session
  note) and `tests/test_toolkit_engage.py` (no greetings; reminders and falls
  engage Ember).
- **Live checks pending:** Gemini's transcription of "Ember disengage" in a real
  room, Vosk accuracy for "Ember engage" at a distance and with TV or
  conversation nearby (false wakes), and the goodbye/hello wording. The
  `[WAKE] heard ...` log helps with tuning.

### Audio processing

Offline checks cover continuous 960- and 1024-to-480 framing, speaker reference,
buffer bounds, disabled/failure behavior, short-speech configuration, mic routing
during playback and untouched raw recordings. Native synthetic steady-noise RMS
fell from 502 to 255 with approximately 0.5 ms processing per 21 ms input block
on this Windows PC. The SDK accepts the activity configuration. These synthetic
results do not establish real speech accuracy or echo-cancellation quality.
Actual quiet yes/no, interruptions, speaker leakage/delay and noisy-room behavior
need user testing. Residual echo/noise can still trigger detection; check native
AEC status and microphone clipping/levels in the Monitor.

Primary references: [binding API](https://github.com/strands-labs/pywebrtc-audio)
and [Gemini activity detection](https://ai.google.dev/api/live#AutomaticActivityDetection).

Additional opt-in native regression, using generated signals only:

```bash
python tests/aec_synthetic_probe.py
```

Windows/Python 3.12.15 with pinned pywebrtc-audio 0.2.0: the reproducible
speech-band echo case improved from 11.0 dB to 26.1 dB net RMS reduction.
Independent voiced/syllabic near-end input remains nonzero during double talk
(clean RMS 750.3 with user RMS about 1200). This is not a real speech-recognition
score or a LattePanda acoustic measurement. No device, recording or provider
request is used. The earlier noise-only comparison is historical DSP evidence.

Ten additional offline regressions in `tests/test_residual_echo.py` cover delayed
echo, polarity reversal, changing delay, quiet/strong double talk, missing/stale
references, reset/bounds, error isolation, prefix matching and warning expiry.
The native synthetic probe still reports 26.1 dB echo reduction and non-muted
voiced double talk with the conservative residual layer enabled; it does not
establish an additional reduction on every signal or on the physical robot.
