#!/bin/bash
# Install (or update) the Orange Pi Wi-Fi setup hotspot so it starts at every boot:
# whenever the board has no working network for 30 s, the open Ember-Setup-XXXX hotspot
# appears, and it closes once the board is online again. Run as root on an Orange Pi:
#   sudo bash deploy/host/install_wifi_setup.sh
# Safe to run again (it is also the update step). Not for x86 robots, which keep their
# own always-on hotspot on a second radio (startup.sh).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
for f in ember_wifi_setup.py ember-wifi-setup.service; do
    [ -f "$HERE/$f" ] || { echo "Missing $HERE/$f"; exit 1; }
done
missing=()
for p in hostapd dnsmasq-base iw; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update
    apt-get install -y --no-install-recommends "${missing[@]}"
fi
systemctl mask hostapd >/dev/null 2>&1 || true   # the script runs hostapd itself, only while offline
install -D -m 644 "$HERE/ember_wifi_setup.py" /usr/local/lib/ember/ember_wifi_setup.py
install -D -m 644 "$HERE/ember-wifi-setup.service" /etc/systemd/system/ember-wifi-setup.service
systemctl daemon-reload
systemctl enable ember-wifi-setup.service
systemctl restart ember-wifi-setup.service
sleep 3
if systemctl is-active --quiet ember-wifi-setup.service; then
    echo "Wi-Fi setup hotspot installed and running: it opens whenever there is no Wi-Fi."
    echo "Log: journalctl -u ember-wifi-setup"
else
    echo "ember-wifi-setup did not start; see: journalctl -u ember-wifi-setup"
    exit 1
fi
