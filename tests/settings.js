import {processingSettings, DEFAULT_PROCESSING} from '../extension/core/settings.js';
function assert(value, message) { if (!value) throw new Error(message); }
assert(processingSettings('{}').backend === 'rnnoise', 'Default engine changed');
for (const backend of ['rnnoise', 'speex', 'webrtc'])
    assert(processingSettings(JSON.stringify({backend})).backend === backend, 'Engine selection lost');
for (const value of ['[]', 'null', '{"backend":"raw"}', '{"webrtcLevel":4}',
    '{"speexAgc":1}', '{"backend":["rnnoise"]}', '{"echoCancel":true}', '{"speexAttenuation":0}']) {
    let rejected = false;
    try { processingSettings(value); } catch { rejected = true; }
    assert(rejected, `Unsafe settings accepted: ${value}`);
}
assert(Object.isFrozen(DEFAULT_PROCESSING), 'Defaults must be immutable');
print('13/13 processing settings cases passed');
if (ARGV.includes('--json')) print(JSON.stringify(processingSettings()));
