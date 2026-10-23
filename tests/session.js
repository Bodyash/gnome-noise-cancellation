import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {AudioSession} from '../extension/audio/session.js';

function assert(value, message) { if (!value) throw new Error(message); }
const python = GLib.find_program_in_path('python3');
if (!python) throw new Error('Python 3 required for protocol fakes');
const key = JSON.stringify(['10', '100', '88', 'alsa_input.usb', 'alsa', [
    ['device.serial', 'USB-123'], ['device.bus-path', null], ['api.bluez5.address', null],
    ['api.alsa.path', null], ['device.vendor.id', null], ['device.product.id', null],
]]);
const authorization = {id: 42, key, epoch: '10', sourceSerial: '100'};
const pause = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms,
    () => { resolve(); return GLib.SOURCE_REMOVE; }));
function create(scenario, faults = [], activities = [], processing = {}, options = {}) {
    return new AudioSession(['/fake/python3', '-B', '-I', '/bundled/worker/main.py'], authorization,
        {processing, cancelled: () => false, onFault: message => faults.push(message),
            onActivity: state => activities.push(state)}, {
            startupTimeoutMs: 300, heartbeatMs: 20, heartbeatTimeoutMs: 200,
            stopTimeoutMs: 75,
            spawn: () => Gio.Subprocess.new([python, 'tests/fixtures/fake-helper.py', scenario],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE),
            ...options,
        });
}
let cases = 0;
function trackedSession(faults = [], activities = []) {
    const signals = [];
    const session = create('ignore-stop', faults, activities, {}, {spawn: () => {
        const child = Gio.Subprocess.new([python, 'tests/fixtures/fake-helper.py', 'ignore-stop'],
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        return {get_stdin_pipe: () => child.get_stdin_pipe(),
            get_stdout_pipe: () => child.get_stdout_pipe(), get_stderr_pipe: () => child.get_stderr_pipe(),
            wait_async: (...args) => child.wait_async(...args),
            send_signal(sig) { signals.push(sig); child.send_signal(sig); },
            force_exit() { signals.push('force'); child.force_exit(); }};
    }});
    return {session, signals};
}
{
    const faults = [], activities = [];
    const {session: s, signals} = trackedSession(faults, activities);
    try {
        await s.verify(); await s.publish();
        const reaped = s.stop({immediate: true});
        assert(JSON.stringify(signals) === '["force"]', 'Unload must force termination without a grace timer');
        assert(!s._timer && !s._killTimer, 'Unload left a Shell timer');
        assert(s.stop({immediate: true}) === reaped && signals.length === 1, 'Immediate stop is not idempotent');
        s._message({event: 'activity'}); // Already queued IPC must be ignored, even if malformed.
        await reaped;
        assert(s.exited && !faults.length && !activities.length, 'Stopped worker emitted state or survived');
        print('ok - immediate unload kills through Gio and cancels timers/late messages'); cases++;
    } finally { await s.stop({immediate: true}); }
}
{
    const {session: s, signals} = trackedSession();
    try {
        await s.verify(); await s.publish();
        const stopping = s.stop();
        assert(s._killTimer, 'Normal toggle must retain bounded graceful cleanup');
        const unloaded = s.stop({immediate: true});
        assert(unloaded === stopping && !s._killTimer && JSON.stringify(signals) === '[15,"force"]',
            'Unload failed to escalate an already stopping worker');
        await unloaded; assert(s.exited, 'Escalated worker was not reaped');
        print('ok - unload escalates an in-progress graceful stop immediately'); cases++;
    } finally { await s.stop({immediate: true}); }
}
{
    const {session: s, signals} = trackedSession();
    try {
        const stopped = s.stop({immediate: true});
        let rejected = false;
        try { await s.verify(); } catch { rejected = true; }
        await stopped;
        assert(rejected && s.exited && JSON.stringify(signals) === '["force"]',
            'Unload during initial worker startup did not cancel and reap');
        print('ok - immediate unload cancels worker startup before readiness'); cases++;
    } finally { await s.stop({immediate: true}); }
}

{
    const faults = [], activities = [];
    const s = create('idle-cycle', faults, activities);
    try {
        await s.verify(); await s.publish(); await pause(100);
        assert(JSON.stringify(activities) === JSON.stringify(['idle', 'resuming', 'processing']),
            `Missing idle activity sequence: ${activities}`);
        assert(!faults.length, 'Healthy idle cycle faulted');
        print('ok - verified idle/resume activity travels over scoped IPC'); cases++;
    } finally { await s.stop(); }
}
{
    const faults = [];
    const s = create('invalid-activity', faults);
    try {
        await s.verify(); try { await s.publish(); } catch { /* fault may race publication */ }
        await pause(50);
        assert(faults.length === 1, 'A different virtual-source activity was accepted');
        print('ok - activity from an unowned source fails closed'); cases++;
    } finally { await s.stop(); }
}
{
    const faults = [];
    const s = create('happy', faults);
    try {
        const prepared = await s.verify();
        assert(prepared.processing && !prepared.published && prepared.key === key, 'Invalid prepared proof');
        const active = await s.publish();
        assert(active.published && active.epoch === '10', 'Invalid active proof');
        await pause(50);
        assert(faults.length === 0, 'Healthy session faulted');
        await s.stop(); await s.stop();
        assert(s.exited, 'stop must reap the helper');
        print('ok - valid handshake, readiness, publication, heartbeat and idempotent cleanup'); cases++;
    } finally { await s.stop(); }
}
for (const backend of ['speex', 'webrtc']) {
    const faults = [];
    const s = create('happy', faults, [], {backend, outputGainDb: -6});
    try {
        await s.verify(); await s.publish();
        assert(faults.length === 0, 'Selected engine configuration proof was rejected');
        print(`ok - explicit ${backend} settings are echoed and verified`); cases++;
    } finally { await s.stop(); }
}
for (const scenario of ['wrong-source', 'wrong-session', 'wrong-rate', 'unprocessed',
    'early-active', 'malformed', 'oversized', 'stall', 'wrong-config', 'missing-config']) {
    const faults = [];
    const s = create(scenario, faults);
    try {
        let rejected = false;
        try { await s.verify(); } catch { rejected = true; }
        assert(rejected && faults.length === 1, `${scenario} did not fail closed`);
        await s.stop(); assert(s.exited, `${scenario} left a child`);
        print(`ok - ${scenario} rejects readiness, reports once and reaps child`); cases++;
    } finally { await s.stop(); }
}
for (const scenario of ['fault', 'crash']) {
    const faults = [];
    const s = create(scenario, faults);
    try {
        await s.verify();
        try { await s.publish(); } catch { /* Fault may race the publication reply. */ }
        await pause(80);
        assert(faults.length === 1, `${scenario} was not reported exactly once`);
        await s.stop(); assert(s.exited, 'Faulted helper not reaped');
        print(`ok - active ${scenario} invalidates and reaps owned process`); cases++;
    } finally { await s.stop(); }
}
{
    const s = create('ignore-stop');
    try {
        await s.verify(); await s.publish();
        await s.stop(); assert(s.exited, 'Unresponsive child was not killed and reaped');
        print('ok - bounded graceful shutdown escalates only the owned child'); cases++;
    } finally { await s.stop(); }
}
print(`${cases}/${cases} audio-session protocol cases passed`);
