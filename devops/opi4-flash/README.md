# OPI4-Flash

Build a personalized bootable microSD image for the **Orange Pi 4 Pro (A733)**.
The image installs Ember, a headless XFCE desktop, AnyDesk and a progress page.
No running Ember device is required to prepare it.

This is based on a DietPi Debian 13 image and the SD provisioning work tested on
one Orange Pi 4 Pro. It is not an image for the Orange Pi 4, 4 LTS, 5 / 5 Max or
Raspberry Pi: the Orange Pi 5 Max is set up with the clickables instead
([ORANGE_PI_SETUP.md](../../Baymax-main/ORANGE_PI_SETUP.md)). The reusable builder has offline checks; a fresh-card boot of this published
version and eye-matrix hardware testing remain pending.

## First-boot choices

Run the private configurator or copy `settings.example.json` to
`settings.private.json` and edit it locally:

```json
{
  "branch": "deploy",
  "wifi_networks": [],
  "gemini_api_key": "",
  "timezone": "America/Los_Angeles"
}
```

- **Source:** the app defaults to the reviewed `deploy` branch and lists open Orange
  Pi 4 Pro PRs from `NateyKim/Baymax`. A PR is for testing; its head can change.
  Settings may use `"branch": "deploy"` or `"pr": NUMBER`. Startup fetches the
  selected ref and bundles it as the first-install fallback if Git is unavailable.
  Older branch and PR settings remain supported.
- **Wi-Fi:** leave the list empty to test headless setup from a phone, or preload
  up to five SSIDs. Each has its own password; blank means an open network.
  The image writes DietPi first-boot slots and the board's `wpa_supplicant` file.
  The old single `"wifi"` setting still works.
  WPA passwords must be 8–63 UTF-8 bytes or a 64-digit hex PSK.
  Captive-portal sign-in is not automated. Use a reachable private network for
  SSH; public apartment/guest networks may isolate clients.
- **Gemini key:** preload a key, or leave it blank to wait for Ember Setup.
  Password/key entry is hidden in the configurator. With a key and usable devices,
  Ember starts automatically after installation, sends microphone/camera context
  to Gemini, and may record session audio locally. Keep the key blank if capture
  should wait for manual setup.
- **Time zone** (optional): the board's clock. Without it a card is set to
  `America/Los_Angeles`, as every card built before this setting is. Use the home's
  IANA name, such as `America/New_York` or `Europe/London` (the build checks that the
  image has it). Daily check-ins (morning 08:00-11:00, afternoon 14:00-16:30, evening
  18:00-20:30 by default), reminders and the greeting's time of day follow this clock,
  so a card on Pacific time in Philadelphia asks "how did you sleep" at 11:00-14:00
  there. To change it on a running card: `sudo timedatectl set-timezone
  America/New_York` (or `sudo dietpi-config`, Language/Regional Options), then
  **Restart Ember**; `timedatectl` shows the current one.

Every build generates a distinct hostname unless you enter a board name such as
`ember-002`. It also generates separate random Linux/AnyDesk passwords and a
unique password for the `Ember-Setup-...` Wi-Fi hotspot. The app
can set `"linux_password"` and `"anydesk_password"`
individually (8–64 characters, no spaces); leaving either blank generates a
random one. When you explicitly set the same printable password for both, the
setup hotspot uses it too; otherwise its password is random. The older shared
`"access_password"` setting still works. AnyDesk
is reachable by its ID from any network, so use a strong password. No person's passwords, SSH identity or Wi-Fi defaults are included.
The outputs contain secrets: keep the settings, generated access file and image
private. They are ignored by Git. Generate a separate image for each board.

## Link a new card to the fleet console

In the admin-only website console, open **OP4's**, enter the board name (for
example `ember-004`), and create its fleet key. It appears only once. Enter the
same board name and paste the key into the desktop image builder before building.
The key is kept in the private image and never written to `ready-image.json`.
After the board reaches the internet, its status-only agent checks in about
once a minute with the AnyDesk ID and actual Git ref/commit. You can import
`ready-image.json` on the same tab to record what image was built. An imported
manifest alone does not establish that a board is online.

