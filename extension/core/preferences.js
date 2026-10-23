// A remembered preference is not live routing authorization. New sessions still
// pin the complete current server/Node/Device incarnation before producing audio.
// Prefer a hardware serial/address; use the bus path when the device exposes no stronger anchor.
const anchors = ['device.serial', 'api.bluez5.address', 'device.bus-path'];
const text = value => typeof value === 'string' && value.length > 0 &&
    value.length <= 1024 && !/[\x00-\x1f\x7f]/.test(value);

function valid(value) {
    return value && value.v === 1 && ['alsa', 'bluez5'].includes(value.api) && text(value.node) &&
        Array.isArray(value.anchor) && value.anchor.length === 2 &&
        anchors.includes(value.anchor[0]) && text(value.anchor[1]) &&
        [value.vendor, value.product].every(item => item === null || text(item));
}

export function preferenceFor(device) {
    try {
        if (typeof device?.key !== 'string' || device.key.length > 8192) return '';
        const key = JSON.parse(device.key);
        if (!Array.isArray(key) || key.length !== 6 || !Array.isArray(key[5])) return '';
        const props = new Map(key[5]);
        const anchor = anchors.find(name => text(props.get(name)));
        if (!anchor) return '';
        const value = {v: 1, api: key[4], node: key[3], anchor: [anchor, props.get(anchor)],
            vendor: props.get('device.vendor.id') == null ? null : String(props.get('device.vendor.id')),
            product: props.get('device.product.id') == null ? null : String(props.get('device.product.id'))};
        return valid(value) ? JSON.stringify(value) : '';
    } catch { return ''; }
}

export function preferenceMatch(devices, saved) {
    try {
        if (typeof saved !== 'string' || saved.length > 8192) return {device: null, status: 'invalid'};
        const value = JSON.parse(saved);
        if (!valid(value)) return {device: null, status: 'invalid'};
        const matches = devices.filter(device => {
            const candidate = JSON.parse(preferenceFor(device) || 'null');
            return candidate && candidate.api === value.api && candidate.node === value.node &&
                candidate.anchor[0] === value.anchor[0] && candidate.anchor[1] === value.anchor[1] &&
                candidate.vendor === value.vendor && candidate.product === value.product;
        });
        // Ambiguity is never resolved by choosing the first similarly named input.
        return {device: matches.length === 1 ? matches[0] : null,
            status: matches.length === 1 ? 'matched' : matches.length ? 'ambiguous' : 'absent'};
    } catch { return {device: null, status: 'invalid'}; }
}
