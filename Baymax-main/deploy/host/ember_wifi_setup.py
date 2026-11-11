"""Start an Ember-Setup hotspot and phone page when no saved Wi-Fi works.

The board has one Wi-Fi radio, so the hotspot and the Wi-Fi client take turns: the
board stays a normal Wi-Fi client while it is online, and only after GRACE seconds
offline does the radio become a password-protected access point with a setup page.

Two network stacks are supported; the script picks one at startup:
- ifupdown + wpa_supplicant (the Orange Pi 4 Pro's DietPi image): hostapd and dnsmasq
  run the hotspot, and networks are saved in wpa_supplicant.conf.
- NetworkManager (Armbian on the Orange Pi 5 Max): a NetworkManager access-point
  connection runs the hotspot, and networks are saved as NetworkManager connections.
"""
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs
import uuid


def default_iface():
    """wlan0, or the board's first other Wi-Fi interface. EMBER_WIFI_IFACE overrides."""
    if os.environ.get('EMBER_WIFI_IFACE'):
        return os.environ['EMBER_WIFI_IFACE']
    radios = sorted(path.parent.name for path in Path('/sys/class/net').glob('*/phy80211'))
    return 'wlan0' if 'wlan0' in radios or not radios else radios[0]


IFACE = default_iface()
ADDRESS = '192.168.4.1'
SUPPLICANT = Path('/etc/wpa_supplicant/wpa_supplicant.conf')
SECRET = Path('/etc/ember/wifi-setup.json')
RUN = Path('/run/ember-wifi-setup')
NM_CONNECTIONS = Path('/etc/NetworkManager/system-connections')
NM_DNS = Path('/etc/NetworkManager/dnsmasq-shared.d/ember-wifi-setup.conf')
NM_HOTSPOT_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ember-wifi-setup:hotspot'))
GRACE = 90            # seconds offline before the hotspot starts
JOIN_WAIT = 90        # seconds from leaving the hotspot for a saved network to come online
RETRY_EVERY = 300     # seconds with no page visit and no phone before checking saved networks
SESSION_LIMIT = 900   # even with a phone connected, check saved networks after this long
AP_START_WAIT = 15    # seconds for the access point to come on the air
EX_CONFIG = 78        # exit status for a bad password file; the unit does not restart on it
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', re.I)


class HotspotError(Exception):
    pass


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def run(*command, timeout=60):
    # Decode leniently: `iw scan` prints the WPS names of nearby routers as raw bytes.
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f'{command[0]} failed: {error}')
        return subprocess.CompletedProcess(command, 1, '', str(error))


def link_state(device):
    try:
        return Path('/sys/class/net', device, 'operstate').read_text().strip()
    except OSError:
        return 'unknown'


def online():
    """A default route through an interface that is really up: Ethernet or a joined Wi-Fi
    network. After Wi-Fi drops, dhclient keeps its lease and the kernel only marks the
    route linkdown; Wi-Fi stays 'dormant' until its handshake is done. Neither counts."""
    for line in run('ip', '-4', 'route', 'show', 'default').stdout.splitlines():
        words = line.split()
        if not words or 'linkdown' in words or 'dead' in words:
            continue
        device = words[words.index('dev') + 1] if 'dev' in words[:-1] else ''
        if device and link_state(device) not in ('up', 'unknown'):
            continue
        return True
    return False


def wait_online(deadline):
    while time.monotonic() < deadline:
        if online():
            return True
        time.sleep(3)
    return online()


def ssid_bytes(ssid):
    return ssid if isinstance(ssid, bytes) else ssid.encode('utf-8')


def shown(ssid):
    return ssid_bytes(ssid).decode('utf-8', 'replace')


