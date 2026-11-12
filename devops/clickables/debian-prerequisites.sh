#!/bin/bash
set -eu
if [ ! -r /etc/os-release ]; then echo "Debian version could not be identified."; exit 1; fi
. /etc/os-release
if [ "${ID:-}" != "debian" ] || [ "${VERSION_ID:-}" != "13" ]; then
  echo "This prerequisite installer is for Debian 13. Ask your lead for your distribution's packages."
  exit 1
fi
PACKAGES=(curl ca-certificates git libportaudio2 libgl1 libegl1 libgles2 libglib2.0-0t64 libsndfile1 libasound2-plugins build-essential cmake pkg-config xdg-utils xfce4-terminal)
BROWSER_FOUND=0
for BROWSER in firefox firefox-esr chromium chromium-browser x-www-browser; do
  if command -v "$BROWSER" >/dev/null 2>&1; then BROWSER_FOUND=1; break; fi
done
if [ "$BROWSER_FOUND" -eq 0 ]; then PACKAGES+=(firefox-esr); fi
echo "This step installs Debian system libraries and build tools using apt:"
printf '  %s\n' "${PACKAGES[@]}"
echo "It does not install NPU drivers, change the kernel, configure an audio server or start capture."
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
ORANGE_PI=0
case "$MODEL" in *"Orange Pi"*|*OrangePi*) ORANGE_PI=1;; esac
if [ "$ORANGE_PI" -eq 1 ]; then
  echo "On this $MODEL it also installs the Wi-Fi setup hotspot service (hostapd, dnsmasq-base, iw):"
  echo "whenever the board has no Wi-Fi for 30 s, the open Ember-Setup hotspot appears so a phone"
  echo "can give it Wi-Fi details, and it closes once the board is online. It starts at every boot."
fi
read -r -p "Install these system packages now? [y/N] " ANSWER
case "$ANSWER" in y|Y|yes|YES) ;; *) echo "Cancelled. No packages installed."; exit 0;; esac
if [ "$(id -u)" -eq 0 ]; then ADMIN=();
elif command -v sudo >/dev/null 2>&1; then ADMIN=(sudo);
elif command -v pkexec >/dev/null 2>&1; then ADMIN=(pkexec);
else echo "Administrator access is needed. Ask your lead to install the packages listed above."; exit 1; fi
"${ADMIN[@]}" apt-get update
"${ADMIN[@]}" apt-get install --no-install-recommends "${PACKAGES[@]}"
if [ "$ORANGE_PI" -eq 1 ]; then
  "${ADMIN[@]}" bash "$(cd "$(dirname "$0")" && pwd)/../../Baymax-main/deploy/host/install_wifi_setup.sh"
fi
echo "System prerequisites installed. Open 1 - Setup next. Ensure a browser and working desktop audio are configured."
