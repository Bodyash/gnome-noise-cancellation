"""Read-only metadata, file hygiene and GJS syntax checks; no graph mutations."""
import ast
import json
import re
from pathlib import Path
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent
metadata = json.loads((ROOT / "extension/metadata.json").read_text())
assert metadata["uuid"] == "noise-cancellation@bodyash.github.io"
assert re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9._-]+', metadata['uuid'])
assert metadata['url'] == 'https://github.com/bodyash/gnome-noise-cancellation'
assert metadata["shell-version"] == ["51"]
assert "session-modes" not in metadata, "Default user mode must not be redundantly declared"
assert type(metadata['version']) is int and metadata['version'] == 1, \
    'Keep source version at 1; GNOME Extensions manages submission numbering'
schema_id = 'org.gnome.shell.extensions.noise-cancellation'
assert metadata['settings-schema'] == schema_id
schema = ROOT / 'extension/schemas' / (schema_id + '.gschema.xml')
assert ET.parse(schema).find('schema').attrib['id'] == schema_id
subprocess.run(['glib-compile-schemas', '--strict', '--dry-run', str(schema.parent)],
               cwd=ROOT, check=True, timeout=10)
for name in ["README.md", "AGENTS.md", "LICENSE", ".gitignore",
             "extension/extension.js", "extension/prefs.js", "extension/preferences/window.js", "docs/ARCHITECTURE.md", "docs/VERIFICATION.md"]:
    assert (ROOT / name).is_file(), f"Missing {name}"
files = sorted(p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
               and p.relative_to(ROOT).parts[0] not in {"build", "runtime", ".cache"})
for path in files:
    if path.suffix in {".js", ".json", ".md", ".css", ".py"}:
        data = path.read_text()
        assert data.endswith("\n"), f"Missing final newline: {path}"
        if path.is_relative_to(ROOT / 'extension'):
            assert not re.search(r'/(?:home|Users)/', data), f"Personal absolute path in extension: {path}"
            assert str(Path.home()) not in data, f"Developer home path embedded in extension: {path}"
        assert all(line == line.rstrip() for line in data.splitlines()), path
        if path.suffix == '.py': ast.parse(data, filename=str(path))
gjs = shutil.which("gjs")
if not gjs:
    raise SystemExit("GJS missing: ES module syntax checks cannot run")
modules = [str(p) for p in files if p.suffix == ".js"]
subprocess.run([gjs, "-m", str(ROOT / "tools/check-syntax.js"), *modules],
               cwd=ROOT, check=True, timeout=10)
print("Metadata, required files and whitespace validated.")
bundle = ROOT / "build" / (metadata["uuid"] + ".shell-extension.zip")
if bundle.exists():
    with zipfile.ZipFile(bundle) as archive:
        assert archive.testzip() is None, "Bundle CRC check failed"
        for name in archive.namelist():
            if not name.endswith("/"):
                data = archive.read(name)
                assert not data.startswith(b"\x7fELF"), f"Native executable in extension ZIP: {name}"
                assert Path(name).suffix.lower() not in {".so", ".dll", ".dylib", ".exe", ".pyc"}, name
                assert '__pycache__' not in Path(name).parts, name
        expected = {'LICENSE'}
        for source in (ROOT / "extension").rglob("*"):
            # gnome-extensions installs/compiles schemas from the bundled XML.
            # The local compiled cache exists only for development/tests.
            if source.is_file() and source.name != 'gschemas.compiled':
                name = source.relative_to(ROOT / "extension").as_posix()
                expected.add(name)
                assert archive.read(name) == source.read_bytes(), f"Stale bundle file: {name}"
        assert {name for name in archive.namelist() if not name.endswith('/')} == expected, \
            "Bundle contains files outside the extension source and license"
        assert archive.read("LICENSE") == (ROOT / "LICENSE").read_bytes()
    print("Bundle integrity, MIT license and source parity validated.")