def validate(ssid, password):
    raw = ssid_bytes(ssid)
    if not 1 <= len(raw) <= 32 or any(byte < 32 or byte == 127 for byte in raw):
        raise ValueError('The network name must be 1-32 characters.')
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError('The password contains an unsupported character.')
    if password and not (8 <= len(password.encode('utf-8')) <= 63 or re.fullmatch(r'[0-9a-fA-F]{64}', password)):
        raise ValueError('Wi-Fi passwords are 8-63 characters; leave it blank for an open network.')


def derived_psk(ssid, password):
    if re.fullmatch(r'[0-9a-fA-F]{64}', password):
        return password.lower()
    return hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), ssid_bytes(ssid), 4096, 32).hex()


def check_hotspot_password(password):
    # WPA2 passphrases are 8-63 printable ASCII characters (IEEE 802.11i).
    if not isinstance(password, str) or not 8 <= len(password) <= 63 or not all(32 <= ord(c) <= 126 for c in password):
        raise ValueError('The setup hotspot password must be 8-63 printable ASCII characters.')


def load_secret(path=SECRET):
    try:
        password = json.loads(path.read_text())['password']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f'{path} must hold {{"password": "..."}} ({type(error).__name__})') from None
    try:
        check_hotspot_password(password)
    except ValueError as error:
        raise ValueError(f'{error} ({path})') from None
    return {'password': password}


