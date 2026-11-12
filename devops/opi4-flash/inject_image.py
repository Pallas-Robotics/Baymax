"""Configure the mounted ARM64 image without executing its ARM binaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import re
import tarfile
from image_settings import load, source, supplicant, dietpi_wifi, timezone

root = Path(sys.argv[1]).resolve()
payload = Path(sys.argv[2]).resolve()
assert root != Path('/') and (root / 'etc/debian_version').is_file()
assert (root / 'etc/os-release').read_text().find('VERSION_ID="13"') >= 0
credentials = json.loads((payload / 'credentials.json').read_text())
hostname = credentials['hostname']
assert re.fullmatch(r'ember-[a-f0-9]{8}', hostname), 'Invalid generated hostname'
settings = load(payload / 'image-settings.private.json')
bundle = json.loads((payload / 'bundle-source.json').read_text())
assert (bundle['repository'], bundle['branch']) == (source(settings)['repository'], source(settings)['branch']), 'Bundle does not match selected PR'
zone = timezone(settings)
assert not (root / 'usr/share/zoneinfo').is_dir() or os.path.lexists(root / 'usr/share/zoneinfo' / zone), \
    f'Time zone {zone} is not in the image (usr/share/zoneinfo)'


def image_directory(path):
    """Create traversable image directories even under the builder's private umask."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        directory.chmod(0o755)


def write(name, content, mode=0o644):
    path = root / name.lstrip('/')
    image_directory(path.parent)
    path.write_text(content)
    path.chmod(mode)
    return path


def copy(source, destination, mode=0o644):
    path = root / destination.lstrip('/')
    image_directory(path.parent)
    shutil.copyfile(payload / source, path)
    if source == 'DEVICE-SETTINGS.txt' or source == 'ember-device-settings':
        path.write_text(path.read_text().replace('@HOSTNAME@', hostname))
    path.chmod(mode)
    return path


# Allocate the normal desktop user and retain password-gated sudo access.
passwd = (root / 'etc/passwd').read_text().splitlines()
assert not any(line.split(':')[2] == '1001' and not line.startswith('ember:') for line in passwd), 'UID 1001 already belongs to another user'
passwd = [line for line in passwd if not line.startswith('ember:')]
passwd.append('ember:x:1001:1001:Ember device:/home/ember:/bin/bash')
write('/etc/passwd', '\n'.join(passwd) + '\n')
groups = (root / 'etc/group').read_text().splitlines()
assert not any(line.split(':')[2] == '1001' and not line.startswith('ember:') for line in groups), 'GID 1001 already belongs to another group'
groups = [line for line in groups if not line.startswith('ember:')]
for index, line in enumerate(groups):
    fields = line.split(':')
    if fields[0] in ('sudo', 'audio', 'video', 'render', 'input', 'dialout', 'plugdev', 'netdev'):
        fields[3] = ','.join(dict.fromkeys(filter(None, fields[3].split(',') + ['ember'])))
        groups[index] = ':'.join(fields)
groups.append('ember:x:1001:')
write('/etc/group', '\n'.join(groups) + '\n')
password_hash = subprocess.check_output(['openssl', 'passwd', '-6', '-stdin'], input=credentials['linux_password'] + '\n', text=True).strip()
shadow = [line for line in (root / 'etc/shadow').read_text().splitlines() if not line.startswith('ember:')]
for index, line in enumerate(shadow):
    if line.startswith(('root:', 'dietpi:')):
        fields = line.split(':'); fields[1] = '!'; shadow[index] = ':'.join(fields)
