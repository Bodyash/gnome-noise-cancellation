import {routingReport} from '../tools/routing-report.js';
const assert = (value, message) => { if (!value) throw new Error(message); };
const node = (id, name, klass, extra = {}, params = {}) => ({id, type: 'PipeWire:Interface:Node',
    info: {state: 'running', props: {'node.name': name, 'media.class': klass,
        'node.description': 'Test microphone', 'device.serial': 'PRIVATE-SERIAL',
        'noise-cancellation.session': 'PRIVATE-TOKEN', ...extra}, params}});
const graph = [node(42, 'alsa_input.private', 'Audio/Source', {}, {Props: [{mute: false, channelVolumes: [1, 1]}]}),
    node(71, 'noise_cancellation.PRIVATE-TOKEN.capture', 'Stream/Input/Audio'),
    node(90, 'noise_cancellation.clean_microphone', 'Audio/Source', {}, {Props: [{mute: true, channelVolumes: [0]}]}),
    node(91, 'obs-input', 'Stream/Input/Audio', {'application.name': 'OBS Studio'}),
    {id: 80, type: 'PipeWire:Interface:Link', info: {state: 'active',
        props: {'link.output.node': 42, 'link.input.node': 71}}},
    {id: 92, type: 'PipeWire:Interface:Link', info: {state: 'active',
        props: {'link.output.node': 90, 'link.input.node': 91}}}];
const report = routingReport(graph);
assert(report.inputSources.length === 1 && report.virtualSources.length === 1, 'Source classification failed');
assert(report.captures.length === 1 && report.captureLinks[0].from === 42, 'Physical capture link missing');
assert(report.virtualSources[0].volume.mute === true && report.virtualSources[0].volume.channelVolumes[0] === 0,
    'Muted/zero virtual source must be visible');
assert(report.consumerLinks[0].application === 'OBS Studio', 'OBS consumer link missing');
assert(!JSON.stringify(report).includes('PRIVATE-'), 'Diagnostic leaked stable identity or session token');
const absent = routingReport([node(42, 'alsa_input.private', 'Audio/Source')]);
assert(absent.virtualSources.length === 0 && absent.inputSources[0].volume === null,
    'Missing virtual source or unknown volume must not be invented');
for (const value of [null, {}, [null]]) {
    let rejected = false;
    try { routingReport(value); } catch { rejected = true; }
    assert(rejected, 'Malformed graph was accepted');
}
print('9/9 read-only routing report cases passed');
