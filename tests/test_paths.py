"""Relocated source installation and worker resolution; no desktop audio access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PathTests(unittest.TestCase):
    def test_relocated_extension_and_worker_ignore_original_path_and_cwd(self):
        gjs = shutil.which('gjs')
        if not gjs: self.skipTest('GJS is not installed; relocation fixture cannot execute.')
        build = ROOT / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='relocated extension ', dir=build) as temporary:
            copied = Path(temporary) / 'installation with spaces'
            shutil.copytree(ROOT / 'extension', copied,
                ignore=shutil.ignore_patterns('__pycache__', 'gschemas.compiled'))
            unrelated = Path(temporary) / 'unrelated working directory'
            unrelated.mkdir()
            result = subprocess.run([gjs, '-m', str(ROOT / 'tests/fixtures/relocated-paths.js'), str(copied)],
                cwd=unrelated, capture_output=True, text=True, check=True, timeout=5)
            command = json.loads(result.stdout)
            self.assertEqual(command[1:3], ['-B', '-I'])
            self.assertEqual(command[3], str(copied / 'worker/main.py'))
            env = {**os.environ, 'GST_REGISTRY': str(build / 'gst-registry.bin'),
                'PIPEWIRE_REMOTE': 'nonexistent-path-test-server', 'PIPEWIRE_RUNTIME_DIR': str(unrelated)}
            result = subprocess.run([*command, '--check-all'], cwd=unrelated, env=env,
                capture_output=True, text=True, check=True, timeout=5)
            report = json.loads(result.stdout)
            self.assertTrue(report['ready'])
            self.assertEqual(len(report['backends']), 3)
            self.assertTrue(all(item['ready'] for item in report['backends']))
