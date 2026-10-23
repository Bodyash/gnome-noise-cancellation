"""Run all device-free checks with installed dependencies; never install or publish."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('run', 'process', 'dependencies', 'session', 'extension-unload', 'backend',
          'preferences', 'settings', 'processing-binding', 'presentation', 'routing-report')


def run_checks(run=subprocess.run, *, package=True):
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GSETTINGS_BACKEND': 'memory',
           'GST_REGISTRY': str(build / 'gst-registry.bin'),
           'PIPEWIRE_REMOTE': 'noise-cancellation-nonexistent-ci-server',
           'PIPEWIRE_RUNTIME_DIR': str(build / 'no-audio-server'),
           'DBUS_SESSION_BUS_ADDRESS': f'unix:path={build}/no-session-bus'}
    commands = [
        ['glib-compile-schemas', '--strict', str(ROOT / 'extension/schemas')],
        [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'],
        *[['gjs', '-m', f'tests/{name}.js'] for name in SUITES],
        ['gjs', '-m', 'tools/validate-preferences.js'],
        [sys.executable, '-B', '-I', 'extension/worker/main.py', '--check-all'],
    ]
    if package:
        commands.extend(([sys.executable, 'tools/pack.py'], [sys.executable, 'tools/validate.py']))
    for command in commands:
        print(f"Check: {' '.join(command)}", flush=True)
        run(command, cwd=ROOT, env=env, check=True, timeout=120)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests-only', action='store_true', help='Leave ZIP build/validation to a later CI step.')
    args = parser.parse_args()
    try: run_checks(package=not args.tests_only)
    except FileNotFoundError as error:
        print(f'Missing check tool: {error.filename}. Install documented development dependencies manually.', file=sys.stderr)
        raise SystemExit(1)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f'Checks stopped: {error}', file=sys.stderr)
        raise SystemExit(1)
