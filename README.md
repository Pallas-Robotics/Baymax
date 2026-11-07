# Ember for Orange Pi

Ember is a voice companion with Gemini conversation, optional eyes and camera,
CPU/NPU pose processing, memories and callable information/support tools.

**OPi4 V1 is under review.** Offline checks do not establish perfect operation on
every Orange Pi. See the [feature log](docs/features/FEATURE_LOG.md) for implemented
features, source locations, tests and hardware checks still pending.

## Board comparison

| Board | Installation | Pose processing | Validation limits |
|---|---|---|---|
| Orange Pi 4 Pro (A733) | DietPi SD image or documented Debian setup | MediaPipe CPU, isolated child process | ARM64/offline checks passed; combined V1 live/fresh-card checks pending |
| Orange Pi 5 Max (RK3588) | Board-specific Armbian/clickables setup | CPU fallback; optional NPU after conversion/checks | Combined V1 hardware checks remain necessary |
| Other boards | Not established by this PR | Board-specific | No blanket support claim |

Start with the [board installation guide](Baymax-main/ORANGE_PI_SETUP.md) and
[device comparison](docs/DEMO_DEVICES.md). Use a suitable power supply, USB
microphone/speaker, and an optional supported camera. Eyes need their own verified
serial wiring/configuration.

## Get and start the application

```bash
git clone https://github.com/Pallas-Robotics/Baymax.git
cd Baymax
```

For development, use `main` and a feature branch. Before V1 is merged, its review
branch is `opi4-chat-timing`. The normal on-board update channel is `release`.
Complete the board guide's system packages, Python environment, device selection
and Gemini-key setup first. Both boards use
`Baymax-main/requirements-orange-pi.txt`.

The device entry point is `Baymax-main/run_mvp.py`, which wraps v10 startup,
device readiness and the eye lifecycle. Setup/Monitor are available through the
[clickables](devops/clickables/README.md) or local `devUI.py`. The runtime needs
connectivity for Gemini conversation; enabling a configured runtime can send
microphone/camera context to Gemini and save local session data. Keep private
keys/settings and runtime data out of Git.

## Features and layout

```text
Baymax-main/          Runtime, setup/debugger and device configuration
  features/          Conversational ability, eyes, pose and grouped toolkits
devops/              Installation, SD builder, graph tools and editing protocol
docs/features/       Organized feature log and Graphify guide
graphify-out/        Interactive graph, report and feature-log viewer
tests/               Overall gate and feature/deployment suite definitions
test/                Standalone chat UI regression check
```

Ember's shared persona follows the speaker's language, follows language switches
and mixed-language speech, and keeps English internal messages from changing the
conversation language. This is model guidance; see the measured limitations below.

Conversation and audio live under `features/conversational_ability`; eyes and pose
have their own packages; toolkits group everyday, family, information and media
behavior. Existing memory/runtime hooks retain their working entry points.
Reserved person-recognition/Memory Palace namespaces are not proof of completed
identity recognition. See the feature log for the precise boundaries.

The chat debugger shows local speech activity before a Gemini transcript,
updating per-turn speech/transcript/reply/playback timings, and one centered
updating bubble per toolkit call. Speech and DAC timings are estimates.

[Multilingual simulations](Baymax-main/evals/languages/README.md) exercise native
conversation, language switches and Hindi-English mixing. The
[language matrix](docs/features/LANGUAGES.md) separates Google's 99 candidate
entries from actual model/config-specific validation; none are claimed validated
until live runs and language/audio review are complete.
The [Pi simulation report](docs/features/OPI4_LANGUAGE_RESULTS.md) records 80/81
automated response runs passing, one Polish switching failure, and separate
synthetic-speech transcription failures. It tested the language instruction in
a temporary copy; the same instruction is now included in `Baymax-main/features/persona.py`.
The running robot was not replaced.

## Orange Pi 4 Pro SD image

The reusable builder is in [devops/opi4-flash](devops/opi4-flash/README.md).
It builds a private, per-card image on a Linux host/VM. Select the runtime branch,
Wi-Fi, Gemini key and time zone; retain the private access file. It does not write
a physical card. Use an image writer's verification step when flashing.
The separate **SD CARD Flash** PR adds the public walkthrough and read-only image
verifier. The 4 Pro image is not a 5 Max image. Fresh-card hardware validation is
separate from checksum and unit tests.

## Tests and future edits

Read [devops/EDITING_PROTOCOL.md](devops/EDITING_PROTOCOL.md) before editing.
Use an environment with the application's test dependencies:

```bash
python tests/run.py                              # full repository validation
python tests/run.py --feature conversational_ability
python tests/run.py --feature eyes --feature pose
python tests/run.py --changed origin/main
```

Feature commands include overall checks. Shared/unknown changes select all
suites. [tests/README.md](tests/README.md) explains ownership, reports and how to
add tests. Existing CI also runs `Baymax-main/check_release.py`; it preserves
separate core/toolkit results and includes root overall checks. Explain skips and
platform failures. Passing offline tests is not permission to merge/deploy or
proof of live hardware behavior; use [the live checklist](Baymax-main/TESTING_PROTOCOL.md).
Full root/deployment CI is provided as a workflow template awaiting a maintainer
with workflow-write access; see the tests guide's CI activation section.

Every feature also follows the [performance protocol](devops/performance/README.md):
compare baseline, feature and combined load for CPU/RAM, NPU telemetry,
temperature and added speech-response latency, with explicit board-specific
budgets. Feature test commands include the sampler's offline checks; actual
measurements run separately on the Orange Pi.

## Graphify

Open [the graph](graphify-out/graph.html) locally after downloading the checkout;
its **Organized feature log** link opens the inventory. GitHub displays HTML as
source. [The guide](docs/features/GRAPHIFY.md) explains regeneration and confidence
limits. Graphify is optional developer documentation, outside the robot runtime.
Private data, recordings, model assets and generated SD images are excluded.