def write_private(path, text):
    """Replace path with text, readable by root only from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.ember-new')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w',
                   encoding='utf-8', errors='surrogateescape') as handle:
        handle.write(text)
    temporary.chmod(0o600)
    temporary.replace(path)


# ifupdown + wpa_supplicant ------------------------------------------------------

def network_block(ssid, password):
    """A wpa_supplicant entry storing a derived PSK, never the typed password."""
    lines = ['network={', '    ssid=' + ssid_bytes(ssid).hex(), '    scan_ssid=1', '    priority=200']
    if password:
        lines += ['    key_mgmt=WPA-PSK', '    psk=' + derived_psk(ssid, password)]
    else:
        lines += ['    key_mgmt=NONE']
    return '\n'.join(lines + ['}']) + '\n'


def block_ssid(block):
    match = re.search(r'^\s*ssid=(?:"(.*)"|([0-9a-fA-F]+))\s*$', block, re.M)
    if not match:
        return None
    try:
        return match[1].encode('utf-8', 'surrogateescape') if match[1] is not None else bytes.fromhex(match[2])
    except ValueError:
        return None


def with_network(text, ssid, password):
    """Replace any saved entry for ssid and append the new one; other networks stay."""
    raw = ssid_bytes(ssid)
    kept = [block for block in re.findall(r'network=\{.*?\n\}\n?', text, re.S) if block_ssid(block) != raw]
    header = re.sub(r'network=\{.*?\n\}\n?', '', text, flags=re.S).rstrip('\n') + '\n'
    return header + ''.join(block if block.endswith('\n') else block + '\n' for block in kept) + network_block(ssid, password)


def read_config(path):
    # A hand-edited file may hold bytes that are not UTF-8; keep them as they are.
    return path.read_text(encoding='utf-8', errors='surrogateescape')


def save_network(ssid, password, path=SUPPLICANT):
    validate(ssid, password)
    write_private(path, with_network(read_config(path), ssid, password))


def supplicant_ssids(text):
    return {ssid for ssid in map(block_ssid, re.findall(r'network=\{.*?\n\}', text, re.S)) if ssid}


def scan_ssids(output):
    """Visible network names (bytes) from `iw dev ... scan`, strongest first."""
    best, signal_dbm = {}, -100.0
    for line in output.splitlines():
        if line.startswith('BSS '):
            signal_dbm = -100.0
        elif match := re.match(r'\s*signal: (-?[\d.]+) dBm', line):
            signal_dbm = float(match[1])
        elif match := re.match(r'\s*SSID: (.+)$', line):
            if match[1].startswith('\\x00'):
                continue  # hidden network
            # iw prints spaces at either end, backslashes and non-ASCII bytes as \xNN.
            name = re.sub(rb'\\x([0-9a-fA-F]{2})', lambda m: bytes.fromhex(m[1].decode()), match[1].encode('utf-8'))
            best[name] = max(signal_dbm, best.get(name, -1000.0))
    return sorted(best, key=lambda name: -best[name])


def merge(*lists):
    merged = []
    for names in lists:
        merged += [name for name in names if name not in merged]
    return merged


def hostapd_config(ssid, password):
    check_hotspot_password(password)
    return '\n'.join([f'interface={IFACE}', 'driver=nl80211', f'ctrl_interface={RUN}/hostapd', f'ssid={ssid}',
                      'country_code=US', 'hw_mode=g', 'channel=6', 'ieee80211n=1', 'wpa=2',
                      'wpa_key_mgmt=WPA-PSK', 'rsn_pairwise=CCMP', f'wpa_passphrase={password}', ''])


def iw_stations():
    return bool(re.search(r'^Station ', run('iw', 'dev', IFACE, 'station', 'dump', timeout=5).stdout, re.M))


class Hostapd:
    """hostapd and dnsmasq run as children. hostapd's own messages say when the access
    point is on the air and which phones are connected: the 4 Pro's aic8800 driver
    cannot list connected phones through `iw`."""

    def __init__(self):
        self.processes = []
        self.enabled = threading.Event()
        self.off_since = None
        self.stations = set()

    def event(self, line):
        if 'AP-ENABLED' in line:
            self.enabled.set()
            self.off_since = None
        elif 'AP-DISABLED' in line or 'INTERFACE-DISABLED' in line:
            self.off_since = self.off_since or time.monotonic()
        match = re.search(r'AP-STA-(CONNECTED|DISCONNECTED) (' + MAC.pattern + ')', line, re.I)
        if match:
            (self.stations.add if match[1] == 'CONNECTED' else self.stations.discard)(match[2].lower())

    def watch(self, process):
        # Read to the end: hostapd blocks once a pipe nobody reads is full.
        for line in process.stdout:
            line = line.rstrip()
            if line:
                log('hostapd: ' + line)
                self.event(line)

    def start(self, name, password):
        run('ip', 'addr', 'add', ADDRESS + '/24', 'dev', IFACE)
        RUN.mkdir(mode=0o700, exist_ok=True)
        config = RUN / 'hostapd.conf'
        write_private(config, hostapd_config(name, password))
        line_buffered = ['stdbuf', '-oL'] if shutil.which('stdbuf') else []
        hostapd = subprocess.Popen(line_buffered + ['hostapd', str(config)], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        self.processes.append(hostapd)
        threading.Thread(target=self.watch, args=(hostapd,), daemon=True).start()
        deadline = time.monotonic() + AP_START_WAIT
        while not self.enabled.wait(0.5):
            if hostapd.poll() is not None:
                raise HotspotError(f'hostapd exited with status {hostapd.returncode}')
            if time.monotonic() > deadline:
                if 'type AP' not in run('iw', 'dev', IFACE, 'info', timeout=5).stdout:
                    raise HotspotError(f'the access point did not start within {AP_START_WAIT} s')
                log('hostapd did not report AP-ENABLED, but the radio is an access point; continuing')
                break
        # The portal answers on wlan0 only; without --except-interface=lo dnsmasq also takes
        # 127.0.0.1:53 and exits if a local resolver already has it.
        dnsmasq = subprocess.Popen(['dnsmasq', '--keep-in-foreground', '--log-facility=-', '--conf-file=/dev/null',
                                    '--no-resolv', '--no-hosts', f'--interface={IFACE}', '--except-interface=lo',
                                    '--bind-interfaces', '--dhcp-authoritative', f'--dhcp-leasefile={RUN}/dnsmasq.leases',
                                    '--dhcp-range=192.168.4.10,192.168.4.100,10m', f'--address=/#/{ADDRESS}',
                                    '--dhcp-option=option:router,' + ADDRESS])
        self.processes.append(dnsmasq)
        time.sleep(1)
        if dnsmasq.poll() is not None:
            raise HotspotError(f'dnsmasq exited with status {dnsmasq.returncode}')

    def alive(self):
        if any(process.poll() is not None for process in self.processes):
            return False
        return self.off_since is None or time.monotonic() - self.off_since < 20

    def phones_connected(self):
        if self.stations:
            return True
        listed = run('hostapd_cli', '-p', str(RUN / 'hostapd'), '-i', IFACE, 'list_sta', timeout=5)
        return (listed.returncode == 0 and bool(MAC.search(listed.stdout))) or iw_stations()

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes = []
        run('ip', 'addr', 'flush', 'dev', IFACE)


class Ifupdown:
    name = 'ifupdown + wpa_supplicant'

    def __init__(self):
        # DietPi brings wlan0 up through ifup@wlan0.service (allow-hotplug). Going through
        # that unit keeps wpa_supplicant and dhclient out of this service, so they
        # survive its restarts.
        self.hotplug = IFACE in run('ifquery', '--list', '--allow=hotplug').stdout.split()

    def client_mode(self, timeout=JOIN_WAIT):
        if self.hotplug:
            run('systemctl', 'start', f'ifup@{IFACE}.service', timeout=timeout)
        else:
            run('ifup', IFACE, timeout=timeout)

    def leave_client(self):
        if self.hotplug:
            run('systemctl', 'stop', f'ifup@{IFACE}.service', timeout=30)
        run('ifdown', '--force', IFACE, timeout=30)
        # hostapd cannot take the radio until the old wpa_supplicant has really exited.
        for program in ('wpa_supplicant', 'dhclient'):
            pattern = f'{program}.*{IFACE}'
            run('pkill', '-f', pattern)
            for _ in range(10):
                if run('pgrep', '-f', pattern).returncode != 0:
                    break
                time.sleep(0.5)
            else:
                run('pkill', '-9', '-f', pattern)
        run('ip', 'addr', 'flush', 'dev', IFACE)

    def recover(self):
        """Clean up after a run that stopped mid-hotspot, then bring the Wi-Fi client up."""
        left = [run('pkill', '-f', f'{program}.*{RUN}').returncode == 0 for program in ('hostapd', 'dnsmasq')]
        if any(left) or f'inet {ADDRESS}/' in run('ip', '-4', 'addr', 'show', 'dev', IFACE).stdout:
            log('Cleaning up a hotspot left by an earlier run')
            time.sleep(1)
            run('ip', 'addr', 'flush', 'dev', IFACE)
        self.client_mode()

    def scan(self):
        run('ip', 'link', 'set', IFACE, 'up')
        for _ in range(3):
            result = run('iw', 'dev', IFACE, 'scan', timeout=30)
            names = scan_ssids(result.stdout)
            if names:
                return names
            log(f'Wi-Fi scan found nothing ({result.stderr.strip() or "no networks"}); retrying')
            time.sleep(2)
        return []

    def prepare(self):
        """Leave client mode; return nearby networks for the page."""
        cached = scan_ssids(run('iw', 'dev', IFACE, 'scan', 'dump', timeout=10).stdout)
        self.leave_client()
        return merge(self.scan(), cached)

    def saved_ssids(self):
        try:
            return supplicant_ssids(read_config(SUPPLICANT))
        except OSError:
            return set()

    def save(self, ssid, password):
        save_network(ssid, password)

    def new_hotspot(self):
        return Hostapd()

    def join(self, ssid, wait, visible=()):
        # wpa_supplicant tries saved networks itself, priority 200 (the page's) first.
        deadline = time.monotonic() + wait
        self.client_mode(timeout=wait)
        return wait_online(deadline)


# NetworkManager -----------------------------------------------------------------

def nm_uuid(ssid):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'ember-wifi-setup:' + ssid_bytes(ssid).hex()))


def keyfile_value(text):
    """A GKeyFile string: backslashes and every space escaped, so nothing is trimmed."""
    return text.replace('\\', '\\\\').replace(' ', '\\s')


def keyfile_ssid(ssid):
    # A list of byte values reads back exactly, whatever the name contains.
    return ';'.join(str(byte) for byte in ssid_bytes(ssid)) + ';'


def nm_client_keyfile(ssid, password):
    lines = ['[connection]', f'id={keyfile_value(shown(ssid) + " (Ember setup)")}', f'uuid={nm_uuid(ssid)}',
             'type=wifi', 'autoconnect=true', 'autoconnect-priority=200', '',
             '[wifi]', 'mode=infrastructure', f'ssid={keyfile_ssid(ssid)}', '']
    if password:  # the derived PSK only, as in wpa_supplicant.conf
        lines += ['[wifi-security]', 'key-mgmt=wpa-psk', f'psk={derived_psk(ssid, password)}', '']
    return '\n'.join(lines + ['[ipv4]', 'method=auto', '', '[ipv6]', 'method=auto', ''])


def nm_hotspot_keyfile(name, password):
    check_hotspot_password(password)
    return '\n'.join(['[connection]', 'id=Ember setup hotspot', f'uuid={NM_HOTSPOT_UUID}', 'type=wifi',
                      f'interface-name={IFACE}', 'autoconnect=false', '',
                      '[wifi]', 'mode=ap', f'ssid={keyfile_ssid(name)}', 'band=bg', 'channel=6', '',
                      '[wifi-security]', 'key-mgmt=wpa-psk', 'proto=rsn', 'pairwise=ccmp', 'group=ccmp',
                      'pmf=1', f'psk={keyfile_value(password)}', '',
                      '[ipv4]', 'method=shared', f'address1={ADDRESS}/24', '',
                      '[ipv6]', 'method=disabled', ''])


def nm_scan_ssids(output):
    """Names (bytes) from `nmcli -t -f SSID-HEX device wifi list`, in nmcli's order."""
    names = []
    for line in output.splitlines():
        try:
            name = bytes.fromhex(line.strip())
        except ValueError:
            continue
        if name.strip(b'\0') and name not in names:
            names.append(name)
    return names


