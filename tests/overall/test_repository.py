import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RepositoryChecks(unittest.TestCase):
    def test_python_sources_parse_without_importing_devices(self):
        excluded = {'.local-review', '.venv', 'venv', '__pycache__'}
        for folder in ['Baymax-main', 'devops', 'tests']:
            for path in (ROOT / folder).rglob('*.py'):
                if not excluded.intersection(path.relative_to(ROOT).parts):
                    with self.subTest(path=str(path.relative_to(ROOT))):
                        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))

    def test_chat_ui_regressions(self):
        node = shutil.which('node')
        if not node:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('CI requires Node for the chat UI regression check')
            self.skipTest('Node is unavailable; install it to validate chat UI on this machine')
        result = subprocess.run([node, str(ROOT / 'test/monitor-chat.cjs')], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
