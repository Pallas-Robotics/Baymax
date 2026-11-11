# Orange Pi boards: one feature set

| | Orange Pi 4 Pro | Orange Pi 5 Max |
|---|---|---|
| SoC / CPU | Allwinner A733: 2x Cortex-A76 + 6x Cortex-A55, ARM64 | Rockchip RK3588: 4x Cortex-A76 + 4x Cortex-A55, ARM64 |
| OS | DietPi (Debian 13) SD image from [OPI4-Flash](../devops/opi4-flash/README.md), or Debian 13 | Armbian Debian 13, vendor 6.1 kernel for the NPU; desktop for Setup/Monitor |
| Setup | the SD image, or `Baymax-main/MVP.md` | a git checkout of `release`, clickables and `Baymax-main/ORANGE_PI_SETUP.md` |
| Dependencies | `requirements-orange-pi.txt` | the same file |
| NPU for pose | none: MediaPipe on the CPU | optional: the RK3588 NPU after `features/pose`'s conversion and on-board check; MediaPipe on the CPU otherwise |
| Fast-core pinning, `BAYMAX_CPU_PINNING=auto` | not pinned (unmeasured; `on` tries the 2 A76s) | pinned to cpu4-7 (measured) |
| Wi-Fi provisioning | the image's preset network; optional setup hotspot installed by hand (`deploy/host`) | the OS's own network setup; optional setup hotspot installed by hand (`deploy/host`, NetworkManager) |
| Eye connection | external Firmata-compatible Arduino; set its real serial path | the same |
| Updates | the image's `ember-update` (branch, default `release`) | opt-in: `devops/mvp/update.py` before each start of the autostart service (git checkout, `install_autostart.py`, `BAYMAX_AUTO_UPDATE=1`; `BAYMAX_UPDATE_BRANCH`, default `release`); otherwise `git pull` by hand |

