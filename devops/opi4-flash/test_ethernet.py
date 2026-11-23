import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).parent / 'payload/ember-ifup-ethernet'


class EthernetBootTests(unittest.TestCase):
    """ember-ifup-ethernet: wait for DHCP only when a cable is plugged in."""

    def run_script(self, carrier, ifup_seconds=2.0):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'net/eth0').mkdir(parents=True)
            if carrier is not None:
                (directory / 'net/eth0/carrier').write_text(carrier + '\n')
            bin_dir = directory / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'ip').write_text('#!/bin/sh\necho "$@" >> "$LOG.ip"\n')
            # A slow fake ifup, like dhclient waiting for an address.
            ifup = bin_dir / 'ifup'
            ifup.write_text(f'#!/bin/sh\necho "$@" > "$LOG.start"\nsleep {ifup_seconds}\necho done > "$LOG.done"\n')
            for path in bin_dir.iterdir():
                path.chmod(0o755)
            log = directory / 'log'
            env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', EMBER_SYS_NET=str(directory / 'net'),
                       EMBER_IFUP=str(ifup), EMBER_CARRIER_WAIT='1', LOG=str(log))
            started = time.monotonic()
            result = subprocess.run(['/bin/sh', str(SCRIPT), 'eth0'], env=env, timeout=20)
            took = time.monotonic() - started
            deadline = time.monotonic() + 10
            while not Path(str(log) + '.done').exists() and time.monotonic() < deadline:
                time.sleep(0.1)  # the background ifup still runs to the end
            return (result.returncode, took, Path(str(log) + '.start').read_text().split(),
                    Path(str(log) + '.done').exists(), Path(str(log) + '.ip').read_text().split())

    def test_cable_in_waits_for_ifup_as_before(self):
        code, took, args, done, ip = self.run_script('1')
        self.assertEqual((code, args, done), (0, ['--allow=hotplug', 'eth0'], True))
        self.assertGreaterEqual(took, 2.0)
        self.assertEqual(ip, ['link', 'set', 'eth0', 'up'])

    def test_no_cable_returns_quickly_and_still_runs_ifup(self):
        for carrier in ('0', None):  # no cable, or the port is still down
            code, took, args, done, _ = self.run_script(carrier, ifup_seconds=3.0)
            self.assertEqual(code, 0)
            self.assertLess(took, 2.5)  # 1 s carrier wait, not the 3 s ifup
            self.assertEqual(args, ['--allow=hotplug', 'eth0'])
            self.assertTrue(done)

    def test_drop_in_replaces_ifup_for_eth0_only(self):
        text = (Path(__file__).parent / 'payload/ember-ethernet.conf').read_text()
        self.assertIn('ExecStart=\nExecStart=/usr/local/sbin/ember-ifup-ethernet %I\n', text)
        inject = (Path(__file__).parent / 'inject_image.py').read_text()
        self.assertIn("'/etc/systemd/system/ifup@eth0.service.d/ember-ethernet.conf'", inject)
        self.assertIn("'/usr/local/sbin/ember-ifup-ethernet', 0o755", inject)


if __name__ == '__main__':
    unittest.main()
