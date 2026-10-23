import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import {preferenceFor, preferenceMatch} from '../extension/core/preferences.js';
import {processingSettings} from '../extension/core/settings.js';
import {Controller, State} from '../extension/core/controller.js';

const tests = [];
const test = (name, fn) => tests.push({name, fn});
const assert = (value, message = 'Assertion failed') => { if (!value) throw new Error(message); };
function device({epoch = '10', id = 42, serial = '100', hardware = 'USB-123',
    name = 'USB mic', node = 'alsa_input.usb', path = 'pci-1-usb-2'} = {}) {
    return {id, epoch, name, key: JSON.stringify([epoch, serial, '88', node, 'alsa', [
        ['device.serial', hardware], ['device.bus-path', path], ['api.bluez5.address', null],
        ['api.alsa.path', 'hw:0'], ['device.vendor.id', '1234'], ['device.product.id', '5678']]])};
}
function backend(devices) {
    return {devices, prepared: 0, epoch: devices[0]?.epoch ?? '10',
        async inspect() { return {devices: this.devices, epoch: this.epoch, issues: []}; },
        async prepare(authorization) {
            this.prepared++;
            const proof = published => ({key: authorization.key, epoch: authorization.epoch,
                processing: true, published});
            return {async verify() { return proof(false); },
                async publish() { return proof(true); }, stop() {}};
        }, dispose() {}};
}

