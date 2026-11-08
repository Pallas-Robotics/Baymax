# Ember for Orange Pi — integrated v10 MVP

This repository combines PR #7 (setup/device compatibility), #11 (toolkits and
feature folders), and #12 (Conversational Ability). `run_mvp.py` starts their
shared runtime, `realtime_gemini_10.py`, through the existing key/model preflight
in `run_pc.py`. (The original MVP, PR #15, ran v8; this copy runs v10, which adds
fall detection, robot-started conversations and `device.toml` settings.)
Targets: the Orange Pi 4 Pro (64-bit DietPi/Debian 13) and the Orange Pi 5 Max
(Armbian Debian 13), ARM64/aarch64, with working audio, camera and serial permissions.
Use a desktop session for the supplied Setup/Monitor workflow.
Actual target-device validation is pending. See
[the boards and what differs](../docs/DEMO_DEVICES.md) and
[Orange Pi setup](ORANGE_PI_SETUP.md) (pose, pinning, memory, the optional NPU).

## Included features

- One-time local configuration of Gemini key, microphone, speaker and camera.
  Choose explicit devices; use **No camera** for audio-only operation.
- Echo cancellation, noise reduction, automatic gain and responsive turn-taking,
  with the existing safe fallback when the audio processor cannot initialize.
- Location/weather and opt-in public information tools, monitor, device tests,
  graceful stop and the existing optional CPU fall-detection worker.
- Organized implementations in `features/conversational_ability/` and
  `features/toolkits/`. Memory/person packages remain reserved namespaces.
- Optional automatic startup at each Linux desktop login using a dedicated
  user service. This does not replace the existing production Ember service.
- LED eyes using the user's normal blink and six-position thinking-dot patterns.
  Normal blinking continues during listening/speaking; thinking begins only
  after more than 2s since the latest user-transcription fragment. Reply audio,
  turn end/interruption and reconnect restore normal blinking. No servo.
- Optional (off by default; `BAYMAX_WAIT_CUE_ENABLED=1`): one short local
  “hmm” once per wait over 2s, through the existing speaker stream/AEC reference.
  Reply audio or detected near-end speech cancels it. Requires working echo cancellation.
  It is a bundled synthetic cue, not Gemini speech or another model request.

