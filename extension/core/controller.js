import {preferenceFor, preferenceMatch} from './preferences.js';
import {processingSettings, ENGINE_NAMES} from './settings.js';

export const State = Object.freeze(Object.fromEntries([
    'UNAVAILABLE', 'DISABLED', 'SETUP_REQUIRED', 'STARTING', 'ACTIVE', 'STOPPING',
    'RECONFIGURATION_REQUIRED', 'ERROR',
].map(state => [state, state])));

// State changes outside this table indicate a lifecycle bug, rather than a recoverable UI choice.
const legal = {
    SETUP_REQUIRED: ['DISABLED', 'UNAVAILABLE', 'RECONFIGURATION_REQUIRED'],
    DISABLED: ['STARTING', 'UNAVAILABLE', 'SETUP_REQUIRED', 'RECONFIGURATION_REQUIRED'],
    UNAVAILABLE: ['DISABLED', 'SETUP_REQUIRED', 'STARTING', 'RECONFIGURATION_REQUIRED'],
    STARTING: ['ACTIVE', 'STOPPING', 'ERROR', 'UNAVAILABLE', 'RECONFIGURATION_REQUIRED'],
    ACTIVE: ['STOPPING', 'RECONFIGURATION_REQUIRED'],
    STOPPING: ['DISABLED', 'SETUP_REQUIRED', 'ERROR', 'RECONFIGURATION_REQUIRED'],
    RECONFIGURATION_REQUIRED: ['DISABLED', 'UNAVAILABLE', 'ERROR'],
    ERROR: ['STARTING', 'DISABLED', 'UNAVAILABLE', 'SETUP_REQUIRED',
        'RECONFIGURATION_REQUIRED', 'STOPPING'],
};

/** Pure lifecycle owner. Backend contract is documented in docs/ARCHITECTURE.md. */
export class Controller {
    constructor(backend, {preferred = '', onPreference = () => {}, now = () => Date.now(),
        lastError = '', onFailure = () => {}, processing = {}} = {}) {
        this.backend = backend;
        this.processing = processingSettings(processing);
        if (backend.configure) backend.configure(this.processing);
        this.state = State.SETUP_REQUIRED;
        this.message = 'Select a physical microphone.';
        this.devices = [];
        this.selection = null;
        this.activity = null;
        this.issues = [];
        // Session actions and discovery have separate counters so a refresh cannot revive cancelled startup.
        this._generation = 0;
        this._discovery = 0;
        this._session = null;
        this._disposed = false;
        this._disposing = false;
        this._reconfigure = false;
        this._cleanupFailed = false;
        this._preparing = null;
        this._closing = new Set();
        this._stoppedSessions = new WeakMap();
        this._listeners = new Set();
        this._preferred = preferred;
        this._restorePending = Boolean(preferred);
        this._onPreference = onPreference;
        this._now = now;
        this._restoreDeadline = now() + 10000;
        this.lastError = this._errorText(lastError);
        this._onFailure = onFailure;
    }

    snapshot() {
        return {state: this.state, message: this.message, activity: this.activity,
            processing: {...this.processing},
            lastError: this.lastError, restorePending: this._restorePending && this._now() < this._restoreDeadline,
            devices: this.devices.map(device => ({...device})),
            selection: this.selection ? {...this.selection} : null,
            issues: [...this.issues]};
    }

    subscribe(listener) {
        this._listeners.add(listener);
        listener(this.snapshot());
        return () => this._listeners.delete(listener);
    }

    _emit() {
        if (this._disposed) return;
        for (const listener of this._listeners) {
            // A broken view must never interrupt audio cleanup or lifecycle work.
            try { listener(this.snapshot()); } catch { /* Isolate UI listeners. */ }
        }
    }

    _set(state, message) {
        if (this.state !== state && !legal[this.state]?.includes(state))
            throw new Error(`Illegal transition ${this.state} -> ${state}`);
        this.state = state;
        if (state !== State.ACTIVE) this.activity = null;
        this.message = message;
        if ([State.ERROR, State.RECONFIGURATION_REQUIRED].includes(state)) {
            this.lastError = this._errorText(message);
            try { this._onFailure(this.lastError); } catch { /* Diagnostic storage must not interrupt cleanup. */ }
        }
        this._emit();
    }

