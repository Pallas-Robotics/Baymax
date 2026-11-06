# Ember for Orange Pi

Ember's v10 MVP runtime (conversation/personality, local eyes, audio cleanup,
engage/disengage, reminders, audiobooks, live radio and Internet Archive recordings,
gentle daily check-ins, brain games, "look closely", a voice volume the person can
change, app integration, memory and built-in fall detection) for the **Orange Pi 4
Pro** and the **Orange Pi 5 Max**: one
codebase and one dependency file (`Baymax-main/requirements-orange-pi.txt`) for
both boards. What differs is decided on the board itself at runtime (the NPU,
fast-core pinning) or by how the board is set up. Live device checks remain
pending on both boards.

| | Orange Pi 4 Pro | Orange Pi 5 Max |
|---|---|---|
| SoC / CPU | Allwinner A733: 2x Cortex-A76 + 6x Cortex-A55 | Rockchip RK3588: 4x Cortex-A76 + 4x Cortex-A55 |
| NPU for pose | none: MediaPipe on the CPU | optional: the RK3588 NPU after the on-board conversion and accuracy check; MediaPipe on the CPU otherwise |
| OS | DietPi (Debian 13) SD image from [OPI4-Flash](devops/opi4-flash/README.md), or Debian 13 | Armbian Debian 13 with the vendor 6.1 kernel (needed only for the NPU) |
| Setup | the SD image, or [MVP.md](Baymax-main/MVP.md) | a git checkout of `release`, [clickables](devops/clickables/README.md) and [ORANGE_PI_SETUP.md](Baymax-main/ORANGE_PI_SETUP.md) |
| Wi-Fi setup hotspot | optional, installed by hand ([deploy/host](Baymax-main/deploy/host/README.md); not part of the SD image; untested on a board) | no |
| Fast-core pinning (`BAYMAX_CPU_PINNING=auto`) | off (unmeasured; `on` tries it) | on (measured) |
| Updates | the image's `ember-update`, following a branch (default `release`) | opt-in: `devops/mvp/update.py` before each start of the autostart service (needs the git checkout, `install_autostart.py` and `BAYMAX_AUTO_UPDATE=1`; `BAYMAX_UPDATE_BRANCH`, default `release`); otherwise `git pull` by hand |

On both boards pose and face inference run in their own processes
(`Baymax-main/features/vision_process.py`), pose tracking runs at 10 fps with
MediaPipe's lite model by default (`camera.pose_model` in `device.toml`; a set-up
Orange Pi 5 Max NPU runs the checked full model instead), the live view is drawn only
while someone watches it, and memories are saved while Ember runs. See
[the board guide](docs/DEMO_DEVICES.md) and [ORANGE_PI_SETUP.md](Baymax-main/ORANGE_PI_SETUP.md).

Where this came from: JoeKojima/Baymax PR #29 (`opi4-demo`, the Orange Pi 4 Pro
demo), PR #22 (the SD-image builder), the Orange Pi 5 Max
branches `orangepi5-npu` and `memory-incremental-saves`, `orangepi-features`
(opi4-demo with Svanik's PRs #23-#28 and #30 and the voice volume control) and the
reconnect fix from `reconnect-any-disconnect` (one commit), merged in November 2026. This
repository deliberately diverges from Baymax's `startup-greeting` (LattePanda)
branch: shared fixes are ported by hand. The x86/LattePanda, macOS, Windows and Docker
code is still in the tree, unchanged, but is not what this repository targets.

## Orange Pi 4 Pro SD image

For a reusable build, verification and card-writing walkthrough, see
[SD CARD Flash](docs/SD_CARD_FLASH.md).

See [OPI4-Flash](devops/opi4-flash/README.md) for a private SD-image builder with
three first-boot choices: the branch to follow (default `release`), preconfigured
Wi-Fi and Gemini key. Builds generate their own device credentials; no personalized
images are included. The image is for the Orange Pi 4 Pro only; the Orange Pi 5 Max
uses the clickables setup.

## Included Conversational Ability

Responsive listening and turn-taking, layered on #11. See
[the feature](Baymax-main/features/conversational_ability/README.md) for echo
cancellation, noise suppression, brief replies and enablement, the voice volume
("Ember, speak louder", 1-10, saved on the board) and reconnecting after any
disconnect from Gemini (also a network that goes silent) without replaying the rest
of a cut-off reply. Memory Palace (Baymax PR #13) is separate from v10's
conversation memory, which is on here.

## Feature organization and toolkits

Baymax PR #11 stacked on PR #7 for Windows, Intel/Apple Silicon macOS, Linux/Orange Pi setup, devices, clickables and local monitoring. PR #11's review diff contains feature organization and callable toolkit behavior. Downloading the whole branch includes the compatibility foundation automatically.

- `Baymax-main/features/` organizes `memory_palace/`, `person_recognition/` and `toolkits/` as sibling namespaces. Memory/person folders reserve integration boundaries; prompt/identity/recall changes are scoped to PR #13.
- Location/weather uses IPWho/Open-Meteo with clarification, caching and bounded provider errors; weather questions go straight to `get_weather`. `BAYMAX_TOOLKIT_ENABLED=0` disables toolkit declarations/preloading.
- Radio and recordings (`listen`: Radio Browser stations, openly licensed Internet Archive audio) play through the audiobook player; gentle daily check-ins (morning, afternoon, evening); brain games played only when asked; "look closely" sends Gemini one sharper camera still on request. Each has its own switch, described in the [toolkit guide](Baymax-main/features/toolkits/README.md). A fall check-in or a due reminder pauses whatever plays so the answer reaches Gemini.
- Optional fall detection uses the existing camera (in v10 it reuses the core's pose instead of running a second model), is disabled by default and adds no caregiver alerts. It runs on the CPU unless the optional Orange Pi 5 NPU pose setup has been done (`Baymax-main/features/pose`).
- Public information tools and explicit local headline/feed profiles are opt-in, with Setup controls and documented sources, data flows and tests. Desktop v8 and robot v10 retain existing handlers and tool-response IDs.

Extract the whole branch and open `devops/clickables` → your OS → **1 - Setup**. Then Save key/devices and use **Monitor → Start Ember → Stop Ember**. On Intel macOS, Setup selects the Intel profile inherited from #7 automatically. Actual Intel/Apple Silicon Mac and ARM installations/devices remain pending.

- [Feature layout](Baymax-main/features/README.md)
- [Toolkit guide](Baymax-main/features/toolkits/README.md)
- [Public-tool experiment](Baymax-main/features/toolkits/EXPERIMENTAL.md)
- [CPU fall toolkit](Baymax-main/features/toolkits/FALL_DETECTION.md)
- [Clickables](devops/clickables/README.md)
- [Testing protocol](Baymax-main/TESTING_PROTOCOL.md)

Run `python check_release.py` from `Baymax-main` for separate core/toolkit offline results. Pull requests and pushes to `release` run it on GitHub (`.github/workflows/offline-checks.yml`), including a Linux ARM64 job with `requirements-orange-pi.txt` (GitHub's runner, not an Orange Pi). `Baymax-main/evals/live_eval.py` plays scripted turns into real Gemini sessions from a PC, without a robot ([evals](Baymax-main/evals/README.md); needs a key, not yet run on this repository). Offline success does not establish real-device, live Gemini or provider accuracy, or authorize deployment. The toolkit work changed no production startup/service. Deliberately starting the runtime sends microphone/camera data to Gemini and records audio locally; toolkit documentation describes additional provider calls.
