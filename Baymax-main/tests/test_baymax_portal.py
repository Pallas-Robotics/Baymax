"""The x86 robots' always-on hotspot: captive portal (deploy/host/baymax_portal.py)
and its boot order in startup.sh. Offline: nmcli, ip and the internet check are faked."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

HOST = Path(__file__).parent / '../deploy/host'

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask:
    with patch.dict(os.environ, {'EMBER_DEVICE_CONFIG': '/nonexistent/device.toml'}):
        spec = importlib.util.spec_from_file_location('baymax_portal', HOST / 'baymax_portal.py')
        portal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(portal)


def done(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, '')


class FakeNmcli:
    """subprocess.run for nmcli/ip: records commands and answers from a table."""
    def __init__(self, scan='', profile=None, fail=None):
        self.calls, self.scan, self.profile, self.fail = [], scan, profile, fail

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.fail and self.fail in command:
            raise subprocess.TimeoutExpired(command, kwargs.get('timeout'))
        if command[:2] == ['ip', '-4']:
            return done('5: wlo1    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlo1\n')
        if command[1:4] == ['-t', '-f', 'DEVICE,TYPE,STATE']:
            return done('wlo1:wifi:connected\np2p-dev-wlo1:wifi-p2p:disconnected\nwlx00:wifi:disconnected\n')
        if command[1:4] == ['-t', '-f', 'SSID']:
            return done(self.scan)
        if command[1:4] == ['-t', '-f', 'connection.type,802-11-wireless.mode']:
            return done(self.profile or '', 0 if self.profile else 10)
        return done()


@unittest.skipUnless(flask, 'Flask is not installed')
class PortalTests(unittest.TestCase):
    def setUp(self):
        self.client = portal.app.test_client()
        portal._internet.update(checked=0.0, up=False)
        portal.last_status.update({'ssid': None, 'state': 'idle', 'detail': ''})

    def test_phone_checks_open_the_page_only_while_offline(self):
        with patch.object(portal.subprocess, 'run', FakeNmcli()):
            with patch.object(portal, 'internet_up', return_value=False):
                for path in ['/generate_204', '/hotspot-detect.html', '/connecttest.txt']:
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 302, path)
                    self.assertEqual(response.headers['Location'], 'http://10.42.0.1/')
            with patch.object(portal, 'internet_up', return_value=True):
                self.assertEqual(self.client.get('/generate_204').status_code, 204)
                response = self.client.get('/hotspot-detect.html')
                self.assertIn(b'<BODY>Success</BODY>', response.data)
                self.assertEqual(self.client.get('/ncsi.txt').data, b'Microsoft NCSI')
            self.assertEqual(self.client.get('/no-such-page').headers['Location'], 'http://10.42.0.1/')

    def test_page_suggests_nearby_networks_without_autocorrect(self):
        with patch.object(portal.subprocess, 'run', FakeNmcli(scan='Home\n<b>x\\:y</b>\n\nHome\nBaymax_Hotspot\n')):
            html = self.client.get('/').get_data(as_text=True)
        self.assertIn('autocapitalize="none"', html)
        self.assertIn('autocorrect="off"', html)
        self.assertIn('<option value="Home">', html)
        self.assertIn('<option value="&lt;b&gt;x:y&lt;/b&gt;">', html)
        self.assertEqual(html.count('<option value="Home">'), 1)
        self.assertNotIn('<option value="Baymax_Hotspot">', html)

    def test_password_is_kept_exactly_and_checked(self):
        with patch.object(portal.subprocess, 'run', FakeNmcli()), \
             patch.object(portal.threading, 'Thread') as thread:
            self.client.post('/setup', data={'ssid': ' Home ', 'password': ' secret pass '})
            self.assertEqual(thread.call_args.kwargs['args'], ('Home', ' secret pass '))
            thread.reset_mock()
            for ssid, password, message in [('Home', 'short', '8-63 characters'),
                                            ('Baymax_Hotspot', 'password1', 'my own hotspot'),
                                            ('x' * 33, 'password1', '1-32 characters')]:
                response = self.client.post('/setup', data={'ssid': ssid, 'password': password})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_data(as_text=True))
            thread.assert_not_called()

    def test_scans_for_the_network_before_joining(self):
        fake = FakeNmcli(scan='Home\n')
        with patch.object(portal.subprocess, 'run', fake), patch.object(portal.time, 'sleep'):
            portal.switch_network('Home', 'password1')
        rescan = fake.calls.index(['nmcli', 'device', 'wifi', 'rescan', 'ifname', 'wlx00', 'ssid', 'Home'])
        connect = fake.calls.index(['nmcli', 'device', 'wifi', 'connect', 'Home', 'ifname', 'wlx00', 'password', 'password1'])
        self.assertLess(rescan, connect)
        self.assertEqual(portal.last_status['state'], 'connected')

    def test_existing_profile_is_moved_to_the_uplink(self):
        fake = FakeNmcli(scan='Home\n', profile='connection.type:802-11-wireless\n802-11-wireless.mode:infrastructure\n')
        with patch.object(portal.subprocess, 'run', fake), patch.object(portal.time, 'sleep'):
            portal.switch_network('Home', 'password1')
        self.assertIn(['nmcli', 'connection', 'modify', 'Home', 'connection.interface-name', 'wlx00',
                       'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', 'password1'], fake.calls)
        self.assertIn(['nmcli', 'connection', 'up', 'Home', 'ifname', 'wlx00'], fake.calls)

    def test_timeout_is_reported_without_the_password(self):
        fake = FakeNmcli(scan='Home\n', fail='connect')
        printed = io.StringIO()
        with patch.object(portal.subprocess, 'run', fake), patch.object(portal.time, 'sleep'), \
             contextlib.redirect_stdout(printed):
            portal.switch_network('Home', 'password1')
        self.assertEqual(portal.last_status['state'], 'failed')
        self.assertEqual(portal.last_status['detail'], 'timed out')
        self.assertNotIn('password1', printed.getvalue())

    def test_uplink_is_never_the_hotspot_radio(self):
        with patch.object(portal.subprocess, 'run', FakeNmcli()):
            self.assertEqual(portal.uplink_iface(), 'wlx00')


class StartupScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = (HOST / 'startup.sh').read_text()

    @unittest.skipUnless(shutil.which('bash'), 'bash is not installed')
    def test_syntax(self):
        for name in ['startup.sh', '../provision/provision.sh']:
            subprocess.run(['bash', '-n', str(HOST / name)], check=True)

    def test_portal_and_hotspot_watch_start_before_any_wait(self):
        main = self.script[self.script.index('\nensure_captive_dns\n'):]
        self.assertLess(main.index('start_portal_supervised'), main.index('sleep 30'))
        self.assertLess(main.index('keep_hotspot_up &'), main.index('sleep 30'))
        self.assertEqual(len(re.findall(r'^start_portal_supervised$', self.script, re.M)), 1)

    def test_captive_names_are_only_phone_checks(self):
        names = re.search(r'for name in (.*?); do', self.script, re.S)[1].replace('\\', ' ').split()
        self.assertIn('captive.apple.com', names)
        self.assertIn('connectivitycheck.gstatic.com', names)
        self.assertNotIn('www.apple.com', names)  # real sites keep working while online
        self.assertNotIn('www.google.com', names)

    def test_provisioned_hotspot_has_fixed_address_and_keeps_retrying(self):
        provision = (HOST / '../provision/provision.sh').read_text()
        self.assertIn('ipv4.addresses 10.42.0.1/24', provision)
        self.assertIn('connection.autoconnect-retries 0', provision)


if __name__ == '__main__':
    unittest.main()
