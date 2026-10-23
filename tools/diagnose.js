import {capabilities, PipeWireBackend} from '../extension/audio/backend.js';
import {ProcessRunner} from '../extension/audio/process.js';
import {routingReport} from './routing-report.js';

const detected = capabilities();
if (ARGV.includes('--routing')) {
    const runner = new ProcessRunner();
    try {
        if (!detected.programs['pw-dump']) throw new Error('pw-dump is missing.');
        const {stdout} = await runner.run([detected.programs['pw-dump']]);
        print(JSON.stringify(routingReport(JSON.parse(stdout)), null, 2));
    } catch (error) {
        printerr(`Cannot inspect live routing: ${String(error.message ?? error)}`);
    } finally { runner.dispose(); }
    // This mode never launches a worker or changes the graph.
} else {
    print('Read-only diagnostic; no graph changes, recording or network requests.');
    print(JSON.stringify(detected, null, 2));
    const backend = new PipeWireBackend();
    try {
        const result = await backend.inspect();
        // Avoid printing stable hardware IDs. Device labels are local display data.
        print(JSON.stringify({devices: result.devices.map(d => ({name: d.name})),
            backends: result.backends, issues: result.issues}, null, 2));
    } finally {
        backend.dispose();
    }

    if (ARGV.includes('--devices') && detected.programs['pw-dump']) {
        const runner = new ProcessRunner();
        try {
            const {stdout} = await runner.run([detected.programs['pw-dump']]);
            const graph = JSON.parse(stdout);
            if (!Array.isArray(graph)) throw new Error('Malformed PipeWire graph');
            const objects = new Map(graph.map(object => [object.id, object]));
            const fields = ['node.description', 'node.nick', 'device.profile.description',
                'card.profile.device', 'api.alsa.pcm.name', 'api.alsa.pcm.device',
                'node.virtual', 'node.hidden', 'node.disabled'];
            const safeText = value => String(value).replace(/[\x00-\x1f\x7f]/g, '').slice(0, 180);
            print('WirePlumber connections (no process IDs or stable serials):');
            print(JSON.stringify(graph.filter(object => {
                const p = object.info?.props ?? {};
                return object.type === 'PipeWire:Interface:Client' &&
                    (p['application.process.binary'] === 'wireplumber' ||
                        String(p['application.name'] ?? '').startsWith('WirePlumber'));
            }).map(object => ({id: object.id, properties: Object.fromEntries(
                ['application.name', 'session.services', 'wireplumber.client-context']
                    .filter(key => object.info.props[key] !== undefined)
                    .map(key => [key, safeText(object.info.props[key])]))})), null, 2));
            print('Input details (no stable serials or identity keys):');
            print(JSON.stringify(graph.filter(object => object.type === 'PipeWire:Interface:Node' &&
                object.info?.props?.['media.class'] === 'Audio/Source').map(object => {
                const props = object.info.props;
                const device = objects.get(Number(props['device.id']));
                const routes = device?.info?.params?.EnumRoute ?? device?.info?.params?.Route ?? [];
                return {id: object.id, properties: Object.fromEntries(fields.filter(key =>
                    props[key] !== undefined).map(key => [key, safeText(props[key])])),
                routes: Array.isArray(routes) ? routes.filter(route => route &&
                    String(route.direction).toLowerCase() === 'input').map(route => ({
                    description: safeText(route.description ?? ''), available: safeText(route.available ?? 'unknown'),
                    device: route.device, devices: route.devices,
                })) : []};
            }), null, 2));
        } catch (error) {
            printerr(`Cannot inspect input details: ${String(error.message ?? error)}`);
        } finally { runner.dispose(); }
    }
}
