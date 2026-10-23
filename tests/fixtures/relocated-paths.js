import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

// Imported from a copied installation; never inspect/connect to the audio server.
const root = Gio.File.new_for_path(ARGV[0]);
const {capabilities} = await import(root.get_child('audio/backend.js').get_uri());
const result = capabilities({
    findProgram: name => name === 'python3' ? GLib.find_program_in_path(name) : `/available/${name}`,
    osInfo: () => 'unknown',
});
const expected = root.get_child('worker/main.py').get_path();
if (result.issues.length || result.worker?.[3] !== expected)
    throw new Error('Worker was not resolved from the relocated extension module');
print(JSON.stringify(result.worker));
