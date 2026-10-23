import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {PreferencesView} from '../extension/preferences/window.js';
import {processingSettings} from '../extension/core/settings.js';

function assert(value, message) { if (!value) throw new Error(message); }
Gtk.init(); Adw.init();
const source = Gio.SettingsSchemaSource.new_from_directory(
    GLib.build_filenamev([GLib.get_current_dir(), 'extension', 'schemas']), null, false);
const settings = new Gio.Settings({settings_schema: source.lookup('org.gnome.shell.extensions.noise-cancellation', false)});
let probes = 0, disposed = 0;
const view = new PreferencesView(settings, {checkOnOpen: false,
    detect: () => ({worker: ['/fake/python3', '-B', '-I', '/bundled/worker/main.py'], issues: [],
        runtimeLibraries: {speex: {installCommand: 'sudo pacman -S speexdsp'}}}),
    runner: {async run(argv) {
        probes++;
        assert(argv.at(-1) === '--check-all', 'Preferences must never launch an audio session or graph discovery');
        return {stdout: JSON.stringify({protocol: 1, worker: 'python', ready: true, rate: 48000,
            channels: 1, frameSize: 480, backends: [{id: 'rnnoise', ready: true},
                {id: 'speex', ready: false, message: 'SpeexDSP missing'}, {id: 'webrtc', ready: true}]})};
    }, dispose() { disposed++; }},
});
const window = new Adw.PreferencesWindow();
window.add(view.page);
let cases = 0;
try {
    assert(view.page instanceof Adw.PreferencesPage && view.rows.size === 9 && probes === 0,
        'Native preferences widgets failed construction or started audio'); cases++;
    const backend = view.rows.get('backend').row;
    backend.selected = 1;
    assert(processingSettings(settings.get_string('processing-settings')).backend === 'speex', 'Backend choice was not saved');
    assert(view._groups.get('speex').visible && !view._groups.get('webrtc').visible, 'Engine-specific controls did not switch');
    assert(!view.rows.get('speexTarget').row.sensitive, 'AGC target must be disabled until AGC is enabled'); cases++;
    view.rows.get('speexAgc').row.active = true;
    view.rows.get('speexAttenuation').row.value = -40;
    assert(processingSettings(settings.get_string('processing-settings')).speexAgc &&
        processingSettings(settings.get_string('processing-settings')).speexAttenuation === -40,
    'Repeated native controls did not save values');
    assert(view.rows.get('speexTarget').row.sensitive, 'AGC target did not become usable'); cases++;
    backend.selected = 2;
    view.rows.get('webrtcLevel').row.selected = 3;
    view.rows.get('webrtcHighPass').row.active = true;
    const saved = processingSettings(settings.get_string('processing-settings'));
    assert(saved.backend === 'webrtc' && saved.webrtcLevel === 3 && saved.webrtcHighPass && saved.speexAgc,
        'Changing engines lost the other engine preferences'); cases++;
    await view.refresh();
    assert(probes === 1 && view.status.title === 'Backend ready', 'Independent dependency status failed');
    backend.selected = 1;
    assert(view.status.title === 'Backend unavailable' && view.status.subtitle.includes('speexdsp'),
        'Missing selected engine must show manual guidance without fallback'); cases++;
    settings.set_string('processing-settings', JSON.stringify({backend: 'rnnoise', outputGainDb: -6}));
    assert(backend.selected === 0 && view.rows.get('outputGainDb').row.value === -6,
        'External preference changes were not reflected'); cases++;
    settings.set_string('processing-settings', '{broken');
    assert(view.notice.visible, 'Malformed saved preferences must show a bounded error');
    view.rows.get('outputGainDb').row.value = -3;
    assert(!view.notice.visible && processingSettings(settings.get_string('processing-settings')).outputGainDb === -3,
        'Editing should replace malformed saved preferences safely'); cases++;
    view.dispose(); view.dispose();
    assert(disposed === 1, 'Preferences cleanup must be idempotent');
    settings.set_string('processing-settings', JSON.stringify({backend: 'webrtc'}));
    assert(view.rows.get('backend').row.selected === 0, 'Closed preferences retained a settings callback'); cases++;
} finally { view.dispose(); window.destroy(); }
print(`${cases}/${cases} native GTK preferences cases passed (isolated display, memory settings, fake runtime probe)`);
