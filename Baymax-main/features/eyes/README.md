# LED eyes

MVP drives two daisy-chained MAX7219 8x8 matrices through the LattePanda onboard
Leonardo running StandardFirmata, or through hardware SPI (`/dev/spidev3.1`) on the
Orange Pi 4 Pro, with the same expressions and behaviour on both. No servo, firmware upload, extra microphone/camera
stream or separate provider request is added; expression commands use the existing
Gemini Live session. Python dependency: `pyfirmata2==2.5.1` in
the desktop/Debian x86_64 requirements profile.

## Behavior

- Ordinary listening and speaking use the standard open eyes with the existing
  blink animation. Alternate neutral eye sizes and left/right narrowing are not
  used. Explicit expressions and status animations retain their supplied shapes.
- Automatic expressions recognize natural sentence openings (greetings,
  encouragement, interest and questions), including curly apostrophes and large
  transcription chunks. Outside the medical override below, explicit expression
  requests win and stay fixed for that reply. Automatic expressions can change
  across successive streamed sentences; repeated cues do not retrigger. Happiness,
  success, excitement and uncertainty cues take precedence over a question mark
  within a sentence. Thinking, fall alerts,
  reconnect and sensor states keep their priority. Gemini is told that its
  natural wording guides this local animation, so it can express
  conversational intent without a command, hidden metadata, extra provider
  request or tool response.
- Clear pain or medical-emergency language in the person's preceding turn forces
  surprised/exclamation eyes for Ember's reply, even if the reply begins with a
  sad cue. This is a local visual reaction only; it does not diagnose, change the
  wellness response, enable fall detection or contact emergency services.
- Offline regressions in `test_eyes.py` and `test_eyes_auto.py` cover motion,
  playback completion, rendering, streaming cues and explicit-request priority.
  Actual matrix speed, naturalness and speech synchronization need a robot check.

- Confused means "Ember is down but recovering by itself": from right after the
  MVP launch lock (Setup, model loading) until Gemini connects, and again while
  it reconnects. A fall alert still overrides with surprise. A background
  check (`network.py`, every 3 s, ~0.5 KB TCP connect to the Gemini host, no
  audio) shows confused 6-9 s after the route to Gemini is lost instead of
  waiting 20-60 s for the Live connection to fail; `BAYMAX_EYES_NETWORK_WATCH=0`
  disables it.
- Sleep eyes also show for as long as Ember is disengaged ("Ember disengage" until
  "Ember engage"; see conversational_ability/README.md).
- Sleep eyes, dimmed from brightness 2 to 1, after `BAYMAX_EYES_SLEEP_AFTER_S`
  (default 60; 0 disables) without
  conversation while connected, counted from when Ember's voice finishes playing.
  Repeated empty-playback monitor heartbeats do not reset this timer; only the
  first playback-complete notification for a finished reply counts as activity.
  Speech, thinking, replies, tool calls and expressions reset the timer and wake
  the eyes to normal blinking; (re)connecting starts it fresh. Down/X/fall faces take priority.
- X (dead) eyes mean "unplug and replug", the only fix a user has. Shown when a
  sensor goes quiet while connected (microphone or speaker over 5 s, camera over
  10 s, or never reported 30 s after connecting; camera only if configured;
  `BAYMAX_EYES_SENSOR_WATCHDOG=0` disables), when Gemini has
  been unreachable for `BAYMAX_EYES_DEAD_AFTER_S` (default 120; 0 disables), and
  left on the matrices after the third consecutive start that never connected
  (counted in `.eyes-failures`). Earlier failed starts leave confused eyes while
  systemd retries; a deliberate stop still blanks them. If the process is
  killed outright (signal, core dump, stop timeout), the service's
  `ExecStopPost` runs `show.py --after-exit`, which draws X. `show.py <face>`
  draws any expression by hand. Existing robots must rerun
  `python3 devops/mvp/install_autostart.py` once to get the hook.
- Open hollow eyes with random calm blinks are the default, including while
  listening and speaking. Patterns follow the user's supplied demo; timing is
  slower than the demo's (`BLINK_GAP_S`, `BLINK_CLOSED_S`): 2.5–5s blink
  opportunities, 75% chance, eyes shut 0.12s, with a rare double blink.
- User transcription starts/restarts the wait. Thinking appears only when
  **more than two seconds** have elapsed since the last nonblank fragment.
  This is a transcription-based wait estimate, not acoustic end-of-speech.
- The supplied thinking animation moves one 2x2 dot through left/center/right
  on the left eye, then the right eye, at 0.75s per frame. Brightness is 3.
- First reply audio clears thinking and shows the requested expression, or normal
  blinking when none was requested. Turn completion,
  interruption, reconnect and stopping also clear the wait. Late user
  transcription during reply audio does not restart thinking.
- All supplied expression bitmaps are included: happy, sad, excited (different
  left/right eyes), four-frame confused spinner (100ms), surprised, curious,
  love, sleep (left closed; right bubble at 250ms), and dead/X eyes. Surprised,
  curious and love retain the demo's random fast blinks. Dead/X eyes are a
  requested theatrical expression, never a health or fall conclusion.
- Expressions are chosen locally by `auto.py` (default): when Ember starts a
  reply, an explicit request in what the person just said ("make a sad face",
  "show me happy eyes", "play dead", "go to sleep") picks that expression,
  including change/set/switch commands targeting eyes, face or expression;
  the common transcription substitution of "too" for "to" is accepted.
  Otherwise clear cues in successive sentence openings guide the expression;
  a neutral or unfinished tail retains the previous expression. A reply with
  no cues keeps normal blinking. Updates follow transcript arrival, which may
  lead spoken audio; exact speech alignment is not established by offline tests.
  It reads the runtime monitor's transcripts (`transcript_observer`); nothing
  is sent anywhere. Reason: Gemini sometimes spoke a whole reply twice after a
  mid-reply `set_eye_expression` call, even NON_BLOCKING with a SILENT result.
  `BAYMAX_EYE_TOOL=1` restores the Gemini tool instead (described below).