Replacing a fleet key revokes the old image's access. Reflash the card with the
new key, or install the new key on the existing board over SSH promptly.
This first agent does not apply fleet-console commands or settings.

## Build on Linux

Use a Linux host/VM with Python 3.12 or newer, root access and loop-device support.
An x86-64 Linux host can build this ARM image. It preinstalls the Debian packages,
AnyDesk and Ember's Python environment by running the image's ARM64 apt/Python in a
chroot through `qemu-user-static`, so first boot downloads almost nothing. An ARM64
Linux host runs them natively. A Windows/macOS user can use a Linux VM with a
writable shared folder. WSL/container environments require working loop devices,
mounts and binfmt emulation and have not been verified. Allow at least **20 GiB
free disk space** (a 7 GiB cached base image plus each 7 GiB card image).
Internet is required during preparation; first boot only checks Git for updates.

From the repository root, on Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install curl python3 git xz-utils util-linux e2fsprogs openssl ca-certificates \
  qemu-user-static binfmt-support
cd devops/opi4-flash
python3 configure_image.py
sudo python3 build.py --settings settings.private.json --output output
# Make the three private outputs available to the invoking desktop account:
sudo chown "$USER:$(id -gn)" output output/ember-opi4.img \
  output/device-access.private.json output/ready-image.json
