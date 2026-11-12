#!/bin/bash
# Unit 001 defaults below. Units set up by deploy/provision get these from
# ember.service: everything in /opt/ember, logs in /var/log/ember.
BAYMAX_HOME=${BAYMAX_HOME:-/home/meowmax/Baymax}         # this script and the portal
APP_DIR=${APP_DIR:-$BAYMAX_HOME/Baymax/Baymax-main}     # device_config.py (and the venv app)
LOG_DIR=${LOG_DIR:-$BAYMAX_HOME}                        # boot logs
EMBER_COMPOSE=${EMBER_COMPOSE:-$APP_DIR/deploy/docker/compose.yaml}
exec >> "$LOG_DIR/startup_boot.log" 2>&1
echo "startup.sh began at $(date)"

# Per-robot settings from /etc/ember/device.toml (see deploy/device.example.toml).
# The second argument is used if the app folder can't be read, so a robot
# still boots with a different branch checked out.
cfg() {
    /usr/bin/python3 "$APP_DIR/device_config.py" "$1" || echo "$2"
}
CORE_USER=$(cfg system.user meowmax)                 # runs the AI core; owns PipeWire
CORE_UID=$(id -u "$CORE_USER")
CORE_HOME=$(getent passwd "$CORE_USER" | cut -d: -f6)
AP_IFACE=$(cfg network.ap_iface wlo1)
HOTSPOT=$(cfg network.hotspot_connection Baymax_Hotspot)
RUNTIME=$(cfg system.runtime venv)                   # venv | docker

# Audio levels applied once per boot. WirePlumber restores whatever level was
# last used (which can be 0% and muted), so set and unmute explicitly.
SPEAKER_VOLUME=$(cfg audio.speaker_volume 75%)
MIC_VOLUME=$(cfg audio.mic_volume 85%)
SPEAKER_SINK_MATCH=$(cfg audio.speaker_match "UACDemo Jieli")   # space-separated; same speaker the AI core pins

# PipeWire is the core user's service, so pactl has to run as that user.
pactl_user() {
    sudo -u "$CORE_USER" env XDG_RUNTIME_DIR="/run/user/$CORE_UID" pactl "$@"
}

