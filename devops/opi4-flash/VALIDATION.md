# OPI4-Flash validation

## Current reusable tooling

The SD CARD Flash quickstart and read-only image verifier apply to the current
Pallas-Robotics repository. New images carry an explicit Orange Pi 4 Pro board
label and manifest format. Verification checks labels, size and SHA256; it does
not establish a fresh-card boot or support for other boards. The records below
are historical evidence, not a claim that this published revision was boot-tested.

## Historical checks

Recorded 2026-11-01. Runtime baseline: PR #20 commit
`e0ea954` (including Svanik's PR #21); application source is unchanged by this
packaging PR. Tests do not start new microphone/camera streams or contact Gemini.

## Passed

- 2026-11-02 fresh-card test exposed root-only directories inherited from the
  builder's `umask 077`: Python failed with permission denied, and Xsession could
  not read its startup directory. AnyDesk then failed with `desk_rt_ipc_error`.
  Directory creation now explicitly uses 0755; extracted Python directories are
  normalized to 0755 while secret files retain 0600. Image validation checks the
  runtime path ancestors. Linux x86-64 preparation VM: **29 provisioning tests
  passed**, including the restrictive-umask regression. A fresh hardware boot of
  this correction is still pending; service-active status is not desktop proof.
- The pristine personalized saved image was repaired offline in the local Linux
  VM. Access checks as UID/GID 1001 passed for Python, device configuration and
  Xsession startup; private credential/key/Wi-Fi files remained 0600. Filesystem
  checks passed before and after editing. Its bundled runtime remains `e212fd1`;
  the updater follows PR #20 and must fetch the latest revision on first boot.
- Latest PR #20 integration: Windows/Python 3.12.15 required application gate —
  **303 core + 163 toolkit = 466 passed**, zero failures/errors/skips. Windows
  timezone data (`tzdata 2026.6`) was installed into an ignored test-only folder.
  Covers voice sleep/wake, DSP failure recovery, reminders/follow-ups, audiobook
  compatibility and built-in v10 fall re-engagement. The Pi remained off; this
  revision has not been deployed or rebuilt into the saved private image.
- Linux ARM64, kernel 6.6.98-vendor-sun60iw2, system Python 3 builder interpreter:
  `python3 -m unittest discover -s devops/opi4-flash -p 'test_*.py'` — **28 passed**,
  no failures, errors or skips. Includes settings validation/quoting, WPA/open Wi-Fi,
  private writes, generated identities, checksummed downloads, device renumbering,
  progress/AnyDesk reporting, update fallback and wrong-PR bundle rejection. Two
  launcher regressions verify `run_mvp.py` routing and independent Setup startup.
- Earlier baseline `e212fd1`, Linux ARM64/Python 3.12.15 application environment:
  `python check_release.py --report .local-review/opi4-flash.md` from `Baymax-main` —
  **284 core + 158 toolkit passed**, no failures, errors or skips.
- Generic `build.py` completed using a dummy key and dummy Wi-Fi, cached assets
  checked against `assets.json`, and PR #20's earlier `233e7aa` head. Produced a **2,147,483,648
  byte image file**, verified its board firmware/services/private-file ownership
  and permissions, ran the then-current 26 provisioning tests inside the assembled build
  workspace, and completed clean filesystem checks. Generated access details and
  images stayed private; no physical card was written.
- Git bundle verification: complete source history and selected PR commit/ref.
- Python and shell syntax; Git whitespace check; explicit staged-file audit against
  the operator's real API key/passwords. No matching secrets, personalized images,
  third-party binaries or machine-specific maintenance scripts were staged.
- Earlier Windows/Python 3.12.15 pure-Python check: 21 passed; five Linux/Bash updater
  cases skipped there and passed on Linux. The two new Linux-only launcher cases
  passed on Linux; the updated suite was not rerun on Windows.

The image build used verified cached asset bytes. Download cache-hit and
bad-checksum behavior were tested with mocks; a complete fresh-download build on a
second machine has not been recorded.

## Pending

- Flash the reusable package's generated image to a spare card and record a full
  first boot, Wi-Fi association/DHCP, package installation, virtual desktop,
  AnyDesk and progress reporting.
- Validate preconfigured/blank-key behavior and spoken v10 conversation, real
  audio/video quality, stop/restart and disabled-camera behavior on that fresh card.
- Confirm with `timedatectl` that a card built with a `timezone` other than the
  default (offline only: the build writes it to `dietpi.txt`, `/etc/timezone` and
  `/etc/localtime`) keeps it after DietPi's first boot.
- Validate ambiguous/missing physical USB devices and mixed-room conversation.
- Connect and validate eye hardware separately. No eyes were connected for these
  checks; synthetic tests do not establish physical eye behavior.
- Verify Linux x86-64 VM preparation and any claimed Windows/macOS VM workflow.
- Software-lead review/approval. Offline checks and a successful image assembly
  are not deployment approval or a completed live-integration checklist.
