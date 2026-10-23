import GLib from 'gi://GLib';
import {processingSettings, ENGINE_NAMES, checkedEngines} from '../core/settings.js';
import {physicalSources} from '../core/devices.js';
import {runtimeLibraryAdvice} from '../core/dependencies.js';
import {ProcessRunner} from './process.js';
import {AudioSession} from './session.js';

export const WORKER_UNAVAILABLE = 'Python 3 or the bundled audio worker is missing. ' +
    'Noise processing is unavailable; microphone discovery can still work. Install Python 3 ' +
    'and the documented runtime dependencies manually, then refresh. No custom native helper is required.';

// Resolve from the installed module; -B avoids bytecode files and -I ignores user Python paths.
function workerCommand(python) {
    const directory = GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]);
    const script = GLib.canonicalize_filename('../worker/main.py', directory);
    return python && GLib.file_test(script, GLib.FileTest.IS_REGULAR)
        ? [python, '-B', '-I', script] : null;
}

export function capabilities({findProgram = name => GLib.find_program_in_path(name),
    osInfo = key => GLib.get_os_info(key)} = {}) {
    const programs = {};
    const issues = [];
    for (const [name, purpose] of [
        ['pipewire', 'audio processing'],
        ['pw-dump', 'microphone discovery'],
        ['wireplumber', 'session management'],
        ['gjs', 'extension JavaScript'],
        ['python3', 'the bundled audio worker'],
    ]) {
        programs[name] = findProgram(name);
        if (!programs[name]) issues.push(`${name} is missing: ${purpose} is unavailable. ` +
            'Install/configure the appropriate distribution package manually; see the setup guide.');
    }
    const worker = workerCommand(programs.python3);
    if (!worker) issues.push(WORKER_UNAVAILABLE);
    const id = osInfo('ID') ?? '';
    const idLike = osInfo('ID_LIKE') ?? '';
    return {programs, worker,
        runtimeLibraries: Object.fromEntries(Object.keys(ENGINE_NAMES).map(engine =>
            [engine, runtimeLibraryAdvice(id, idLike, engine)])), issues};
}

/** Source-only audio adapter. Discovery is read-only; explicit prepare creates a session. */
export class PipeWireBackend {
    constructor({runner = new ProcessRunner(), detect = capabilities, previousShutdown = null,
        createSession = (command, authorization, context) => new AudioSession(command, authorization, context)} = {}) {
        this._runner = runner;
        this._previousShutdown = previousShutdown;
        this._detect = detect;
        this._disposed = false;
        this._inspection = null;
        this._createSession = createSession;
        this._validatedWorker = null;
        this._snapshot = null;
        this._sessions = new Set();
        this.processing = processingSettings();
        this._configGeneration = 0;
        this._reads = new Map();
    }

    configure(value) {
        if (this._disposed || this._sessions.size) throw new Error('Disable processing before changing settings');
        this.processing = processingSettings(value);
        ++this._configGeneration;
        this._inspection = null;
        this._validatedWorker = null;
    }

    async inspect() {
        if (this._disposed) throw new Error('Audio backend disposed');
        if (!this._inspection) {
            const pending = this._inspect();
            this._inspection = pending;
            try { return await pending; }
            finally { if (this._inspection === pending) this._inspection = null; }
        }
        return this._inspection;
    }

    async _read(name, argv, limits) {
        // Option clicks can supersede discovery, but must not spawn more checks.
        // These probes are independent of options and never authorize a session.
        const key = JSON.stringify([name, argv]);
        let pending = this._reads.get(key);
        if (!pending) {
            pending = this._runner.run(argv, limits);
            this._reads.set(key, pending);
        }
        try { return await pending; }
        finally { if (this._reads.get(key) === pending) this._reads.delete(key); }
    }

    async _inspect() {
        const generation = this._configGeneration;
        const processing = this.processing;
        const detected = this._detect();
        const issues = [...detected.issues];
        let backends = [];
        this._validatedWorker = null;
        this._snapshot = null;
        // Self-checks exercise runtime APIs only; they do not establish live routing readiness.
        if (detected.worker) {
            try {
                const {stdout} = await this._read('runtime', [...detected.worker, '--check-all'],
                    {timeoutMs: 3000, maxBytes: 16384});
                const check = JSON.parse(stdout);
                const checked = checkedEngines(check);
                if (generation !== this._configGeneration || this._disposed) throw new Error('Processing settings changed during discovery');
                backends = checked;
                const selected = backends.find(item => item.id === processing.backend);
                if (!selected.ready) throw new Error(`${ENGINE_NAMES[processing.backend]} unavailable: ${selected.message ?? 'Runtime check failed'}`);
                this._validatedWorker = [...detected.worker];
            } catch (error) {
                issues.push(`Audio worker self-check failed: ${String(error.message ?? error)} ` +
                    'Verify runtime library compatibility manually. ' + (detected.runtimeLibraries?.[processing.backend]?.note ?? ''));
            }
        } else if (!issues.includes(WORKER_UNAVAILABLE)) issues.push(WORKER_UNAVAILABLE);
        if (!detected.programs['pw-dump'])
            return {backends, devices: [], epoch: 'unavailable', issues};
        try {
            const {stdout} = await this._read('graph', [detected.programs['pw-dump']]);
            const snapshot = physicalSources(JSON.parse(stdout));
            if (generation === this._configGeneration && !this._disposed) {
                this._snapshot = snapshot;
                if (issues.length) this._validatedWorker = null;
            }
            return {...snapshot, issues, backends};
        } catch (error) {
            return {backends, devices: [], epoch: 'unavailable', issues: [
                `Cannot inspect PipeWire: ${String(error.message ?? error)} ` +
                    'Check access to the user PipeWire socket and running services manually.',
                ...issues,
            ]};
        }
    }

    async prepare(authorization, context) {
        // A rapid extension re-enable must wait for the previous lifecycle to reap its children.
        if (this._previousShutdown) {
            try { await this._previousShutdown; }
            catch { throw new Error('Previous audio worker termination was not confirmed. Reload the desktop session before enabling.'); }
        }
        if (this._disposed || !this._validatedWorker)
            throw new Error('The bundled Python audio worker must pass its runtime self-check before activation.');
        if (context.cancelled()) throw new Error('Audio startup cancelled');
        if (this._sessions.size) throw new Error('The previous audio session has not finished stopping');
        if (!authorization || !this._snapshot || authorization.epoch !== this._snapshot.epoch ||
            !this._snapshot.devices.some(d => d.key === authorization.key && d.id === authorization.id))
            throw new Error('The explicitly selected physical microphone is no longer current');
        // Register ownership before returning, so unload also catches an in-flight prepare.
        const session = this._createSession(this._validatedWorker, authorization,
            {...context, processing: this.processing});
        this._sessions.add(session);
        session.whenStopped.then(() => this._sessions.delete(session), () => this._sessions.delete(session));
        return session;
    }

    dispose() {
        if (this._disposed) return this._disposal;
        this._disposed = true;
        this._validatedWorker = this._snapshot = null;
        const tasks = [];
        try { tasks.push(this._runner.dispose()); }
        catch (error) { tasks.push(Promise.reject(error)); }
        for (const session of this._sessions) {
            try { tasks.push(session.stop({immediate: true})); }
            catch (error) { tasks.push(Promise.reject(error)); }
        }
        this._disposal = Promise.all(tasks).then(() => undefined);
        this._disposal.catch(() => {}); // Callers retain the actual failure for re-enable gating.
        return this._disposal;
    }
}