set_audio_levels() {
    local sink="" i
    # The USB speaker can take a few seconds to appear after boot.
    for i in {1..15}; do
        sink=$(pactl_user list sinks short 2>/dev/null | awk -v m="$SPEAKER_SINK_MATCH" '
            BEGIN { n = split(tolower(m), want, " ") }
            { for (j = 1; j <= n; j++) if (index(tolower($2), want[j])) { print $2; exit } }')
        [ -n "$sink" ] && break
        sleep 2
    done
    if [ -n "$sink" ]; then
        pactl_user set-sink-mute "$sink" 0
        pactl_user set-sink-volume "$sink" "$SPEAKER_VOLUME"
        echo "Speaker $sink set to $SPEAKER_VOLUME at $(date)"
    else
        echo "WARNING: USB speaker ($SPEAKER_SINK_MATCH) not found; speaker volume not set"
    fi

    # The AI core records from PipeWire's default source, so that is the one to set.
    if pactl_user set-source-mute @DEFAULT_SOURCE@ 0 && pactl_user set-source-volume @DEFAULT_SOURCE@ "$MIC_VOLUME"; then
        echo "Microphone $(pactl_user get-default-source) set to $MIC_VOLUME at $(date)"
    else
        echo "WARNING: could not set microphone volume"
    fi
}

start_webapp() {
    cd "$APP_DIR" || exit
    source venv/bin/activate
    echo "Starting Baymax web app at $(date)" >> "$BAYMAX_HOME/gemini_boot.log"
    python3 baymax_app.py >> "$BAYMAX_HOME/webapp_boot.log" 2>&1 &
    WEBAPP_PID=$!
    echo "Web app started (PID $WEBAPP_PID)" >> "$BAYMAX_HOME/gemini_boot.log"
    # Wait for Flask to be ready
    for i in {1..15}; do
        if wget -q --spider --timeout=2 http://localhost:5000/ 2>/dev/null; then
            echo "Web app is ready" >> "$BAYMAX_HOME/gemini_boot.log"
            break
        fi
        sleep 1
    done
}

run_gemini_with_retry() {
    # Added the while loop so it ACTUALLY retries!
    while true; do
        # Ensure ChromaDB store is writable by all users (root creates it, the core user uses it)
        chmod -R 777 "$APP_DIR/chroma_store/" 2>/dev/null

        echo "Starting Gemini script at $(date)" >> "$BAYMAX_HOME/gemini_boot.log"

        # Run as the core user so PipeWire audio devices are accessible (PipeWire is a user service)
        sudo -u "$CORE_USER" env \
            HOME="$CORE_HOME" \
            XDG_RUNTIME_DIR="/run/user/$CORE_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$CORE_UID/bus" \
            APP_DIR="$APP_DIR" \
            bash -c '
            # Route OpenCV V4L2 calls through PipeWire so camera is shareable
            export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/pipewire-0.3/v4l2/libpw-v4l2.so
            cd "$APP_DIR"
            source venv/bin/activate
            # v10 only exists on the fleet-management branch until it merges.
            # If another branch is checked out, run v8 instead of crash-looping.
            core=realtime_gemini_10.py
            if [ ! -f "$core" ]; then
                echo "realtime_gemini_10.py not found (branch: $(git branch --show-current 2>/dev/null)); falling back to realtime_gemini_8.py"
                core=realtime_gemini_8.py
            fi
            python3 "$core"
        ' >> "$BAYMAX_HOME/gemini_boot.log" 2>&1

        echo "Gemini script stopped/crashed with exit code $?. Restarting in 5 seconds..." >> "$BAYMAX_HOME/gemini_boot.log"
        sleep 5
    done
}

# docker runtime: the AI core and dashboard run from the published image
# (tagged ember:current by provisioning and ember-update.sh). Compose restarts
# them if they crash; nothing starts them at boot except this script, so they
# still wait for internet like the venv path.
start_containers() {
    echo "Starting containers from ember:current at $(date)"
    EMBER_UID=$CORE_UID EMBER_IMAGE=ember:current \
        docker compose -f "$EMBER_COMPOSE" up -d --no-build --pull never
}

start_robot() {
    if [ "$RUNTIME" = docker ]; then
        start_containers
        wait    # keep running alongside the portal supervisor
    else
        start_webapp
        run_gemini_with_retry
    fi
}

# Keep the captive portal up for the WHOLE session, not just while offline.
# It is the only way to hand Baymax new Wi-Fi credentials, so it has to be
# reachable on the always-on hotspot even when internet is working fine.
start_portal_supervised() {
    local i
    pkill -f baymax_portal.py
    fuser -k 80/tcp 2>/dev/null
    for i in {1..10}; do    # until port 80 is free
        fuser 80/tcp >/dev/null 2>&1 || break
        sleep 0.5
    done
    (
        cd "$BAYMAX_HOME" || exit
        while true; do
            echo "Starting captive portal at $(date)" >> "$LOG_DIR/portal_boot.log"
            /usr/bin/python3 baymax_portal.py >> "$LOG_DIR/portal_boot.log" 2>&1
            echo "Portal exited ($?). Restarting in 5s..." >> "$LOG_DIR/portal_boot.log"
            sleep 5
        done
    ) &
    echo "Captive portal supervisor started (always-on)"
}

# Phones on the hotspot look up these names to test for internet. Point them at the
# portal, which answers "online" while the robot has internet and sends the phone to
# the setup page while it does not. Without this, an offline hotspot just shows
# "No Internet" and the page never opens. NetworkManager's hotspot DNS reads
# dnsmasq-shared.d when the hotspot starts, so a change is applied by restarting it.
CAPTIVE_DNS=/etc/NetworkManager/dnsmasq-shared.d/ember-captive.conf
ensure_captive_dns() {
    local addr want name
    addr=$(nmcli -g ipv4.addresses connection show "$HOTSPOT" 2>/dev/null | cut -d, -f1 | cut -d/ -f1)
    addr=${addr:-10.42.0.1}    # NetworkManager's default for a shared connection
    want="# Written by startup.sh: phones' internet checks go to the Wi-Fi setup portal."
    for name in captive.apple.com connectivitycheck.gstatic.com connectivitycheck.android.com \
                clients3.google.com www.msftconnecttest.com www.msftncsi.com \
                detectportal.firefox.com nmcheck.gnome.org; do
        want+=$'\n'"address=/$name/$addr"
    done
    [ "$(cat "$CAPTIVE_DNS" 2>/dev/null)" = "$want" ] && return
    mkdir -p "$(dirname "$CAPTIVE_DNS")"
    printf '%s\n' "$want" > "$CAPTIVE_DNS"
    echo "Captive DNS for the hotspot written ($addr) at $(date)"
    if nmcli -t -f NAME connection show --active | grep -qxF "$HOTSPOT"; then
        nmcli --wait 30 connection up "$HOTSPOT" >/dev/null && echo "$HOTSPOT restarted to apply it"
    fi
}

# NetworkManager starts the hotspot by itself (connection.autoconnect=yes), but it
# gives up after a few failed tries and never retries after some radio errors.
# Check every 30 s for the whole session and bring it back when it is down.
keep_hotspot_up() {
    local state said=""
    while true; do
        if nmcli -t -f NAME,DEVICE connection show --active | grep -qxF "$HOTSPOT:$AP_IFACE"; then
            said=""
        else
            state=$(nmcli -t -f DEVICE,STATE,CONNECTION device status | awk -F: -v d="$AP_IFACE" '$1 == d { print $2 " (" $3 ")" }')
            case "$state" in
                connected*|connecting*)
                    # The radio is joined to some other network; taking it would cut that off.
                    [ "$said" = "$state" ] || echo "Hotspot down: $AP_IFACE is in use, $state, at $(date)" ;;
                "")
                    [ "$said" = none ] || echo "Hotspot down: no $AP_IFACE device at $(date)"
                    state=none ;;
                *)
                    [ "$said" = "$state" ] || echo "Hotspot down ($AP_IFACE $state) at $(date); bringing it up (retried every 30 s)"
                    if nmcli --wait 30 connection up "$HOTSPOT" ifname "$AP_IFACE" >/dev/null 2>&1; then
                        echo "$HOTSPOT back up at $(date)"
                        state=""
                    fi ;;
            esac
            said=$state
        fi
        sleep 30
    done
}