shadow.append(f'ember:{password_hash}:20754:0:99999:7:::')
write('/etc/shadow', '\n'.join(shadow) + '\n', 0o640)
write('/etc/sudoers.d/ember', 'ember ALL=(ALL:ALL) ALL\n', 0o440)
write('/etc/hostname', hostname + '\n')
# The board's clock (check-in windows, reminders); DietPi's first boot applies the same zone.
write('/etc/timezone', zone + '\n')
(root / 'etc/localtime').unlink(missing_ok=True)
(root / 'etc/localtime').symlink_to('/usr/share/zoneinfo/' + zone)
if (root / 'boot/dietpi.txt').is_file():
    dietpi = (root / 'boot/dietpi.txt').read_text()
    dietpi_options = {
        'AUTO_SETUP_GLOBAL_PASSWORD': '',
        'AUTO_SETUP_AUTOMATED': '0',
        'AUTO_SETUP_NET_ETHERNET_ENABLED': '1',
        'AUTO_SETUP_NET_WIFI_ENABLED': '1',
        'AUTO_SETUP_NET_WIFI_COUNTRY_CODE': 'US',
        'AUTO_SETUP_NET_HOSTNAME': hostname,
        'AUTO_SETUP_TIMEZONE': zone,
        'CONFIG_CHECK_DIETPI_UPDATES': '0',
        'CONFIG_CHECK_APT_UPDATES': '0',
    }
    for key, value in dietpi_options.items():
        dietpi, count = re.subn(r'^' + key + r'=.*$', key + '=' + value, dietpi, flags=re.M)
        assert count == 1, key
    write('/boot/dietpi.txt', dietpi, 0o600)
    wifi = (root / 'boot/dietpi-wifi.txt').read_text()
    wifi = dietpi_wifi(wifi, settings['wifi'])
    write('/boot/dietpi-wifi.txt', wifi, 0o600)
    # Persistent logs survive removal of the card after an unsuccessful boot.
    fstab = (root / 'etc/fstab').read_text()
    fstab = re.sub(r'^tmpfs /var/log .*$', '# Ember: keep logs on SD for offline recovery', fstab, flags=re.M)
    write('/etc/fstab', fstab)
hosts = (root / 'etc/hosts').read_text()
hosts = '\n'.join(line for line in hosts.splitlines() if not line.startswith('127.0.1.1'))
write('/etc/hosts', hosts + '\n127.0.1.1 ' + hostname + '\n')
for path in (root / 'root/.not_logged_in_yet', root / 'root/.desktop_autologin', root / 'root/.automated_script.sh'):
    path.unlink(missing_ok=True)
write('/etc/machine-id', '')
dbus_id = root / 'var/lib/dbus/machine-id'
dbus_id.unlink(missing_ok=True)
image_directory(dbus_id.parent)
dbus_id.symlink_to('/etc/machine-id')
write('/etc/wpa_supplicant/wpa_supplicant.conf', supplicant(settings['wifi']), 0o600)
write('/etc/network/interfaces.d/wlan0.conf', 'allow-hotplug wlan0\niface wlan0 inet dhcp\nwpa-conf /etc/wpa_supplicant/wpa_supplicant.conf\n')
interfaces = (root / 'etc/network/interfaces').read_text()
if not re.search(r'^source(?:-directory)?\s+/etc/network/interfaces.d', interfaces, re.M):
    write('/etc/network/interfaces', interfaces + '\nsource /etc/network/interfaces.d/*\n')
write('/etc/systemd/journald.conf.d/ember.conf', '[Journal]\nStorage=persistent\nSystemMaxUse=128M\n')
copy('credentials.json', '/etc/ember/credentials.json', 0o600)
for name in ['ember.bundle', 'bundle-source.json', 'anydesk-arm64.deb', 'ember-status.py', 'ember-device-config.py', 'ember-anydesk-id.py']:
    copy(name, '/opt/ember-provision/' + name)
python_root = root / 'opt/ember-python'
image_directory(python_root)
with tarfile.open(payload / 'python-arm64.tar.gz') as archive:
    archive.extractall(python_root, filter='data')
for parent, directories, files in os.walk(python_root):
    os.chown(parent, 0, 0)
    os.chmod(parent, 0o755)
    for name in files:
        path = Path(parent) / name
        os.lchown(path, 0, 0)
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode & ~0o022)
copy('ember-provision', '/usr/local/sbin/ember-provision', 0o755)
copy('ember-hardware.py', '/opt/ember-provision/ember-hardware.py')
copy('system-packages.txt', '/opt/ember-provision/system-packages.txt')
for wheel in sorted(payload.glob('*.whl')):
    copy(wheel.name, '/opt/ember-provision/wheels/' + wheel.name)
for name in ['ember-update', 'ember-run', 'ember-status', 'ember-device-settings']:
    copy(name, '/usr/local/bin/' + name, 0o755)
for name in ['ember-provision.service', 'ember-update.service', 'ember-status.service']:
    copy(name, '/etc/systemd/system/' + name)
    # Dependency installation is guarded by provisioned marker.
    link = root / 'etc/systemd/system/multi-user.target.wants' / name
    image_directory(link.parent)
    link.unlink(missing_ok=True)
    link.symlink_to('../' + name)