```

On Ubuntu 26.04 and other newer releases, install `qemu-user-binfmt` instead of
`qemu-user-static binfmt-support`. `/proc/sys/fs/binfmt_misc/qemu-aarch64` must exist.

Confirm `python3 --version` is at least 3.12. The interactive configurator asks for
the branch (default `release`). Enter `-` at the Wi-Fi password prompt for open Wi-Fi, or at the key
prompt to leave manual Setup. Enter preserves an existing secret. To rebuild,
change the settings and choose a new `--output` directory; existing builds are
never overwritten. `--cache` can select an existing verified asset cache.

The first build is slow (often an hour or more under emulation): it installs every
system package into a cached base image, `.cache/packages-<key>.img`. Later cards
reuse it and only add their settings and the Ember release; Python wheels built
under emulation are cached in `.cache/uv`. Packages PyPI only ships as source
(praat-parselmouth, miniaudio) download prebuilt from the `opi4-wheels-1` release of
JoeKojima/Baymax (the repository this one came from; `assets.json` pins each wheel's
SHA-256), so a new PC skips that compile; a version that no longer matches still compiles.
Changing the non-wheel `assets.json` entries,
the package stage of `build_linux.sh` or `payload/system-packages.txt` builds a new base.

The builder verifies all downloads against `assets.json`, bundles the selected
branch, injects the three choices and generated credentials, validates firmware and
private-file permissions, runs the offline provisioning tests and checks the
filesystem. It only mounts a **regular image file**. It has no physical-disk
writer. Output:

- `output/ember-opi4.img` — the private boot image.
- `output/device-access.private.json` — hostname, user `ember`, and generated
  Linux/sudo and AnyDesk passwords. These passwords are different.
- `output/ready-image.json` — image SHA256/size, selected repository/ref/commit and
  non-secret build metadata.

Upstream DietPi's URL can be updated in place. A checksum mismatch stops the build;
review the new release and its board layout before updating `assets.json` and the
layout assertions. Do not bypass verification. Third-party assets are downloaded
from [DietPi](https://dietpi.com/downloads/images/),
[AnyDesk](https://deb.anydesk.com/pool/main/a/anydesk/) and
[Astral's Python builds](https://github.com/astral-sh/python-build-standalone).
They are not redistributed in this repository.

## Clickable app for Windows and Mac

Download **Ember Image Builder** for Windows, Apple Silicon Mac or Intel Mac
from the artifacts of the [flash app workflow](../../.github/workflows/flash-app.yml).
Install [Multipass](https://canonical.com/multipass/docs/latest/how-to-guides/install-multipass/)
first, then open the app. No separate Linux computer or Python installation is
needed. The app creates an Ubuntu 24.04 VM named `ember-opi4-builder`, installs
the builder's Linux tools there, and reuses its package cache for later cards.
Allow at least 35 GiB free on the laptop; the VM is configured with a 50 GiB
virtual disk and 4 GiB RAM. The virtual disk grows as used. The cache and
working image each occupy about 7 GiB, the copied image another 7 GiB, and
Ubuntu, downloads and temporary files need additional space. The first image
can take an hour or more.

Leave Wi-Fi empty for headless phone setup, or preload networks. Choose stable `deploy` or an open Orange Pi PR,
set the board's time zone, and optionally enter a Gemini key and separate
SSH/sudo and AnyDesk passwords. Choose an output folder, then click **Build image**. The
app transfers these private settings into the local VM, builds one image, copies
back `ember-opi4.img`, `device-access.private.json` and `ready-image.json`,
and checks the image's SHA-256 against the manifest. It removes that build's VM
work files after a successful transfer, leaving the reusable package cache.
If a build fails, its VM work files remain for recovery and may consume space.
The app does not send the settings to a cloud build service. The builder still
fetches the selected Ember branch and its verified dependencies over the Internet.

Use an image writer such as [balenaEtcher](https://etcher.balena.io/) to write
and verify `ember-opi4.img` on the intended microSD card. The app never selects
or erases a physical disk. Generated access passwords and any configured key are
private; keep the output folder private. These app bundles are unsigned, so
Windows or macOS may ask you to confirm opening them. Building in Multipass on
Windows and Mac still needs a fresh-image hardware test before fleet use.

## Flash and first boot

1. Use an image writer such as [balenaEtcher](https://etcher.balena.io/) to write
   `ember-opi4.img` to the intended microSD card. Select the card carefully; writing
   erases that card. Use the writer's verification step. An 8 GiB or larger card is
   recommended; larger cards leave room for packages, updates and recordings.
2. Power the Orange Pi off, insert the card, connect the usual USB microphone,
   speaker and optional camera, make the chosen Wi-Fi available, and power it on
   with a suitable board power supply. Eye hardware is optional and unverified.
   For a blank-Wi-Fi image, wait for the `Ember-Setup-...` hotspot named in
   `device-access.private.json` to appear. Join it from a phone or laptop with
   the setup password in that file, then open `http://192.168.4.1/`. Pick the
   Wi-Fi network and enter its password, or leave the field blank for open Wi-Fi.
   The hotspot runs on a second interface (`ap0`, 2.4 GHz channel 6) while the
   board keeps trying saved networks, so you can save several networks on one
   visit; the page lists them and shows the current connection. It closes once
   the board is online and 10 minutes have passed since the last save. If the
   driver refuses `ap0`, the older behavior is used: the hotspot closes while the
   board tries the network and returns if joining fails. The setup service also
   watches the Wi-Fi address: if the board joins a network but DHCP has given up
   (for example the router was off at boot), or it moves to another network while
   keeping the old address, it asks DHCP again, at most once a minute. To add networks while
   Ember is already online, run `sudo python3 /usr/local/lib/ember/ember_wifi_setup.py --now`
   over SSH; the hotspot stays open for 10 minutes. No keyboard, monitor or internet is needed to
   reach this setup page. An open network may still require a captive-portal
   sign-in for internet access. To connect Ember to an iPhone hotspot, keep the
   iPhone hotspot on and open the Ember setup page from a second phone or laptop.
3. From a device on the same reachable network, open
   `http://HOSTNAME.local:8765/`, using the generated hostname in the private access
   file. If mDNS does not resolve, use the board's IP from the router/hotspot list.
   The page shows numbered setup stages, recent package activity and AnyDesk ID.
   ARM voice-analysis builds may be slow; keep power and internet available.
4. Connect to that ID in AnyDesk with the generated **AnyDesk** password. The
   desktop uses HDMI when a monitor is connected and a virtual 1280×720 display
   otherwise. Adding or removing HDMI briefly disconnects AnyDesk while the
   desktop restarts; reconnect to the same ID after about 10–20 seconds. For
   terminal/sudo use the generated **Linux** password, not the AnyDesk password.
