"""Exercise real GTK preferences on a private Unix-socket Broadway display."""
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
server = shutil.which('gtk4-broadwayd')
gjs = shutil.which('gjs')
if not server or not gjs:
    raise SystemExit('Missing optional gtk4-broadwayd or gjs; native preferences test cannot run. Install nothing automatically.')
base = ROOT / 'build/preferences-test'
for name in ['runtime', 'cache', 'config', 'data']:
    (base / name).mkdir(parents=True, exist_ok=True, mode=0o700)
runtime = base / 'runtime'
os.chmod(runtime, 0o700)
env = {**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'XDG_CACHE_HOME': str(base / 'cache'),
       'XDG_CONFIG_HOME': str(base / 'config'), 'XDG_DATA_HOME': str(base / 'data'),
       'GSETTINGS_BACKEND': 'memory', 'GDK_BACKEND': 'broadway', 'BROADWAY_DISPLAY': ':73',
       'GTK_A11Y': 'none', 'GSK_RENDERER': 'cairo',
       'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/no-session-bus'}
# --unixsocket keeps Broadway's HTTP service off TCP; no browser is launched.
with subprocess.Popen([server, '--unixsocket', str(runtime / 'http.socket'), ':73'],
                      env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as child:
    try:
        deadline = time.monotonic() + 3
        while child.poll() is None and not list(runtime.glob('broadway*.socket')) and time.monotonic() < deadline:
            time.sleep(0.02)
        if child.poll() is not None:
            raise SystemExit(f'Private display unavailable: {child.stderr.read(2048).decode(errors="replace")}')
        subprocess.run([gjs, '-m', 'tests/preferences-window.js'], env=env, cwd=ROOT, check=True, timeout=15)
    finally:
        child.terminate()
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=2)
        for socket in runtime.glob('*.socket'): socket.unlink(missing_ok=True)
