# Host boot tooling

Tracked copies of the files that boot a robot. They are **copies, not the live files yet**: the robot still runs them from `/home/meowmax/Baymax/` and `/etc/systemd/system/`. Running the robot directly from these copies is a later fleet-management step that needs root.

`startup.sh` launches `realtime_gemini_10.py`. Until this branch merges, v10 only exists on `fleet-management`, so if another branch is checked out, `startup.sh` falls back to `realtime_gemini_8.py` rather than crash-looping.

| File | Live location | Role |
|---|---|---|
| `startup.sh` | `/home/meowmax/Baymax/startup.sh` | Boot orchestrator: audio levels, hotspot, portal, web app, AI core restart loop |
| `baymax_portal.py` | `/home/meowmax/Baymax/baymax_portal.py` | Always-on captive portal (port 80) for entering Wi-Fi credentials over the hotspot |
| `test_hotspot.sh` | `/home/meowmax/Baymax/test_hotspot.sh` | Headless test of the no-internet boot path |
| `baymax.service` | `/etc/systemd/system/baymax.service` | systemd unit that runs `startup.sh` as root |
| `ember_wifi_setup.py` | `/usr/local/lib/ember/ember_wifi_setup.py` | Orange Pi boards (one radio): setup hotspot and phone page when no saved Wi-Fi works |
| `ember-wifi-setup.service` | `/etc/systemd/system/ember-wifi-setup.service` | Orange Pi boards: runs `ember_wifi_setup.py` as root |

If you change a live file, copy it back here so the two don't drift.

## Per-robot settings

The user, AP radio, hotspot connection, speaker, camera indices, audio levels and fall detection on/off come from `/etc/ember/device.toml` (see `../device.example.toml`). Without that file, everything defaults to this LattePanda's values. To install it on a robot:

```bash
sudo mkdir -p /etc/ember
sudo cp deploy/device.example.toml /etc/ember/device.toml   # then edit for this unit
```

`startup.sh` reads values with `python3 device_config.py section.key`, falling back to built-in defaults if the app folder can't be read. The portal reads the file directly, since it runs outside the app folder.

## Orange Pi Wi-Fi setup hotspot

The Orange Pi boards have one Wi-Fi radio, so `ember_wifi_setup.py` takes turns
between the Wi-Fi client and a setup hotspot on it:

- While a default route through a working interface exists (a joined Wi-Fi network
  or Ethernet), it does nothing. A route the kernel marks `linkdown` after Wi-Fi
  drops, or Wi-Fi that has not finished its handshake, does not count.
- After 90 s offline it starts a WPA2 hotspot `Ember-Setup-XXXX` (last four
  characters of the hostname) on `192.168.4.1`, with DHCP and DNS pointing every
  name at the board, so phones open the page as a sign-in page. The page lists
  nearby networks (strongest first) and takes a name and password.
- A submitted network is saved (priority 200, derived PSK only, same-name entry
  replaced, other networks kept), the hotspot closes and the board tries for 90 s.
  If that fails, the hotspot returns with an error message.
- The hotspot stays up while the page is in use or a phone is connected. After five
  minutes with neither, or 15 minutes in any case, it looks for saved networks:
  it scans (about 10 s off the air) and leaves the hotspot only when a saved
  network is in range, or every third time in case one was missed or is hidden.
- If the hotspot fails or goes off the air (hostapd or dnsmasq exits, the access
  point is disabled), it is restarted within seconds and the error is logged.
- If Ethernet starts working while the hotspot is up, the hotspot closes within
  about 10 s and the Wi-Fi client comes back.

The script picks the network stack at startup:

| | Orange Pi 4 Pro (DietPi) | Orange Pi 5 Max (Armbian) |
|---|---|---|
| Stack | ifupdown + `wpa_supplicant` | NetworkManager |
| Hotspot | `hostapd` and `dnsmasq`, run by the script | a NetworkManager access-point connection (`ipv4.method shared`) |
| Saved networks | `/etc/wpa_supplicant/wpa_supplicant.conf` | NetworkManager connections named "*network* (Ember setup)" |
| Wi-Fi client | `ifup@wlan0.service` (allow-hotplug) or `ifup wlan0` | NetworkManager |

`EMBER_WIFI_STACK=ifupdown|networkmanager` and `EMBER_WIFI_IFACE` override the
choice and the interface (default `wlan0`, else the first Wi-Fi interface). It uses
only the system `python3` standard library. Install on the board, with a password of
8-63 printable ASCII characters:

```bash
# Orange Pi 4 Pro (DietPi):
sudo apt-get install -y --no-install-recommends hostapd dnsmasq-base iw
sudo systemctl mask hostapd   # the script runs hostapd itself, only when needed
# Orange Pi 5 Max (Armbian, NetworkManager):
sudo apt-get install -y --no-install-recommends dnsmasq-base iw
# Both:
sudo install -D -m 644 deploy/host/ember_wifi_setup.py /usr/local/lib/ember/ember_wifi_setup.py
sudo install -m 644 deploy/host/ember-wifi-setup.service /etc/systemd/system/
sudo -v   # sudo asks for its password first, so the two prompts don't mix
python3 -c 'import getpass, json; print(json.dumps({"password": getpass.getpass("Hotspot password: ")}))' \
    | sudo install -D -m 600 /dev/stdin /etc/ember/wifi-setup.json
sudo systemctl daemon-reload
sudo systemctl enable --now ember-wifi-setup
```

To update an installed copy, repeat the two `install` lines, then
`sudo systemctl daemon-reload && sudo systemctl restart ember-wifi-setup`.

The hotspot never runs open: if `/etc/ember/wifi-setup.json` is missing or its
password is not 8-63 printable ASCII characters, the service logs why once and
stops (exit 78, not restarted). Give each robot its own password and put it on the
robot's label. The script is installed outside the user's release folder because it
runs as root. A stop or restart of the service closes the hotspot and hands the
radio back to the Wi-Fi client; a run that died mid-hotspot is cleaned up at the
next start. Log: `journalctl -u ember-wifi-setup`.

No board has run the hotspot yet. Hardware test pending, on each board: boot with
the saved network off and confirm the hotspot appears and needs the password; join
from an iPhone and an Android phone and confirm the page opens and lists networks;
a correct password joins and the hotspot closes; a wrong one brings it back with the
error; a phone left connected does not keep the hotspot up past 15 minutes; a saved
network coming back is picked up within about six minutes; no hotspot runs while a
saved network or Ethernet works; turning the router off while online brings the
hotspot within about two minutes; `systemctl restart ember-wifi-setup` while online
keeps the Wi-Fi connection; the build-time network still works after adding one.
On the 4 Pro the DietPi Wi-Fi menu (`dietpi-network`, linked from Device Settings)
rewrites `wpa_supplicant.conf` from its own list, which removes networks saved from
the page.

## Known issues

- Fixed 2026-10-06: root's crontab also launched `startup.sh` at boot (`@reboot`), so two AI cores ran. The line was removed; after the next reboot only one copy started. `baymax.service` should be the only launcher on every robot.
- `startup.sh` sets `LD_PRELOAD=.../libpw-v4l2.so`, but the `pipewire-v4l2` package is not installed, so the preload is ignored and OpenCV opens the camera directly.
- `startup.sh` still hardcodes `BAYMAX_HOME=/home/meowmax/Baymax` (where it, the portal and the logs live), and `test_hotspot.sh` hardcodes this unit's USB dongle (`wlx3c3300008209`).
