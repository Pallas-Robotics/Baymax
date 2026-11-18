import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
# Folders whose Python files are not the repository's own: hidden folders (.venv, .models,
# .local-review, a bootstrap's .venv.previous-*), virtual environments and installed packages.
# The robots run this check before each update: the Orange Pi 5 Max keeps its NPU converter
# venvs (TensorFlow, torch, rknn-toolkit2: about 10,000 files) in Baymax-main/.models.
SKIPPED_FOLDERS = {'venv', '__pycache__', 'site-packages', 'node_modules'}


def python_sources(folder):
    """The repository's .py files under `folder`, without descending into skipped folders."""
    for parent, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders
                            if not name.startswith('.') and name not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if name.endswith('.py'):
                yield Path(parent, name)


class RepositoryChecks(unittest.TestCase):
    def test_python_sources_parse_without_importing_devices(self):
        for folder in ['Baymax-main', 'devops', 'tests']:
            for path in python_sources(ROOT / folder):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))

    def test_local_folders_and_installed_packages_are_not_parsed(self):
        # A Python 2 file in a third-party package there failed the robot's gate, so the
        # update rolled back (and the 5 Max never retries that commit).
        broken = 'print "Python 2 only"\n'
        local = ['Baymax-main/.models/pose_npu/build/venv-onnx/lib/python3.12/site-packages/legacy/__init__.py',
                 'Baymax-main/.venv.previous-20261107-101500/lib/python3.11/site-packages/old.py',
                 'Baymax-main/.venv/lib/python3.12/site-packages/package.py',
                 'Baymax-main/.local-review/scratch.py', 'Baymax-main/features/__pycache__/cached.py',
                 'devops/venv/bin/tool.py', 'tests/node_modules/package/build.py',
                 'Baymax-main/vendor/lib/site-packages/package.py']
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in local:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(broken, encoding='utf-8')
            own = root / 'Baymax-main/features/module.py'
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text('VALUE = 1\n', encoding='utf-8')
            self.assertEqual(self.parse_check(root), [])
            own.write_text(broken, encoding='utf-8')       # the repository's own files still count
            self.assertEqual(len(self.parse_check(root)), 1)

    @staticmethod
    def parse_check(root):
        """The parse check's errors and failures with the repository at `root`."""
        result = unittest.TestResult()
        with patch.object(sys.modules[__name__], 'ROOT', root):
            RepositoryChecks('test_python_sources_parse_without_importing_devices').run(result)
        return result.errors + result.failures

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