def nm_fields(output):
    """`nmcli -t -f ...` lines as (field, value), with nmcli's backslash escapes removed."""
    for line in output.splitlines():
        field, _, value = line.partition(':')
        yield field, re.sub(r'\\(.)', r'\1', value)


class NMHotspot:
    def __init__(self, nm):
        self.nm = nm
        self.started = False

    def start(self, name, password):
        self.started = True
        # NetworkManager's hotspot DNS reads this directory when it starts; every name
        # points at the board so phones open the page as a sign-in page.
        write_private(NM_DNS, f'# Written by ember_wifi_setup.py while the setup hotspot is up\naddress=/#/{ADDRESS}\n')
        path = NM_CONNECTIONS / 'ember-setup-hotspot.nmconnection'
        write_private(path, nm_hotspot_keyfile(name, password))
        self.nm.load(path)
        result = self.nm.nmcli('--wait', str(2 * AP_START_WAIT), 'connection', 'up', 'uuid', NM_HOTSPOT_UUID,
                               'ifname', IFACE, timeout=2 * AP_START_WAIT + 10)
        if result.returncode != 0:
            raise HotspotError('NetworkManager could not start the hotspot: ' + (result.stderr or result.stdout).strip())

    def alive(self):
        return NM_HOTSPOT_UUID in self.nm.active()

    def phones_connected(self):
        return iw_stations()

    def stop(self):
        if self.started:
            self.nm.nmcli('connection', 'down', 'uuid', NM_HOTSPOT_UUID, timeout=30)
            self.started = False
        NM_DNS.unlink(missing_ok=True)


