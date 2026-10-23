import GLib from 'gi://GLib';
import {ProcessRunner} from '../extension/audio/process.js';
import {PipeWireBackend} from '../extension/audio/backend.js';

function assert(value, message) { if (!value) throw new Error(message); }
async function rejects(promise, message) {
    let rejected = false;
    try { await promise; } catch { rejected = true; }
    assert(rejected, message);
}
const python = GLib.find_program_in_path('python3');
if (!python) throw new Error('Python 3 missing: subprocess tests cannot run');
const runner = new ProcessRunner();
const result = await runner.run([python, '-c', 'print("snapshot")']);
assert(result.stdout.trim() === 'snapshot', 'stdout transport failed');
print('ok - bounded subprocess output');
await rejects(runner.run([python, '-c', 'raise SystemExit(3)']), 'nonzero exit accepted');
print('ok - nonzero exit rejected and reaped');
await rejects(runner.run(['/nonexistent-noise-cancellation-program']), 'missing binary accepted');
print('ok - missing binary rejected');
await rejects(runner.run([python, '-c', 'import time; time.sleep(10)'], {timeoutMs: 50}),
    'deadline ignored');
print('ok - timeout terminates and reaps child');
await rejects(runner.run([python, '-c', 'print("x"*100000)'], {maxBytes: 1024}),
    'output overflow accepted');
print('ok - excessive output rejected');
const pending = runner.run([python, '-c', 'import time; time.sleep(10)']);
const shutdown = runner.dispose();
assert(shutdown && typeof shutdown.then === 'function', 'Unload must expose probe reaping');
assert(runner.dispose() === shutdown, 'Repeated probe disposal must share completion');
await rejects(pending, 'unload failed to cancel child');
await shutdown;
assert(runner._jobs.size === 0, 'Disposed runner retained child jobs');
await rejects(runner.run([python, '-c', 'print(1)']), 'disposed runner spawned child');
print('ok - unload cancels and prevents new processes');

const backend = new PipeWireBackend();
await rejects(backend.prepare({}, {}), 'production activation safety gate missing');
backend.dispose(); backend.dispose();
print('ok - missing or unvalidated Python worker cannot activate audio');

const detect = () => ({programs: {'pw-dump': '/fake/pw-dump'}, issues: [], worker: null});
for (const malformed of ['not json', '{}', '[null]', '[]']) {
    const b = new PipeWireBackend({detect,
        runner: {async run() { return {stdout: malformed}; }, dispose() {}},
    });
    const result = await b.inspect();
    assert(result.devices.length === 0 && result.issues[0].includes('Cannot inspect'),
        'malformed graph must produce a dependency error');
    b.dispose();
}
print('ok - four malformed graph variants handled');
let spawned = false;
const missing = new PipeWireBackend({
    detect: () => ({programs: {}, issues: ['PipeWire missing']}),
    runner: {async run() { spawned = true; }, dispose() {}},
});
const missingResult = await missing.inspect();
assert(!spawned && missingResult.issues.includes('PipeWire missing'),
    'missing dependency spawned a process');
missing.dispose();
print('ok - missing PipeWire skips subprocess creation');

let calls = 0;
let resolve;
const gate = new Promise(r => { resolve = r; });
const coalesced = new PipeWireBackend({detect, runner: {
    async run() { calls++; await gate; return {stdout:
        '[{"id":0,"type":"PipeWire:Interface:Core","info":{"cookie":1}}]'}; },
    dispose() {},
}});
const first = coalesced.inspect();
const second = coalesced.inspect();
resolve();
await Promise.all([first, second]);
assert(calls === 1, 'concurrent discovery must share one subprocess');
coalesced.dispose();
await rejects(coalesced.inspect(), 'disposed backend inspected devices');
print('ok - concurrent inspections coalesce and disposal is terminal');
print('13/13 process/backend cases passed');