5. Open **Ember Setup** to inspect devices/key or **Ember Device Settings** for
   Wi-Fi, Git source, update, start/stop and logs. A single unambiguous USB input,
   USB output and camera can be selected automatically. Saved choices are resolved
   across Linux device renumbering. With several matching devices, choose in Setup
   and restart; the launcher stops rather than guesses. No camera permits audio-only
   operation. A saved disabled camera stays disabled.

To save another network without replacing the existing ones, open a terminal on
the board and run `sudo ember-wifi`, or choose **Add a Wi-Fi network** in Ember
Device Settings. Enter the SSID and a password (blank for open Wi-Fi), then
choose whether it should take priority over saved networks. The default adds a
fallback network; choosing preferred can switch Wi-Fi and disconnect SSH or
AnyDesk. The command uses the board's `wpa_supplicant` setup, not NetworkManager.

The builder has already installed the system packages and the selected branch's
Python packages from its `Baymax-main/requirements-orange-pi.txt` (audio cleanup, eyes,
audiobooks, wake words and memory included; the only dependency list), checked that
the native ones import, and run its complete offline release gate. First boot enables
services, sets the AnyDesk password and checks Git. Only if the selected branch has
new commits does it install that release, downloading any changed Python packages.
On its first start Ember downloads its pose model (`pose_landmarker_lite.task` by
default, from storage.googleapis.com) and the memory model (about 90 MB from
huggingface.co) into `~/ember-deployment/shared`, which every later release links to.
If the pose model cannot be downloaded, Ember runs with video only (no fall detection
or face presence) and tries again on its next start.
No extra microphone/camera streams are opened by hardware enumeration or tests.

The conversation service starts **`run_mvp.py`**, which wraps the same v10
conversation in `run_pc.py` with saved-device readiness, single-instance handling,
the eye lifecycle and `run_mvp.py`'s MVP defaults: 400 ms turn-end silence,
interruptions off, the wait cue off, voice analysis off and at most 10 s for saving
memories when Ember stops. Existing `.env` overrides still win: a `BAYMAX_TURN_END_SILENCE_MS` saved on a
board (for example 600 from an earlier test) replaces the 400 ms default there.
No connected eye board is required for conversation; physical eyes and their
serial port must be configured/verified separately. v10's built-in pose/fall
settings come from `/etc/ember/device.toml`; its optional fall toolkit is a separate
setting (it reuses the built-in pose but is a second detector) and should not be
mistaken for that built-in detector. Pose runs on the CPU here (this board has no NPU
that Ember uses) in its own process, and Ember is not pinned to the fast cores
(`BAYMAX_CPU_PINNING=auto`; see ORANGE_PI_SETUP.md).

## Wipe, reflash and test a fresh card

For a clean Ember install, reflash the boot microSD. The image writer replaces
that card's partition table and filesystem; a separate Windows format is not
needed. This resets the SD installation, not any onboard eMMC/SPI storage, and is
not a secure erase of old secrets. Back up anything needed from the card first.

1. Shut down the board (`sudo poweroff`), wait for shutdown, unplug power and
   remove the microSD. Insert it into the PC through a USB card reader. A USB
   reader is the flashing adapter; this guide boots the board from microSD.
2. To change branch, Wi-Fi or key, run the configurator and builder above with a new
   output directory. Otherwise reuse the existing private image. Keep its private
   access file; reflashing the same image restores that image's credentials.
3. In the image writer, select `ember-opi4.img` (or your existing personalized
   `.img`), select the microSD by capacity/device identity, flash and verify.
   Everything on the selected card is erased. Cancel Windows prompts to format
   Linux partitions, then safely eject the reader.
4. Follow **Flash and first boot** above with internet and the configured Wi-Fi
   available. Record the setup page completing, the selected Git branch/commit,
   AnyDesk access and an audible multi-turn conversation. Confirm the intended
   400 ms timing and enabled built-in pose/fall settings in Setup/settings;
   restored device-local overrides can change those defaults.
5. Test "Ember disengage", then "Ember
   engage". Disengage withholds Gemini-bound microphone audio while an offline
   wake listener runs; camera frames and existing local recordings continue.
   Test fall check-ins with safe recorded/synthetic input separately.
6. Reboot once more with internet available. Confirm the updater finishes and
   conversation starts again. Record the installed commit; unchanged source can
   report "already installed". Verify eyes separately when connected.