Both start `Baymax-main/run_mvp.py` -> `run_pc.py` -> `realtime_gemini_10.py` (the
4 Pro image's service, the autostart service, and **3 - Start Ember** on Linux ARM64) and
include the same conversation/personality, Gemini session (reconnecting after any
disconnect), audio cleanup, 400 ms turn-end default, reply-time echo protection, a
voice volume the person can change by asking, local eye expressions, 60-second idle
sleep eyes, engage/disengage, reminders/repeats/check-ins, gentle daily check-ins,
audiobooks/wake recognition, live radio and Internet Archive recordings, brain games,
"look closely" (one sharper camera still on request), app integration, public tools,
conversation memory (saved while Ember runs) and built-in pose/fall detection.
Existing feature flags, opt-ins and saved settings still apply; app integration needs
its device key and alerts need their recipients. Memory Palace/people-recognition work
outside this MVP is not added.

The same code runs on both boards; the board decides at runtime:

- **Pose** runs in its own process (`features/vision_process.py`, so it cannot hold
  Ember's GIL) at 10 fps with `camera.pose_model` (lite by default). On an Orange Pi
  5 Max that passed the NPU check, lite or full means the checked full model on the
  NPU; heavy always stays on the CPU. Everywhere else it is MediaPipe on the CPU. A
  pose process that cannot start or dies is started again once, on the CPU; video
  continues either way.
- **Pinning:** `auto` pins only on the RK3588. Only then does the pose process stay
  with Ember on the fast cores (at nice 5) while the face process goes to the slow ones
  (at nice 10), with a 1 ms GIL switch interval and no idle library thread pools
  (`Baymax-main/ORANGE_PI_SETUP.md`, Fast cores). On the 4 Pro that would leave Ember
  and pose on its 2 A76 cores, so it is left off until measured.
- **The camera** keeps one buffered frame on Linux ARM64 (unmeasured on both boards).
- **rknn-toolkit-lite2** installs on both boards (same requirements file) but is
  imported only on an RK3588 with converted, checked models.

This repository deliberately diverges from JoeKojima/Baymax's `startup-greeting`
(LattePanda) branch: it carries the Orange Pi 5 Max work (NPU pose, pinning, memory on
the board, the per-platform updater) and the Orange Pi 4 Pro image builder. Fixes that
both need are ported by hand in each direction; keep board differences in
`pc_hardware.py`, `features/pose`, the dependency profile, docs and deployment files.

## Setup

Orange Pi 4 Pro: build an SD image with [OPI4-Flash](../devops/opi4-flash/README.md),
or follow `Baymax-main/MVP.md`. Orange Pi 5 Max: follow
`Baymax-main/ORANGE_PI_SETUP.md` (clickables; the NPU steps are optional). Setup
detects Linux ARM64 and installs `requirements-orange-pi.txt` under Python 3.12. It
includes pywebrtc-audio (audio cleanup), pyfirmata2 (eyes), miniaudio (audiobook,
radio and recording decoding), Vosk (offline wake), ChromaDB/ONNX Runtime/tokenizers
(memory) and rknn-toolkit-lite2. ARM miniaudio and praat-parselmouth may compile
from source (the SD image downloads prebuilt wheels instead); install compiler tools.
ARM MediaPipe/NumPy pins differ from x86 pins.

The prerequisite helper supports Debian 13 only. On DietPi/another Debian
version, provision equivalent system libraries first: curl, ca-certificates, git,
libportaudio2, libgl1, libegl1, libgles2, libglib2.0-0 (or the distro's t64
variant), libsndfile1, libasound2-plugins, build-essential, cmake, pkg-config,
and xdg-utils; a browser/terminal and working desktop audio for Setup/Monitor.
Kernel/audio-server provisioning is separate work; the SD image covers it for the
Orange Pi 4 Pro. The root-owned single-radio hotspot service is for the Orange Pi
boards only (ifupdown on the 4 Pro, NetworkManager on the 5 Max), never an x86 robot.

Save the actual microphone, speaker and camera in Setup. Never copy another
robot's `.env`, API/device keys or `/etc/ember/device.toml`. Confirm the local
clock/timezone (`timedatectl`; `sudo timedatectl set-timezone America/New_York`,
then restart Ember): check-ins and reminders follow it, and an OPI4-Flash card is
set to `America/Los_Angeles` unless its image settings name another `timezone`.
Stop the older runtime/eye demo before launching this one.
Connect a Firmata-compatible Arduino with the documented MAX7219 wiring and set
`BAYMAX_EYES_PORT` to its actual `/dev/serial/by-id/...` path; the LattePanda
Leonardo default is not an Orange Pi port. Grant the desktop user serial access
(usually `dialout`, followed by logging in again).

For voice sleep, keep audio enhancement healthy and allow the first Vosk model
download (or prefetch with `.venv/bin/python features/toolkits/wakeword.py --download`
from `Baymax-main`). Missing cleanup or wake recognition refuses/undoes sleep.
Disengage withholds Gemini-bound microphone audio; camera frames and existing
raw local recordings continue. This is voice sleep, not a privacy-off switch.

## Validation

Run `python check_release.py` in `Baymax-main`; preserve separate core/toolkit
results. The profile test checks the shared feature dependencies and the installer
architecture selection. Pull requests also run the checks with
`requirements-orange-pi.txt` on GitHub's Linux ARM64 runner
(`.github/workflows/offline-checks.yml`): same pins and CPU architecture, not an
Orange Pi. Offline checks do not establish physical equivalence between the boards.

On each board, verify installation/native imports, selected devices, audible
multi-turn conversation/echo, engage/disengage (including reminder/fall wake),
eyes, audiobook, radio and recording playback (and that a fall check-in or due
reminder pauses it), the voice volume on the board's speaker, "look closely" with the
board's camera, daily check-ins on the board's clock, reconnecting after a Wi-Fi drop,
reminders/app integration, memory, fall behavior,
sustained camera+audio performance (CPU summed over Ember and its pose/face
processes), clean stop/restart and cold boot. On the Orange Pi 5 Max also the NPU
path inside Ember; on both boards the hotspot (if installed); on the Orange Pi 4 Pro the SD
image's first boot and updates. Hardware/provider checks remain pending; no device,
service, saved SD image or production alert is changed by preparing this
repository. Software-lead review remains required for release.
