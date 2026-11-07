import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from image_settings import REPOSITORY, TARGET_BOARD
import verify_image
from verify_image import main, verify


class VerifyImageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.image = self.root / 'ember-opi4.img'
        self.image.write_bytes(b'public test image bytes')
        self.manifest = self.root / 'ready-image.json'
        self.data = {'format': 1, 'board': TARGET_BOARD, 'repository': REPOSITORY,
                     'image': self.image.name, 'length': self.image.stat().st_size,
                     'sha256': hashlib.sha256(self.image.read_bytes()).hexdigest(),
                     'wifi_ssid': 'PRIVATE_TEST_NETWORK', 'hostname': 'PRIVATE_TEST_HOST'}
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.data), encoding='utf-8')

    def test_valid_image_is_read_only_and_result_excludes_private_metadata(self):
        before = self.image.read_bytes()
        result = verify(self.manifest)
        self.assertEqual(result['board'], TARGET_BOARD)
        self.assertNotIn('wifi_ssid', result)
        self.assertEqual(self.image.read_bytes(), before)

    def test_cli_does_not_print_private_network_or_host(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['--manifest', str(self.manifest)]), 0)
        self.assertNotIn('PRIVATE_TEST', output.getvalue())
        self.assertIn('No disk was written', output.getvalue())

    def test_wrong_board_repository_and_legacy_manifest_are_rejected(self):
        for field, value in [('board', 'orange-pi-5-max'), ('repository', 'https://example.com/untrusted.git'),
                             ('format', None), ('format', True)]:
            original = self.data[field]
            self.data[field] = value
            self.save()
            with self.assertRaises(ValueError):
                verify(self.manifest)
            self.data[field] = original

    def test_truncated_or_changed_image_is_rejected(self):
        self.image.write_bytes(b'short')
        with self.assertRaisesRegex(ValueError, 'length'):
            verify(self.manifest)
        self.image.write_bytes(b'x' * self.data['length'])
        with self.assertRaisesRegex(ValueError, 'SHA256 does not match'):
            verify(self.manifest)

    def test_invalid_hash_and_boolean_size_are_rejected(self):
        self.data['sha256'] = 'bad'
        self.save()
        with self.assertRaisesRegex(ValueError, 'SHA256 must'):
            verify(self.manifest)
        self.data['length'] = True
        self.save()
        with self.assertRaisesRegex(ValueError, 'length'):
            verify(self.manifest)

    def test_path_traversal_absolute_and_windows_paths_are_rejected(self):
        for name in ['../other.img', '/tmp/other.img', r'C:\other.img', r'..\other.img']:
            self.data['image'] = name
            self.save()
            with self.assertRaisesRegex(ValueError, 'filename'):
                verify(self.manifest)

    def test_python_before_3_11_verifies_an_image_of_several_reads(self):
        # macOS's own python3 is 3.9, whose hashlib has no file_digest (added in 3.11).
        self.image.write_bytes(bytes(range(256)) * 9000)  # 2.2 MiB: three 1 MiB reads
        self.data.update(length=self.image.stat().st_size, sha256=hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.save()
        output = io.StringIO()
        with patch.object(verify_image, 'hashlib', SimpleNamespace(sha256=hashlib.sha256)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(['--manifest', str(self.manifest)]), 0)
        self.assertIn('Verified ember-opi4.img: 2304000 bytes', output.getvalue())

    def test_missing_image_and_invalid_json_return_failure(self):
        self.image.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            verify(self.manifest)
        self.manifest.write_text('invalid', encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--manifest', str(self.manifest)]), 2)


if __name__ == '__main__':
    unittest.main()
