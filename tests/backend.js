import {PipeWireBackend} from '../extension/audio/backend.js';

function assert(value, message) { if (!value) throw new Error(message); }
const graph = [{id: 0, type: 'PipeWire:Interface:Core', info: {cookie: 10}},
    {id: 30, type: 'PipeWire:Interface:Device', info: {props: {
        'device.api': 'alsa', 'device.serial': 'USB-123', 'object.serial': 88,
    }}}, {id: 42, type: 'PipeWire:Interface:Node', info: {props: {
        'media.class': 'Audio/Source', 'device.id': 30, 'object.serial': 100,
        'node.name': 'alsa_input.usb', 'node.description': 'Test mic',
    }}}];
const check = {protocol: 1, backend: 'rnnoise', rate: 48000, channels: 1, frameSize: 480, ready: true, worker: 'python', backends: [
    {id: 'rnnoise', ready: true}, {id: 'speex', ready: true}, {id: 'webrtc', ready: true}]};
function fixture(overrides = {}) {
    const calls = [];
    let release;
    const stopped = new Promise(resolve => { release = resolve; });
    let created = 0;
    const stops = [];
    const backend = new PipeWireBackend({
        previousShutdown: overrides.previousShutdown,
        detect: () => ({programs: {'pw-dump': '/fake/pw-dump'}, worker: ['/fake/python3', '-B', '-I', '/bundled/worker/main.py'], issues: [],
            runtimeLibraries: {rnnoise: {note: 'Install manually.'}}}),
        runner: {async run(argv) {
            calls.push(argv);
            return {stdout: JSON.stringify(argv.at(-1) === '--check-all' ? (overrides.check ?? check) : graph)};
        }, dispose() {}},
        createSession: () => {
            created++;
            return {whenStopped: stopped, stop(options) { stops.push(options); release(); return stopped; }};
        },
    });
    return {backend, calls, stops, created: () => created};
}
{
    const {backend, calls, created} = fixture();
    try {
        const inspection = await backend.inspect();
        assert(inspection.devices.length === 1 && inspection.issues.length === 0,
            'Valid Python worker self-check and graph should permit configuration');
        assert(calls.some(args => args.at(-1) === '--check-all'), 'Native self-check was skipped');
        const session = await backend.prepare(inspection.devices[0], {cancelled: () => false, onFault() {}});
        assert(created() === 1, 'Backend did not create a Python worker session');
        await session.stop();
        print('ok - readiness uses a bounded Python worker self-check and fresh physical identity');
    } finally { await backend.dispose(); }
}
for (const patch of [{protocol: 999}, {ready: false}, {rate: 44100}, {frameSize: 960}]) {
    const {backend, created} = fixture({check: {...check, ...patch}});
    try {
        const inspection = await backend.inspect();
        assert(inspection.issues.length > 0, 'Unsupported Python worker self-check was accepted');
        let rejected = false;
        try { await backend.prepare(inspection.devices[0], {cancelled: () => false}); }
        catch { rejected = true; }
        assert(rejected && created() === 0, 'Incompatible helper created a session');
    } finally { await backend.dispose(); }
}
print('ok - four incompatible library/protocol responses cannot activate audio');
{
    const {backend, created} = fixture();
    try {
        const inspection = await backend.inspect();
        let rejected = false;
        try { await backend.prepare({...inspection.devices[0], id: 99}, {cancelled: () => false}); }
        catch { rejected = true; }
        assert(rejected && created() === 0, 'Stale source ID was accepted');
        rejected = false;
        try { await backend.prepare(inspection.devices[0], {cancelled: () => true}); }
        catch { rejected = true; }
        assert(rejected && created() === 0, 'Cancelled startup spawned a session');
    } finally { await backend.dispose(); }
}
print('ok - stale selection and cancellation cannot spawn a helper');

