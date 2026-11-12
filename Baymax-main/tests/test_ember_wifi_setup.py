import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

HOST = Path(__file__).parent / '../deploy/host'
spec = importlib.util.spec_from_file_location('wifi_setup', HOST / 'ember_wifi_setup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SAVED = '''country=US
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
network={
    ssid=6e61746579
    priority=100
    key_mgmt=NONE
}
network={
    ssid="Home"
    psk="old-password"
}
'''


def done(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Clock:
    """time.monotonic and Event.wait that advance a fake clock instead of sleeping."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WifiSetupTests(unittest.TestCase):
    def test_new_network_replaces_same_name_and_keeps_others(self):
        text = module.with_network(SAVED, 'Home', 'new-password')
        self.assertTrue(text.startswith('country=US\nctrl_interface='))
        self.assertIn('ssid=6e61746579', text)  # preloaded hotspot survives
        self.assertNotIn('old-password', text)
        self.assertNotIn('new-password', text)  # only the derived PSK is stored
        psk = hashlib.pbkdf2_hmac('sha1', b'new-password', b'Home', 4096, 32).hex()
        self.assertIn('psk=' + psk, text)
        self.assertEqual(text.count('network={'), 2)
        self.assertEqual(module.with_network(text, 'Home', 'new-password'), text)
        self.assertEqual(module.supplicant_ssids(text), {b'natey', b'Home'})

    def test_open_network_and_validation(self):
        self.assertIn('key_mgmt=NONE', module.network_block('Cafe', ''))
        for ssid, password in [('', 'password1'), ('x' * 33, 'password1'), ('Home', 'short'),
                               ('Home', 'x' * 64), ('Bad\nName', 'password1'), (b'', 'password1')]:
            with self.subTest(ssid=ssid, password=password), self.assertRaises(ValueError):
                module.validate(ssid, password)
        module.validate(b'\xff\xferaw', 'password1')  # names need not be UTF-8

    def test_save_is_private_and_rejects_bad_input_without_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wpa_supplicant.conf'
            path.write_text(SAVED)
            with self.assertRaises(ValueError):
                module.save_network('Home', 'short', path)
            self.assertEqual(path.read_text(), SAVED)
            module.save_network('Café', 'password1', path)
            self.assertIn('ssid=' + 'Café'.encode().hex(), path.read_text())
            if module.os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['wpa_supplicant.conf'])
            # A hand-edited name that is not UTF-8 is kept, not a crash.
            path.write_bytes(SAVED.encode() + b'network={\n    ssid="Caf\xe9"\n    key_mgmt=NONE\n}\n')
            module.save_network('Home', 'password2', path)
            self.assertIn(b'ssid="Caf\xe9"', path.read_bytes())
            self.assertIn(b'Caf\xe9', module.supplicant_ssids(module.read_config(path)))

    def test_scan_lists_visible_names_once_strongest_first(self):
        output = '\tSSID: Home\n\tSSID: \n\tSSID: Home\n\tSSID: Caf\\xc3\\xa9\n\tSSID: \\x00\\x00\n'
        self.assertEqual(module.scan_ssids(output), [b'Home', 'Café'.encode()])
        output = ('BSS aa:aa:aa:aa:aa:aa(on wlan0)\n\tsignal: -80.00 dBm\n\tSSID: Far\n'
                  'BSS bb:bb:bb:bb:bb:bb(on wlan0)\n\tsignal: -40.00 dBm\n\tSSID: \\x20Near\\x5c\n'
                  'BSS cc:cc:cc:cc:cc:cc(on wlan0)\n\tsignal: -30.00 dBm\n\tSSID: Far\n')
        # iw escapes a leading space and backslashes; both are kept as part of the name.
        self.assertEqual(module.scan_ssids(output), [b'Far', b' Near\\'])

    def test_run_survives_output_that_is_not_utf8(self):
        result = module.run(sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"SSID: x\\xff\\xfe")')
        self.assertEqual(result.returncode, 0)
        self.assertIn('SSID: x', result.stdout)

    def test_dead_or_unfinished_routes_are_offline(self):
        states = {'wlan0': 'up', 'eth0': 'up'}
        cases = [
            ('default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.5 metric 600 linkdown\n', False),
            ('default via 192.168.1.1 dev wlan0 proto dhcp metric 600 dead linkdown\n', False),
            ('', False),
            ('default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n', True),
            ('default via 10.0.0.1 dev eth0 linkdown\ndefault via 192.168.1.1 dev wlan0\n', True),
        ]
        with patch.object(module, 'link_state', side_effect=lambda device: states[device]):
            for routes, expected in cases:
                with self.subTest(routes=routes), patch.object(module, 'run', return_value=done(routes)):
                    self.assertIs(module.online(), expected)
            states['wlan0'] = 'dormant'  # associated but the WPA handshake has not finished
            with patch.object(module, 'run', return_value=done('default via 192.168.1.1 dev wlan0\n')):
                self.assertFalse(module.online())

    def test_hotspot_password_file_is_checked_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wifi-setup.json'
            path.write_text(json.dumps({'password': 'abcd2345ef'}))
            self.assertEqual(module.load_secret(path), {'password': 'abcd2345ef'})
            for text in ['', '{"password": "a"bc"}', '["abcd2345ef"]', '"abcd2345ef"', '{}',
                         '{"password": "short"}', '{"password": 12345678}', json.dumps({'password': 'x' * 64}),
                         json.dumps({'password': 'pässwörd1'}), json.dumps({'password': 'abcd\n2345ef'})]:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                    module.load_secret(path)
                self.assertNotIn('abcd', str(caught.exception))  # never logged
            with self.assertRaises(ValueError):
                module.load_secret(Path(directory) / 'missing.json')
        self.assertEqual(module.EX_CONFIG, 78)
        unit = (HOST / 'ember-wifi-setup.service').read_text()
        self.assertIn('RestartPreventExitStatus=78', unit)
        self.assertIn('KillMode=process', unit)

    def test_hotspot_is_password_protected_and_page_escapes_names(self):
        config = module.hostapd_config('Ember-Setup-1A2B', 'abcd2345ef')
        self.assertIn('wpa=2', config)
        self.assertIn('wpa_passphrase=abcd2345ef', config)
        self.assertIn(f'ctrl_interface={module.RUN}/hostapd', config)
        with self.assertRaises(ValueError):
            module.hostapd_config('Ember-Setup-1A2B', '')
        html = module.page(['<script>'], 'bad "x"')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_page_lists_networks_by_their_exact_bytes(self):
        html = module.page([b' Home', b'\xff\xfe'])
        self.assertIn('<option value="">Choose a network</option>', html)
        self.assertIn('<option value="20486f6d65"> Home</option>', html)
        self.assertIn('<option value="fffe">', html)
        self.assertIn('autocapitalize="none"', html)
        self.assertIn('No networks found', module.page([]))


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.portal = module.Portal(lambda ssid, password: self.saved.append((ssid, password)))
        self.portal.networks = [b' Home', b'Cafe']
        self.server = module.ThreadingHTTPServer(('127.0.0.1', 0), self.portal.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path='/', form=None, agent='Mozilla/5.0'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        body = urlencode(form) if form is not None else None
        connection.request(method, path, body, {'User-Agent': agent, 'Content-Type': 'application/x-www-form-urlencoded'})
        response = connection.getresponse()
        text = response.read().decode()
        connection.close()
        return response.status, text

    def test_listed_network_is_saved_exactly_with_its_password(self):
        status, text = self.request('POST', '/connect', {'choice': b' Home'.hex(), 'ssid': '', 'password': ' pass word '})
        self.assertEqual(status, 200)
        self.assertEqual(self.saved, [(b' Home', ' pass word ')])
        self.assertTrue(self.portal.submitted.is_set())
        self.assertEqual(self.portal.chosen, b' Home')

    def test_typed_name_wins_and_is_trimmed_unless_listed(self):
        self.request('POST', '/connect', {'choice': b'Cafe'.hex(), 'ssid': ' Office ', 'password': ''})
        self.request('POST', '/connect', {'choice': '', 'ssid': ' Home', 'password': ''})
        self.assertEqual([ssid for ssid, _ in self.saved], [b'Office', b' Home'])

    def test_no_choice_is_an_error_not_the_first_network(self):
        status, text = self.request('POST', '/connect', {'choice': '', 'ssid': '  ', 'password': 'password1'})
        self.assertEqual(status, 400)
        self.assertIn('Choose a network', text)
        self.assertEqual(self.saved, [])
        self.assertFalse(self.portal.submitted.is_set())

    def test_rejected_network_shows_why(self):
        self.portal.save = module.validate
        status, text = self.request('POST', '/connect', {'choice': b'Cafe'.hex(), 'ssid': '', 'password': 'short'})
        self.assertEqual(status, 400)
        self.assertIn('8-63 characters', text)

    def test_visits_count_but_ios_background_probes_do_not(self):
        self.request('GET', '/hotspot-detect.html', agent='CaptiveNetworkSupport-443.400.1 wispr')
        self.assertEqual(self.portal.last_visit, 0.0)
        status, text = self.request('GET', '/anything')
        self.assertEqual(status, 200)
        self.assertIn('Connect Ember to Wi-Fi', text)
        self.assertGreater(self.portal.last_visit, 0.0)


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class HotspotTests(unittest.TestCase):
    def test_hostapd_messages_track_the_air_and_phones(self):
        hotspot = module.Hostapd()
        hotspot.processes = [FakeProcess(), FakeProcess()]
        hotspot.event('wlan0: AP-ENABLED')
        self.assertTrue(hotspot.enabled.is_set())
        hotspot.event('wlan0: AP-STA-CONNECTED 3A:bb:cc:dd:ee:ff')
        self.assertTrue(hotspot.phones_connected())
        hotspot.event('wlan0: AP-STA-DISCONNECTED 3a:bb:cc:dd:ee:ff')
        self.assertEqual(hotspot.stations, set())
        self.assertTrue(hotspot.alive())
        clock = Clock()
        with patch.object(module.time, 'monotonic', clock.monotonic):
            hotspot.event('wlan0: AP-DISABLED')
            self.assertTrue(hotspot.alive())  # a short drop is allowed
            clock.sleep(30)
            self.assertFalse(hotspot.alive())
            hotspot.event('wlan0: AP-ENABLED')
            self.assertTrue(hotspot.alive())
        hotspot.processes[1].code = 2  # dnsmasq exited
        self.assertFalse(hotspot.alive())

    def test_stations_from_hostapd_cli_when_messages_say_nothing(self):
        hotspot = module.Hostapd()
        with patch.object(module, 'run', side_effect=[done('3a:bb:cc:dd:ee:ff\n')]):
            self.assertTrue(hotspot.phones_connected())
        with patch.object(module, 'run', side_effect=[done('', 1), done('')]):
            self.assertFalse(hotspot.phones_connected())

    def wait(self, portal, hotspot, clock):
        def event_wait(timeout):
            clock.sleep(timeout)
            return portal.submitted.is_set()
        with patch.object(module.time, 'monotonic', clock.monotonic), \
             patch.object(portal.submitted, 'wait', side_effect=event_wait):
            return module.wait_for_phone(portal, hotspot), clock.now

    def test_wait_for_phone(self):
        class Hotspot:
            phone = False
            on_air = True
            def alive(self):
                return self.on_air
            def phones_connected(self):
                return self.phone

        portal, hotspot = module.Portal(None), Hotspot()
        self.enterContext(patch.object(module, 'online', return_value=False))
        outcome, now = self.wait(portal, hotspot, Clock())
        self.assertEqual((outcome, now), ('idle', 1000 + module.RETRY_EVERY))

        hotspot.phone = True  # a connected phone keeps it up, but not forever
        outcome, now = self.wait(portal, hotspot, Clock())
        self.assertEqual((outcome, now), ('idle', 1000 + module.SESSION_LIMIT))

        hotspot.on_air = False
        self.assertEqual(self.wait(portal, hotspot, Clock())[0], 'failed')

        hotspot.on_air = True
        with patch.object(module, 'online', return_value=True):  # Ethernet plugged in
            self.assertEqual(self.wait(portal, hotspot, Clock())[0], 'online')

        portal.submitted.set()
        self.assertEqual(self.wait(portal, hotspot, Clock())[0], 'submitted')


class FakeStack:
    name = 'fake'

    def __init__(self, sessions, joins, visible=(), saved=()):
        self.sessions, self.joins = list(sessions), list(joins)
        self.visible, self.saved = list(visible), set(saved)
        self.join_calls = []

    def scan(self):
        return self.visible

    def saved_ssids(self):
        return self.saved

    def join(self, ssid, wait, visible=()):
        self.join_calls.append((ssid, tuple(visible)))
        return self.joins.pop(0)

    def save(self, ssid, password):
        pass


class SetupLoopTests(unittest.TestCase):
    def run_loop(self, stack):
        outcomes = iter(stack.sessions)
        def session(stack_, portal, secret):
            outcome = next(outcomes)
            if outcome == 'submitted':
                portal.chosen = b'Home'
            return outcome
        with patch.object(module, 'hotspot_session', side_effect=session) as sessions, \
             patch.object(module.time, 'sleep'):
            module.setup_until_online(stack, {'password': 'abcd2345ef'})
        return sessions.call_count

    def test_failed_join_reopens_hotspot_until_online(self):
        stack = FakeStack(['submitted', 'submitted'], [False, True])
        self.assertEqual(self.run_loop(stack), 2)
        self.assertEqual(stack.join_calls, [(b'Home', ()), (b'Home', ())])

    def test_failed_hotspot_restarts_quickly_then_tries_saved_networks(self):
        stack = FakeStack(['failed', 'failed', 'failed'], [True], visible=[b'Home'], saved=[b'Home'])
        self.assertEqual(self.run_loop(stack), 3)
        self.assertEqual(stack.join_calls, [(None, (b'Home',))])

    def test_idle_hotspot_only_leaves_for_a_saved_network_in_range(self):
        stack = FakeStack(['idle', 'idle', 'idle'], [True], visible=[b'Neighbour'], saved=[b'Home'])
        self.assertEqual(self.run_loop(stack), 3)  # the third time it tries anyway
        self.assertEqual(stack.join_calls, [(None, (b'Neighbour',))])
        stack = FakeStack(['idle'], [True], visible=[b'Neighbour', b'Home'], saved=[b'Home'])
        self.assertEqual(self.run_loop(stack), 1)

    def test_ethernet_back_closes_the_hotspot_and_restores_the_client(self):
        stack = FakeStack(['online'], [])
        with patch.object(stack, 'client_mode', create=True) as client:
            self.assertEqual(self.run_loop(stack), 1)
        client.assert_called_once()
        self.assertEqual(stack.join_calls, [])

    def test_session_always_stops_the_hotspot(self):
        class Hotspot:
            stopped = False
            def start(self, name, password):
                raise module.HotspotError('hostapd exited with status 1')
            def stop(self):
                self.stopped = True
        hotspot = Hotspot()
        stack = FakeStack([], [])
        stack.prepare = lambda: [b'Home']
        stack.new_hotspot = lambda: hotspot
        portal = module.Portal(None)
        self.assertEqual(module.hotspot_session(stack, portal, {'password': 'abcd2345ef'}), 'failed')
        self.assertTrue(hotspot.stopped)
        self.assertEqual(portal.networks, [b'Home'])


class IfupdownTests(unittest.TestCase):
    def test_client_goes_through_ifup_unit_when_hotplug(self):
        calls = []
        def fake_run(*command, timeout=60):
            calls.append(command)
            if command[0] == 'pgrep':
                return done('', 1)  # wpa_supplicant and dhclient have exited
            return done('wlan0\n' if command[0] == 'ifquery' else '')
        with patch.object(module, 'run', side_effect=fake_run), patch.object(module.time, 'sleep'):
            stack = module.Ifupdown()
            stack.client_mode()
            stack.leave_client()
        self.assertIn(('systemctl', 'start', 'ifup@wlan0.service'), calls)
        self.assertIn(('systemctl', 'stop', 'ifup@wlan0.service'), calls)
        self.assertNotIn(('ifup', 'wlan0'), calls)

    def test_plain_ifup_otherwise(self):
        calls = []
        def fake_run(*command, timeout=60):
            calls.append(command)
            return done('eth0\n' if command[0] == 'ifquery' else '')
        with patch.object(module, 'run', side_effect=fake_run):
            module.Ifupdown().client_mode()
        self.assertIn(('ifup', 'wlan0'), calls)


class NetworkManagerTests(unittest.TestCase):
    def test_saved_network_keyfile(self):
        text = module.nm_client_keyfile(b'My Wi;Fi', 'pass word1')
        psk = hashlib.pbkdf2_hmac('sha1', b'pass word1', b'My Wi;Fi', 4096, 32).hex()
        self.assertIn('psk=' + psk, text)
        self.assertNotIn('pass word1', text)
        self.assertIn('ssid=' + ';'.join(str(b) for b in b'My Wi;Fi') + ';', text)
        self.assertIn('id=My\\sWi;Fi\\s(Ember\\ssetup)', text)
        self.assertIn('autoconnect-priority=200', text)
        self.assertIn(f'uuid={module.nm_uuid(b"My Wi;Fi")}', text)
        self.assertEqual(module.nm_uuid('My Wi;Fi'), module.nm_uuid(b'My Wi;Fi'))
        self.assertNotIn('[wifi-security]', module.nm_client_keyfile('Cafe', ''))

    def test_hotspot_keyfile(self):
        text = module.nm_hotspot_keyfile('Ember-Setup-5MAX', 'a b\\cdefgh')
        self.assertIn('psk=a\\sb\\\\cdefgh', text)
        self.assertIn('mode=ap', text)
        self.assertIn('method=shared', text)
        self.assertIn(f'address1={module.ADDRESS}/24', text)
        self.assertIn('autoconnect=false', text)
        with self.assertRaises(ValueError):
            module.nm_hotspot_keyfile('Ember-Setup-5MAX', 'short')

    def test_nmcli_parsing(self):
        self.assertEqual(module.nm_scan_ssids('486F6D65\n\n--\n486F6D65\n00000000\nFFFE\n'), [b'Home', b'\xff\xfe'])
        self.assertEqual(list(module.nm_fields('802-11-wireless.ssid:a\\:b\\\\c\n')),
                         [('802-11-wireless.ssid', 'a:b\\c')])

    def test_saved_connections_and_join_tries_visible_ones(self):
        outputs = {
            ('-t', '-f', 'UUID,TYPE', 'connection', 'show'):
                f'u-home:802-11-wireless\nu-eth:802-3-ethernet\n{module.NM_HOTSPOT_UUID}:802-11-wireless\nu-ap:802-11-wireless\n',
            'u-home': '802-11-wireless.ssid:Home\n802-11-wireless.mode:infrastructure\n',
            'u-ap': '802-11-wireless.ssid:Other\n802-11-wireless.mode:ap\n',
        }
        calls = []
        def nmcli(*args, timeout=60):
            calls.append(args)
            if args[:2] == ('-t', '-f') and args[2].startswith('802-11-wireless'):
                return done(outputs[args[-1]])
            return done(outputs.get(args, ''))
        stack = module.NetworkManager()
        with patch.object(stack, 'nmcli', side_effect=nmcli):
            self.assertEqual(stack.saved(), {b'Home': ['u-home']})
            with patch.object(module, 'wait_online', return_value=True):
                self.assertTrue(stack.join(None, 60, [b'Neighbour', b'Home']))
        self.assertTrue(any(args[-4:] == ('uuid', 'u-home', 'ifname', module.IFACE) for args in calls))

    def test_stack_choice(self):
        with patch.dict(module.os.environ, {'EMBER_WIFI_STACK': 'ifupdown'}), \
             patch.object(module, 'run', return_value=done('')):
            self.assertIsInstance(module.network_stack(), module.Ifupdown)
        with patch.dict(module.os.environ, {'EMBER_WIFI_STACK': 'networkmanager'}):
            self.assertIsInstance(module.network_stack(), module.NetworkManager)
        states = {('nmcli', '-t', '-f', 'DEVICE,STATE', 'device'): 'eth0:connected\nwlan0:disconnected\n'}
        with patch.object(module.shutil, 'which', return_value='/usr/bin/nmcli'), \
             patch.object(module, 'run', side_effect=lambda *c, timeout=60: done(states.get(c, ''))):
            self.assertTrue(module.nm_manages('wlan0'))
            states[('nmcli', '-t', '-f', 'DEVICE,STATE', 'device')] = 'wlan0:unmanaged\n'
            self.assertFalse(module.nm_manages('wlan0'))


AIC8800 = """Wiphy phy0
	valid interface combinations:
		 * #{ managed } <= 1, #{ AP } <= 1, #{ P2P-client, P2P-GO } <= 1, #{ P2P-device } <= 1,
		   total <= 4, #channels <= 3
	HT Capability overrides:
"""
BCMDHD = """Wiphy phy0
	valid interface combinations:
		 * #{ AP } <= 2, #{ managed } <= 4, #{ P2P-client, P2P-GO } <= 2, #{ P2P-device } <= 1, #{ IBSS } <= 1,
		   total <= 5, #channels <= 2
"""


class FakeEvent:
    """threading.Event whose wait() advances a fake clock instead of blocking."""
    clock = None

    def __init__(self):
        self.flag = False

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def is_set(self):
        return self.flag

    def wait(self, timeout=None):
        if not self.flag and FakeEvent.clock:
            FakeEvent.clock.sleep(timeout or 0)
        return self.flag


class FakeAP:
    starts, fail_channels, alive_after = [], set(), None

    def __init__(self, iface, address, channel):
        self.iface, self.address, self.channel = iface, address, channel

    def start(self, name, password):
        FakeAP.starts.append(self.channel)
        if self.channel in FakeAP.fail_channels:
            raise module.HotspotError(f'channel {self.channel} not allowed')

    def alive(self):
        return True

    def stop(self):
        pass


class AlwaysOnTests(unittest.TestCase):
    def test_radio_capability(self):
        self.assertEqual(module.ap_with_client(AIC8800), 3)
        self.assertEqual(module.ap_with_client(BCMDHD), 2)
        for info, expected in [
            ('\tvalid interface combinations:\n\t\t * #{ AP } <= 4,\n\t\t   total <= 4, #channels <= 1, STA/AP BI must match\n', 0),
            ('\tvalid interface combinations:\n\t\t * #{ managed, AP } <= 1,\n\t\t   total <= 1, #channels <= 1\n', 0),
            ('\tvalid interface combinations:\n\t\t * #{ managed, AP } <= 2,\n\t\t   total <= 2, #channels <= 1\n', 1),
            ('\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n', 0),
            ('', 0)]:
            with self.subTest(info=info):
                self.assertEqual(module.ap_with_client(info), expected)

    def test_hotspot_shares_the_client_channel_when_it_may(self):
        cases = [(None, 6), (2412, 1), (2437, 6), (2462, 11), (5180, 36), (5745, 149),
                 (5260, 6), (5500, 6), (5955, 6)]  # 52 and 100 are radar channels; 5955 is 6 GHz
        for freq, channel in cases:
            with self.subTest(freq=freq):
                self.assertEqual(module.ap_channel(freq), channel)
        self.assertEqual(module.ap_channel(5180, five_ghz=False), 6)
        config = module.hostapd_config('Ember-Setup-5MAX', 'abcd2345ef', 'ember-ap', 36)
        self.assertIn('interface=ember-ap', config)
        self.assertIn('hw_mode=a', config)
        self.assertIn('channel=36', config)

    def test_client_link(self):
        link = 'Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: Home\\x20\n\tfreq: 2437.0\n\tsignal: -50 dBm\n'
        with patch.object(module, 'run', return_value=done(link)):
            self.assertEqual(module.client_link(), (b'Home ', 2437))
        with patch.object(module, 'run', return_value=done('Not connected.\n')):
            self.assertEqual(module.client_link(), (None, None))

    def test_hotspot_address_avoids_the_home_network(self):
        for routes, expected in [
            ('default via 192.168.1.1 dev wlan0\n192.168.1.0/24 dev wlan0 proto kernel scope link\n', '192.168.4.1'),
            ('default via 192.168.4.1 dev wlan0\n192.168.4.0/24 dev wlan0 proto kernel scope link\n', '10.88.0.1'),
            ('192.168.4.0/24 dev wlan0\n10.0.0.0/8 dev eth0\n', '172.31.88.1'),
            ('192.168.4.0/24 dev ember-ap proto kernel scope link\n', '192.168.4.1')]:  # its own route
            with self.subTest(routes=routes), patch.object(module, 'run', return_value=done(routes)):
                self.assertEqual(module.ap_address(), expected)
        self.assertEqual(module.local_mac('00:11:22:33:44:55'), '02:11:22:33:44:55')
        self.assertEqual(module.local_mac('02:11:22:33:44:55'), '06:11:22:33:44:55')

    def run_always_on(self, links, rounds, online=False, cached=(b'Home',)):
        clock = Clock()
        FakeEvent.clock = clock
        FakeAP.starts = []
        links = list(links)
        stack = FakeStack([], [])
        stack.refresh_networks = lambda: []
        stack.cached_networks = lambda: list(cached)
        count = iter(range(rounds))
        with patch.object(module.threading, 'Event', FakeEvent), patch.object(module, 'Hostapd', FakeAP), \
             patch.object(module, 'add_ap_iface'), patch.object(module, 'remove_ap_iface') as removed, \
             patch.object(module.Portal, 'serve'), patch.object(module, 'ap_address', return_value='192.168.4.1'), \
             patch.object(module, 'client_link', side_effect=lambda: links.pop(0) if len(links) > 1 else links[0]), \
             patch.object(module, 'online', return_value=online), \
             patch.object(module.time, 'monotonic', clock.monotonic), patch.object(module.time, 'sleep', clock.sleep):
            reason = module.always_on(stack, {'password': 'abcd2345ef'}, lambda: next(count, None) is not None)
        removed.assert_called_once()
        return reason

    def test_hotspot_follows_the_client_to_its_channel(self):
        FakeAP.fail_channels = set()
        reason = self.run_always_on([(None, None), (None, None), (b'Home', 2412)], 5, online=True)
        self.assertEqual(reason, 'stopped')
        self.assertEqual(FakeAP.starts, [6, 1])

    def test_five_ghz_refused_falls_back_to_channel_6(self):
        FakeAP.fail_channels = {36}
        self.run_always_on([(b'Home', 5180)], 5, online=True)
        self.assertEqual(FakeAP.starts, [36, 6])  # and stays there

    def test_gives_up_when_the_hotspot_keeps_failing(self):
        FakeAP.fail_channels = {6}
        reason = self.run_always_on([(None, None)], 20)
        self.assertIn('kept failing', reason)
        self.assertEqual(FakeAP.starts, [6, 6, 6])

    def test_gives_up_when_the_client_sees_no_networks(self):
        FakeAP.fail_channels = set()
        reason = self.run_always_on([(None, None)], 1000, cached=())
        self.assertIn('found no networks', reason)
        FakeAP.fail_channels = set()
        self.assertEqual(self.run_always_on([(None, None)], 300, cached=(b'Neighbour',)), 'stopped')

    def test_join_in_place_reports_on_the_page(self):
        portal, stack = module.Portal(None, refresh=lambda: []), FakeStack([], [])
        applied = []
        stack.apply_saved = lambda ssid, wait: applied.append(ssid)
        portal.message = 'Could not join "Home". Check the name and password and try again.'
        clock = Clock()
        with patch.object(module, 'client_link', return_value=(b'Home', 2437)), \
             patch.object(module, 'online', return_value=True), patch.object(module.time, 'sleep', clock.sleep):
            self.assertTrue(module.join_in_place(stack, portal, b'Home'))
        self.assertEqual(portal.status, {'ssid': 'Home', 'state': 'joined'})
        self.assertEqual(applied, [b'Home'])
        with patch.object(module, 'client_link', return_value=(b'Other', 2437)), \
             patch.object(module, 'online', return_value=True), \
             patch.object(module.time, 'monotonic', clock.monotonic), patch.object(module.time, 'sleep', clock.sleep):
            portal.message = 'Could not join "Home". Check the name and password and try again.'
            self.assertFalse(module.join_in_place(stack, portal, b'Home', wait=10))
        self.assertEqual(portal.status['state'], 'failed')
        self.assertIn('Check the name and password', module.status_page(portal.status))
        self.assertIn('refresh', module.status_page({'ssid': 'Home', 'state': 'joining'}))


class InPlacePortalTests(PortalTests):
    def setUp(self):
        super().setUp()
        self.refreshed = threading.Event()
        self.portal.refresh = lambda: (self.refreshed.set(), [b'Fresh'])[1]
        self.portal.in_place = True

    def test_submission_keeps_the_hotspot_and_shows_status(self):
        status, text = self.request('POST', '/connect', {'choice': b'Cafe'.hex(), 'ssid': '', 'password': 'password1'})
        self.assertEqual(status, 200)
        self.assertIn('Joining Cafe', text)
        self.assertEqual(self.portal.status, {'ssid': 'Cafe', 'state': 'joining'})
        status, text = self.request('GET', '/status')
        self.assertIn('Joining Cafe', text)

    def test_page_rescans_while_up(self):
        status, text = self.request('GET', '/')
        self.assertIn('stays on while Ember joins', text)
        self.assertTrue(self.refreshed.wait(5))
        for _ in range(50):
            if self.portal.networks == [b'Fresh']:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.portal.networks, [b'Fresh'])

if __name__ == '__main__':
    unittest.main()
