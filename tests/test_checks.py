"""The shared CI/local runner must gate packaging and fail before later checks."""
import importlib.util
import contextlib
import io
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_checks', ROOT / 'tools/check.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class CheckRunnerTests(unittest.TestCase):
    def test_all_device_free_suites_gate_packaging_in_an_isolated_environment(self):
        calls = []
        def run(argv, **kwargs): calls.append((argv, kwargs))
        with contextlib.redirect_stdout(io.StringIO()): checks.run_checks(run=run)
        commands = [args for args, _ in calls]
        self.assertEqual(commands[0], ['glib-compile-schemas', '--strict', str(ROOT / 'extension/schemas')])
        self.assertEqual(commands[1][1:], ['-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'])
        suites = {args[-1] for args in commands if args[0] == 'gjs'}
        self.assertTrue({'tests/run.js', 'tests/session.js', 'tests/backend.js',
                         'tests/preferences.js', 'tests/extension-unload.js'} <= suites)
        self.assertNotIn('tests/preferences-window.js', suites)
        self.assertEqual(commands[-2][-1], 'tools/pack.py')
        self.assertEqual(commands[-1][-1], 'tools/validate.py')
        for _, kwargs in calls:
            self.assertTrue(kwargs['check'])
            self.assertEqual(kwargs['cwd'], ROOT)
            self.assertGreater(kwargs['timeout'], 0)
            env = kwargs['env']
            self.assertEqual(env['GSETTINGS_BACKEND'], 'memory')
            self.assertIn('nonexistent', env['PIPEWIRE_REMOTE'])
            self.assertEqual(Path(env['GST_REGISTRY']).parent, ROOT / 'build')

    def test_tests_only_mode_does_not_build_or_validate_an_archive(self):
        commands = []
        def run(argv, **_kwargs): commands.append(argv)
        with contextlib.redirect_stdout(io.StringIO()): checks.run_checks(run=run, package=False)
        self.assertEqual(commands[-1][-1], '--check-all')
        self.assertFalse(any('tools/pack.py' in args or 'tools/validate.py' in args for args in commands))

    def test_failure_stops_the_check_sequence(self):
        calls = []
        def run(argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 2: raise subprocess.CalledProcessError(1, argv)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
            checks.run_checks(run=run)
        self.assertEqual(len(calls), 2)