PR #8 people awareness and #13 Memory Palace are outside this MVP. v10's own
conversation memory (summaries saved while Ember runs) is on: see
[ORANGE_PI_SETUP.md](ORANGE_PI_SETUP.md#memory).
The v10 baseline includes reminders, audiobooks and app integration; the later
conversation, engage/disengage and vision performance improvements are retained.
See [LED eye setup](features/eyes/README.md) for serial access, enabling/disabling
and wiring. Physical second-matrix behavior still needs validation with MVP.
v8 remains in the repository but this MVP runs v10. The strict local
name/20-second reply gate is off by default; Gemini judges conversational
intent. "Ember disengage" enters voice sleep and "Ember engage" wakes it.
Camera frames and local raw recordings continue during voice sleep.
Transcripts are not uploaded to the fleet cloud; Gemini still receives
audio/video while engaged.

## Install and configure

- **Orange Pi 4 Pro:** the simplest path is a personalized SD image from
  [OPI4-Flash](../devops/opi4-flash/README.md), which installs everything below and
  updates itself from the `release` branch. The steps below also work on Debian 13.
- **Orange Pi 5 Max:** follow [ORANGE_PI_SETUP.md](ORANGE_PI_SETUP.md) (the same
  steps through the clickables, plus the optional NPU setup).

Use a complete checkout of this repository's `release` branch (or an extracted ZIP),
with `Baymax-main/` and `devops/` together. From the project root:

On a fresh device, stop any existing Ember runtime and standalone eye demo, then download this branch:

```bash
sudo apt update
sudo apt install -y git
git clone --branch release --single-branch https://github.com/Pallas-Robotics/Baymax.git ~/Baymax
cd ~/Baymax
```

For an existing clean checkout of `release`, use `git pull --ff-only`.
Preserve local robot edits and private configuration before updating; a pull on another branch does not select this release.
An existing Orange Pi checkout of JoeKojima/Baymax switches with
`git remote set-url origin https://github.com/Pallas-Robotics/Baymax.git` (keep
`.models/pose_npu`: the checked NPU models stay valid while rknn-toolkit-lite2 and
librknnrt are unchanged).
Run the commands below as the desktop user; the prerequisite script requests
administrator access for system packages. Do not run Ember itself with sudo.

```bash
# Debian 13: install system libraries with this helper.
# DietPi/other Debian versions: use the equivalent packages described in docs/DEMO_DEVICES.md;
# the Debian 13 helper intentionally rejects other distributions.
bash "devops/clickables/Linux/0 - Debian Prerequisites.sh"

# Installs private Python 3.12, dependencies/model and runs offline checks.
# Then opens Setup; save explicit devices/key and close Setup with Ctrl+C.
bash "devops/clickables/Linux/1 - Setup.sh"

# Starts MVP. If configuration is incomplete, Setup opens once and saving
# explicit devices plus a key starts the runtime automatically.
Baymax-main/.venv/bin/python Baymax-main/run_mvp.py
```

Save choices once. Subsequent MVP launches reuse `.env` without opening Setup.
Audio choices retain device names and host APIs to handle index reordering.
Linux camera selection retains its explicit OpenCV index; keep the same USB
ports and verify camera enumeration across reboot. A disconnected device or
rejected key is an error, not a reason to silently choose a different device or
reopen setup. Explicitly change configuration with `run_mvp.py --setup`.
Do not start a second runtime from the Start Ember shortcut at the same time.

Starting Ember sends microphone audio and enabled camera frames to Gemini and
records session audio locally. Preflight sends only key/model metadata requests.
Toolkits contact their documented providers; public tools and fall detection
remain opt-in. Setup device discovery opens cameras to check availability;
device-test buttons deliberately record a short sample/play a tone/take a snapshot.
No extra capture streams are added to the runtime.

## Gemini model

Ember talks to `gemini-2.5-flash-native-audio-preview-12-2025`. Google shuts it down as
early as 2026-11-17 (the exact date is announced later); its replacement is
`gemini-3.8-live`. Ember tries the Live models in `BAYMAX_LIVE_MODELS` in order (default:
2.5, then 3.8) and moves to the next when the key can't use one (not found or retired),
until it restarts. A quota close only retries the same model: a rate or session limit says
the same and passes in seconds. The key's Google project needs Live quota for `gemini-3.8-live`
(check https://aistudio.google.com/rate-limit for the project that owns the key): on
2026-11-06 Ember's keys had none ("1011 You exceeded your current quota"), so 3.8 has not
run with Ember yet and 2.5 stays first. Once the project has 3.8 quota and
`python evals/live_eval.py --model gemini-3.8-live` passes, put 3.8 first in
`Baymax-main/.env` and restart Ember:

```bash
BAYMAX_LIVE_MODELS=gemini-3.8-live,gemini-2.5-flash-native-audio-preview-12-2025
```

What changes between the models and what is still to be checked on 3.8:
[conversational ability](features/conversational_ability/README.md#gemini-live-model-and-fallback-live_modelspy).

## Automatic startup

The instructions below require a working Linux desktop/user session. A headless
DietPi service is the [OPI4-Flash](../devops/opi4-flash/README.md) image's path;
these steps do not install one.

Stop any manually started runtime with Ctrl+C first. Then, as the robot's desktop
user (`meowmax` on ember-002) in the robot's desktop session, without sudo:

```bash
python3 devops/mvp/install_autostart.py
# Deliberately start now, or log out and back in to exercise autostart:
bash devops/mvp/start-session.sh
```

If an earlier install reports `Loaded: bad-setting`, update and regenerate the
service (the installer fixes the previously quoted WorkingDirectory):

```bash
git pull --ff-only
python3 devops/mvp/install_autostart.py
systemctl --user reset-failed ember-mvp.service
bash devops/mvp/start-session.sh
systemctl --user status ember-mvp.service -l --no-pager
```

The installer writes `ember-mvp.service` and `ember-monitor.service` under the
user's configuration `systemd/user/` directory, `ember-mvp.desktop` under
`autostart/`, and Monitor launchers in Applications and the localized Desktop.
It reloads the user service manager and enables both services at `default.target`
but does not start capture. At desktop login, the autostart entry imports the
desktop environment and starts both services.
The service uses the installed private Python directly: no package installation,
upgrade or model-install prompt is performed on subsequent boots.

Boot-to-conversation without intervention requires Debian desktop **automatic
login** for `meowmax`. This installer does not change display-manager settings,
enable lingering, or configure a headless boot service. A remote-desktop session
alone does not establish that boot auto-login is configured. Do not enable this
alongside an already running production Ember service.

```bash
systemctl --user status ember-mvp.service
journalctl --user -u ember-mvp.service -b
systemctl --user stop ember-mvp.service
```

SIGTERM follows the runtime's existing Ctrl+C cleanup path, with up to 180s for
session cleanup. Failed starts retry after 15s; saved settings are retained.
To remove autostart, disable and stop both services with
`systemctl --user disable --now ember-mvp.service ember-monitor.service`, delete
the generated service/autostart files and Monitor launchers, then run
`systemctl --user daemon-reload`.
Removing autostart does not remove `.env` or recordings.

## Debugging Monitor and interruption tuning

All supplied eye poses now work through explicit Gemini expression commands:
happy, sad, excited, confused spinner, surprised, curious, love, sleep bubbles
and dead/X eyes. Ask "sound sad" or "act sleepy" to request matching voice/eyes.
Normal blinking remains the default; >2s thinking dots remain automatic. Actual
fall-alert dispatch forces surprise over other eye modes. This does not enable
fall detection or infer emotion from speech audio. Expressions remain through
buffered playback, reset afterward, and have a safety expiry. See
[`features/eyes/README.md`](features/eyes/README.md) for priority and timing limits.

MVP defaults to `BAYMAX_INTERRUPTION_ENABLED=0`: while Ember speaks, the existing
echo guard sends silence to Gemini through playback plus its 250 ms tail. This
prevents leaked speaker audio from cutting off a reply or becoming a follow-up
turn. MVP also sets `BAYMAX_COMFORT_NOISE=1`, so that silence is steady noise at the
room's level rather than digital zeros: on the Orange Pi 5 Max, zeros made Gemini
commit the next question seconds late, or miss it (features/conversational_ability/README.md). Set the option to `1` only after live echo-cancellation testing confirms
barge-in works on the target hardware. Saved overrides still win. AEC/residual
subtraction remain enabled. **3 - Start Ember** runs `run_mvp.py` on the Orange Pis,
so it has these defaults too; on other computers it keeps `run_pc.py`'s.

After updating, reinstall the startup files and start the desktop session:

```bash
cd ~/Baymax
python3 devops/mvp/install_autostart.py
bash devops/mvp/start-session.sh
```

It opens `http://127.0.0.1:8766/monitor` on the robot, showing transcripts,
listening/thinking/speaking state, microphone/DSP metrics and tool activity.
Monitor opens automatically at login when a physical display is connected
(Linux DRM connector status). Without one, it remains available locally: use
the **Ember Monitor** desktop/Applications launcher, including in remote desktop.
GNOME may require right-click **Allow Launching** once; desktop icons depend on
the desktop's icon support. Display hot-plug after login uses the launcher.
First-run Setup uses port 8767, so it can coexist with Monitor. The Monitor
service remains available before configuration is saved and never opens sensor streams.
Closing this UI does not stop either service. Use Monitor while running; device
tests should be performed with Ember stopped so they do not compete for capture.

If the browser freezes, close/reopen it through the launcher. If the server
stalls, use `systemctl --user restart ember-monitor.service`; Ember keeps running.
Requests time out after five seconds and polling retries. Check startup with:

```bash
systemctl --user is-enabled ember-mvp.service ember-monitor.service
systemctl --user status ember-mvp.service ember-monitor.service --no-pager -l
journalctl --user -u ember-mvp.service -u ember-monitor.service -b -n 60 --no-pager
```

For unattended boot on GNOME/GDM, enable **Automatic Login** for `meowmax` in
Settings → System → Users (unlock first). This allows access to that account
at the physical console without a login password. The installer does not change
GDM. Verify a real cold boot; remote desktop login alone does not prove it works.

Conversational Ability now defaults to low speech-start sensitivity to reduce
false interruptions. MVP waits for 400 ms of end-of-turn silence, configurable
with `BAYMAX_TURN_END_SILENCE_MS` (300-2000). Intentional interruptions
may need clearer speech. `BAYMAX_SPEECH_START_SENSITIVITY=high` in private `.env`
restores the earlier detection setting; restart the service after changes.

The v10 built-in vision path defaults to `camera.pose_tracking = true` and
`fall_detection.enabled = true` in `/etc/ember/device.toml`. Both are enabled for
a fresh device; saved `false` settings (including a temporary demo opt-out) still
win until changed explicitly. These settings are separate from
`BAYMAX_FALL_DETECTION_ENABLED`, the optional fall toolkit. With pose tracking on it
reuses this pose (no second model) and runs its own detector on it for the Monitor;
with `fall_detection.enabled` on it leaves the check-in to the built-in detection, so
one fall gives one check-in. This default
does not provision caregiver recipients or validate fall accuracy on new hardware.

Echo protection removes positive AGC boost while the speaker/device buffer and
a 250 ms echo tail are active, so cancelled robot speech is not re-amplified.
Healthy processing continues to pass user speech during replies; failed DSP
uses a visible half-duplex fallback through the echo tail. Monitor shows the
protection state and AEC delay, using callback ADC/DAC timing where available.
These status indicators do not certify physical echo cancellation. Verify in
silence first, then with deliberate interruptions at normal speaking volume.

Residual echo detection now searches recent actual speaker audio over changing
delays and subtracts a confidently fitted component while keeping unexplained
user audio. Uncertain/strong double-talk frames pass unchanged. It does not
require a fixed mic/speaker distance. To compare primary WebRTC alone, set
`BAYMAX_RESIDUAL_ECHO_ENABLED=0` in `.env` and restart.

Monitor also warns when a substantial user-transcript prefix overlaps recent
Ember speech. The text is always retained; this late diagnostic never blocks
an interruption or assumes a person repeating Ember is an echo. Check the new
residual correlation/state alongside the warning rather than treating either
as proof of speaker identity.

## Validation and remaining checks

Windows/Python 3.12.15: **180 core + 73 toolkit offline tests pass**, with zero
failures, errors or skips, using `python check_release.py`. New checks cover
first setup, subsequent startup without setup, explicit devices/no-camera,
cancellation, preserving settings after failure, concurrent launch protection,
HTTP save/shutdown and user-scoped autostart generation with spaces in paths.
Twenty-two eye tests verify timing/state transitions, expressions, normal blinking, decoded
MAX7219 transfers, mounting, hardware failure and lifecycle using fakes.
Three eye-tool tests cover declaration gating, validation, hardware failures,
and real async dispatch/call IDs; fall check-in tests verify the forced event.
Twelve waiting-cue checks cover PCM priority, microphone cancellation, echo
reference, timing, one-shot behavior and invalid assets. Saved feature overrides
are checked before feature startup.
Five echo regressions cover speaker-active gain compensation without muting
near-end samples, quiet-period gain, device-buffer/tail timing, callback delay
hints and failed-DSP fallback after the Python playback queue drains. The opt-in
native `tests/aec_synthetic_probe.py` reports 11.0 -> 26.1 dB echo reduction on
generated signals and non-muted voiced double talk. Physical validation remains pending.
Ten residual/text tests cover multiple delays, polarity, movement, quiet/strong
double talk, stale/missing references, failure isolation and non-destructive
transcript warnings. Native synthetic behavior remains verified with this layer.
Actual room/movement behavior, quiet interruptions and the robot's CPU cost remain pending.
These use simulated devices/providers and do not measure conversation latency.

Pending on ember-002: clean install/native audio processor imports, actual
mic/speaker/camera, echo/interruptions/short answers, key rejection before
capture, public tools/provider recovery, graceful stop, desktop auto-login,
first-run configuration and two cold boots with no repeated setup prompt.
Verify both eyes on real hardware, default blinking while listening/speaking,
no thinking for waits <=2s, thinking dots on longer waits, and reset/cleanup.
Verify the local cue's voice/volume, once-only playback, no echo-triggered reply,
and cancellation on real reply/user interruption with working native AEC.
Confirm no duplicate production runtime. Follow `TESTING_PROTOCOL.md` for live
core/toolkit evidence and software-lead approval before merge/deployment.