class NetworkManager:
    """NetworkManager owns the radio: the hotspot is an access-point connection with
    NetworkManager's own DHCP and DNS ('shared'), and a network from the page becomes a
    NetworkManager connection that it rejoins by itself."""
    name = 'NetworkManager'

    def nmcli(self, *args, timeout=60):
        return run('nmcli', *args, timeout=timeout)

    def active(self):
        return self.nmcli('-t', '-f', 'UUID', 'connection', 'show', '--active').stdout.split()

    def load(self, path):
        result = self.nmcli('connection', 'load', str(path))
        if result.returncode != 0:
            raise OSError('NetworkManager did not load the connection: ' + (result.stderr or result.stdout).strip())

    def client_mode(self, timeout=JOIN_WAIT):
        # Clears a device-level autoconnect block, so saved networks are joined again.
        self.nmcli('device', 'set', IFACE, 'autoconnect', 'yes', timeout=10)

    def recover(self):
        if NM_HOTSPOT_UUID in self.active():
            log('Closing a hotspot left by an earlier run')
            self.nmcli('connection', 'down', 'uuid', NM_HOTSPOT_UUID, timeout=30)
        NM_DNS.unlink(missing_ok=True)
        self.client_mode()

    def scan(self):
        for _ in range(3):
            result = self.nmcli('-t', '-f', 'SSID-HEX', 'device', 'wifi', 'list', 'ifname', IFACE,
                                '--rescan', 'yes', timeout=30)
            names = nm_scan_ssids(result.stdout)
            if names:
                return names
            log(f'Wi-Fi scan found nothing ({result.stderr.strip() or "no networks"}); retrying')
            time.sleep(2)
        return []

    def prepare(self):
        return self.scan()

    def saved(self):
        """{ssid: [uuid, ...]} of saved client Wi-Fi connections."""
        saved = {}
        for field, kind in nm_fields(self.nmcli('-t', '-f', 'UUID,TYPE', 'connection', 'show').stdout):
            if kind != '802-11-wireless' or field == NM_HOTSPOT_UUID:
                continue
            settings = dict(nm_fields(self.nmcli('-t', '-f', '802-11-wireless.ssid,802-11-wireless.mode',
                                                 'connection', 'show', 'uuid', field).stdout))
            ssid = settings.get('802-11-wireless.ssid', '')
            if ssid and settings.get('802-11-wireless.mode', '') in ('', 'infrastructure'):
                saved.setdefault(ssid.encode('utf-8'), []).append(field)
        return saved

    def saved_ssids(self):
        return set(self.saved())

    def save(self, ssid, password):
        validate(ssid, password)
        path = NM_CONNECTIONS / f'ember-{nm_uuid(ssid)}.nmconnection'
        write_private(path, nm_client_keyfile(ssid, password))
        self.load(path)

    def new_hotspot(self):
        return NMHotspot(self)

    def join(self, ssid, wait, visible=()):
        deadline = time.monotonic() + wait
        self.client_mode()
        if ssid is not None:
            candidates = [nm_uuid(ssid)]
        else:
            saved = self.saved()
            candidates = [uid for name in visible for uid in saved.get(name, [])]
        for uid in candidates:
            left = int(deadline - time.monotonic())
            if left < 5:
                break
            result = self.nmcli('--wait', str(left), 'connection', 'up', 'uuid', uid, 'ifname', IFACE, timeout=left + 10)
            if result.returncode == 0 and wait_online(min(deadline, time.monotonic() + 15)):
                return True
        return wait_online(deadline)


