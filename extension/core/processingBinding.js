import {processingSettings} from './settings.js';

/** Read persisted preferences; controller alone decides when they can apply. */
export class ProcessingBinding {
    constructor(controller, read, onError = () => {}) {
        this._controller = controller;
        this._read = read;
        this._onError = onError;
        this._syncing = false;
        this._disposed = false;
        this._lastError = null;
        this._unsubscribe = controller.subscribe(() => this.sync());
    }

    sync() {
        if (this._disposed || this._syncing) return;
        // configure() emits another snapshot synchronously; prevent that event from recursing.
        this._syncing = true;
        try {
            const desired = processingSettings(this._read());
            this._lastError = null;
            if (JSON.stringify(desired) !== JSON.stringify(this._controller.processing))
                this._controller.configure(desired);
            // Busy/closing controllers reject configure. The next state event
            // rereads the latest saved value; never stop or restart audio here.
        } catch (error) {
            const message = String(error.message ?? error).slice(0, 300);
            if (message !== this._lastError) {
                this._lastError = message;
                try { this._onError(message); } catch { /* Observer cannot affect audio. */ }
            }
        } finally { this._syncing = false; }
    }

    dispose() {
        if (this._disposed) return;
        this._disposed = true;
        this._unsubscribe?.();
        this._controller = this._read = this._onError = this._unsubscribe = null;
    }
}
