import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {processingSettings} from '../core/settings.js';

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    // Cancellation can happen before the controller starts awaiting readiness.
    promise.catch(() => {});
    return {promise, resolve, reject};
}
const now = () => GLib.get_monotonic_time() / 1000;
const objectId = value => Number.isSafeInteger(value) && value >= 0 && value < 0xffffffff;

/** One private bundled worker per explicit authorization. No shell or installer. */
export class AudioSession {
    constructor(command, authorization, context, {
        spawn = argv => Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDIN_PIPE |
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE),
        startupTimeoutMs = 8000, heartbeatMs = 500, heartbeatTimeoutMs = 2000,
        stopTimeoutMs = 750,
    } = {}) {
        if (!Array.isArray(command) || !command.length || command.length > 8 ||
            command.some(arg => typeof arg !== 'string' || arg.length > 4096 || arg.includes('\0')) ||
            !command[0].startsWith('/') ||
            !objectId(authorization.id) || typeof authorization.key !== 'string' ||
            authorization.key.length > 8192 || !/^[0-9]+$/.test(authorization.epoch) ||
            !/^[0-9]+$/.test(authorization.sourceSerial))
            throw new Error('Invalid audio session authorization');
        const identity = JSON.parse(authorization.key);
        if (!Array.isArray(identity) || identity.length !== 6 || identity[0] !== authorization.epoch ||
            identity[1] !== authorization.sourceSerial)
            throw new Error('Microphone authorization does not match its identity key');
        this._processing = processingSettings(context.processing ?? {});
        this._authorization = Object.freeze({...authorization});
        this._context = context;
        this._token = GLib.uuid_string_random();
        // Separate promises prove filtered input, published output and confirmed child exit.
        this._prepared = deferred();
        this._active = null;
        this._done = deferred();
        this._cancellable = new Gio.Cancellable();
        this._phase = 'preparing';
        this._activity = 'processing';
        this._expectedStop = false;
        this._faulted = false;
        this.exited = false;
        this._line = [];
        this._queuedWrites = 0;
        this._writes = Promise.resolve();
        this._started = this._lastPong = now();
        this._publishStarted = 0;
        this._startupTimeoutMs = startupTimeoutMs;
        this._heartbeatTimeoutMs = heartbeatTimeoutMs;
        this._stopTimeoutMs = stopTimeoutMs;
        this._child = spawn([...command, '--session']);
        this._stdin = this._child.get_stdin_pipe();
        this._stdout = this._child.get_stdout_pipe();
        this._stderr = this._child.get_stderr_pipe();
        // Pipe I/O can be cancelled during stop; the child wait must still run to completion.
        this._child.wait_async(null, (child, result) => {
            try {
                child.wait_finish(result);
                this.exited = true;
                if (!this._expectedStop) this._fail('Audio worker exited unexpectedly. Select the microphone again.');
                this._cleanup();
                this._done.resolve();
            } catch (error) {
                this._fail('Cannot confirm audio worker termination.');
                this._cleanup();
                this._done.reject(error);
            }
        });
        void this._readOutput().catch(error => this._fail(error.message));
        void this._readErrors().catch(error => {
            if (!this._expectedStop) this._fail(error.message);
        });
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, heartbeatMs, () => {
            if (this._expectedStop) return GLib.SOURCE_REMOVE;
            if (context.cancelled()) { void this.stop(); return GLib.SOURCE_REMOVE; }
            const time = now();
            if ((this._phase === 'preparing' && time - this._started > this._startupTimeoutMs) ||
                (this._phase === 'publishing' && time - this._publishStarted > this._startupTimeoutMs))
                this._fail('Audio worker startup timed out.');
            else if (time - this._lastPong > this._heartbeatTimeoutMs)
                this._fail('Audio worker stopped responding.');
            else if (this._queuedWrites === 0)
                void this._send('ping').catch(error => this._fail(error.message));
            return this._expectedStop ? GLib.SOURCE_REMOVE : GLib.SOURCE_CONTINUE;
        });
        void this._send('prepare', {key: authorization.key, sourceId: authorization.id, processing: this._processing})
            .catch(error => this._fail(error.message));
    }

    _send(command, fields = {}) {
        if (this._expectedStop || this._queuedWrites >= 4)
            return Promise.reject(new Error('Audio worker control channel is unavailable'));
        const bytes = new TextEncoder().encode(JSON.stringify({
            protocol: 1, session: this._token, command, ...fields,
        }) + '\n');
        if (bytes.length > 16384) return Promise.reject(new Error('Microphone authorization is too large'));
        this._queuedWrites++;
        // Serialize bounded control messages so ping/publish cannot interleave their pipe writes.
        const task = this._writes.then(() => new Promise((resolve, reject) => {
            if (this._expectedStop) { reject(new Error('Audio session cancelled')); return; }
            this._stdin.write_all_async(bytes, GLib.PRIORITY_DEFAULT, this._cancellable, (stream, result) => {
                try {
                    const [success, count] = stream.write_all_finish(result);
                    if (!success || count !== bytes.length) throw new Error('Incomplete audio worker request');
                    resolve();
                } catch (error) { reject(error); }
            });
        })).finally(() => { this._queuedWrites--; });
        this._writes = task.catch(() => {});
        return task;
    }

    _read(stream) {
        return new Promise((resolve, reject) => stream.read_bytes_async(4096,
            GLib.PRIORITY_DEFAULT, this._cancellable, (source, result) => {
                try { resolve(source.read_bytes_finish(result).toArray()); }
                catch (error) { reject(error); }
            }));
    }

    async _readOutput() {
        while (!this._expectedStop) {
            const bytes = await this._read(this._stdout);
            if (!bytes.length) {
                if (!this._expectedStop) this._fail('Audio worker closed its control channel.');
                return;
            }
            for (const byte of bytes) {
                if (byte === 10) {
                    const line = new TextDecoder('utf-8', {fatal: true}).decode(Uint8Array.from(this._line));
                    this._line = [];
                    this._message(JSON.parse(line));
                    if (this._expectedStop) return;
                } else {
                    if (this._line.length >= 4095 || byte === 0)
                        throw new Error('Audio worker response exceeded the protocol limit');
                    this._line.push(byte);
                }
            }
        }
    }

    async _readErrors() {
        // Drain but do not log untrusted stderr or allow an unbounded producer.
        let size = 0;
        while (!this._expectedStop) {
            const bytes = await this._read(this._stderr);
            if (!bytes.length) return;
            size += bytes.length;
            if (size > 16384) throw new Error('Audio worker produced excessive diagnostic output');
        }
    }

    _message(message) {
        if (this._expectedStop) return;
        if (!message || typeof message !== 'object' || message.protocol !== 1 ||
            message.session !== this._token || typeof message.event !== 'string')
            throw new Error('Audio worker sent a stale or incompatible protocol message');
        if (message.event === 'pong') { this._lastPong = now(); return; }
        if (message.event === 'fault') {
            this._fail(typeof message.message === 'string'
                ? message.message.replace(/[\x00-\x1f\x7f]/g, '').slice(0, 400)
                : 'Audio worker routing failed.');
            return;
        }
        // Valid routing is insufficient if the worker is processing a different engine/options snapshot.
        if (!message.configuration ||
            JSON.stringify(processingSettings(message.configuration)) !== JSON.stringify(this._processing))
            throw new Error('Audio worker did not prove the selected engine and settings');
        const a = this._authorization;
        if (message.event === 'activity') {
            const processing = message.state === 'processing';
            if (this._phase !== 'active' || !['idle', 'suspending', 'resuming', 'processing'].includes(message.state) ||
                message.sourceId !== a.id || message.sourceSerial !== a.sourceSerial ||
                String(message.cookie) !== a.epoch || message.outputId !== this._outputId ||
                message.captureLinkId !== this._captureLinkId || message.published !== true ||
                message.processing !== processing || message.rate !== 48000 || message.channels !== 1 ||
                !Number.isSafeInteger(message.processedFrames) || message.processedFrames < 1 ||
                !Number.isSafeInteger(message.resumeBaseline) || message.resumeBaseline < 0 ||
                message.resumeBaseline > message.processedFrames ||
                (processing && message.processedFrames <= message.resumeBaseline))
                throw new Error('Audio worker sent invalid idle/resume evidence');
            this._activity = message.state;
            this._context.onActivity?.(message.state);
            return;
        }
        const published = message.event === 'active';
        if (!['prepared', 'active'].includes(message.event) ||
            message.sourceId !== a.id || message.sourceSerial !== a.sourceSerial ||
            String(message.cookie) !== a.epoch || message.processing !== true ||
            message.published !== published || message.rate !== 48000 || message.channels !== 1 ||
            !Number.isSafeInteger(message.processedFrames) || message.processedFrames < 1 ||
            !objectId(message.captureLinkId))
            throw new Error('Audio worker did not prove the authorized processing route');
        if (message.event === 'prepared' && this._phase === 'preparing' && message.outputId === undefined) {
            this._captureLinkId = message.captureLinkId;
            this._phase = 'prepared';
            this._prepared.resolve({key: a.key, epoch: a.epoch, processing: true, published: false});
        } else if (message.event === 'active' && this._phase === 'publishing' &&
            objectId(message.outputId) && message.outputId !== a.id &&
            message.captureLinkId === this._captureLinkId) {
            this._phase = 'active';
            this._outputId = message.outputId;
            this._active.resolve({key: a.key, epoch: a.epoch, processing: true, published: true});
        } else throw new Error('Audio worker published out of order or changed its capture route');
    }

    verify() {
        return this._prepared.promise;
    }

    get whenStopped() {
        return this._done.promise;
    }

    async publish() {
        await this.verify();
        if (this._expectedStop || this._context.cancelled() || this._phase !== 'prepared')
            throw new Error('Audio session publication was cancelled or repeated');
        this._phase = 'publishing';
        this._publishStarted = now();
        this._active = deferred();
        try { await this._send('publish'); }
        catch (error) { this._fail(error.message); }
        const proof = await this._active.promise;
        return {...proof, activity: this._activity};
    }

    _fail(message) {
        if (this._expectedStop || this._faulted) return;
        this._faulted = true;
        const error = new Error(String(message));
        this._prepared.reject(error);
        this._active?.reject(error);
        try { this._context.onFault(error.message); }
        catch { /* A UI observer cannot prevent owned-process cleanup. */ }
        finally { void this.stop().catch(() => {}); }
    }

    _forceExit() {
        if (this.exited || this._forcedExit) return;
        try {
            this._child.force_exit();
            this._forcedExit = true;
        } catch { this._done.reject(new Error('Cannot terminate owned audio worker')); }
    }

    stop({immediate = false, handoff = false} = {}) {
        if (!this._expectedStop) {
            this._expectedStop = true;
            this._prepared.reject(new Error('Audio session stopped'));
            this._active?.reject(new Error('Audio session stopped'));
            if (this._timer) { GLib.source_remove(this._timer); this._timer = 0; }
            const gracefulHandoff = handoff && !immediate && this._phase === 'active' && !this._faulted;
            if (!gracefulHandoff) this._cancellable.cancel();
            if (!this.exited && !immediate) {
                if (gracefulHandoff)
                    this._requestHandoffStop();
                else this._signalStop();
                this._killTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, this._stopTimeoutMs, () => {
                    this._killTimer = 0;
                    this._forceExit();
                    return GLib.SOURCE_REMOVE;
                });
            }
            if (immediate) {
                try { this._stdin.close(null); } catch { /* A cancelled write may still own the stream. */ }
            }
        }
        if (immediate) {
            // GNOME does not await Extension.disable(). Do not leave a grace
            // timer or depend on the Python/GLib loop handling SIGTERM on unload.
            // This also escalates a toggle stop that was already in progress.
            this._forceExit();
            this._cleanup();
        }
        // Always reap asynchronously; never block GNOME Shell on a child wait.
        return this._done.promise;
    }

    _signalStop() {
        try { this._child.send_signal(15); } catch { /* Exit notification may already be queued. */ }
        // EOF also revokes a worker whose parent stops writing.
        try { this._stdin.close(null); } catch { /* A cancelled write may still own the stream. */ }
    }

    _requestHandoffStop() {
        const bytes = new TextEncoder().encode(JSON.stringify({protocol: 1, session: this._token,
            command: 'stop', handoff: true}) + '\n');
        this._stopCancellable = new Gio.Cancellable();
        // Finish any already-started bounded message before the final user stop;
        // cancellation must not leave a partial JSON line ahead of it. The same
        // parent stop deadline still bounds a stuck write and the entire handoff.
        void this._writes.then(() => {
            if (this.exited || this._forcedExit || this._stopCancellable.is_cancelled()) return;
            this._cancellable.cancel();
            this._stdin.write_all_async(bytes, GLib.PRIORITY_DEFAULT, this._stopCancellable, (stream, result) => {
                try {
                    const [success, count] = stream.write_all_finish(result);
                    if (!success || count !== bytes.length) throw new Error('Incomplete stop request');
                } catch {
                    if (!this.exited && !this._forcedExit) this._signalStop();
                }
            });
        }).catch(() => { if (!this.exited && !this._forcedExit) this._signalStop(); });
    }

    _cleanup() {
        if (this._timer) { GLib.source_remove(this._timer); this._timer = 0; }
        if (this._killTimer) { GLib.source_remove(this._killTimer); this._killTimer = 0; }
        this._cancellable.cancel();
        this._stopCancellable?.cancel();
        for (const stream of [this._stdin, this._stdout, this._stderr]) {
            try { stream.close(null); } catch { /* Already closed or completing cancellation. */ }
        }
    }
}