test('preference excludes graph IDs and survives reboot and USB port changes', () => {
    const saved = preferenceFor(device());
    const current = device({epoch: '20', id: 90, serial: '900', path: 'pci-2-usb-4'});
    assert(preferenceMatch([current], saved).device === current);
    assert(!saved.includes('object.serial') && !saved.includes('epoch'));
});
test('name alone, another endpoint and replacement hardware never match', () => {
    const saved = preferenceFor(device());
    for (const candidate of [device({hardware: 'replacement'}), device({node: 'alsa_input.headset'})])
        assert(preferenceMatch([candidate], saved).device === null);
});
test('ambiguous, malformed and absent preferences do not choose an input', () => {
    const saved = preferenceFor(device());
    assert(preferenceMatch([device(), device({id: 43, serial: '101'})], saved).device === null);
    assert(preferenceMatch([], saved).device === null);
    for (const bad of ['', '{}', 'null', '[]', 'x'.repeat(8193)])
        assert(preferenceMatch([device()], bad).device === null);
    assert(preferenceFor({key: 'malformed'}) === '');
});
test('built-in microphone uses bus path when no hardware serial is present', () => {
    const original = device({hardware: null});
    const saved = preferenceFor(original);
    assert(preferenceMatch([device({hardware: null, epoch: '20'})], saved).device);
    assert(preferenceMatch([device({hardware: null, path: 'another-pci-device'})], saved).device === null);
});
test('restored preference stays disabled until explicit enable', async () => {
    const current = device({epoch: '20', serial: '200'});
    const b = backend([current]);
    const c = new Controller(b, {preferred: preferenceFor(device())});
    await c.refresh();
    assert(c.state === State.DISABLED && c.selection.key === current.key);
    assert(b.prepared === 0);
    await c.enable(); assert(c.state === State.ACTIVE && b.prepared === 1);
    c.dispose();
});
test('unrelated discovery preserves selection; unplug revokes authorization but keeps preference', async () => {
    const current = device();
    const writes = [];
    const b = backend([current]);
    const c = new Controller(b, {onPreference: value => writes.push(value)});
    await c.refresh(); c.select(current.key);
    assert(writes.length === 1 && writes[0]);
    await c.enable();
    b.devices = [current, device({id: 43, hardware: 'other', node: 'bluez_input.other'})];
    await c.refresh(); assert(c.state === State.ACTIVE);
    b.devices = []; await c.refresh();
    assert(c.state === State.RECONFIGURATION_REQUIRED && writes.at(-1) === preferenceFor(current));
    b.devices = [current]; await c.refresh(); await c.enable();
    assert(!c.selection && b.prepared === 1);
    c.dispose();
});
test('startup discovery waits for a late device within a bounded grace period', async () => {
    let clock = 0;
    const b = backend([]);
    const c = new Controller(b, {preferred: preferenceFor(device()), now: () => clock});
    await c.refresh();
    assert(c.snapshot().restorePending && !c.selection);
    clock = 1000; b.devices = [device()]; await c.refresh();
    assert(c.selection && c.state === State.DISABLED && b.prepared === 0);
    c.dispose();
});
test('an absent preference cannot silently reselect after the startup grace period', async () => {
    let clock = 0;
    const b = backend([]);
    const c = new Controller(b, {preferred: preferenceFor(device()), now: () => clock});
    await c.refresh();
    clock = 11000;
    b.devices = [device()]; await c.refresh();
    assert(!c.selection && c.state === State.SETUP_REQUIRED);
    c.dispose();
});
test('processing/shutdown faults keep preference for a disabled selection next login', async () => {
    const current = device();
    let saved = '';
    const c = new Controller(backend([current]), {onPreference: value => { saved = value; }});
    await c.refresh(); c.select(current.key); await c.enable();
    c.invalidate('Audio worker exited unexpectedly.');
    assert(!c.selection && saved === preferenceFor(current));
    await c.refresh(); await c.enable();
    assert(c.state === State.RECONFIGURATION_REQUIRED && !c.selection);
    c.dispose();
    const next = device({epoch: '20', serial: '500', id: 90});
    const b = backend([next]);
    const afterLogin = new Controller(b, {preferred: saved});
    await afterLogin.refresh();
    assert(afterLogin.selection?.id === 90 && afterLogin.state === State.DISABLED && b.prepared === 0);
    afterLogin.dispose();
});
test('a fault during startup discovery cancels all pending preference restoration', async () => {
    const b = backend([]);
    const c = new Controller(b, {preferred: preferenceFor(device()), now: () => 0});
    await c.refresh(); c.invalidate('Routing authorization lost.');
    b.devices = [device()]; await c.refresh();
    assert(!c.selection && !c.snapshot().restorePending);
    c.dispose();
});
test('ambiguous startup matching never later chooses the remaining candidate', async () => {
    const b = backend([device(), device({id: 43, serial: '200'})]);
    const c = new Controller(b, {preferred: preferenceFor(device()), now: () => 0});
    await c.refresh();
    assert(!c.selection && !c.snapshot().restorePending);
    b.devices = [device()]; await c.refresh();
    assert(!c.selection);
    c.dispose();
});
test('the last fault remains available after selection and a new controller lifecycle', async () => {
    let savedError = '';
    const current = device();
    const b = backend([current]);
    const c = new Controller(b, {onFailure: message => { savedError = message; }});
    await c.refresh(); c.select(current.key); await c.enable();
    c.invalidate('Microphone resume timed out.');
    assert(savedError.includes('resume timed out'));
    c.select(current.key);
    assert(c.snapshot().lastError === savedError);
    c.dispose();
    const next = new Controller(backend([]), {lastError: savedError});
    assert(next.snapshot().lastError === savedError);
    next.dispose();
});
test('preference storage failure cannot interrupt audio cleanup', async () => {
    const current = device();
    const c = new Controller(backend([current]), {onPreference() { throw new Error('storage unavailable'); }});
    await c.refresh(); assert(c.select(current.key)); await c.enable();
    c.invalidate('Microphone disappeared');
    assert(c.state === State.RECONFIGURATION_REQUIRED && !c.selection);
    c.dispose();
});
test('bundled GSettings schema round-trips preferences in an isolated memory backend', () => {
    GLib.setenv('GSETTINGS_BACKEND', 'memory', true);
    const source = Gio.SettingsSchemaSource.new_from_directory(
        GLib.build_filenamev([GLib.get_current_dir(), 'extension', 'schemas']),
        Gio.SettingsSchemaSource.get_default(), false);
    const schema = source.lookup('org.gnome.shell.extensions.noise-cancellation', false);
    assert(schema);
    const settings = new Gio.Settings({settings_schema: schema});
    const processing = processingSettings({backend: 'speex', speexAgc: true});
    settings.set_string('processing-settings', JSON.stringify(processing));
    assert(processingSettings(settings.get_string('processing-settings')).speexAgc);
    assert(processingSettings(settings.get_string('processing-settings')).backend === 'speex');
    assert(settings.get_string('preferred-microphone') === '');
    const saved = preferenceFor(device());
    assert(settings.set_string('preferred-microphone', saved));
    assert(settings.get_string('preferred-microphone') === saved);
    settings.reset('preferred-microphone');
    settings.set_string('last-error', 'Example idle timeout');
    assert(settings.get_string('last-error') === 'Example idle timeout');
    settings.reset('last-error');
});

let passed = 0;
for (const {name, fn} of tests) {
    try { await fn(); print(`ok - ${name}`); passed++; }
    catch (error) { printerr(`FAIL - ${name}: ${error.stack ?? error}`); }
}
print(`${passed}/${tests.length} preference cases passed`);
if (passed !== tests.length) throw new Error('Preference tests failed');