def nm_manages(device):
    if not shutil.which('nmcli') or run('systemctl', 'is-active', '--quiet', 'NetworkManager').returncode != 0:
        return False
    for _ in range(15):  # the radio can show up a little after NetworkManager starts
        for name, state in nm_fields(run('nmcli', '-t', '-f', 'DEVICE,STATE', 'device').stdout):
            if name == device:
                return not state.startswith('unmanaged')
        time.sleep(2)
    return True


def network_stack():
    choice = os.environ.get('EMBER_WIFI_STACK', '').lower()
    if choice == 'networkmanager' or (choice != 'ifupdown' and nm_manages(IFACE)):
        return NetworkManager()
    return Ifupdown()


# The phone page -----------------------------------------------------------------

def page(networks, message=''):
    options = ''.join(f'<option value="{ssid_bytes(name).hex()}">{escape(shown(name))}</option>' for name in networks)
    first = 'Choose a network' if networks else 'No networks found: type the name below'
    notice = f'<p class="note">{escape(message)}</p>' if message else ''
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ember Wi-Fi setup</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:420px;margin:auto;padding:24px}}
label{{display:block;margin-top:16px}}select,input,button{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;margin-top:6px}}
button{{background:#E03C31;color:#fff;border:0;border-radius:8px;margin-top:20px}}.note{{background:#fff3cd;padding:12px;border-radius:8px}}</style>
</head><body><h1>Connect Ember to Wi-Fi</h1>{notice}
<form method="post" action="/connect">
<label>Network<select name="choice"><option value="">{first}</option>{options}</select></label>
<label>Or type the network name<input name="ssid" autocapitalize="none" autocorrect="off" spellcheck="false" autocomplete="off"></label>
<label>Password (blank for open Wi-Fi)<input name="password" type="password"></label>
<button type="submit">Connect</button></form>
<p>Ember will leave this hotspot to try the network. If <b>{escape(hotspot_name())}</b> comes back
in about two minutes, the network could not be joined: reconnect and try again.</p></body></html>'''


def hotspot_name():
    return 'Ember-Setup-' + socket.gethostname()[-4:].upper()


class Portal:
    def __init__(self, save):
        self.save = save
        self.networks, self.message = [], ''
        self.submitted = threading.Event()
        self.chosen = None
        self.last_visit = 0.0
        self.server = None

    def choose(self, form):
        """The network the form names (bytes): a typed name, else the chosen list entry."""
        typed = form.get('ssid', [''])[0]
        if typed.strip():
            exact = typed.encode('utf-8')
            return exact if exact in self.networks else typed.strip().encode('utf-8')
        try:
            return bytes.fromhex(form.get('choice', [''])[0])
        except ValueError:
            return b''

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

            def visited(self):
                # iOS's background probe is not a person using the page.
                if 'CaptiveNetworkSupport' not in (self.headers.get('User-Agent') or ''):
                    portal.last_visit = time.monotonic()

            def do_GET(self):
                # Every URL gets the form, so phones open it as a sign-in page.
                self.visited()
                self.reply(200, page(portal.networks, portal.message))

            def do_POST(self):
                self.visited()
                length = max(0, min(int(self.headers.get('Content-Length') or 0), 4096))
                form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'), keep_blank_values=True)
                ssid = portal.choose(form)
                password = form.get('password', [''])[0]
                if not ssid:
                    return self.reply(400, page(portal.networks, 'Choose a network or type its name.'))
                try:
                    portal.save(ssid, password)
                except (ValueError, OSError) as error:
                    return self.reply(400, page(portal.networks, str(error)))
                log(f'Saved Wi-Fi network {shown(ssid)!r} from the setup page')
                portal.chosen = ssid
                portal.message = f'Could not join "{shown(ssid)}". Check the name and password and try again.'
                portal.submitted.set()
                self.reply(200, f'<!DOCTYPE html><meta name="viewport" content="width=device-width, initial-scale=1">'
                                f'<body style="font-family:sans-serif;padding:24px"><h2>Saved</h2><p>Ember is joining '
                                f'<b>{escape(shown(ssid))}</b> now and this hotspot will close.</p></body>')

            def log_message(self, *args):
                pass
        return Handler

    def serve(self):
        self.server = ThreadingHTTPServer((ADDRESS, 80), self.handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# Taking turns -------------------------------------------------------------------

def wait_for_phone(portal, hotspot):
    """'submitted', 'online' (Ethernet came back), 'idle' (time to look for saved
    networks) or 'failed' (off the air)."""
    started = time.monotonic()
    while not portal.submitted.wait(10):
        if online():
            return 'online'
        if not hotspot.alive():
            log('The hotspot went off the air')
            return 'failed'
        now = time.monotonic()
        idle = now - max(started, portal.last_visit)
        # A phone that auto-joins the hotspot must not keep the robot off its own Wi-Fi.
        if now - started >= SESSION_LIMIT and idle >= 120:
            return 'idle'
        if idle >= RETRY_EVERY and not hotspot.phones_connected():
            return 'idle'
    return 'submitted'


def hotspot_session(stack, portal, secret):
    """Run the hotspot once; wlan0 is out of client mode afterwards."""
    portal.submitted.clear()
    hotspot = stack.new_hotspot()
    try:
        portal.networks = stack.prepare() or portal.networks
        hotspot.start(hotspot_name(), secret['password'])
        portal.serve()
        log(f'Hotspot {hotspot_name()} is on the air with {len(portal.networks)} nearby networks listed')
        outcome = wait_for_phone(portal, hotspot)
        if outcome == 'submitted':
            time.sleep(2)  # let the confirmation page reach the phone
        return outcome
    except (HotspotError, OSError) as error:
        log(f'Hotspot failed: {error}')
        return 'failed'
    except Exception:  # never leave the radio with neither client nor hotspot
        log('Hotspot failed:\n' + traceback.format_exc())
        return 'failed'
    finally:
        portal.close()
        hotspot.stop()
        log('Hotspot stopped')


def setup_until_online(stack, secret):
    portal = Portal(stack.save)
    failures = misses = 0
    while True:
        outcome = hotspot_session(stack, portal, secret)
        if outcome == 'online':
            log('Online through another interface; setup hotspot closed')
            stack.client_mode()
            return
        if outcome == 'failed':
            failures += 1
            if failures < 3:
                time.sleep(5 * failures)
                continue
        failures = 0
        if outcome == 'submitted':
            if stack.join(portal.chosen, JOIN_WAIT):
                log(f'Online on {shown(portal.chosen)!r}; setup hotspot closed')
                return
            log(f'Could not join {shown(portal.chosen)!r}; reopening the hotspot')
            continue
        # Leave the hotspot for a while only when a saved network is in range. Every
        # third time try anyway, for hidden networks and scans that miss one.
        visible = stack.scan()
        portal.networks = visible or portal.networks
        misses += 1
        if visible and not set(visible) & stack.saved_ssids() and misses < 3:
            log('No saved Wi-Fi network in range; reopening the hotspot')
            continue
        misses = 0
        portal.message = ''
        if stack.join(None, JOIN_WAIT, visible):
            log('Online; setup hotspot closed')
            return
        log('No saved Wi-Fi network joined; reopening the hotspot')


def main():
    stack = network_stack()
    log(f'Wi-Fi setup hotspot for {IFACE} with {stack.name}')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    offline_since = None if online() else time.monotonic()
    stack.recover()  # after a run that stopped mid-hotspot
    try:
        secret = load_secret()
    except ValueError as error:
        log(f'Not starting: {error}')
        sys.exit(EX_CONFIG)
    while True:
        if online():
            offline_since = None
        elif offline_since is None:
            offline_since = time.monotonic()
        elif time.monotonic() - offline_since >= GRACE:
            log(f'Offline for {GRACE} s; starting the setup hotspot')
            try:
                setup_until_online(stack, secret)
            except SystemExit:  # stopped: give the radio back to the Wi-Fi client
                stack.client_mode(timeout=30)
                raise
            offline_since = None
        time.sleep(5)


if __name__ == '__main__':
    main()
