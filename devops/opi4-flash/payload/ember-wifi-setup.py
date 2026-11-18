"""Start an Ember-Setup hotspot and phone page when no saved Wi-Fi works.

After GRACE seconds offline the hotspot starts on a second interface (ap0, 2.4 GHz
channel 6) while wlan0 keeps trying saved networks, so the page can save several
networks without the hotspot closing. On ember-001 (aicwf_sdio) a 5 GHz DFS
channel refused the AP, so ap0 never follows the client's channel. If ap0 cannot
start, the hotspot falls back to taking turns with the client on wlan0.
"""
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs

IFACE = 'wlan0'
AP_IFACE = 'ap0'
AP_CHANNEL = 6
IW = '/usr/sbin/iw'  # outside the ember user's PATH; the service runs as root anyway
ADDRESS = '192.168.4.1'
SUPPLICANT = Path('/etc/wpa_supplicant/wpa_supplicant.conf')
SECRET = Path('/etc/ember/wifi-setup.json')
RUN = Path('/run/ember-wifi-setup')
GRACE = 90          # seconds offline before the hotspot starts
JOIN_WAIT = 60      # seconds to wait for a saved network after leaving the hotspot
RETRY_EVERY = 300   # hotspot seconds with no phone before retrying saved networks
STAY_OPEN = 600     # shared hotspot: seconds kept open after the last save, even once online
ADDRESS_WAIT = 30   # seconds joined to Wi-Fi without an address before asking DHCP again
RENEW_EVERY = 60    # least seconds between two DHCP renewals


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def run(*command, timeout=60):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f'{command[0]} failed: {error}')
        return subprocess.CompletedProcess(command, 1, '', str(error))


def online():
    # Any default route counts: Ethernet or a saved Wi-Fi network.
    return bool(run('ip', '-4', 'route', 'show', 'default').stdout.strip())


def validate(ssid, password):
    if not 1 <= len(ssid.encode('utf-8')) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError('The network name must be 1-32 characters.')
    if any(ord(c) < 32 for c in password):
        raise ValueError('The password contains an unsupported character.')
    if password and not (8 <= len(password.encode('utf-8')) <= 63 or re.fullmatch(r'[0-9a-fA-F]{64}', password)):
        raise ValueError('Wi-Fi passwords are 8-63 characters; leave it blank for an open network.')


def network_block(ssid, password, priority=200):
    """A wpa_supplicant entry storing a derived PSK, never the typed password."""
    lines = ['network={', '    ssid=' + ssid.encode('utf-8').hex(), '    scan_ssid=1', f'    priority={priority}']
    if password:
        psk = password.lower() if re.fullmatch(r'[0-9a-fA-F]{64}', password) else hashlib.pbkdf2_hmac(
            'sha1', password.encode('utf-8'), ssid.encode('utf-8'), 4096, 32).hex()
        lines += ['    key_mgmt=WPA-PSK', '    psk=' + psk]
    else:
        lines += ['    key_mgmt=NONE']
    return '\n'.join(lines + ['}']) + '\n'


def block_ssid(block):
    match = re.search(r'^\s*ssid=(?:"(.*)"|([0-9a-fA-F]+))\s*$', block, re.M)
    if not match:
        return None
    return match[1] if match[1] is not None else bytes.fromhex(match[2]).decode('utf-8', 'replace')


def block_priority(block):
    match = re.search(r'^\s*priority=(\d+)\s*$', block, re.M)
    return int(match[1]) if match else 0


def blocks(text):
    return [b if b.endswith('\n') else b + '\n' for b in re.findall(r'network=\{.*?\n\}\n?', text, re.S)]


def with_network(text, ssid, password, preferred=True):
    """Replace any saved entry for ssid and append the new one; other networks stay.

    preferred: try it before every other saved network; otherwise keep it as a backup."""
    kept = [block for block in blocks(text) if block_ssid(block) != ssid]
    header = re.sub(r'network=\{.*?\n\}\n?', '', text, flags=re.S).rstrip('\n') + '\n'
    others = [block_priority(block) for block in kept]
    if not others:
        priority = 200
    else:
        priority = max(others) + 1 if preferred else max(0, min(others) - 1)
    return header + ''.join(kept) + network_block(ssid, password, priority)


