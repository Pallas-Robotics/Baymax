# SD CARD Flash

Build and flash a private Ember card for the **Orange Pi 4 Pro (Allwinner A733)**.
This is not an image for other Orange Pi boards. For the 5 Max, use
[its installation guide](../Baymax-main/ORANGE_PI_SETUP.md).

The reusable builder is source code, not a ready-made public personalized image.
Each operator builds their own image. Settings, image, passwords and access
metadata must remain private. A fresh-card boot of this published revision still
needs verification; checksum success alone does not establish hardware support.

## 1. Get this PR's tools

On a Linux build host or Linux VM with root and loop-device support:

```bash
git clone --branch sd-card-flash --single-branch https://github.com/Pallas-Robotics/Baymax.git
cd Baymax
python3 --version  # Python 3.12 or newer
```

After this PR is merged, cloning `main` provides the same tools. Windows/macOS
can use a suitable Linux VM for building and an image writer on their desktop
for the final card write. Direct Windows/macOS image building is not supported.
Allow at least 20 GiB of free build space and an 8 GiB or larger microSD card.

## 2. Configure and build

Follow the [builder prerequisites](../devops/opi4-flash/README.md#build-on-linux)
for your Linux release, then:

```bash
cd devops/opi4-flash
python3 configure_image.py
sudo python3 build.py --settings settings.private.json --output output
sudo chown "$USER:$(id -gn)" output output/ember-opi4.img \
  output/ready-image.json output/device-access.private.json
```

Choose Wi-Fi, time zone, and a runtime branch. `release` is the default runtime;
this tool branch does not automatically select an unmerged runtime PR. To test
**OPi4 V1** before it is merged, select `opi4-chat-timing` in the configurator.
Leave the Gemini key blank to wait for manual Setup before capture. A configured
key can enable automatic microphone/camera use after installation.

The builder writes an image file, not a physical card. It checks pinned asset
hashes, installs the selected runtime and runs its offline gate. Keep the private
access file with the image; it contains the generated login/AnyDesk passwords.
Use a new output directory for another card; builds do not overwrite old outputs.

## 3. Verify before writing

Run this PR's read-only verifier on Linux, Windows or macOS with Python 3.9 or
newer (the `python3` that comes with macOS works):

```bash
python3 verify_image.py --manifest output/ready-image.json
```

It checks the builder's board/repository label, image filename, byte count and
SHA256. It prints no Wi-Fi or access credentials and never opens a physical disk.
An older image without a board-labelled manifest must be rebuilt to use this
check. A checksum detects corruption relative to its manifest; it is not a
signature, boot test or independent verification of the manifest's publisher.

## 4. Flash the card

Open your image writer, select `output/ember-opi4.img`, then select the intended
microSD by its identity and capacity. Flash and enable the writer's verification
step. Writing erases that selected card; back up anything needed first. Do not
select your computer's system disk. Eject the card after verification and cancel
desktop prompts to format its Linux partitions.

## 5. First boot

Insert the card with board power off. Connect the USB microphone/speaker and
optional camera, provide the configured Wi-Fi, then power on. From the same
reachable network open `http://HOSTNAME.local:8765/`, replacing HOSTNAME with the
generated name in the private access file. Use the router's IP if mDNS fails.
Check provisioning, AnyDesk, Setup, actual device selection and a spoken reply.
Record the installed runtime branch/commit and physical behavior before calling
the card validated. See [full first-boot checks](../devops/opi4-flash/README.md#flash-and-first-boot).

## Offline tests

From the repository root:

```bash
python3 -m unittest discover -s devops/opi4-flash -p 'test_*.py'
```

These tests never flash a card, mount a real image, or start microphone/camera
capture. Building a real image, writing a spare card, board-specific boot,
networking, conversation and optional eyes remain separate hardware checks.