- Expressions persist through generation completion while reply audio remains
  buffered. The existing runtime heartbeat clears them when playback drains;
  new turns, interruption/reconnect/stop and a 30s safety expiry prevent stuck
  faces. Thinking overrides an expression until the first reply audio arrives.
- The enabled fall detector's actual alert dispatch forces surprised eyes above
  thinking and model requests, until check-in playback ends or a 15s safety
  timeout. This neither enables detection nor sends an external alert.

`run_mvp.py` owns the eye thread for the conversation lifetime. Existing
`runtime_monitor.emit()` events and nonblank user-transcription notifications
request state changes only; one render thread owns all serial writes. The
conversation/audio loop never waits for an animation or a Firmata transfer.
The eyes use existing monitor events. The optional waiting-audio feature adds
guarded speaker/microphone hooks. The base prompt, models and sample rates stay
unchanged; the explicit eye-tool instruction is appended to session configuration.

## Hardware and configuration

Arduino D6 -> eye 1 DIN; D13 -> shared CLK; D10 -> shared CS/LOAD;
5V/GND -> both matrices; eye 1 DOUT -> eye 2 DIN. D9 is unused.
The user's current mounting uses left device 1 and right device 0, with no flips.

### Orange Pi 4 Pro (SPI3)

Header pin 2 (5V) -> VCC, pin 6 (GND) -> GND, pin 19 (SPI3 MOSI) -> eye 1 DIN,
pin 23 (SPI3 SCLK) -> shared CLK, pin 26 (SPI3 CS1) -> shared CS; eye 1 DOUT -> eye 2
DIN. The 4 Pro's 3.3 V logic drives the 5 V MAX7219 reliably at 1 MHz (tested for
hours on ember-002), so no level shifter is used. Linux spidev only: no Python package.

The `spi3-cs0-cs1-spidev` overlay (`overlay_path=allwinner`, `overlays=...` in
`/boot/dietpiEnv.txt`) creates `/dev/spidev3.1`; udev rule
`/etc/udev/rules.d/60-ember-eyes.rules` gives it to `dialout`, so Ember opens it
without sudo. OPI4-Flash images set both. When `/dev/spidev3.1` exists it is the
default port; a `BAYMAX_EYES_PORT` starting with `/dev/spidev` always uses SPI.

A matrix that shows a fixed grid or a couple of lines, even in the chip's own
display test (register 15 = 1), has its socketed 8x8 LED block in the wrong way
round: if shutdown and intensity commands still blink and dim it, the MAX7219 is
receiving fine. Pull the block out, turn it 180 degrees and reseat it. On ember-002
this looked like a 3.3 V logic problem, but speed, SPI mode and a 3.3 V supply
changed nothing; reseating fixed it.

### LattePanda (Firmata)

Default serial path:
`/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00`.
The user must have serial access (normally membership in `dialout`). Run as the
normal desktop user, not with sudo. Stop standalone eye demos before starting
MVP so only one process owns the Arduino.

Eyes default on for the Linux MVP launcher and off on Windows/macOS. Add to
the local `.env` to override; Setup preserves these values:

```dotenv
BAYMAX_EYES_ENABLED=1
BAYMAX_EYES_PORT=/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00
```

Use `BAYMAX_EYES_ENABLED=0` to disable. Other OS profiles can install pyfirmata2
separately if explicitly enabling eyes. Missing dependency, denied access,
missing hardware or a serial/render error logs `[EYES] Unavailable` and leaves
conversation running. After fixing a connection, restart MVP to reconnect eyes.
Stopping requests shutdown on both matrices and closes serial; a disconnected
device cannot be guaranteed to receive shutdown. No physical second-eye fix is
claimed by this integration.

## Tests

`python -m unittest discover -s tests -p test_eyes.py` checks the strict 2s
boundary, transcription reset, fast replies, late transcripts, reconnect/end,
normal blinking during speech/listening, six single-dot frames, observer wiring
and failure isolation, shutdown, and missing/disabled hardware. A fake board
decodes the actual MAX7219 bitstream to check chain addressing, row orientation,
brightness, diffing/retry and absence of servo commands. The SPI driver's 32-bit
transfers are decoded the same way (same registers, mode 0, 1 MHz), and a supplied
test board never opens the real SPI device.

Physical Firmata speed, second-matrix behavior and combined Gemini/audio/eye
operation on ember-002 still require live verification. Pattern hold durations
exclude transfer time; this bit-banged serial path does not guarantee exact
0.12s physical blink duration. No keys, transcripts or biometric information are
sent to the Arduino.

With `BAYMAX_EYE_TOOL=1`, the eye tool uses the existing Gemini tool dispatcher, independently of public
location/weather enablement. Inputs are a validated expression enum; output is
`requested` or a nonfatal unavailable/invalid result, not confirmation that the
physical display changed. Gemini sees the declaration/instruction and tool result
within the existing session; no extra model request or audio analysis is added.
Monitor records the validated expression name. No new dependencies are required.
Disabling eyes also removes the declaration. Hardware failures leave speech active.

`tests/test_toolkit_eyes.py` checks declaration enablement, unchanged base config,
idempotence, argument validation, disabled/disconnected hardware and real async
dispatch with preserved call IDs. `tests/test_eyes.py` also covers supplied
animations, fall priority, late fragments, buffered playback and expiry/reset.