def with_preferred(text, ssid):
    """Raise ssid above every other saved network; nothing else changes."""
    found = [block for block in blocks(text) if block_ssid(block) == ssid]
    if not found:
        raise ValueError(f'"{ssid}" is not saved.')
    top = max([block_priority(block) for block in blocks(text) if block_ssid(block) != ssid] or [199]) + 1
    block = found[0]
    if re.search(r'^\s*priority=\d+\s*$', block, re.M):
        updated = re.sub(r'^(\s*)priority=\d+\s*$', rf'\g<1>priority={top}', block, count=1, flags=re.M)
    else:
        updated = block.replace('network={\n', f'network={{\n    priority={top}\n', 1)
    return text.replace(block, updated, 1)


def write_supplicant(text, path):
    temporary = path.with_name(path.name + '.ember-new')
    temporary.write_text(text)
    temporary.chmod(0o600)
    temporary.replace(path)


def save_network(ssid, password, path=SUPPLICANT, preferred=True):
    validate(ssid, password)
    write_supplicant(with_network(path.read_text(), ssid, password, preferred), path)


def prefer_network(ssid, path=SUPPLICANT):
    write_supplicant(with_preferred(path.read_text(), ssid), path)


def scan_ssids(output):
    names = []
    for line in output.splitlines():
        match = re.match(r'\s*SSID: (.+)$', line)
        if not match or match[1].startswith('\\x00'):
            continue  # hidden network
        # iw prints non-ASCII SSID bytes as \xNN.
        name = re.sub(rb'\\x([0-9a-fA-F]{2})', lambda m: bytes.fromhex(m[1].decode()), match[1].encode()).decode('utf-8', 'replace')
        if name not in names:
            names.append(name)
    return names


def hostapd_config(ssid, password, iface=IFACE):
    if not 8 <= len(password) <= 63:
        raise ValueError('The setup hotspot password must be 8-63 characters.')
    return '\n'.join([f'interface={iface}', 'driver=nl80211', f'ssid={ssid}', 'country_code=US',
                      'hw_mode=g', f'channel={AP_CHANNEL}', 'ieee80211n=1', 'wpa=2', 'wpa_key_mgmt=WPA-PSK',
                      'rsn_pairwise=CCMP', f'wpa_passphrase={password}', ''])


def saved_ssids(path=None):
    try:
        text = (path or SUPPLICANT).read_text()
    except OSError:
        return []
    ordered = sorted(blocks(text), key=block_priority, reverse=True)  # the order Ember tries them
    return [name for name in map(block_ssid, ordered) if name]


def connected_ssid():
    match = re.search(r'^ssid=(.*)$', run('wpa_cli', '-i', IFACE, 'status', timeout=10).stdout, re.M)
    return match[1] if match and online() else ''


def page(networks, message='', shared=False):
    options = ''.join(f'<option>{escape(name)}</option>' for name in networks)
    notice = f'<p class="note">{escape(message)}</p>' if message else ''
    names = saved_ssids()
    rows = ''.join(
        f'<li>{escape(name)}' + (' <i>(tried first)</i>' if index == 0 else
        f'<form method="post" action="/prefer" class="inline"><input type="hidden" name="ssid" value="{escape(name)}">'
        '<button class="small" type="submit">Use first</button></form>') + '</li>'
        for index, name in enumerate(names)) or '<li>None yet</li>'
    saved = f'<p>Saved networks, in the order Ember tries them:</p><ol>{rows}</ol>'
    # While Ember is online a new network defaults to a backup so it cannot drop a working connection.
    first = '' if shared and online() else ' checked'
    if shared:
        current = connected_ssid()
        status = f'Connected to <b>{escape(current)}</b>.' if current else 'Not connected to Wi-Fi yet.'
        footer = (f'<p class="note">{status}</p>{saved}'
                  '<p>You can add several networks. Ember tries them while this hotspot stays on, '
                  'and keeps the hotspot open for 10 minutes after your last change. '
                  'Reload this page to see the connection status.</p>')
    else:
        footer = (f'{saved}<p>Ember will leave this hotspot to try the network. If <b>{escape(hotspot_name())}</b> comes back '
                  'in about two minutes, the network could not be joined: reconnect and try again.</p>')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ember Wi-Fi setup</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:420px;margin:auto;padding:24px}}
