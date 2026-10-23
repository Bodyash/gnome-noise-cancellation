/** Immutable per-session settings shared by UI and backend; Python revalidates. */
export const ENGINE_NAMES = Object.freeze({rnnoise: 'RNNoise', speex: 'SpeexDSP', webrtc: 'WebRTC'});
export const DEFAULT_PROCESSING = Object.freeze({
    backend: 'rnnoise', outputGainDb: 0, speexAttenuation: -25, speexAgc: false,
    speexTarget: 8000, webrtcLevel: 2, webrtcAgc: false, webrtcHighPass: false, webrtcTarget: 3,
});
// The allowlist and bounds are repeated in Python at the subprocess trust boundary.
export function processingSettings(value = '{}') {
    const parsed = typeof value === 'string' ? JSON.parse(value) : value;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) ||
        Object.keys(parsed).some(key => !Object.hasOwn(DEFAULT_PROCESSING, key)))
        throw new Error('Unknown processing settings');
    const config = {...DEFAULT_PROCESSING, ...parsed};
    if (typeof config.backend !== 'string' || !Object.hasOwn(ENGINE_NAMES, config.backend)) throw new Error('Unknown processing engine');
    for (const key of ['speexAgc', 'webrtcAgc', 'webrtcHighPass'])
        if (typeof config[key] !== 'boolean') throw new Error(`Invalid ${key}`);
    for (const [key, low, high] of [['outputGainDb', -12, 12], ['speexAttenuation', -60, -5],
        ['speexTarget', 1000, 16000], ['webrtcLevel', 0, 3], ['webrtcTarget', 0, 31]]) {
        if (!Number.isInteger(config[key]) || config[key] < low || config[key] > high)
            throw new Error(`Invalid ${key}`);
    }
    return Object.freeze(config);
}

/** Shared validation for the Shell and preferences runtime-only checks. */
export function checkedEngines(report) {
    if (report?.ready === false)
        throw new Error(String(report.message ?? 'Audio runtime dependencies are incomplete').slice(0, 800));
    if (report?.protocol !== 1 || report.ready !== true || report.worker !== 'python' ||
        report.rate !== 48000 || report.channels !== 1 || report.frameSize !== 480)
        throw new Error('Unsupported audio worker protocol or processing format');
    if (!Array.isArray(report.backends) || report.backends.length !== 3 ||
        report.backends.some(item => !item || typeof item.id !== 'string' || !Object.hasOwn(ENGINE_NAMES, item.id) ||
            typeof item.ready !== 'boolean' || (item.message !== undefined && typeof item.message !== 'string')) ||
        new Set(report.backends.map(item => item.id)).size !== 3)
        throw new Error('Unsupported processing engine availability response');
    return report.backends.map(item => ({id: item.id, ready: item.ready,
        ...(item.message === undefined ? {} : {message: item.message.slice(0, 400)})}));
}
