import Gio from 'gi://Gio';

// SpiderMonkey's parse-only API: no module evaluation and no desktop activation.
for (const path of ARGV) {
    const [ok, bytes] = Gio.File.new_for_path(path).load_contents(null);
    if (!ok) throw new Error(`Cannot read ${path}`);
    Reflect.parse(new TextDecoder().decode(bytes), {target: 'module'});
}
print(`Parsed ${ARGV.length} ES modules without executing them.`);
