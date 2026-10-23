import {Controller, State} from '../extension/core/controller.js';
import {ProcessingBinding} from '../extension/core/processingBinding.js';
import {processingSettings} from '../extension/core/settings.js';
import {tileSubtitle} from '../extension/core/presentation.js';

function assert(value, message) { if (!value) throw new Error(message); }
const mic = {id: 42, key: 'authorized', name: 'Test microphone', epoch: '10'};
function fixture() {
    let saved = '{}';
    let closing;
    const backend = {
        starts: 0, owned: 0, peakOwned: 0, config: null,
        configure(value) { this.config = value; },
        async inspect() { return {devices: [mic], epoch: '10', issues: []}; },
        async prepare(a) {
            this.starts++;
            this.owned++;
            this.peakOwned = Math.max(this.peakOwned, this.owned);
            let stopped = false;
            return {async verify() { if (backend.gate) await backend.gate; return {key: a.key, epoch: a.epoch, processing: true, published: false}; },
                async publish() { return {key: a.key, epoch: a.epoch, processing: true, published: true}; },
                async stop() {
                    if (stopped) throw new Error('Session stopped twice');
                    stopped = true;
                    await closing;
                    backend.owned--;
                }};
        }, dispose() {},
    };
    const controller = new Controller(backend);
    const errors = [];
    const binding = new ProcessingBinding(controller, () => saved, error => errors.push(error));
    return {controller, backend, binding, errors,
        save(value) { saved = typeof value === 'string' ? value : JSON.stringify(value); binding.sync(); },
        closing(value) { closing = value; }};
}
let count = 0;
{
    const f = fixture();
    f.save({backend: 'speex', speexAgc: true});
    assert(f.controller.processing.backend === 'speex' && f.backend.starts === 0,
        'External settings must apply without starting processing');
    f.binding.dispose(); f.controller.dispose(); count++;
}
{
    const f = fixture();
    await f.controller.refresh(); f.controller.select(mic.key); await f.controller.enable();
    f.save({backend: 'speex'}); f.save({backend: 'webrtc', webrtcLevel: 3});
    assert(f.controller.state === State.ACTIVE && f.controller.processing.backend === 'rnnoise',
        'Preferences changed a live processing session');
    let release;
    f.closing(new Promise(resolve => { release = resolve; }));
    const stopping = f.controller.disable();
    assert(f.controller.state === State.STOPPING && f.controller.processing.backend === 'rnnoise',
        'Preferences applied before audio cleanup');
    release(); await stopping;
    assert(f.controller.processing.backend === 'webrtc' && f.controller.processing.webrtcLevel === 3,
        'Latest pending preferences were not applied after cleanup');
    assert(f.backend.starts === 1 && f.controller.state === State.DISABLED,
        'Applying preferences automatically re-enabled processing');
    f.binding.dispose(); f.controller.dispose(); count++;
}
{
    const f = fixture();
    await f.controller.refresh(); f.controller.select(mic.key); await f.controller.enable();
    f.controller.invalidate('Microphone unplugged');
    f.save({backend: 'speex'}); await f.controller.enable();
    assert(!f.controller.selection && f.backend.starts === 1 &&
        f.controller.state === State.RECONFIGURATION_REQUIRED,
    'New preferences restored a revoked microphone authorization');
    f.binding.dispose(); f.controller.dispose(); count++;
}
{
    const f = fixture();
    f.save('{broken'); f.binding.sync();
    assert(f.errors.length === 1 && f.controller.processing.backend === 'rnnoise',
        'Invalid settings must be rejected with bounded diagnostic reporting');
    f.save({backend: 'speex'});
    assert(f.controller.processing.backend === 'speex', 'Valid settings did not recover after invalid data');
    f.binding.dispose(); f.save({backend: 'webrtc'});
    assert(f.controller.processing.backend === 'speex', 'Disposed binding still applied preferences');
    f.controller.dispose(); count++;
}
{
    const f = fixture();
    await f.controller.refresh(); f.controller.select(mic.key);
    let release;
    f.backend.gate = new Promise(resolve => { release = resolve; });
    const starting = f.controller.enable();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    f.save({backend: 'webrtc'});
    assert(f.controller.state === State.STARTING && f.controller.processing.backend === 'rnnoise',
        'Settings changed an in-flight startup');
    const stopping = f.controller.disable(); release();
    await Promise.all([starting, stopping]);
    assert(f.controller.state === State.DISABLED && f.backend.starts === 1 &&
        f.controller.processing.backend === 'webrtc', 'Cancelled startup reapplied old settings or restarted audio');
    f.binding.dispose(); f.controller.dispose(); count++;
}
{
    const f = fixture();
    await f.controller.refresh(); f.controller.select(mic.key);
    for (let cycle = 0; cycle < 6; cycle++) {
        let ready, reaped;
        f.backend.gate = new Promise(resolve => { ready = resolve; });
        const starting = f.controller.enable();
        for (let i = 0; i < 10; i++) await Promise.resolve();
        const engine = f.controller.processing.backend;
        for (let i = 0; i < 30; i++) {
            f.save({backend: ['rnnoise', 'speex', 'webrtc'][i % 3]});
            await f.controller.enable();
        }
        assert(f.backend.owned === 1 && f.backend.starts === cycle + 1 &&
            f.controller.processing.backend === engine, 'Rapid edits/enables changed or duplicated startup');
        ready(); await starting;
        assert(tileSubtitle(f.controller.snapshot()).startsWith(
            {rnnoise: 'RNNoise', speex: 'SpeexDSP', webrtc: 'WebRTC'}[engine]),
        'Tile displayed pending preferences instead of the live engine');
        f.closing(new Promise(resolve => { reaped = resolve; }));
        const stopping = f.controller.disable();
        for (let i = 0; i < 30; i++) {
            f.save({backend: ['rnnoise', 'webrtc', 'speex'][(i + cycle) % 3]});
            void f.controller.disable();
            await f.controller.enable();
        }
        assert(f.controller.state === State.STOPPING && f.backend.owned === 1 &&
            f.backend.starts === cycle + 1 && f.controller.processing.backend === engine,
        'Rapid clicks overlapped workers or changed a stopping session');
        reaped(); await stopping;
        assert(f.backend.owned === 0 && f.controller.state === State.DISABLED &&
            f.controller.processing.backend === ['rnnoise', 'webrtc', 'speex'][(29 + cycle) % 3],
        'Cleanup lost the latest options or automatically restarted');
    }
    assert(f.backend.peakOwned === 1, 'More than one worker owned audio');
    f.binding.dispose(); f.controller.dispose(); count++;
}
assert(Object.isFrozen(processingSettings()), 'Processing snapshot lost immutability');
print(`${count}/${count} cross-process preferences binding cases passed`);
