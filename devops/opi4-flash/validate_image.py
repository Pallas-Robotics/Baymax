"""Offline assertions for this board image and its secret boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import sys
from unittest.mock import patch
from image_settings import load, source, supplicant, timezone, wifi_networks

root = Path(sys.argv[1])
settings = load(sys.argv[2])
assert 'VERSION_ID="13"' in (root / 'etc/os-release').read_text()
assert 'sun60i-a733-orangepi-4-pro.dtb' in (root / 'boot/dietpiEnv.txt').read_text()
assert list((root / 'usr/lib/modules').rglob('aic8800_fdrv.ko.xz')), 'Wi-Fi driver missing'
assert (root / 'usr/lib/firmware/aic8800/SDIO/aic8800D80').is_dir()
assert 'ember:x:1001:1001:' in (root / 'etc/passwd').read_text()
assert 'AUTO_SETUP_NET_WIFI_ENABLED=1' in (root / 'boot/dietpi.txt').read_text()
assert f'AUTO_SETUP_TIMEZONE={timezone(settings)}' in (root / 'boot/dietpi.txt').read_text().splitlines()
assert os.readlink(root / 'etc/localtime') == '/usr/share/zoneinfo/' + timezone(settings)
assert (root / 'etc/timezone').read_text() == timezone(settings) + '\n'
assert (root / 'etc/wpa_supplicant/wpa_supplicant.conf').read_text() == supplicant(wifi_networks(settings))
boot_wifi = (root / 'boot/dietpi-wifi.txt').read_text().splitlines()
for index, network in enumerate(wifi_networks(settings)):
    assert f"aWIFI_SSID[{index}]={shlex.quote(network['ssid'])}" in boot_wifi
    assert f"aWIFI_KEY[{index}]={shlex.quote(network['password'])}" in boot_wifi
    assert f"aWIFI_KEYMGR[{index}]={'WPA-PSK' if network['password'] else 'NONE'}" in boot_wifi
assert json.loads((root / 'home/ember/.config/ember/device.json').read_text()) == source(settings)
assert (root / 'home/ember/.config/ember/initial.env').read_text() == ('GOOGLE_API_KEY=' + json.dumps(settings['gemini_api_key'])
        + '\nBAYMAX_CAMERA_DEVICE=\nBAYMAX_DATA_DIR=/home/ember/ember-deployment/data\n')
bundle = json.loads((root / 'opt/ember-provision/bundle-source.json').read_text())
assert bundle['branch'] == source(settings)['branch']
for name in ['etc/ember/credentials.json', 'boot/dietpi.txt', 'boot/dietpi-wifi.txt',
             'etc/wpa_supplicant/wpa_supplicant.conf', 'home/ember/.config/ember/device.json',
             'home/ember/.config/ember/initial.env']:
    assert stat.S_IMODE((root / name).stat().st_mode) == 0o600, name
credentials = json.loads((root / 'etc/ember/credentials.json').read_text())
assert len(credentials['anydesk_password']) >= 8
setup_secret = root / 'etc/ember/wifi-setup.json'
assert json.loads(setup_secret.read_text()) == {'password': credentials['setup_wifi_password']}
assert stat.S_IMODE(setup_secret.stat().st_mode) == 0o600
assert credentials['setup_wifi_ssid'] == 'Ember-Setup-' + credentials['hostname'].removeprefix('ember-')[-8:].upper()
assert (root / 'usr/local/lib/ember/ember_wifi_setup.py').is_file()
assert (root / 'etc/systemd/system/ifup@eth0.service.d/ember-ethernet.conf').is_file()
assert (root / 'usr/local/lib/ember/ember_prewarm.py').is_file()
assert (root / 'etc/systemd/system/multi-user.target.wants/ember-prewarm.service').is_symlink()
assert (root / 'var/lib/systemd/linger/ember').is_file()
assert (root / 'usr/local/sbin/ember-ifup-ethernet').stat().st_mode & 0o111
assert (root / 'etc/systemd/system/multi-user.target.wants/ember-wifi-setup.service').is_symlink()
assert os.readlink(root / 'etc/systemd/system/hostapd.service') == '/dev/null'
for name in ('linux_password', 'anydesk_password'):
    if name in settings:
        assert credentials[name] == settings[name]
shared_password = settings.get('access_password') or (
    'linux_password' in settings and 'anydesk_password' in settings and
    settings['linux_password'] == settings['anydesk_password'])
assert credentials['linux_password'] != credentials['anydesk_password'] or shared_password
assert (root / 'etc/hostname').read_text().strip() == credentials['hostname']
assert not (root / 'home/ember/.env').exists()
assert (root / 'opt/ember-python/python/bin/python3.12').exists()
assert (root / 'opt/ember-python/python/bin/python3.12').stat().st_uid == 0
assert 'Driver "dummy"' in (root / 'etc/X11/ember-virtual.conf').read_text()
assert (root / 'etc/X11/xorg.conf.d/20-ember-virtual.conf').is_symlink()
assert os.readlink(root / 'etc/X11/xorg.conf.d/20-ember-virtual.conf') == '/etc/X11/ember-virtual.conf'
assert (root / 'usr/local/sbin/ember-select-display').stat().st_mode & 0o111
assert 'ember-select-display --no-restart' in (root / 'etc/systemd/system/lightdm.service.d/ember-display.conf').read_text()
assert (root / 'etc/systemd/system/ember-display-watch.timer').is_file()
assert (root / 'etc/systemd/system/ember-display-watch.service').is_file()
assert (root / 'usr/local/bin/ember-device-settings').is_file()
assert (root / 'opt/ember-provision/ember-hardware.py').is_file()
assert (root / 'home/ember/.config/ember/initial.env').stat().st_uid == 1001
assert (root / 'home/ember/.local/share/applications/Ember-Device-Settings.desktop').is_file()
for unit in ['ember-provision.service', 'ember-update.service', 'ember-status.service']:
    assert (root / 'etc/systemd/system/multi-user.target.wants' / unit).is_symlink()
    assert (root / 'etc/systemd/system' / unit).is_file()
spec = importlib.util.spec_from_file_location('ember_status', root / 'opt/ember-provision/ember-status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with patch.object(module, 'command', return_value='TEST-STATUS'), patch.object(module, 'anydesk_id', return_value='TEST-STATUS'):
    output = module.status()
    assert 'AnyDesk ID: TEST-STATUS' in output
    assert credentials['anydesk_password'] not in output
    assert credentials['linux_password'] not in output
print('PASS: Debian/board/Wi-Fi, account, secret permissions, virtual desktop, service links, private Python, and status output')

# Every runtime ancestor must be traversable by the non-root desktop user.
for name in ['opt/ember-python/python/bin/python3.12',
             'opt/ember-provision/ember-device-config.py',
             'etc/X11/Xsession.d/45-ember-primary-display']:
    path = root / name
    assert path.stat().st_mode & 0o004, f'Not readable by ember: {name}'
    for parent in path.parents:
        if parent == root:
            break
        assert parent.stat().st_mode & 0o001, f'Not traversable by ember: {parent}'
for directory in (root / 'opt/ember-python').rglob('*'):
    if directory.is_dir():
        assert directory.stat().st_mode & 0o005 == 0o005, directory

# The cached base must carry the packages first boot would otherwise download.
assert (root / 'var/lib/ember/packages-baked').is_file()
assert (root / 'usr/bin/anydesk').exists() and (root / 'usr/sbin/lightdm').exists()
assert (root / 'opt/ember-provision/system-packages.txt').read_text().split()