# Wi-Fi setup hotspot: opens whenever the board has no network, at every boot.
copy('ember_wifi_setup.py', '/usr/local/lib/ember/ember_wifi_setup.py')
copy('ember-wifi-setup.service', '/etc/systemd/system/ember-wifi-setup.service')
link = root / 'etc/systemd/system/multi-user.target.wants/ember-wifi-setup.service'
image_directory(link.parent)
link.unlink(missing_ok=True)
link.symlink_to('../ember-wifi-setup.service')
hostapd_unit = root / 'etc/systemd/system/hostapd.service'  # the script runs hostapd itself
hostapd_unit.unlink(missing_ok=True)
hostapd_unit.symlink_to('/dev/null')
copy('20-ember-virtual.conf', '/etc/X11/xorg.conf.d/20-ember-virtual.conf')
copy('45-ember-primary-display', '/etc/X11/Xsession.d/45-ember-primary-display')
for name in ['ember-anydesk-id.service', 'ember-anydesk-id.timer']:
    copy(name, '/etc/systemd/system/' + name)
timer_link = root / 'etc/systemd/system/timers.target.wants/ember-anydesk-id.timer'
image_directory(timer_link.parent)
timer_link.unlink(missing_ok=True)
timer_link.symlink_to('../ember-anydesk-id.timer')
copy('50-ember.conf', '/etc/lightdm/lightdm.conf.d/50-ember.conf')
home = root / 'home/ember'
image_directory(home)
if (root / 'etc/skel').is_dir():
    shutil.copytree(root / 'etc/skel', home, dirs_exist_ok=True)
write('/home/ember/.config/ember/device.json', json.dumps(source(settings), indent=2) + '\n', 0o600)
# BAYMAX_DATA_DIR: memory, reminders and logs outside the release folders, so updates keep them.
write('/home/ember/.config/ember/initial.env', 'GOOGLE_API_KEY=' + json.dumps(settings['gemini_api_key'])
      + '\nBAYMAX_CAMERA_DEVICE=\nBAYMAX_DATA_DIR=/home/ember/ember-deployment/data\n', 0o600)
for name in ['ember.service', 'ember-settings.service']:
    copy(name, '/home/ember/.config/systemd/user/' + name)
    link = home / '.config/systemd/user/default.target.wants' / name
    image_directory(link.parent)
    link.unlink(missing_ok=True)
    link.symlink_to('../' + name)
for name in ['Ember-Setup.desktop', 'Ember-Status.desktop', 'Ember-Restart.desktop', 'Ember-Device-Settings.desktop']:
    copy(name, '/home/ember/Desktop/' + name, 0o755)
    copy(name, '/home/ember/.local/share/applications/' + name)
copy('ember-session.desktop', '/home/ember/.config/autostart/ember-session.desktop')
copy('DEVICE-SETTINGS.txt', '/home/ember/Desktop/DEVICE-SETTINGS.txt')
write('/home/ember/Desktop/START-HERE.txt', f'''{hostname}
AnyDesk has an automatic 1280x720 Xorg virtual desktop; no monitor is needed.
Status from another device on the same network: http://{hostname}.local:8765/
Use the AnyDesk ID on that page with the password in the private PC handoff file.
The first boot installs system and Python packages; allow time and keep power on.
The selected PR, Wi-Fi and optional Gemini key were supplied during image preparation.
With a preconfigured key Ember starts automatically after installation, sending
microphone/camera context to Gemini. A blank key waits for Ember Setup.
After saving a replacement key, use Restart Ember to apply it.
System setup log: /var/lib/ember/provision.log
Ember update log: /home/ember/ember-deployment/logs/update.log
Terminal status: ember-status
Retry system setup: sudo systemctl restart ember-provision
Retry Ember installation: sudo systemctl restart ember-update
User services: systemctl --user status ember ember-settings
If boot fails, power off and reinsert this SD card in the preparation PC.
No onboard storage or bootloader is programmed by the added Ember setup.
''')
for parent, directories, files in os.walk(home):
    os.chown(parent, 1001, 1001)
    for name in files:
        os.lchown(Path(parent) / name, 1001, 1001)
print('Injected Ember configuration. No ARM code or device capture executed.')
