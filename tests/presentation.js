import {tileSubtitle} from '../extension/core/presentation.js';
import {processingSettings} from '../extension/core/settings.js';

let count = 0;
function check(backend, state, activity, expected) {
    const snapshot = {processing: processingSettings({backend}), state, activity};
    if (tileSubtitle(snapshot) !== expected) throw new Error(`Wrong tile: ${tileSubtitle(snapshot)}`);
    count++;
}
check('rnnoise', 'ACTIVE', 'processing', 'RNNoise · Active');
check('speex', 'ACTIVE', 'processing', 'SpeexDSP · Active');
check('webrtc', 'ACTIVE', 'processing', 'WebRTC · Active');
check('speex', 'ACTIVE', 'idle', 'SpeexDSP · Idle');
check('speex', 'ACTIVE', 'suspending', 'SpeexDSP · Suspending');
check('rnnoise', 'ACTIVE', 'resuming', 'RNNoise · Resuming');
for (const [state, label] of Object.entries({DISABLED: 'Disabled', STARTING: 'Starting',
    STOPPING: 'Stopping', SETUP_REQUIRED: 'Setup required', UNAVAILABLE: 'Unavailable',
    ERROR: 'Error', RECONFIGURATION_REQUIRED: 'Select microphone again'}))
    check('webrtc', state, null, `WebRTC · ${label}`);
const translated = tileSubtitle({state: 'ACTIVE', activity: 'idle', processing: {backend: 'speex'}},
    value => `translated ${value}`);
if (translated !== 'SpeexDSP · translated Idle') throw new Error('Status must remain translatable');
count++;
print(`${count}/${count} tile presentation cases passed`);
