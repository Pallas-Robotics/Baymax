from flask import Flask, request, render_template_string, redirect
from html import escape
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.request

app = Flask(__name__)


def _device_setting(section, key, default):
    """Reads /etc/ember/device.toml, the same file as the app's device_config.py.
    The portal runs outside the app checkout, so it reads the file directly."""
    try:
        with open(os.getenv("EMBER_DEVICE_CONFIG", "/etc/ember/device.toml"), "rb") as f:
            return tomllib.load(f).get(section, {}).get(key, default)
    except (OSError, tomllib.TOMLDecodeError):
        return default


# The AP radio. The hotspot lives here permanently and must never be torn down --
# it is the only way back in if the client connection fails.
AP_IFACE = _device_setting("network", "ap_iface", "wlo1")
HOTSPOT = _device_setting("network", "hotspot_connection", "Baymax_Hotspot")

# Last attempt's outcome, shown on the status page.
last_status = {"ssid": None, "state": "idle", "detail": ""}


def nmcli_fields(output):
    """`nmcli -t` lines as lists of fields, with nmcli's backslash escapes removed."""
    for line in output.splitlines():
        yield [re.sub(r"\\(.)", r"\1", field) for field in re.split(r"(?<!\\):", line)]


def uplink_iface():
    """Pick the Wi-Fi device to join the user's network with: any managed wifi
    device that is NOT the AP radio (i.e. the USB dongle). Never AP_IFACE, or we
    would kill the hotspot we are being reached over."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for fields in nmcli_fields(out):
            if len(fields) >= 3 and fields[1] == "wifi" and fields[0] != AP_IFACE \
                    and not fields[2].startswith("unmanaged"):
                return fields[0]
    except Exception as e:
        print(f"uplink_iface lookup failed: {e}", flush=True)
    return None


def client_profile(name):
    """True if a saved connection called `name` is a Wi-Fi client profile."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "connection.type,802-11-wireless.mode", "connection", "show", name],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return False
        settings = {fields[0]: fields[-1] for fields in nmcli_fields(out.stdout)}
        return settings.get("connection.type") == "802-11-wireless" \
            and settings.get("802-11-wireless.mode", "") in ("", "infrastructure")
    except Exception:
        return False


def nearby_networks(iface):
    """Network names in the device's last scan, for the page's suggestions."""
    if not iface:
        return []
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", iface, "--rescan", "no"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    names = []
    for fields in nmcli_fields(out):
        if fields[0] and fields[0] not in names:
            names.append(fields[0])
    return names


def portal_url():
    """The hotspot's own address, where phones are sent for the setup page."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", AP_IFACE],
                             capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
        if match:
            return f"http://{match[1]}/"
    except Exception:
        pass
    return "http://10.42.0.1/"


_internet = {"checked": 0.0, "up": False}
_internet_lock = threading.Lock()


def internet_up():
    """Whether the robot itself has internet, checked at most every 15 s."""
    with _internet_lock:
        if time.monotonic() - _internet["checked"] < 15:
            return _internet["up"]
    try:
        with urllib.request.urlopen("http://connectivitycheck.gstatic.com/generate_204", timeout=3) as response:
            up = response.status == 204
    except Exception:
        up = False
    with _internet_lock:
        _internet.update(checked=time.monotonic(), up=up)
    return up


SETUP_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Baymax Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, sans-serif; padding: 30px; max-width: 400px; margin: auto; text-align: center; }
        input { width: 90%; padding: 12px; margin: 10px 0; border-radius: 8px; border: 1px solid #ccc; font-size: 16px; }
        button { width: 95%; padding: 15px; background: #E03C31; color: white; border: none; border-radius: 8px; font-size: 18px; font-weight: bold; cursor: pointer; margin-top: 15px;}
        .note { color: #666; font-size: 14px; margin-top: 20px; }
        .error { background: #fff3cd; padding: 12px; border-radius: 8px; }
    </style>
</head>
<body>
    <h1 style="color: #333;">Hello, I am Baymax.</h1>
    <p style="color: #666; margin-bottom: 25px;">Please connect me to your Wi-Fi network.</p>
    {% if message %}<p class="error">{{ message }}</p>{% endif %}
    <form action="/setup" method="POST">
        <input type="text" name="ssid" placeholder="Wi-Fi Network Name" list="nearby" value="{{ ssid }}" required
               autocapitalize="none" autocorrect="off" spellcheck="false" autocomplete="off">
        <datalist id="nearby">{% for name in networks %}<option value="{{ name }}">{% endfor %}</datalist>
        <input type="password" name="password" placeholder="Wi-Fi Password (leave blank if none)">
        <button type="submit">Connect Baymax</button>
    </form>
    <p class="note">This hotspot stays up the whole time, so you will not be
    disconnected. Check <a href="/status">status</a> after submitting.</p>
</body>
</html>
"""


def setup_page(message="", ssid="", status=200):
    networks = [name for name in nearby_networks(uplink_iface()) if name != HOTSPOT]
    return render_template_string(SETUP_PAGE, message=message, ssid=ssid, networks=networks), status