An existing image can contain an older bundled revision. Its boot updater
fetches the latest commit of the selected branch when GitHub is reachable; the image
file itself does not change when that branch is updated. Without Git access, a fresh card can
install its older bundle, so check the installed commit before judging a test.

## Updates, recovery and data flows

Automatic updates run on boot. Packages and tests are installed in a separate
release directory before switching `~/ember-deployment/current`. The existing
`.env` survives a release update; the original key preset is used only for the
first release. Memory, reminders, the voice volume, daily check-ins and logs live in
`~/ember-deployment/data` (`BAYMAX_DATA_DIR`, set in the first `.env` of a card built
by this builder; the updater adds it back if a `.env` lacks it) and downloaded models
in `~/ember-deployment/shared`, so a new release keeps them.

A card flashed from an image built before this repository (JoeKojima/Baymax PR #22)
keeps that image's own `/usr/local/bin/ember-update`: it is copied into the image when
the image is built, and no release replaces it. Pointed at this repository in Device
Settings, such a card still installs each release, but its updater never sets
`BAYMAX_DATA_DIR` or links the shared models, so every update starts with empty
memory, reminders and family messages, forgets the voice volume and which daily
check-ins were done, and downloads the models again. Reflash it with an image from
this builder (recommended), or once, by hand, as the `ember` user: stop
Ember (`systemctl --user stop ember.service`), run `mkdir -p ~/ember-deployment/data`,
move what the current release kept in `~/ember-deployment/current/Baymax-main/`
(`chroma_store/`, `day_utterance/`, `toolkit_*.sqlite3`, `toolkit_*.json` (feed
preferences, daily check-ins), `voice_volume.json`, `transcript_*.txt`, `session_*.wav`)
into it, add `BAYMAX_DATA_DIR=/home/ember/ember-deployment/data` to
`~/ember-deployment/current/Baymax-main/.env` (the old updater copies that `.env` into
every later release), and start Ember again (`systemctl --user start ember.service`).
Models are still downloaded again after each update on such a card.

Installer failures keep an existing working release. A runtime
failure after promotion is not an automatic rollback; select the previous release
and restart if needed. Retain the previous release and back up private settings.

Changing Wi-Fi can disconnect AnyDesk/SSH. Keep a known network available and
verify the new connection before turning it off. To inspect the board in its own
terminal: `sudo wpa_cli -i wlan0 status` shows `ssid` and `ip_address`.

Logs: `/var/lib/ember/provision.log`,
`/home/ember/ember-deployment/logs/update.log`, and `journalctl -b`.
Retry system setup with `sudo systemctl restart ember-provision`; retry the
updater with `sudo systemctl restart ember-update`. A preinstalled card boots
without internet; an image without `/var/lib/ember/packages-baked` falls back to
downloading `system-packages.txt` on first boot.
No entry in the router list can indicate a power, boot or Wi-Fi problem; a status
page or ping alone does not establish a working live conversation.

Preparation contacts the listed asset hosts, GitHub, Debian and Python package
hosts. First boot contacts GitHub, plus Python package/model hosts for a newer
release, and AnyDesk; remote sessions use AnyDesk's service. Ember's first start
downloads the pose model (storage.googleapis.com) and the memory model
(huggingface.co).
Deliberately starting Ember sends audio/video to Google and the runtime may store
recordings locally. The chosen release's toolkit documentation describes its additional
provider calls. These scripts do not configure caregiver alerts.

## Validation

```bash
python3 -m unittest discover -s devops/opi4-flash -p 'test_*.py'
cd Baymax-main
.venv/bin/python check_release.py --report .local-review/opi4-flash.md
```

Run the provisioning suite on Linux for its Bash updater integration tests.
Windows can run the pure-Python cases; Linux-only updater cases skip there.
The builder also runs the provisioning suite against its assembled payload and
checks the mounted image. Tests use fake devices, synthetic data and mocked
providers; they do not start capture or certify a fresh boot, live conversation,
AnyDesk session, speaker quality or eye hardware. Record those checks and obtain
lead review separately. See the repository testing protocol.