# The hotspot and portal need no internet, and a phone looks for the sign-in page
# only right after it joins, so they come before every wait below.
ensure_captive_dns
nmcli connection modify "$HOTSPOT" connection.autoconnect-retries 0 2>/dev/null \
    || echo "WARNING: no $HOTSPOT connection to keep retrying"
start_portal_supervised
keep_hotspot_up &

# Give Debian time to initialize the Wi-Fi hardware
sleep 30

# Before the network checks, so levels are right even on the offline path.
set_audio_levels

# Wait for DNS to be ready
for i in {1..10}; do
    if host google.com > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# The hotspot is always-on via NetworkManager (connection.autoconnect=yes) and
# keep_hotspot_up; log radio-level proof once.
/usr/sbin/iw dev "$AP_IFACE" info 2>&1 | grep -E "ssid|type|channel"

# Ping a reliable server to check for an existing internet connection
if wget -q --spider --timeout=10 https://www.google.com; then
    echo "Internet verified. Booting main robot processes..."
    start_robot
else
    echo "No internet detected. Hotspot + portal are up; waiting for credentials..."

    while true; do
        sleep 10
        if wget -q --spider --timeout=10 https://www.google.com; then
            echo "Internet detected. Launching realtime gemini script (portal stays up)"

            # Hotspot is now always-on via NetworkManager autoconnect; do NOT take it down --
            # it is the only way in when Wi-Fi drops.  (was: nmcli connection down Baymax_Hotspot)
            sleep 2

            for i in {1..10}; do
                if host google.com > /dev/null 2>&1; then
                    break
                fi
                sleep 2
            done

            start_robot
            break
        fi
        echo "Still no internet, portal still running. Clients on hotspot: $(/usr/sbin/iw dev "$AP_IFACE" station dump 2>/dev/null | grep -c Station)"
    done
fi
