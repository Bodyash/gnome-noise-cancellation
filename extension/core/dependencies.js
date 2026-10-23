/** Display-only instructions. Never pass these strings to a subprocess. */
export function runtimeLibraryAdvice(id, idLike = '', engine = 'rnnoise') {
    const family = [id, ...idLike.split(/\s+/)];
    if (engine !== 'rnnoise') {
        const packages = {
            speex: {arch: 'speexdsp', fedora: 'speexdsp', debian: 'libspeexdsp1'},
            webrtc: {arch: 'gst-plugins-bad', fedora: 'gstreamer1-plugins-bad-free', debian: 'gstreamer1.0-plugins-bad'},
        }[engine];
        const distro = family.includes('arch') ? 'arch' : family.includes('fedora') ? 'fedora' :
            family.some(value => ['debian', 'ubuntu'].includes(value)) ? 'debian' : null;
        if (!packages || !distro) return {packageName: null, installCommand: null, checkCommand: null,
            note: 'See the dependency guide for this engine and distribution.'};
        const packageName = packages[distro];
        return {packageName, installCommand: `${{arch: 'sudo pacman -S', fedora: 'sudo dnf install',
            debian: 'sudo apt install'}[distro]} ${packageName}`,
        checkCommand: distro === 'debian' ? `apt-cache policy ${packageName}` : null,
        note: `Install ${packageName} manually if your release provides it; refresh and select this engine. ` +
            'Other installed engines remain independently selectable. No automatic fallback.'};
    }
    if (family.includes('arch')) return {
        packageName: 'rnnoise', installCommand: 'sudo pacman -S rnnoise',
        checkCommand: null, note: 'Official Arch runtime package; development files are included.',
    };
    if (family.includes('fedora')) return {
        packageName: 'rnnoise', installCommand: 'sudo dnf install rnnoise',
        checkCommand: null, note: 'Official Fedora runtime package; rnnoise-devel is only for builders.',
    };
    if (family.includes('debian') || family.includes('ubuntu')) return {
        packageName: 'librnnoise0', installCommand: 'sudo apt install librnnoise0',
        checkCommand: 'apt-cache policy librnnoise0',
        note: 'Install only if your release provides a candidate. Select another installed engine ' +
            'when RNNoise is absent; do not add testing/unstable repositories.',
    };
    return {packageName: null, installCommand: null, checkCommand: null,
        note: 'No verified package command for this distribution. Follow the dependency guide manually.'};
}