{
    const {backend} = fixture({check: {...check, backends: [
        {id: 'rnnoise', ready: false, message: 'RNNoise missing'},
        {id: 'speex', ready: true}, {id: 'webrtc', ready: false, message: 'Plugin missing'}]}});
    const missing = await backend.inspect();
    assert(missing.issues.length > 0, 'Missing explicitly selected engine must be unavailable');
    backend.configure({backend: 'speex'});
    const available = await backend.inspect();
    assert(available.issues.length === 0, 'Missing RNNoise must not block installed SpeexDSP');
    assert(available.backends.length === 3, 'All engine availability must be exposed');
    const session = await backend.prepare(available.devices[0], {cancelled: () => false});
    let rejected = false;
    try { backend.configure({backend: 'webrtc'}); } catch { rejected = true; }
    assert(rejected, 'An enabled engine must not change in place');
    await session.stop(); await backend.dispose();
    print('ok - independent engine availability, explicit selection, no automatic fallback or live changes');
}


{
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let checks = 0;
    const backend = new PipeWireBackend({
        detect: () => ({programs: {'pw-dump': '/fake/pw-dump'}, worker: ['/fake/python3'], issues: []}),
        runner: {async run(argv) {
            if (argv.at(-1) === '--check-all') { checks++; await gate; return {stdout: JSON.stringify(check)}; }
            return {stdout: JSON.stringify(graph)};
        }, dispose() {}},
    });
    const first = backend.inspect();
    backend.configure({backend: 'speex'});
    const second = backend.inspect();
    backend.configure({backend: 'webrtc'});
    const third = backend.inspect();
    release();
    await Promise.all([first, second, third]);
    assert(checks === 1, 'Rapid settings changes must share the bounded in-flight runtime check');
    assert(backend.processing.backend === 'webrtc' && backend._validatedWorker,
        'Stale checks must not erase the newest readiness');
    await backend.dispose();
    print('ok - rapid settings changes coalesce checks and stale discovery cannot erase current readiness');
}
{
    const f = fixture();
    const result = await f.backend.inspect();
    await f.backend.prepare(result.devices[0], {cancelled: () => false});
    const disposed = f.backend.dispose();
    assert(f.stops.length === 1 && f.stops[0]?.immediate === true,
        'Backend disposal must synchronously request immediate worker termination');
    assert(f.backend.dispose() === disposed, 'Repeated backend disposal lost its reap promise');
    await disposed;
    print('ok - backend unload immediately terminates owned sessions and preserves the reap promise');
}
{
    let release;
    const f = fixture({previousShutdown: new Promise(resolve => { release = resolve; })});
    const result = await f.backend.inspect();
    const next = f.backend.prepare(result.devices[0], {cancelled: () => false});
    for (let i = 0; i < 5; i++) await Promise.resolve();
    assert(f.created() === 0, 'Re-enabled extension overlapped a previous worker');
    release(); const session = await next;
    assert(f.created() === 1, 'Explicit startup did not proceed after confirmed reaping');
    await session.stop(); await f.backend.dispose();
    print('ok - re-enable waits for the previous extension lifecycle to finish reaping');
}
{
    let release;
    const f = fixture({previousShutdown: new Promise(resolve => { release = resolve; })});
    const result = await f.backend.inspect();
    const next = f.backend.prepare(result.devices[0], {cancelled: () => false});
    await f.backend.dispose(); release();
    let rejected = false;
    try { await next; } catch { rejected = true; }
    assert(rejected && f.created() === 0, 'Disposal while waiting started a stale worker');
    print('ok - unloading a re-enabled extension cancels startup waiting on old cleanup');
}
{
    const previousShutdown = Promise.reject(new Error('Previous cleanup failed'));
    previousShutdown.catch(() => {});
    const f = fixture({previousShutdown});
    const result = await f.backend.inspect();
    let rejected = false;
    try { await f.backend.prepare(result.devices[0], {cancelled: () => false}); } catch { rejected = true; }
    assert(rejected && f.created() === 0, 'Failed previous cleanup authorized a new worker');
    await f.backend.dispose();
    print('ok - unconfirmed previous termination blocks a new extension session');
}
print('13/13 Python worker backend cases passed');
