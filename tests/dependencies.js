import {capabilities} from '../extension/audio/backend.js';
import {runtimeLibraryAdvice} from '../extension/core/dependencies.js';

function assert(value, message) { if (!value) throw new Error(message); }
const lookups = [];
const result = capabilities({
    findProgram(name) { lookups.push(name); return `/installed/${name}`; },
    osInfo(key) { return key === 'ID' ? 'arch' : ''; },
});
assert(result.worker[0] === '/installed/python3' && result.worker.includes('-I') &&
    result.worker.some(arg => arg.endsWith('/worker/main.py')), 'Bundled worker discovery failed');
assert(!('plugin' in result) && !('filterModule' in result), 'Legacy LADSPA requirement retained');
assert(!result.issues.some(issue => /LADSPA|filter-chain/.test(issue)), 'Wrong dependency guidance');
assert(result.issues.length === 0,
    'Present executables should proceed to the bounded Python worker self-check');
assert(lookups.every(name => ['pipewire', 'pw-dump', 'wireplumber', 'gjs', 'python3']
    .includes(name)), 'Capability checks looked up an installation tool');
print('ok - bundled source worker discovery is read-only and does not require LADSPA');

const missing = capabilities({findProgram: () => null, osInfo: () => 'unknown'});
assert(missing.worker === null, 'Absent Python runtime was invented');
assert(missing.issues.some(issue => issue.includes('pw-dump is missing')), 'Missing tool not reported');
assert(missing.issues.some(issue => /manually/.test(issue)), 'Manual installation requirement missing');
assert(!missing.issues.some(issue => /Arch package/.test(issue)), 'Arch instructions shown on unknown distro');
print('ok - missing dependencies have manual, distribution-neutral errors');

for (const [id, expected] of [['arch', 'rnnoise'], ['fedora', 'rnnoise'],
    ['debian', 'librnnoise0'], ['ubuntu', 'librnnoise0']]) {
    const advice = runtimeLibraryAdvice(id);
    assert(advice.packageName === expected, `Incorrect ${id} runtime package`);
    assert(!/\b(rnnoise-devel|librnnoise-dev)\b/.test(advice.installCommand),
        'End users must not need development packages');
    if (['debian', 'ubuntu'].includes(id)) {
        assert(advice.checkCommand === 'apt-cache policy librnnoise0', 'Release availability check missing');
        assert(advice.note.includes('candidate'), 'Conditional apt guidance missing');
    }
    print(`ok - ${id} runtime guidance distinguishes package availability from installation`);
}
assert(runtimeLibraryAdvice('derivative', 'arch').packageName === 'rnnoise', 'ID_LIKE arch ignored');
assert(runtimeLibraryAdvice('derivative', 'ubuntu debian').packageName === 'librnnoise0',
    'ID_LIKE Debian family ignored');
print('ok - derivatives use known family guidance');
const unknown = runtimeLibraryAdvice('arch; untrusted-command');
assert(unknown.installCommand === null && unknown.packageName === null,
    'Unknown distribution must not produce guessed executable instructions');
print('ok - unknown distribution remains unsupported without string interpolation');
print('8/8 dependency guidance cases passed');
