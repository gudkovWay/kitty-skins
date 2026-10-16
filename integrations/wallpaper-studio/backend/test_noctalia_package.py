"""A Studio distribution must install without unrelated root skin sources."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


class StudioPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        integrations = Path(__file__).resolve().parents[2]
        self.integrations = self.root / 'distribution/integrations'
        for name in ('wallpaper-studio', 'noctalia-theme'):
            shutil.copytree(integrations / name, self.integrations / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'),
            'WALLPAPER_STUDIO_LAUNCHER': str(self.root / 'bin/studio'),
            'WALLPAPER_CONTEXT_SCRIPTS': str(self.root / 'context'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        path = self.integrations / 'wallpaper-studio/install.py'
        spec = importlib.util.spec_from_file_location('isolated_studio_package', path)
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)

    def test_standalone_distribution_dry_run_needs_no_root_skin_checkout(self):
        self.assertFalse((self.root / 'distribution/assets').exists())
        output = io.StringIO()
        with patch.object(sys, 'argv', ['install.py', '--dry-run']), contextlib.redirect_stdout(output):
            result = self.installer.main()
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())['dry_run'])
        self.assertFalse((self.root / 'config').exists())
        self.assertFalse((self.root / 'data').exists())

    def test_bundled_reference_change_creates_a_new_immutable_package_version(self):
        before = self.installer.package_root()
        reference = self.installer.RESOURCE_DIR / 'source.json'
        reference.write_bytes(reference.read_bytes() + b'\n')
        self.assertNotEqual(before, self.installer.package_root())


if __name__ == '__main__':
    unittest.main()