label{{display:block;margin-top:16px}}select,input,button{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;margin-top:6px}}
button{{background:#E03C31;color:#fff;border:0;border-radius:8px;margin-top:20px}}.note{{background:#fff3cd;padding:12px;border-radius:8px}}
.check input{{width:auto;margin-right:8px}}.inline{{display:inline}}button.small{{width:auto;padding:4px 10px;font-size:14px;margin:0 0 0 8px}}li{{margin:8px 0}}</style>
</head><body><h1>Connect Ember to Wi-Fi</h1>{notice}
<form method="post" action="/connect">
<label>Network<select name="choice">{options}<option value="">Other (type below)</option></select></label>
<label>Or type the network name<input name="ssid" autocapitalize="none" autocorrect="off"></label>
<label>Password (blank for open Wi-Fi)<input name="password" type="password"></label>
<label class="check"><input type="checkbox" name="first" value="1"{first}>Try this network first</label>
<button type="submit">{'Save network' if shared else 'Connect'}</button></form>
{footer}<p>An open network may still need a browser sign-in before it provides internet.</p></body></html>'''


def hotspot_name():
    return 'Ember-Setup-' + socket.gethostname().removeprefix('ember-')[-8:].upper()


class Portal:
    def __init__(self):
        self.networks, self.message = [], ''
        self.submitted = threading.Event()
        self.server = None
        self.shared = False      # True while ap0 runs beside the wlan0 client
        self.last_save = None    # monotonic time of the last saved network

    def handler(portal):
        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body):
                data = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                # Every URL gets the form, so phones open it as a sign-in page.
                self.reply(200, page(portal.networks, portal.message, portal.shared))

            def do_POST(self):
                length = max(0, min(int(self.headers.get('Content-Length') or 0), 4096))
                form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
                ssid = (form.get('ssid', [''])[0] or form.get('choice', [''])[0]).strip()
                if self.path == '/prefer':
                    try:
                        prefer_network(ssid)
                    except (ValueError, OSError) as error:
                        return self.reply(400, page(portal.networks, str(error), portal.shared))
                    log(f'Wi-Fi network {ssid!r} moved first from the setup page')
                    if portal.shared:
                        run('wpa_cli', '-i', IFACE, 'reconfigure', timeout=15)
                        portal.last_save = time.monotonic()
                    portal.message = f'Ember now tries "{ssid}" first.'
                    return self.reply(200, page(portal.networks, portal.message, portal.shared))
                password = form.get('password', [''])[0]
                preferred = form.get('first', [''])[0] == '1'
                try:
                    save_network(ssid, password, preferred=preferred)
                except (ValueError, OSError) as error:
                    return self.reply(400, page(portal.networks, str(error), portal.shared))
                log(f'Saved Wi-Fi network {ssid!r} from the setup page (first={preferred})')
                if portal.shared:
                    # wlan0 picks up the new entry; ap0 and this page stay up for more networks.
                    run('wpa_cli', '-i', IFACE, 'reconfigure', timeout=15)
                    portal.last_save = time.monotonic()
                    portal.message = (f'Saved "{ssid}"' + (' and Ember is trying it now.' if preferred else ' as a backup.')
                                      + ' You can add another network.')
                    return self.reply(200, page(portal.networks, portal.message, True))
                portal.message = f'Could not join "{ssid}". Check the password and try again.'
                portal.submitted.set()
                self.reply(200, f'<!DOCTYPE html><meta name="viewport" content="width=device-width, initial-scale=1">'
                                f'<body style="font-family:sans-serif;padding:24px"><h2>Saved</h2><p>Ember is joining '
                                f'<b>{escape(ssid)}</b> now and this hotspot will close.</p></body>')

            def log_message(self, *args):
                pass
        return Handler

    def serve(self):
        self.server = ThreadingHTTPServer((ADDRESS, 80), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def client_mode():
    run('ifup', IFACE, timeout=90)


def wifi_status():
    """wpa_supplicant's view of wlan0 as a dict; empty if it is not running."""
    result = run('wpa_cli', '-i', IFACE, 'status', timeout=10)
    if result.returncode:
        return {}
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def client_address():
    """wlan0's DHCP address, or '' (never the take-turns hotspot's own address)."""
    for match in re.finditer(r'\binet (\d+\.\d+\.\d+\.\d+)/', run('ip', '-4', 'addr', 'show', 'dev', IFACE).stdout):
        if match[1] != ADDRESS:
            return match[1]
    return ''


def renew_address():
    """Ask for a fresh DHCP lease on wlan0 without restarting wpa_supplicant, so ap0 stays up.

    ifupdown runs `dhclient -1`, which exits for good if the first try fails (router off at boot,
    a network saved from the page later) and never notices wlan0 moving to another network.
    Same pid and lease files as ifupdown, so `ifdown wlan0` still releases it."""
    if shutil.which('dhclient'):
        files = ['-pf', f'/run/dhclient.{IFACE}.pid', '-lf', f'/var/lib/dhcp/dhclient.{IFACE}.leases']
        run('dhclient', '-4', '-r', *files, IFACE, timeout=20)  # drop the old address and route, stop the old client
        run('dhclient', '-4', '-nw', *files, IFACE, timeout=20)  # keeps retrying in the background
    elif shutil.which('dhcpcd'):
        run('dhcpcd', '-4', '-n', IFACE, timeout=20)  # rebind, or start it if it exited
    else:
        log('No DHCP client found to renew the Wi-Fi address')


class Lease:
    """Keep wlan0's address in step with the network wpa_supplicant joined.

    1. Joined but no address for ADDRESS_WAIT s (DHCP gave up, or never ran): renew.
    2. Joined a different network than the address came from (a network saved or moved first
       from the page, or the first one went away): renew at once, before the old address and
       gateway are mistaken for being online.
    3. wpa_supplicant not running at all: run ifup again.
    Renewals are at least RENEW_EVERY s apart."""

    def __init__(self):
        self.ssid = None            # network the current address was obtained on
        self.address = None
        self.waiting_since = None   # when wlan0 was first seen joined without an address
        self.renewed = None

    def due(self, now):
        return self.renewed is None or now - self.renewed >= RENEW_EVERY

    def check(self):
        status = wifi_status()
        now = time.monotonic()
        if not status:
            self.ssid = self.address = self.waiting_since = None
            if not online() and self.due(now):
                log('wpa_supplicant is not running on wlan0; bringing the client back up')
                self.renewed = now
                client_mode()
            return
        if status.get('wpa_state') != 'COMPLETED':
            self.waiting_since = None
            return
        ssid, address = status.get('ssid', ''), client_address()
        if address:
            self.waiting_since = None
            if address != self.address:
                self.ssid, self.address = ssid, address  # a new lease (ifup, or our renewal) belongs to this network
            elif ssid != self.ssid:
                log(f'Wi-Fi moved from {self.ssid!r} to {ssid!r} but kept its old address; asking for a new one')
                self.ssid, self.address, self.renewed = ssid, None, now
                renew_address()
            return
        if self.waiting_since is None:
            self.waiting_since = now
        elif now - self.waiting_since >= ADDRESS_WAIT and self.due(now):
            log(f'Joined {ssid!r} but no address after {ADDRESS_WAIT} s; asking DHCP again')
            self.renewed = now
            renew_address()


lease = Lease()


def keep_address():
    try:
        lease.check()
    except Exception as error:  # never let the watch stop the hotspot loop
        log(f'Address check failed: {error}')


def join_saved(wait=JOIN_WAIT):
    client_mode()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if online():
            return True
        keep_address()
        time.sleep(3)
    return online()


def launch_hotspot(portal, secret, iface):
    run('ip', 'addr', 'add', ADDRESS + '/24', 'dev', iface)
    RUN.mkdir(mode=0o700, exist_ok=True)
    config = RUN / 'hostapd.conf'
    config.write_text(hostapd_config(hotspot_name(), secret['password'], iface))
    config.chmod(0o600)
    processes = [
        subprocess.Popen(['hostapd', str(config)]),
        subprocess.Popen(['dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null', '--no-resolv',
                          f'--interface={iface}', '--bind-interfaces', '--dhcp-range=192.168.4.10,192.168.4.100,10m',
                          f'--address=/#/{ADDRESS}', '--dhcp-option=option:router,' + ADDRESS]),
    ]
    portal.serve()
    log(f'Hotspot {hotspot_name()} started on {iface} with {len(portal.networks)} nearby networks')
    return processes


def start_hotspot(portal, secret):
    """Take-turns fallback: stop the wlan0 client and run the hotspot on wlan0."""
    portal.shared = False
    run('ifdown', '--force', IFACE, timeout=30)
    run('pkill', '-f', f'wpa_supplicant.*{IFACE}')
    run('ip', 'addr', 'flush', 'dev', IFACE)
    run('ip', 'link', 'set', IFACE, 'up')
    portal.networks = scan_ssids(run(IW, 'dev', IFACE, 'scan', timeout=30).stdout)
    return launch_hotspot(portal, secret, IFACE)


def ap_mac(client_mac):
    """A locally administered MAC beside wlan0's, so ap0 and wlan0 differ."""
    return '02' + client_mac.strip()[2:]


def start_shared_hotspot(portal, secret):
    """Run the hotspot on ap0 while wlan0 stays a client. Returns None if the driver refuses."""
    run(IW, 'dev', AP_IFACE, 'del')
    added = run(IW, 'dev', IFACE, 'interface', 'add', AP_IFACE, 'type', '__ap')
    if added.returncode:
        log(f'Could not add {AP_IFACE}: {added.stderr.strip()}')
        return None
    try:
        run('ip', 'link', 'set', AP_IFACE, 'address', ap_mac(Path(f'/sys/class/net/{IFACE}/address').read_text()))
    except OSError:
        pass
    run('ip', 'link', 'set', AP_IFACE, 'up')
    # wpa_supplicant may hold wlan0's scanner; its cached results are a fine fallback.
    output = run(IW, 'dev', IFACE, 'scan', timeout=30).stdout or run(IW, 'dev', IFACE, 'scan', 'dump').stdout
    portal.networks = scan_ssids(output)
    portal.shared = True
    processes = launch_hotspot(portal, secret, AP_IFACE)
    time.sleep(3)
    if processes[0].poll() is not None:
        log('hostapd refused the shared hotspot')
        stop_hotspot(portal, processes, AP_IFACE)
        return None
    return processes


def stop_hotspot(portal, processes, iface=IFACE):
    portal.close()
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
    if iface == AP_IFACE:
        run(IW, 'dev', AP_IFACE, 'del')
        portal.shared = False
    else:
        run('ip', 'addr', 'flush', 'dev', iface)
    log('Hotspot stopped')


def phones_connected():
    return bool(run(IW, 'dev', IFACE, 'station', 'dump').stdout.strip())


def has_saved_network():
    return 'network={' in SUPPLICANT.read_text()


def shared_until_online(portal, processes):
    """Keep the shared hotspot up until online and STAY_OPEN has passed since the last save.

    Returns False if hostapd/dnsmasq exit, so the caller can fall back."""
    while True:
        time.sleep(10)
        if any(process.poll() is not None for process in processes):
            return False
        keep_address()
        recent =portal.last_save is not None and time.monotonic() - portal.last_save < STAY_OPEN
        if online() and not recent:
            return True


def setup_until_online(secret):
    portal = Portal()
    processes = start_shared_hotspot(portal, secret)
    if processes is not None:
        closed_online = shared_until_online(portal, processes)
        stop_hotspot(portal, processes, AP_IFACE)
        if closed_online:
            log('Online; setup hotspot closed')
            return
        log('Shared hotspot exited; falling back to take-turns mode')
    portal.message = ''
    while True:
        portal.submitted.clear()
        processes = start_hotspot(portal, secret)
        started = time.monotonic()
        while not portal.submitted.wait(10):
            if any(process.poll() is not None for process in processes):
                log('Setup hotspot process exited; retrying')
                break
            if time.monotonic() - started >= RETRY_EVERY and not phones_connected() and has_saved_network():
                break
        time.sleep(2)  # let the confirmation page reach the phone
        stop_hotspot(portal, processes)
        if join_saved():
            log('Online; setup hotspot closed')
            return
        if not portal.submitted.is_set():
            portal.message = ''
        log('No saved Wi-Fi network joined; reopening the hotspot')


def main():
    secret = json.loads(SECRET.read_text())
    if sys.argv[1:] == ['--now']:
        # Manual "add networks" mode: open the shared hotspot now without touching the client.
        # Exits when it closes; never falls back to take-turns while online.
        portal = Portal()
        processes = start_shared_hotspot(portal, secret)
        if processes is None:
            raise SystemExit('The shared hotspot could not start on this board.')
        portal.last_save = time.monotonic()
        shared_until_online(portal, processes)
        stop_hotspot(portal, processes, AP_IFACE)
        return
    run(IW, 'dev', AP_IFACE, 'del')  # recover if a previous run stopped mid-hotspot
    client_mode()
    offline_since = None
    while True:
        keep_address()
        if online():
            offline_since = None
        elif offline_since is None:
            offline_since = time.monotonic()
        elif time.monotonic() - offline_since >= GRACE:
            log(f'Offline for {GRACE} s; starting the setup hotspot')
            setup_until_online(secret)
            offline_since = None
        time.sleep(5)


if __name__ == '__main__':
    main()
