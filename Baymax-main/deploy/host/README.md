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

## x86 robot hotspot and portal

The hotspot is a NetworkManager connection on the built-in radio (`network.ap_iface`,
`wlo1`), up the whole time; the robot joins the user's Wi-Fi on a USB dongle.
`startup.sh` starts, before anything that waits:

- `baymax_portal.py` on port 80, restarted whenever it exits;
- a watchdog that checks the hotspot every 30 s and brings it back up when it is down
  and the radio is free (NetworkManager stops retrying after a few failed tries); it
  also sets the connection's `autoconnect-retries` to 0 (retry forever) on every boot;
- `/etc/NetworkManager/dnsmasq-shared.d/ember-captive.conf`, which points the names
  phones use to check for internet (`captive.apple.com`,
  `connectivitycheck.gstatic.com` and others; no real sites) at the hotspot's address.
  While the robot has internet the portal answers those checks the way each phone
  expects, so phones keep using the hotspot normally; while it is offline it sends
  them to the setup page, which iOS and Android then open as a sign-in page. The page
  is also at `http://10.42.0.1` (the hotspot's address; `provision.sh` fixes it).

The page suggests the networks the dongle can see, does not let the phone keyboard
capitalize or autocorrect the name, keeps the password exactly as typed, rescans for
the network before joining (`nmcli` only joins networks in its last scan), moves a
saved profile of the same name onto the dongle, and reports failures and timeouts on
`/status` without the password. Logs: `portal_boot.log` and `startup_boot.log` in the
log folder.

Hardware test pending (unit 001): reboot with the dongle's network off and confirm
the hotspot and page appear within a minute and an iPhone and an Android phone open
the page by themselves; with internet, confirm a phone on the hotspot shows no
sign-in page and browses; take the hotspot down (`nmcli connection down
Baymax_Hotspot`) and confirm it is back within a minute; join a network whose name
starts with a lowercase letter; a password with a space at the end.

## Orange Pi Wi-Fi setup hotspot

The Orange Pi boards have one Wi-Fi radio. `ember_wifi_setup.py` runs an open setup
hotspot `Ember-Setup-XXXX` (last four letters or digits of the hostname, e.g.
`Ember-Setup-5MAX`). A phone joins it with no password, the sign-in page opens by
itself, and the person picks or types their home Wi-Fi and its password; Ember then
joins that network. It runs in one of two ways.

The hotspot opens after 30 s without a working network (Wi-Fi or Ethernet) and closes
about 15 s after Ember is online again, every time. While Ember is online there is no
hotspot, so it costs nothing then. It runs in one of two ways:

**Next to the Wi-Fi client (both boards' drivers support it).** At startup the script
reads the radio's `iw phy` interface combinations. The 4 Pro's aic8800 allows one Wi-Fi
client plus one access point on up to 3 channels, and the 5 Max's bcmdhd (AP6611S) allows
up to 2. When a combination allows a client and an access point on 2 or more channels,
the hotspot runs on a second interface, `ember-ap`, on the same radio, and `wlan0` stays
the Wi-Fi client:

- The client keeps looking for its saved networks the whole time, so Ember rejoins by
  itself when the router comes back, and the hotspot then closes.
- A network from the page is joined with the phone still connected, and the page shows
  "Connected" or why it failed. The result counts only if the page's own saved network
  is the one in use.
- The hotspot is on 2.4 GHz channel 6, or on the client's channel when the client is
  linked but not yet online.
- Its address is `192.168.4.1`, or `10.88.0.1` / `172.31.88.1` if the home network uses
  that range. DHCP and DNS point every name at the board, so phones open the page as a
  sign-in page.
- If the access point cannot start three times in a row, or the client finds no
  networks for 10 minutes while it is up, the hotspot falls back to taking turns until
  Ember is online. The next time Ember goes offline, the second interface is tried again.
  `EMBER_WIFI_TAKE_TURNS=1` (`sudo systemctl edit ember-wifi-setup`, under `[Service]`)
  always takes turns.

Tested on the Orange Pi 5 Max, 2026-11-12:
- Ember was taken off its Wi-Fi. The open hotspot appeared 31 s later.
- A phone joined and the page opened.
- Verizon and its password were submitted; Ember was online on the page's own network
  2 s later, and the hotspot closed 18 s after that.
- With the hotspot up and Ember online (the always-on mode this replaced), the radio
  time-shared between the router's 5 GHz radar channel and the hotspot. Router
  round-trips went from 2.5 ms to 18 ms (median) and 70 ms (worst), and the page was
  slow. That is why the hotspot now runs only while Ember is offline.

**Never left stuck.** The service is `Type=notify` with `WatchdogSec=60`: the script
pings systemd while it makes progress, so if it hangs for 10 minutes, systemd restarts
it; if it crashes, `Restart=always` restarts it within 10 s. An error in one round is
logged and the next round runs. A run that died mid-hotspot is cleaned up at start.

**Taking turns (any other radio, or the fallback above):**

- While a default route through a working interface exists (a joined Wi-Fi network
  or Ethernet), it does nothing. A route the kernel marks `linkdown` after Wi-Fi
  drops, or Wi-Fi that has not finished its handshake, does not count.
- After 30 s offline it starts the open hotspot `Ember-Setup-XXXX` (last four
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
| Always-on hotspot | `hostapd` and `dnsmasq` on `ember-ap`, run by the script | the same; NetworkManager is told to leave `ember-ap` alone (`/etc/NetworkManager/conf.d/ember-wifi-setup.conf`) |
| Taking-turns hotspot | `hostapd` and `dnsmasq` on `wlan0` | a NetworkManager access-point connection (`ipv4.method shared`) |
| Saved networks | `/etc/wpa_supplicant/wpa_supplicant.conf` | NetworkManager connections named "*network* (Ember setup)" |
| Wi-Fi client | `ifup@wlan0.service` (allow-hotplug) or `ifup wlan0` | NetworkManager |

`EMBER_WIFI_STACK=ifupdown|networkmanager` and `EMBER_WIFI_IFACE` override the
choice and the interface (default `wlan0`, else the first Wi-Fi interface);
`EMBER_WIFI_AP_IFACE` names the second interface (default `ember-ap`) and
`EMBER_WIFI_TAKE_TURNS=1` never uses it. It uses
only the system `python3` standard library. Install on the board:

```bash
sudo apt-get install -y --no-install-recommends hostapd dnsmasq-base iw
sudo systemctl mask hostapd   # the script runs hostapd itself
sudo install -D -m 644 deploy/host/ember_wifi_setup.py /usr/local/lib/ember/ember_wifi_setup.py
sudo install -m 644 deploy/host/ember-wifi-setup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ember-wifi-setup
```

To update an installed copy, repeat the two `install` lines, then
`sudo systemctl daemon-reload && sudo systemctl restart ember-wifi-setup`.

The hotspot is open, so anyone within Wi-Fi range can open the page and change the
network Ember joins (not read its saved passwords, which are stored only as derived
keys). To require a password instead, write one (8-63 printable ASCII characters) to
`/etc/ember/wifi-setup.json` and restart the service; with an invalid file the
service logs why once and stops (exit 78, not restarted):

```bash
sudo -v   # sudo asks for its password first, so the two prompts don't mix
python3 -c 'import getpass, json; print(json.dumps({"password": getpass.getpass("Hotspot password: ")}))' \
    | sudo install -D -m 600 /dev/stdin /etc/ember/wifi-setup.json
```

The script is installed outside the user's release folder because it
runs as root. A stop or restart of the service closes the hotspot and hands the
radio back to the Wi-Fi client; a run that died mid-hotspot is cleaned up at the
next start. Log: `journalctl -u ember-wifi-setup`.

Hardware test pending: the Orange Pi 4 Pro (all of it), and on the 5 Max an iPhone
and an Android phone each, a wrong password (the page should say why and the hotspot
stay up), the router switched off while Ember is online (the hotspot should appear
within about 40 s and close again when the router is back), Ethernet, a reboot with no
Wi-Fi, and `EMBER_WIFI_TAKE_TURNS=1`.
On the 4 Pro the DietPi Wi-Fi menu (`dietpi-network`, linked from Device Settings)
rewrites `wpa_supplicant.conf` from its own list, which removes networks saved from
the page.

## Known issues

- Fixed 2026-10-06: root's crontab also launched `startup.sh` at boot (`@reboot`), so two AI cores ran. The line was removed; after the next reboot only one copy started. `baymax.service` should be the only launcher on every robot.
- `startup.sh` sets `LD_PRELOAD=.../libpw-v4l2.so`, but the `pipewire-v4l2` package is not installed, so the preload is ignored and OpenCV opens the camera directly.
- `startup.sh` still hardcodes `BAYMAX_HOME=/home/meowmax/Baymax` (where it, the portal and the logs live), and `test_hotspot.sh` hardcodes this unit's USB dongle (`wlx3c3300008209`).
