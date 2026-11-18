import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wifi_setup', Path(__file__).parent / 'payload/ember-wifi-setup.py')
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

    def test_new_network_can_be_first_or_backup(self):
        first = module.with_network(SAVED, 'Cafe', '', preferred=True)
        self.assertIn('priority=101', first)  # above natey's 100
        backup = module.with_network(SAVED, 'Cafe', '', preferred=False)
        self.assertRegex(backup, r'ssid=43616665\n    scan_ssid=1\n    priority=0\n')
        self.assertIn('priority=200', module.with_network('country=US\n', 'Cafe', ''))

    def test_prefer_moves_one_network_first_and_lists_in_try_order(self):
        text = module.with_preferred(SAVED, 'Home')  # Home had no priority line
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wpa_supplicant.conf'
            path.write_text(text)
            self.assertEqual(module.saved_ssids(path), ['Home', 'natey'])
            module.prefer_network('natey', path)
            self.assertEqual(module.saved_ssids(path), ['natey', 'Home'])
            self.assertIn('psk="old-password"', path.read_text())  # credentials untouched
        with self.assertRaises(ValueError):
            module.with_preferred(SAVED, 'Missing')

    def test_open_network_and_validation(self):
        self.assertIn('key_mgmt=NONE', module.network_block('Cafe', ''))
        for ssid, password in [('', 'password1'), ('x' * 33, 'password1'), ('Home', 'short'),
                               ('Home', 'x' * 64), ('Bad\nName', 'password1')]:
            with self.subTest(ssid=ssid, password=password), self.assertRaises(ValueError):
                module.validate(ssid, password)

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

    def test_blank_card_keeps_setup_hotspot_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wpa_supplicant.conf'
            path.write_text('country=US\nupdate_config=1\n')
            with patch.object(module, 'SUPPLICANT', path):
                self.assertFalse(module.has_saved_network())
                module.save_network('Open', '', path)
                self.assertTrue(module.has_saved_network())

    def test_scan_lists_visible_names_once(self):
        output = '\tSSID: Home\n\tSSID: \n\tSSID: Home\n\tSSID: Caf\\xc3\\xa9\n\tSSID: \\x00\\x00\n'
        self.assertEqual(module.scan_ssids(output), ['Home', 'Café'])

    def test_hotspot_is_password_protected_and_page_escapes_names(self):
        config = module.hostapd_config('Ember-Setup-1A2B', 'abcd2345ef')
        self.assertIn('wpa=2', config)
        self.assertIn('wpa_passphrase=abcd2345ef', config)
        with self.assertRaises(ValueError):
            module.hostapd_config('Ember-Setup-1A2B', '')
        html = module.page(['<script>'], 'bad "x"')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_hotspot_name_matches_private_access_file(self):
        with patch.object(module.socket, 'gethostname', return_value='ember-003'):
            self.assertEqual(module.hotspot_name(), 'Ember-Setup-003')

    def test_failed_join_reopens_hotspot_until_online(self):
        portal_runs = []
        def start(portal, secret):
            portal_runs.append(secret)
            portal.submitted.set()
            return []
        with patch.object(module, 'start_shared_hotspot', return_value=None), \
             patch.object(module, 'start_hotspot', side_effect=start), \
             patch.object(module, 'stop_hotspot'), patch.object(module.time, 'sleep'), \
             patch.object(module, 'join_saved', side_effect=[False, True]):
            module.setup_until_online({'password': 'abcd2345ef'})
        self.assertEqual(len(portal_runs), 2)

    def test_shared_hotspot_closes_once_online_without_take_turns(self):
        alive = unittest.mock.Mock(**{'poll.return_value': None})
        with patch.object(module, 'start_shared_hotspot', return_value=[alive]), \
             patch.object(module, 'start_hotspot') as take_turns, \
             patch.object(module, 'stop_hotspot') as stop, patch.object(module.time, 'sleep'), \
             patch.object(module, 'online', side_effect=[False, False, True]), \
             patch.object(module, 'keep_address') as keep:
            module.setup_until_online({'password': 'abcd2345ef'})
        take_turns.assert_not_called()
        self.assertEqual(keep.call_count, 3)  # the DHCP watch runs while the hotspot is up
        self.assertEqual(stop.call_args.args[2], 'ap0')

    def test_shared_hotspot_stays_open_after_a_recent_save(self):
        portal = module.Portal()
        alive = unittest.mock.Mock(**{'poll.return_value': None})
        clock = iter([100.0, 100.0 + module.STAY_OPEN])
        portal.last_save = 50.0
        with patch.object(module.time, 'sleep'), patch.object(module, 'online', return_value=True), \
             patch.object(module, 'keep_address'), \
             patch.object(module.time, 'monotonic', side_effect=lambda: next(clock)):
            self.assertTrue(module.shared_until_online(portal, [alive]))
        self.assertRaises(StopIteration, next, clock)  # first check (recent save) kept it open

    def test_shared_hotspot_exit_falls_back_to_take_turns(self):
        dead = unittest.mock.Mock(**{'poll.return_value': 1})
        with patch.object(module, 'start_shared_hotspot', return_value=[dead]), \
             patch.object(module, 'start_hotspot', side_effect=lambda portal, secret: portal.submitted.set() or []) as take_turns, \
             patch.object(module, 'stop_hotspot'), patch.object(module.time, 'sleep'), \
             patch.object(module, 'join_saved', return_value=True):
            module.setup_until_online({'password': 'abcd2345ef'})
        take_turns.assert_called_once()

    def test_shared_ap_uses_its_own_interface_and_mac(self):
        self.assertIn('interface=ap0', module.hostapd_config('Ember-Setup-1', 'abcd2345ef', 'ap0'))
        self.assertIn('channel=6', module.hostapd_config('Ember-Setup-1', 'abcd2345ef', 'ap0'))
        self.assertEqual(module.ap_mac('a8:49:ba:19:2b:be\n'), '02:49:ba:19:2b:be')

    def test_shared_page_lists_saved_networks_and_offers_more(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wpa_supplicant.conf'
            path.write_text(SAVED)
            with patch.object(module, 'SUPPLICANT', path), patch.object(module, 'connected_ssid', return_value=''), \
                 patch.object(module, 'online', return_value=True):
                html = module.page(['Cafe'], '', shared=True)
        self.assertIn('<li>natey <i>(tried first)</i></li>', html)
        self.assertIn('name="ssid" value="Home"', html)  # Use first button
        self.assertNotIn('value="1" checked', html)  # online: new network defaults to backup
        self.assertIn('add several networks', html)
        self.assertNotIn('leave this hotspot', html)




class LeaseTests(unittest.TestCase):
    """The DHCP watch: wlan0 joined without an address, or moved networks with the old one."""

    def watch(self, steps, online=False):
        """steps: (seconds, wpa_cli status dict, wlan0 address). Returns the times DHCP was renewed."""
        lease, renewed, ifup = module.Lease(), [], []
        for now, status, address in steps:
            with patch.object(module, 'wifi_status', return_value=status), \
                 patch.object(module, 'client_address', return_value=address), \
                 patch.object(module, 'online', return_value=online), \
                 patch.object(module, 'client_mode', side_effect=lambda: ifup.append(now)), \
                 patch.object(module, 'renew_address', side_effect=lambda: renewed.append(now)), \
                 patch.object(module.time, 'monotonic', return_value=now):
                lease.check()
        return renewed, ifup

    def joined(self, ssid):
        return {'wpa_state': 'COMPLETED', 'ssid': ssid}

    def test_joined_without_an_address_asks_dhcp_again_spaced_out(self):
        home = self.joined('Home')
        renewed, _ = self.watch([(0, home, ''), (29, home, ''), (30, home, ''), (60, home, ''),
                                 (89, home, ''), (90, home, ''), (95, home, '192.168.1.5')])
        self.assertEqual(renewed, [30, 90])

    def test_moving_networks_with_the_old_address_renews_at_once(self):
        renewed, _ = self.watch([(0, self.joined('Home'), '192.168.1.5'),
                                 (10, self.joined('Phone'), '192.168.1.5'),
                                 (15, self.joined('Phone'), '172.20.10.3'),
                                 (20, self.joined('Phone'), '172.20.10.3')])
        self.assertEqual(renewed, [10])

    def test_a_fresh_lease_on_a_new_network_is_left_alone(self):
        renewed, _ = self.watch([(0, self.joined('Home'), '192.168.1.5'),
                                 (5, {'wpa_state': 'SCANNING'}, '192.168.1.5'),
                                 (10, self.joined('Phone'), '172.20.10.3'),
                                 (15, self.joined('Home'), '192.168.1.5')])
        self.assertEqual(renewed, [])

    def test_not_joined_waits_for_wpa_supplicant(self):
        renewed, ifup = self.watch([(t, {'wpa_state': 'SCANNING'}, '') for t in range(0, 300, 10)])
        self.assertEqual((renewed, ifup), ([], []))

    def test_missing_wpa_supplicant_brings_the_client_back_only_when_offline(self):
        _, ifup = self.watch([(0, {}, ''), (30, {}, ''), (60, {}, '')])
        self.assertEqual(ifup, [0, 60])
        _, ifup = self.watch([(0, {}, ''), (60, {}, '')], online=True)  # Ethernet
        self.assertEqual(ifup, [])

    def test_renew_uses_ifupdown_dhclient_files_or_dhcpcd(self):
        with patch.object(module.shutil, 'which', side_effect=lambda name: name == 'dhclient'), \
             patch.object(module, 'run') as run:
            module.renew_address()
        release, start = (call.args for call in run.call_args_list)
        self.assertEqual(release[:3], ('dhclient', '-4', '-r'))
        self.assertEqual(start[:3], ('dhclient', '-4', '-nw'))
        for command in (release, start):
            self.assertIn('/run/dhclient.wlan0.pid', command)
            self.assertIn('/var/lib/dhcp/dhclient.wlan0.leases', command)
            self.assertEqual(command[-1], 'wlan0')
        with patch.object(module.shutil, 'which', side_effect=lambda name: name == 'dhcpcd'), \
             patch.object(module, 'run') as run:
            module.renew_address()
        self.assertEqual(run.call_args.args, ('dhcpcd', '-4', '-n', 'wlan0'))

    def test_status_and_address_parsing(self):
        status = 'bssid=aa:bb:cc:dd:ee:ff\nssid=Home Wi-Fi\nwpa_state=COMPLETED\nip_address=192.168.1.5\n'
        with patch.object(module, 'run', return_value=module.subprocess.CompletedProcess([], 0, status, '')):
            self.assertEqual(module.wifi_status()['ssid'], 'Home Wi-Fi')
        with patch.object(module, 'run', return_value=module.subprocess.CompletedProcess([], 1, '', 'no')):
            self.assertEqual(module.wifi_status(), {})
        addr = ('3: wlan0: <UP>\n    inet 192.168.4.1/24 scope global wlan0\n'
                '    inet 192.168.1.5/24 brd 192.168.1.255 scope global dynamic wlan0\n')
        with patch.object(module, 'run', return_value=module.subprocess.CompletedProcess([], 0, addr, '')):
            self.assertEqual(module.client_address(), '192.168.1.5')
        with patch.object(module, 'run', return_value=module.subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(module.client_address(), '')

    def test_a_failing_check_never_stops_the_hotspot_loop(self):
        with patch.object(module.lease, 'check', side_effect=OSError('boom')):
            module.keep_address()

if __name__ == '__main__':
    unittest.main()
