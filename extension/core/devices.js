const NODE = 'PipeWire:Interface:Node';
const DEVICE = 'PipeWire:Interface:Device';
const serial = value => /^(0|[1-9][0-9]*)$/.test(String(value)) &&
    Number.isSafeInteger(Number(value));
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 1024;
const truth = value => value === true || value === 'true' || value === 1;
const label = value => String(value ?? '').replace(/[\x00-\x1f\x7f]/g, '').slice(0, 160);

function inputRoutes(device, props) {
    if (!serial(props['card.profile.device'])) return [];
    const profile = Number(props['card.profile.device']);
    const params = device.info?.params ?? {};
    const routes = Array.isArray(params.EnumRoute) ? params.EnumRoute : params.Route;
    if (!Array.isArray(routes)) return [];
    return routes.filter(route => route && String(route.direction).toLowerCase() === 'input' &&
        (Number(route.device ?? -1) === profile ||
            (Array.isArray(route.devices) && route.devices.some(id => Number(id) === profile))));
}

function sourceName(props, routes) {
    const base = label(props['node.description'] ?? props['node.name']);
    const detail = [props['node.nick'], props['device.profile.description'],
        routes.find(route => String(route.available).toLowerCase() === 'yes')?.description,
        props['api.alsa.pcm.name']].find(value => text(value) &&
            !base.toLowerCase().includes(label(value).toLowerCase()));
    return detail ? `${base.slice(0, 100)} — ${label(detail).slice(0, 60)}` : base;
}

/** Conservative ALSA/BlueZ physical input allowlist. Never infer identity from name alone. */
export function physicalSources(graph) {
    if (!Array.isArray(graph) || graph.length > 50000 ||
        graph.some(o => !o || !Number.isSafeInteger(o.id) || typeof o.type !== 'string'))
        throw new Error('Malformed PipeWire snapshot');
    const objects = new Map(graph.map(object => [object.id, object]));
    if (objects.size !== graph.length)
        throw new Error('Duplicate PipeWire object IDs');
    const core = graph.find(o => o.type === 'PipeWire:Interface:Core');
    if (!core || !serial(core.info?.cookie))
        throw new Error('PipeWire server identity is unavailable');
    // The server cookie distinguishes this graph from one recreated after a PipeWire restart.
    const epoch = String(core.info.cookie);
    const devices = [];
    for (const node of graph.filter(o => o.type === NODE)) {
        const p = node.info?.props ?? {};
        if (p['media.class'] !== 'Audio/Source' || truth(p['node.virtual']) ||
            truth(p['node.hidden']) || truth(p['node.disabled']) ||
            truth(p['stream.monitor']) || !text(p['node.name']) ||
            /monitor|clean_microphone|noise_cancellation/i.test(p['node.name']) ||
            !serial(p['object.serial']) || !serial(p['device.id'])) continue;
        const candidate = objects.get(Number(p['device.id']));
        const device = candidate?.type === DEVICE ? candidate : null;
        const d = device?.info?.props ?? {};
        if (!['alsa', 'bluez5'].includes(d['device.api']) || !serial(d['object.serial'])) continue;
        const routes = inputRoutes(device, p);
        // Do not guess availability from a duplicate label or from missing metadata.
        if (routes.length && routes.every(route => String(route.available).toLowerCase() === 'no')) continue;
        const stable = ['device.serial', 'device.bus-path', 'api.bluez5.address',
            'api.alsa.path', 'device.vendor.id', 'device.product.id']
            .map(key => [key, d[key] ?? p[key] ?? null]);
        // A card index alone is not stable physical identity.
        if (!stable.slice(0, 3).some(([, value]) => text(value))) continue;
        // Live authorization combines graph incarnations with hardware properties; labels are display-only.
        const key = JSON.stringify([epoch, String(p['object.serial']),
            String(d['object.serial']), p['node.name'], d['device.api'], stable]);
        devices.push(Object.freeze({id: node.id, key, epoch,
            sourceSerial: String(p['object.serial']),
            name: sourceName(p, routes)}));
    }
    // Duplicate identities are uncertain, even if object IDs differ.
    const counts = new Map();
    for (const d of devices) counts.set(d.key, (counts.get(d.key) ?? 0) + 1);
    const eligible = devices.filter(d => counts.get(d.key) === 1);
    const names = new Map();
    for (const device of eligible) names.set(device.name, (names.get(device.name) ?? 0) + 1);
    return {epoch, devices: eligible.map(device => names.get(device.name) > 1
        ? Object.freeze({...device, name: `${device.name.slice(0, 135)} — Input ${device.id}`}) : device)};
}