def problem(ssid, password):
    """Why this name and password cannot be a Wi-Fi network, or None."""
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
        return "The network name must be 1-32 characters."
    if ssid == HOTSPOT:
        return f"{HOTSPOT} is my own hotspot. Enter your home Wi-Fi network."
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        return "The password contains an unsupported character."
    if password and not (8 <= len(password.encode("utf-8")) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        return "Wi-Fi passwords are 8-63 characters; leave it blank for an open network."
    return None


def switch_network(ssid, password):
    """Join the user's Wi-Fi on the uplink radio. The hotspot is left running
    throughout, so whoever submitted this form keeps their connection and can
    read the result on /status."""
    last_status.update({"ssid": ssid, "state": "connecting", "detail": ""})
    try:
        _switch_network(ssid, password)
    except Exception as e:
        # Only the error's type: a timed-out nmcli command line includes the password.
        detail = "timed out" if isinstance(e, subprocess.TimeoutExpired) else type(e).__name__
        print(f"Connection to {ssid} failed: {detail}", flush=True)
        last_status.update({"state": "failed", "detail": detail})


def _switch_network(ssid, password):
    time.sleep(2)

    iface = uplink_iface()
    if not iface:
        msg = f"no uplink Wi-Fi device found (all wifi devices are {AP_IFACE})"
        print(f"Connect to {ssid} failed: {msg}", flush=True)
        last_status.update({"state": "failed", "detail": msg})
        return

    print(f"Connecting to {ssid} on {iface}...", flush=True)

    # nmcli only joins a network in the device's last scan, which can be minutes
    # old (or have missed it) while the dongle is disconnected. Scan for it first.
    subprocess.run(["nmcli", "device", "wifi", "rescan", "ifname", iface, "ssid", ssid],
                   capture_output=True, text=True, timeout=30)
    for _ in range(8):
        if ssid in nearby_networks(iface):
            break
        time.sleep(2)

    # Reuse an existing profile of the same name instead of letting nmcli mint
    # "<ssid> 1", "<ssid> 2", ... on every attempt. Point it at the uplink: a
    # profile made on an earlier dongle or the built-in radio is tied to that one.
    if client_profile(ssid):
        modify = ["nmcli", "connection", "modify", ssid, "connection.interface-name", iface]
        if password:
            modify += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        cmds = [modify, ["nmcli", "connection", "up", ssid, "ifname", iface]]
    else:
        base = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
        if password:
            base += ["password", password]
        cmds = [base]

    result = None
    for cmd in cmds:
        # nmcli gives up after 90 s itself; leave it time to say why.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            break

    if result is not None and result.returncode == 0:
        print(f"Baymax is online on {iface}!", flush=True)
        last_status.update({"state": "connected", "detail": f"joined {ssid} on {iface}"})
    else:
        err = (result.stderr or result.stdout or "unknown error").strip() if result else "no command ran"
        print(f"Connection to {ssid} failed: {err}", flush=True)
        last_status.update({"state": "failed", "detail": err})
    # NOTE: the hotspot is never taken down or brought back up here. It is
    # always-on via NetworkManager (connection.autoconnect=yes) and runs on a
    # different radio from the uplink, so the two coexist.


# Phones test for internet by fetching these. startup.sh points their host names
# at this portal on the hotspot's DNS. While the robot is online, answer the way
# each phone expects, so it keeps using the hotspot's shared internet; while it is
# offline, send the phone to the setup page, which it then opens as a sign-in page.
APPLE_SUCCESS = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
PROBE_ANSWERS = {
    "/generate_204": ("", 204, "text/plain"),
    "/gen_204": ("", 204, "text/plain"),
    "/hotspot-detect.html": (APPLE_SUCCESS, 200, "text/html"),
    "/library/test/success.html": (APPLE_SUCCESS, 200, "text/html"),
    "/connecttest.txt": ("Microsoft Connect Test", 200, "text/plain"),
    "/ncsi.txt": ("Microsoft NCSI", 200, "text/plain"),
    "/success.txt": ("success\n", 200, "text/plain"),
    "/canonical.html": ('<meta http-equiv="refresh" content="0;url=https://support.mozilla.org/kb/captive-portal"/>',
                        200, "text/html"),
    "/check_network_status.txt": ("NetworkManager is online\n", 200, "text/plain"),
}


def _probe(path):
    def answer():
        if internet_up():
            body, status, kind = PROBE_ANSWERS[path]
            return body, status, {"Content-Type": kind, "Cache-Control": "no-store"}
        return redirect(portal_url(), code=302)
    return answer


for _path in PROBE_ANSWERS:
    app.add_url_rule(_path, endpoint="probe" + _path, view_func=_probe(_path))


@app.route('/', methods=['GET'])
def home():
    return setup_page()


@app.route('/status', methods=['GET'])
def status():
    s = last_status
    return f"""
    <div style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px;">
        <h2>Status: {escape(str(s['state']))}</h2>
        <p>Network: <b>{escape(str(s['ssid'] or '-'))}</b></p>
        <p style="color:#666;">{escape(str(s['detail']))}</p>
        <p><a href="/">Back</a> &middot; <a href="/status">Refresh</a></p>
    </div>
    """


@app.route('/setup', methods=['POST'])
def setup():
    ssid = request.form.get('ssid', '').strip()
    # Not stripped: a Wi-Fi password may begin or end with a space.
    password = request.form.get('password', '')
    if not ssid:
        return redirect(portal_url())
    why = problem(ssid, password)
    if why:
        return setup_page(why, ssid, 400)
    threading.Thread(target=switch_network, args=(ssid, password), daemon=True).start()
    safe_ssid = escape(ssid)
    return f"""
    <div style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px;">
        <h2 style="color: #28a745;">Connecting...</h2>
        <p>Attempting to connect to <b>{safe_ssid}</b>.</p>
        <p>You will stay on the Baymax hotspot while this happens.</p>
        <p><a href="/status">Check status</a></p>
    </div>
    """


@app.errorhandler(404)
def page_not_found(e):
    return redirect(portal_url())


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
