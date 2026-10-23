/** Read-only graph summary. Excludes identity keys, serials, tokens and audio. */
export function routingReport(graph) {
    if (!Array.isArray(graph) || graph.length > 50000 || graph.some(object =>
        !object || !Number.isSafeInteger(object.id) || typeof object.type !== 'string'))
        throw new Error('Malformed or excessive PipeWire snapshot');
    const nodes = graph.filter(object => object.type === 'PipeWire:Interface:Node');
    const indexed = new Map(graph.map(object => [object.id, object]));
    const props = object => object?.info?.props ?? {};
    const name = object => props(object)['node.name'] ?? '';
    const clean = value => String(value ?? '').replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 120);
    const output = nodes.filter(object => name(object) === 'noise_cancellation.clean_microphone');
    const captures = nodes.filter(object => /^noise_cancellation\.[A-Za-z0-9_-]+\.capture$/.test(name(object)));
    const sources = nodes.filter(object => props(object)['media.class'] === 'Audio/Source' &&
        !output.includes(object));
    const volume = object => {
        const params = object.info?.params?.Props;
        if (!Array.isArray(params)) return null;
        const result = {};
        for (const item of params.slice(0, 16)) {
            for (const key of ['mute', 'softMute'])
                if (typeof item?.[key] === 'boolean') result[key] = item[key];
            if (typeof item?.volume === 'number' && Number.isFinite(item.volume)) result.volume = item.volume;
            for (const key of ['channelVolumes', 'softVolumes'])
                if (Array.isArray(item?.[key]) && item[key].length <= 64 &&
                    item[key].every(value => typeof value === 'number' && Number.isFinite(value)))
                    result[key] = [...item[key]];
        }
        return Object.keys(result).length ? result : null;
    };
    const summary = object => ({id: object.id, label: clean(props(object)['node.description']),
        state: clean(object.info?.state ?? 'unknown'), volume: volume(object)});
    const links = graph.filter(object => object.type === 'PipeWire:Interface:Link');
    const endpoint = (link, key) => {
        const value = Number(props(link)[key]);
        return Number.isSafeInteger(value) && value >= 0 ? value : null;
    };
    const linkSummary = link => {
        const from = endpoint(link, 'link.output.node'), to = endpoint(link, 'link.input.node');
        const input = indexed.get(to), client = indexed.get(Number(props(input)['client.id']));
        return {id: link.id, from, to, state: clean(link.info?.state ?? 'unknown'),
            inputState: clean(input?.info?.state ?? 'unknown'),
            application: clean(props(input)['application.name'] ?? props(client)['application.name'])};
    };
    return {inputSources: sources.slice(0, 32).map(summary), captures: captures.slice(0, 16).map(summary),
        virtualSources: output.slice(0, 16).map(summary),
        captureLinks: links.filter(link => captures.some(capture =>
            capture.id === endpoint(link, 'link.input.node'))).slice(0, 128).map(linkSummary),
        consumerLinks: links.filter(link => output.some(source =>
            source.id === endpoint(link, 'link.output.node'))).slice(0, 128).map(linkSummary)};
}
