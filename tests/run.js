import {Controller, State} from '../extension/core/controller.js';
import {physicalSources} from '../extension/core/devices.js';

const tests = [];
function test(name, fn) { tests.push({name, fn}); }
function assert(value, message = 'Assertion failed') {
    if (!value) throw new Error(message);
}
function equal(actual, expected) {
    assert(JSON.stringify(actual) === JSON.stringify(expected),
        `Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}
function deferred() {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return {promise, resolve};
}
const mic = Object.freeze({id: 42, key: 'usb-microphone-incarnation-1',
    name: 'USB microphone', epoch: 'server-1'});
function fixture() {
    const backend = {
        devices: [mic], epoch: mic.epoch, dependencies: [], stopped: 0,
        prepared: 0, published: 0, ready: true, fault: null,
        async inspect() {
            return {devices: this.devices, epoch: this.epoch, issues: this.dependencies};
        },
        async prepare(authorization, {onFault, cancelled}) {
            this.prepared++;
            this.fault = onFault;
            const self = this;
            let stopped = false;
            return {
                async verify() {
                    if (self.gate) await self.gate.promise;
                    return {key: authorization.key, epoch: authorization.epoch,
                        processing: self.ready, published: false};
                },
                async publish() {
                    if (stopped || cancelled()) throw new Error('Cancelled');
                    self.published++;
                    return {key: authorization.key, epoch: authorization.epoch,
                        processing: self.ready, published: true};
                },
                stop(options) {
                    self.stopOptions = options;
                    if (!stopped) self.stopped++;
                    stopped = true;
                },
            };
        },
        dispose() { this.disposed = true; },
    };
    return {backend, controller: new Controller(backend)};
}
async function configured() {
    const f = fixture();
    await f.controller.refresh();
    f.controller.select(mic.key);
    return f;
}

test('enable verifies processing before publishing and disabling cleans once', async () => {
    const {controller: c, backend: b} = await configured();
    equal(c.state, State.DISABLED);
    await c.enable();
    equal(c.state, State.ACTIVE);
    equal(b.published, 1);
    c.disable(); c.disable();
    equal(c.state, State.DISABLED);
    equal(b.stopped, 1);
    assert(b.stopOptions?.handoff, 'Normal active tile-off must request the default-input handoff');
});
test('faults and extension unload never request a default-input handoff', async () => {
    const {controller: c, backend: b} = await configured();
    await c.enable(); c.invalidate('Mic lost');
    assert(!b.stopOptions?.handoff, 'Fault cleanup must not change the system input');
    const other = await configured();
    await other.controller.enable(); await other.controller.dispose();
    assert(other.backend.stopOptions?.immediate && !other.backend.stopOptions?.handoff,
        'Extension unload must terminate immediately without default changes');
});
for (const missing of ['PipeWire is missing', 'RNNoise runtime is missing']) {
    test(missing, async () => {
        const {controller: c, backend: b} = fixture();
        b.dependencies = [missing];
        await c.refresh(); await c.enable();
        equal(c.state, State.UNAVAILABLE);
        assert(c.message.includes(missing));
        equal(b.prepared, 0);
    });
}
test('no microphone and invalid selection are safe', async () => {
    const {controller: c, backend: b} = fixture();
    b.devices = []; await c.refresh();
    equal(c.state, State.SETUP_REQUIRED);
    assert(!c.select('invented'));
    await c.enable(); equal(b.prepared, 0);
});
test('initialization failure never activates', async () => {
    const {controller: c, backend: b} = await configured();
    b.prepare = async () => { throw new Error('Cannot create graph'); };
    await c.enable(); equal(c.state, State.ERROR);
    equal(b.published, 0);
});
test('unverified pipeline is stopped without publication', async () => {
    const {controller: c, backend: b} = await configured();
    b.ready = false; await c.enable();
    equal(c.state, State.ERROR);
    equal(b.published, 0); equal(b.stopped, 1);
});
for (const change of ['unplug', 'replacement', 'server-restart']) {
    test(`${change} invalidates identity and replug requires fresh selection`, async () => {
        const {controller: c, backend: b} = await configured();
        await c.enable();
        if (change === 'unplug') b.devices = [];
        if (change === 'replacement') b.devices = [{...mic, key: 'new-incarnation'}];
        if (change === 'server-restart') b.epoch = 'server-2';
        await c.refresh();
        equal(c.state, State.RECONFIGURATION_REQUIRED);
        equal(c.selection, null); equal(b.stopped, 1);
        b.devices = [mic]; b.epoch = mic.epoch;
        await c.refresh(); await c.enable();
        c.disable(); await c.enable();
        equal(c.state, State.RECONFIGURATION_REQUIRED);
        equal(b.published, 1);
        assert(c.select(mic.key));
        await c.enable(); equal(c.state, State.ACTIVE);
    });
}
for (const reason of ['Unexpected link removal', 'PipeWire disconnected',
    'WirePlumber restarted', 'Controller crashed', 'Routing is uncertain']) {
    test(`${reason} fails closed`, async () => {
        const {controller: c, backend: b} = await configured();
        await c.enable(); b.fault(reason);
        equal(c.state, State.RECONFIGURATION_REQUIRED);
        equal(b.stopped, 1); equal(c.selection, null);
        await c.enable(); equal(b.published, 1);
    });
}
test('repeated enable coalesces and repeated cycles clean up', async () => {
    const {controller: c, backend: b} = await configured();
    for (let i = 0; i < 5; i++) {
        await Promise.all([c.enable(), c.enable()]);
        equal(c.state, State.ACTIVE); c.disable();
    }
    equal(b.prepared, 5); equal(b.stopped, 5);
});
test('idle activity preserves enabled authorization and late activity cannot restore a stopped session', async () => {
    const {controller: c, backend: b} = await configured();
    const prepare = b.prepare.bind(b);
    let activity;
    b.prepare = (authorization, context) => {
        activity = context.onActivity;
        return prepare(authorization, context);
    };
    await c.enable(); activity('idle');
    equal(c.state, State.ACTIVE); equal(c.activity, 'idle');
    assert(c.message.includes('Waiting'));
    activity('resuming'); equal(c.activity, 'resuming');
    activity('processing'); equal(c.activity, 'processing');
    c.invalidate('Physical microphone disappeared');
    activity('processing');
    equal(c.state, State.RECONFIGURATION_REQUIRED); equal(c.activity, null);
    equal(b.published, 1);
});
test('startup cancellation rejects stale verification callback', async () => {
    const {controller: c, backend: b} = await configured();
    b.gate = deferred();
    const start = c.enable();
    // Let inspection and prepare finish before cancelling verification.
    for (let i = 0; i < 10; i++) await Promise.resolve();
    c.disable(); b.gate.resolve(); await start;
    equal(c.state, State.DISABLED); equal(b.published, 0); equal(b.stopped, 1);
});
test('late prepare result is disposed after cancellation', async () => {
    const {controller: c, backend: b} = await configured();
    const gate = deferred(); const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        await gate.promise; return session;
    };
    const start = c.enable();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    c.disable(); gate.resolve(); await start;
    equal(b.stopped, 1); equal(b.published, 0);
});
test('old session faults cannot invalidate a new session', async () => {
    const {controller: c, backend: b} = await configured();
    await c.enable(); const oldFault = b.fault;
    c.disable(); await c.enable(); oldFault('old exit');
    equal(c.state, State.ACTIVE);
});
test('unplug during startup cannot publish after replug', async () => {
    const {controller: c, backend: b} = await configured();
    b.gate = deferred(); const start = c.enable();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    b.devices = []; await c.refresh();
    b.devices = [mic]; await c.refresh();
    b.gate.resolve(); await start; await c.enable();
    equal(c.state, State.RECONFIGURATION_REQUIRED); equal(b.published, 0);
});
test('extension unload cancels startup and cleans backend', async () => {
    const {controller: c, backend: b} = await configured();
    b.gate = deferred(); const start = c.enable();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    c.dispose(); c.dispose(); b.gate.resolve(); await start;
    equal(b.published, 0); equal(b.stopped, 1); assert(b.disposed);
    assert(b.stopOptions?.immediate, 'Controller unload requested a graceful worker stop');
    await c.enable(); equal(b.prepared, 1);
});
test('extension disposal retains and awaits backend shutdown across repeated calls', async () => {
    const {controller: c, backend: b} = fixture();
    const reaped = deferred();
    let calls = 0;
    b.dispose = () => { calls++; return reaped.promise; };
    const stopped = c.dispose();
    assert(stopped && typeof stopped.then === 'function', 'Disposal must expose completion of backend cleanup');
    assert(c.dispose() === stopped && calls === 1, 'Repeated disposal lost the shared cleanup result');
    let completed = false;
    stopped.then(() => { completed = true; });
    await Promise.resolve(); assert(!completed, 'Disposal resolved before backend cleanup');
    reaped.resolve(); await stopped; assert(completed);
    await c.enable(); equal(b.prepared, 0);
});
test('malformed discovery fails closed while active', async () => {
    const {controller: c, backend: b} = await configured();
    await c.enable(); b.inspect = async () => { throw new Error('Malformed JSON'); };
    await c.refresh(); equal(c.state, State.RECONFIGURATION_REQUIRED);
    equal(b.stopped, 1);
});
test('wrong routing proof and early publication are rejected', async () => {
    for (const patch of [{key: 'other'}, {epoch: 'other'}, {published: true}]) {
        const {controller: c, backend: b} = await configured();
        const original = b.prepare.bind(b);
        b.prepare = async (...args) => {
            const session = await original(...args);
            const verify = session.verify.bind(session);
            session.verify = async () => ({...await verify(), ...patch});
            return session;
        };
        await c.enable(); equal(c.state, State.ERROR); equal(b.published, 0);
    }
});

function graph() {
    return [{id: 0, type: 'PipeWire:Interface:Core', info: {cookie: 10}},
        {id: 30, type: 'PipeWire:Interface:Device', info: {props: {
            'device.api': 'alsa', 'object.serial': 88, 'device.serial': 'USB-123',
        }}},
        {id: 42, type: 'PipeWire:Interface:Node', info: {props: {
            'media.class': 'Audio/Source', 'device.id': 30,
            'object.serial': 100, 'node.name': 'alsa_input.usb',
            'node.description': 'USB mic',
        }}}];
}
test('physical discovery requires device identity and excludes virtual and monitors', () => {
    const g = graph(); equal(physicalSources(g).devices.length, 1);
    const virtual = JSON.parse(JSON.stringify(g[2]));
    virtual.id = 43; virtual.info.props['node.virtual'] = true;
    g.push(virtual); equal(physicalSources(g).devices.length, 1);
    g[2].info.props['node.name'] = 'sink.monitor';
    equal(physicalSources(g).devices.length, 0);
});
test('microphone labels distinguish nodes on the same sound card without merging identities', () => {
    const g = graph();
    g[2].info.props['node.description'] = '500 Series Chipset Family';
    g[2].info.props['node.nick'] = 'Digital Microphone';
    const jack = JSON.parse(JSON.stringify(g[2]));
    jack.id = 43;
    Object.assign(jack.info.props, {'object.serial': 101, 'node.name': 'alsa_input.headset',
        'node.nick': 'Headset Microphone'});
    g.push(jack);
    const devices = physicalSources(g).devices;
    equal(devices.length, 2);
    assert(devices[0].name.includes('Digital Microphone'));
    assert(devices[1].name.includes('Headset Microphone'));
    assert(devices[0].key !== devices[1].key);
});
test('identical friendly labels still identify each distinct input', () => {
    const g = graph();
    const second = JSON.parse(JSON.stringify(g[2]));
    second.id = 43;
    Object.assign(second.info.props, {'object.serial': 101, 'node.name': 'alsa_input.second'});
    g.push(second);
    const devices = physicalSources(g).devices;
    equal(devices.length, 2);
    assert(devices[0].name !== devices[1].name);
});
test('confirmed unavailable routes and hidden inputs are excluded; unknown availability is retained', () => {
    const g = graph();
    g[2].info.props['card.profile.device'] = 0;
    g[1].info.params = {EnumRoute: [{direction: 'Input', devices: [0], available: 'no'}]};
    equal(physicalSources(g).devices.length, 0);
    g[1].info.params.EnumRoute[0].available = 'unknown';
    equal(physicalSources(g).devices.length, 1);
    g[2].info.props['node.hidden'] = true;
    equal(physicalSources(g).devices.length, 0);
});
test('identity changes with serial, stable properties, node incarnation or epoch', () => {
    const first = physicalSources(graph()).devices[0].key;
    for (const mutate of [
        g => { g[1].info.props['device.serial'] = 'different'; },
        g => { g[2].info.props['object.serial'] = 101; },
        g => { g[1].info.props['object.serial'] = 89; },
        g => { g[0].info.cookie = 11; },
    ]) {
        const g = graph(); mutate(g);
        assert(physicalSources(g).devices[0].key !== first);
    }
});
test('ambiguous identities, missing device and malformed snapshots are rejected', () => {
    const g = graph(); delete g[1].info.props['device.serial'];
    equal(physicalSources(g).devices.length, 0);
    equal(physicalSources(graph().filter(o => o.id !== 30)).devices.length, 0);
    for (const value of [null, {}, [null], []]) {
        let failed = false;
        try { physicalSources(value); } catch { failed = true; }
        assert(failed);
    }
});
test('late discovery cannot replace a newer snapshot', async () => {
    const {controller: c, backend: b} = fixture();
    const gate = deferred(); let calls = 0;
    b.inspect = async () => {
        if (++calls === 1) return gate.promise;
        return {devices: [], epoch: mic.epoch, issues: []};
    };
    const old = c.refresh(); await c.refresh();
    gate.resolve({devices: [mic], epoch: mic.epoch, issues: []});
    await old; equal(c.devices, []); equal(c.state, State.SETUP_REQUIRED);
});
test('fault during prepare disposes the late session without publication', async () => {
    const {controller: c, backend: b} = await configured();
    const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        b.fault('Device changed during creation');
        return session;
    };
    await c.enable(); equal(c.state, State.RECONFIGURATION_REQUIRED);
    equal(b.stopped, 1); equal(b.published, 0);
});
test('invalid post-publication evidence causes immediate cleanup', async () => {
    const {controller: c, backend: b} = await configured();
    const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        session.publish = async () => ({key: 'wrong', processing: true, published: true});
        return session;
    };
    await c.enable(); equal(c.state, State.ERROR); equal(b.stopped, 1);
});
test('cleanup failures block all subsequent authorization and enable attempts', async () => {
    const {controller: c, backend: b} = await configured();
    const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        session.stop = () => { throw new Error('Cannot revoke output'); };
        return session;
    };
    await c.enable(); c.disable();
    equal(c.state, State.RECONFIGURATION_REQUIRED);
    assert(!c.select(mic.key)); await c.enable(); equal(b.prepared, 1);
    assert(c.message.includes('cleanup failed'));
});
test('view errors cannot prevent cleanup and selection cannot change an active session', async () => {
    const {controller: c, backend: b} = await configured();
    const unsubscribe = c.subscribe(() => {}); unsubscribe();
    // subscribe delivers an initial snapshot synchronously; subsequent notifications are isolated.
    let initial = true;
    c.subscribe(() => { if (!initial) throw new Error('View destroyed'); initial = false; });
    await c.enable(); assert(!c.select(mic.key)); c.disable(); equal(b.stopped, 1);
});
test('asynchronous helper cleanup holds STOPPING and blocks new authorization', async () => {
    const {controller: c, backend: b} = await configured();
    const gate = deferred(); const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        const stop = session.stop.bind(session);
        session.stop = () => { stop(); return gate.promise; };
        return session;
    };
    await c.enable();
    const stopping = c.disable();
    equal(c.state, State.STOPPING);
    assert(!c.select(mic.key)); await c.enable(); equal(b.prepared, 1);
    gate.resolve(); await stopping;
    equal(c.state, State.DISABLED);
});
test('rejected asynchronous cleanup fails closed without an unhandled rejection', async () => {
    const {controller: c, backend: b} = await configured();
    const original = b.prepare.bind(b);
    b.prepare = async (...args) => {
        const session = await original(...args);
        session.stop = async () => { throw new Error('Helper did not exit'); };
        return session;
    };
    await c.enable(); await c.disable();
    equal(c.state, State.RECONFIGURATION_REQUIRED);
    assert(!c.select(mic.key)); await c.enable(); equal(b.prepared, 1);
});
test('disable waits for a pending prepare to return and release its resources', async () => {
    const {controller: c, backend: b} = await configured();
    const gate = deferred(); const original = b.prepare.bind(b);
    b.prepare = async (...args) => { const session = await original(...args); await gate.promise; return session; };
    const start = c.enable();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    const stopping = c.disable(); equal(c.state, State.STOPPING);
    gate.resolve(); await Promise.all([start, stopping]);
    equal(c.state, State.DISABLED); equal(b.stopped, 1); equal(b.published, 0);
});
test('processing configuration never activates audio or changes live authorization', async () => {
    const {controller: c, backend: b} = await configured();
    b.configure = value => { b.processing = value; };
    assert(c.configure({backend: 'speex', speexAgc: true}));
    equal(c.snapshot().processing.backend, 'speex'); equal(b.prepared, 0);
    await c.enable(); equal(c.state, State.ACTIVE);
    assert(c.message.includes('SpeexDSP'));
    assert(!c.configure({backend: 'webrtc'}));
    c.invalidate('Selected mic lost');
    assert(c.configure({backend: 'webrtc'}));
    await c.refresh(); await c.enable();
    equal(c.state, State.RECONFIGURATION_REQUIRED); equal(b.published, 1);
    equal(c.selection, null);
});
test('processing configuration errors are contained and not persisted', async () => {
    const {controller: c, backend: b} = await configured();
    b.configure = () => { throw new Error('Still stopping'); };
    assert(!c.configure({backend: 'webrtc'}));
    equal(c.snapshot().processing.backend, 'rnnoise');
    assert(!c.configure({backend: 'raw'}));
    equal(b.prepared, 0);
});
let failed = 0;
for (const {name, fn} of tests) {
    try { await fn(); print(`ok - ${name}`); }
    catch (e) { failed++; printerr(`FAIL - ${name}: ${e.stack ?? e}`); }
}
print(`${tests.length - failed}/${tests.length} tests passed`);
if (failed) throw new Error(`${failed} tests failed`);
