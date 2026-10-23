import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

/** Bounded argument-array subprocesses. No shell and no audio subprocesses. */
export class ProcessRunner {
    constructor() {
        this._jobs = new Set();
        this._disposed = false;
    }

    async run(argv, {timeoutMs = 3000, maxBytes = 8 * 1024 * 1024} = {}) {
        if (this._disposed) throw new Error('Process runner disposed');
        if (this._jobs.size >= 2) throw new Error('Too many discovery processes');
        if (!Array.isArray(argv) || !argv.length || argv.length > 32 ||
            argv.some(arg => typeof arg !== 'string' || arg.includes('\0') || arg.length > 4096) ||
            !argv[0].startsWith('/') || !Number.isInteger(timeoutMs) ||
            timeoutMs < 1 || timeoutMs > 10000 || !Number.isInteger(maxBytes) ||
            maxBytes < 1 || maxBytes > 16 * 1024 * 1024)
            throw new Error('Invalid process arguments or limits');
        const child = Gio.Subprocess.new(argv,
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        const cancellable = new Gio.Cancellable();
        let failure = null;
        let exited = false;
        let size = 0;
        let timer = 0;
        let reaped, reapFailed;
        const done = new Promise((resolve, reject) => { reaped = resolve; reapFailed = reject; });
        done.catch(() => {});
        const terminate = message => {
            failure ??= new Error(message);
            if (timer) { GLib.source_remove(timer); timer = 0; }
            if (!exited) {
                try { child.force_exit(); } catch { /* Already exited. */ }
            }
            cancellable.cancel();
        };
        const job = {done, cancel: () => terminate('Discovery cancelled')};
        this._jobs.add(job);
        timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
            timer = 0;
            terminate('PipeWire discovery timed out');
            return GLib.SOURCE_REMOVE;
        });
        const read = async stream => {
            const chunks = [];
            let length = 0;
            try {
                while (true) {
                    const bytes = await new Promise((resolve, reject) => {
                        stream.read_bytes_async(8192, GLib.PRIORITY_DEFAULT, cancellable,
                            (source, result) => {
                                try { resolve(source.read_bytes_finish(result).toArray()); }
                                catch (error) { reject(error); }
                            });
                    });
                    if (!bytes.length) break;
                    // stdout and stderr share one budget, including diagnostics from a failing child.
                    size += bytes.length;
                    if (size > maxBytes) throw new Error('Discovery output exceeds size limit');
                    chunks.push(bytes);
                    length += bytes.length;
                }
                const buffer = new Uint8Array(length);
                let offset = 0;
                for (const chunk of chunks) {
                    buffer.set(chunk, offset);
                    offset += chunk.length;
                }
                return new TextDecoder('utf-8', {fatal: true}).decode(buffer);
            } catch (error) {
                terminate(String(error.message ?? error));
                throw error;
            } finally {
                // Closing an owned pipe descriptor does not wait for the child.
                try { stream.close(null); } catch { /* Already closed/cancelled. */ }
            }
        };
        const wait = new Promise((resolve, reject) => {
            // Never cancel wait: the owned process must always be reaped.
            child.wait_async(null, (source, result) => {
                try { source.wait_finish(result); exited = true; resolve(); }
                catch (error) { reject(error); }
            });
        });
        try {
            const results = await Promise.allSettled([
                read(child.get_stdout_pipe()), read(child.get_stderr_pipe()), wait,
            ]);
            if (failure) throw failure;
            const rejected = results.find(result => result.status === 'rejected');
            if (rejected) throw rejected.reason;
            if (!child.get_successful())
                throw new Error('Discovery process failed; check PipeWire access and service status.');
            return {stdout: results[0].value, stderr: results[1].value};
        } finally {
            if (timer) GLib.source_remove(timer);
            this._jobs.delete(job);
            if (exited) reaped();
            else reapFailed(new Error('Cannot confirm discovery process termination'));
        }
    }

    dispose() {
        if (this._disposed) return this._disposal;
        this._disposed = true;
        const jobs = [...this._jobs];
        for (const job of jobs) job.cancel();
        this._disposal = Promise.all(jobs.map(job => job.done)).then(() => undefined);
        this._disposal.catch(() => {});
        return this._disposal;
    }
}
