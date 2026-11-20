import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('prewarm', Path(__file__).parent / 'payload/ember-prewarm.py')
prewarm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prewarm)


class PrewarmTests(unittest.TestCase):
    def test_reads_each_allowed_listed_file_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(os.path.realpath(directory))
            allowed = root / 'home'
            allowed.mkdir()
            for name in ('a.so', 'b.pyc'):
                (allowed / name).write_bytes(b'x' * 3000)
            (root / 'secret').write_text('outside')
            lists = root / 'lists'
            lists.mkdir()
            (lists / 'ember.txt').write_text(f'{allowed}/a.so\n{allowed}/b.pyc\n{allowed}/gone.so\n{root}/secret\n')
            (lists / 'vision-pose.txt').write_text(f'{allowed}/a.so\n')
            (lists / 'notes.md').write_text(f'{allowed}/b.pyc\n')
            paths = prewarm.startup_files(lists, (str(allowed) + '/',))
            self.assertEqual(paths, [f'{allowed}/a.so', f'{allowed}/b.pyc'])
            self.assertEqual(sum(prewarm.warm(p) for p in paths), 6000)
            self.assertEqual(prewarm.warm(f'{allowed}/gone.so'), 0)

    def test_no_lists_yet_is_nothing_to_do(self):
        self.assertEqual(prewarm.startup_files(Path('/nonexistent/startup-files')), [])

    def test_installed_as_a_non_blocking_boot_service_with_linger(self):
        here = Path(__file__).parent
        unit = (here / 'payload/ember-prewarm.service').read_text()
        self.assertIn('Type=exec', unit)          # never holds multi-user.target
        self.assertIn('User=ember', unit)
        inject = (here / 'inject_image.py').read_text()
        self.assertIn("'ember-prewarm.service']", inject)
        self.assertIn("write('/var/lib/systemd/linger/ember', '')", inject)


if __name__ == '__main__':
    unittest.main()