    _errorText(value) {
        return String(value ?? '').replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 400);
    }

    // Share one cleanup result per session; retain pending reaping to block overlapping workers.
    _stop(session = this._session) {
        if (session === this._session) this._session = null;
        if (!session) return null;
        if (this._stoppedSessions.has(session)) return this._stoppedSessions.get(session);
        const failed = () => {
            this._cleanupFailed = true;
            this._reconfigure = true;
            this.selection = null;
        };
        try {
            const result = session.stop({immediate: this._disposing || this._disposed});
            if (result && typeof result.then === 'function') {
                const pending = Promise.resolve(result).catch(failed).then(() => {
                    this._closing.delete(pending);
                    if (this._cleanupFailed && !this._disposed)
                        this.invalidate('Audio cleanup failed.');
                });
                this._stoppedSessions.set(session, pending);
                this._closing.add(pending);
                return pending;
            }
            this._stoppedSessions.set(session, null);
        } catch {
            failed();
            this._stoppedSessions.set(session, null);
        }
        return null;
    }

    invalidate(reason) {
        if (this._disposed) return;
        const detail = this._errorText(reason).replace(/\s*Select the microphone again\.\s*$/, '');
        ++this._generation;
        this._reconfigure = true;
        this.selection = null;
        this._restorePending = false;
        // The preference is not authorization. Preserve it across faults and
        // logout, while permanently cancelling restoration in this lifecycle.
        this._stop();
        this._set(State.RECONFIGURATION_REQUIRED,
            this._cleanupFailed
                ? 'Resource cleanup failed. Disable the extension and inspect owned resources.'
                : `${detail} Select the microphone again, then enable explicitly.`);
    }

    async refresh() {
        if (this._disposed) return;
        const request = ++this._discovery;
        try {
            const result = await this.backend.inspect();
            if (this._disposed || request !== this._discovery) return;
            if (!result || !Array.isArray(result.devices) || !Array.isArray(result.issues) ||
                typeof result.epoch !== 'string' ||
                result.devices.some(d => !d || typeof d.key !== 'string' ||
                    !Number.isSafeInteger(d.id) || d.epoch !== result.epoch))
                throw new Error('Malformed discovery result');
            this.devices = result.devices.map(device => Object.freeze({...device}));
            this.issues = result.issues.map(String);
            if (this.selection && (this.selection.epoch !== result.epoch ||
                !this.devices.some(d => d.key === this.selection.key &&
                    d.id === this.selection.id))) {
                this.invalidate('The configured microphone disappeared or changed identity.');
                return;
            }
            if (this.issues.length && this.state === State.ACTIVE) {
                this.invalidate(this.issues.join(' '));
                return;
            }
            if (this._reconfigure) {
                this._emit();
                return;
            }
            if (this._restorePending && !this.issues.length) {
                if (this._now() >= this._restoreDeadline) this._restorePending = false;
                else {
                    const result = preferenceMatch(this.devices, this._preferred);
                    if (result.status !== 'absent') this._restorePending = false;
                    if (!this.selection && result.device) {
                        this.selection = Object.freeze({...result.device});
                        this._restorePending = false;
                    }
                }
            }
            if (this.state === State.STARTING || this.state === State.ACTIVE ||
                this.state === State.STOPPING) {
                this._emit();
                return;
            }
            if (this.issues.length)
                this._set(State.UNAVAILABLE, this.issues.join(' '));
            else if (!this.selection)
                this._set(State.SETUP_REQUIRED, this.snapshot().restorePending
                    ? 'Waiting briefly for the preferred microphone at login…' : this.devices.length
                    ? 'Select a physical microphone.'
                    : 'No eligible physical microphone. Connect one and refresh.');
            else
                this._set(State.DISABLED, 'Ready to enable.');
        } catch (error) {
            if (this._disposed || request !== this._discovery) return;
            this.devices = [];
            this.issues = [String(error.message ?? error)];
            if (this.selection || this.state === State.STARTING || this.state === State.ACTIVE)
                this.invalidate(`Audio discovery failed: ${this.issues[0]}.`);
            else if (!this._reconfigure)
                this._set(State.UNAVAILABLE, this.issues[0]);
        }
    }

    // Only this explicit selection clears the reconfiguration latch after identity loss.
    select(key) {
        if (this._disposed || this._cleanupFailed || this._closing.size || this._preparing ||
            [State.ACTIVE, State.STARTING, State.STOPPING].includes(this.state)) return false;
        const device = this.devices.find(d => d.key === key);
        if (!device) return false;
        ++this._generation;
        this.selection = Object.freeze({...device});
        this._restorePending = false;
        this._remember(preferenceFor(device));
        this._reconfigure = false;
        this._set(this.issues.length ? State.UNAVAILABLE : State.DISABLED,
            this.issues.length ? this.issues.join(' ') : 'Ready to enable.');
        return true;
    }

    // Freeze engine/options through startup, processing and cleanup; saved edits can wait.
    configure(value) {
        if (this._disposed || this._cleanupFailed || this._closing.size || this._preparing ||
            [State.ACTIVE, State.STARTING, State.STOPPING].includes(this.state)) return false;
        try {
            const processing = processingSettings(value);
            this.backend.configure(processing);
            this.processing = processing;
        } catch { return false; }
        ++this._generation;
        ++this._discovery;
        this._emit();
        void this.refresh();
        return true;
    }

    _remember(value) {
        this._preferred = value;
        // Optional preference storage must never interrupt fail-closed cleanup.
        try { this._onPreference(value); } catch { /* Keep lifecycle independent of storage. */ }
    }

    async enable() {
        if (this._disposed || this._reconfigure || this._cleanupFailed || this._closing.size ||
            [State.STARTING, State.ACTIVE, State.STOPPING].includes(this.state)) return;
        if (!this.selection) {
            await this.refresh();
            return;
        }
        // Every awaited startup step checks this token before publishing or changing state.
        const generation = ++this._generation;
        const current = () => !this._disposed && generation === this._generation;
        this._set(State.STARTING, 'Validating microphone and noise suppression…');
        let session;
        let latestActivity = null;
        try {
            await this.refresh();
            if (!current()) return;
            if (this.issues.length) {
                this._set(State.UNAVAILABLE, this.issues.join(' '));
                return;
            }
            const authorization = this.selection;
            const preparing = Promise.resolve(this.backend.prepare(authorization, {
                cancelled: () => !current(),
                onFault: reason => {
                    if (current()) this.invalidate(String(reason));
                },
                onActivity: activity => {
                    if (!current()) return;
                    latestActivity = activity;
                    if (this.state === State.ACTIVE) this._showActivity(activity);
                },
            }));
            this._preparing = preparing;
            try { session = await preparing; }
            finally { if (this._preparing === preparing) this._preparing = null; }
            if (!current()) { await this._stop(session); return; }
            this._session = session;
            const proof = await session.verify();
            if (!current()) return;
            this._checkProof(proof, authorization, false);
            // Discovery is an additional check, never a replacement for native supervision.
            await this.refresh();
            if (!current()) return;
            if (this.issues.length) throw new Error(this.issues.join(' '));
            const ready = await session.publish();
            if (!current()) { await this._stop(session); return; }
            this._checkProof(ready, authorization, true);
            this._showActivity(latestActivity ?? ready.activity ?? 'processing');
        } catch (error) {
            if (!current()) return;
            await this._stop();
            if (!current()) return;
            if (this._cleanupFailed) {
                this.invalidate('Audio cleanup failed.');
                return;
            }
            this._set(State.ERROR, `Noise cancellation failed: ${String(error.message ?? error)}`);
        }
    }

    _checkProof(proof, authorization, published) {
        if (!proof || proof.key !== authorization.key || proof.epoch !== authorization.epoch ||
            proof.processing !== true || proof.published !== published)
            throw new Error('Pipeline routing/processing readiness was not proven');
    }

    _showActivity(activity) {
        const messages = {
            processing: `${ENGINE_NAMES[this.processing.backend]} active — Clean Microphone`,
            idle: 'Waiting for an application — physical capture suspended.',
            suspending: 'Releasing the physical microphone…',
            resuming: 'An application opened Clean Microphone — validating fresh audio…',
        };
        if (!Object.hasOwn(messages, activity)) {
            this.invalidate('Unknown audio activity state.');
            return;
        }
        this.activity = activity;
        this._set(State.ACTIVE, messages[activity]);
    }

    // Tile-off keeps the microphone preference and waits for owned resource cleanup.
    disable() {
        if (this._disposed) return;
        const generation = ++this._generation;
        if (this.state === State.STARTING || this.state === State.ACTIVE || this._session)
            this._set(State.STOPPING, 'Stopping noise cancellation…');
        const tasks = [...this._closing];
        const stopping = this._stop();
        if (stopping) tasks.push(stopping);
        if (this._preparing)
            tasks.push(this._preparing.then(session => this._stop(session), () => null));
        const finish = () => {
            if (this._disposed || generation !== this._generation) return;
            if (this._cleanupFailed) {
                this.invalidate('Audio cleanup failed.');
                return;
            }
            if (this._reconfigure || this.state === State.UNAVAILABLE) return;
            this._set(this.selection ? State.DISABLED : State.SETUP_REQUIRED,
                this.selection ? 'Noise cancellation disabled.' : 'Select a physical microphone.');
        };
        if (tasks.length) return Promise.all(tasks).then(finish);
        finish();
    }

    // Extension unload also cancels discovery and forces worker termination through the backend.
    dispose() {
        if (this._disposed) return this._disposal;
        this._disposing = true;
        const cleanup = this.disable();
        this._disposed = true;
        ++this._generation;
        ++this._discovery;
        this._listeners.clear();
        let backendCleanup;
        try { backendCleanup = this.backend.dispose(); }
        catch (error) { backendCleanup = Promise.reject(error); }
        this._disposal = Promise.all([cleanup, backendCleanup]).then(() => undefined);
        this._disposal.catch(() => {});
        return this._disposal;
    }
}
