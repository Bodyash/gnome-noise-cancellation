"""Build the source-only extension ZIP. Does not install or enable it."""
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
program = shutil.which('gnome-extensions')
if not program:
    raise SystemExit('gnome-extensions is missing; install GNOME development tools manually to pack.')
(ROOT / 'build').mkdir(exist_ok=True)
compiler = shutil.which('glib-compile-schemas')
if not compiler:
    raise SystemExit('glib-compile-schemas is missing; install GLib development tools manually to pack.')
subprocess.run([compiler, '--strict', str(ROOT / 'extension/schemas')],
    cwd=ROOT, check=True, timeout=10)
subprocess.run([program, 'pack', '--force', '--extra-source=core', '--extra-source=audio',
    '--extra-source=worker', '--extra-source=preferences', '--extra-source=../LICENSE', '--out-dir=build', 'extension'],
    cwd=ROOT, check=True, timeout=15)
print('Built source-only extension ZIP under build/; nothing installed or enabled.')
