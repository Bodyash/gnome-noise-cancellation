import Pango from 'gi://Pango';
import GLib from 'gi://GLib';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import {PACKAGE_VERSION} from 'resource:///org/gnome/shell/misc/config.js';
import {panel} from 'resource:///org/gnome/shell/ui/main.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {PopupMenuItem, PopupMenuSection, PopupSeparatorMenuItem, Ornament}
    from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Controller, State} from './core/controller.js';
import {PipeWireBackend} from './audio/backend.js';
import {processingSettings} from './core/settings.js';
import {ProcessingBinding} from './core/processingBinding.js';
import {tileSubtitle} from './core/presentation.js';

export default class NoiseCancellationExtension extends Extension {
    enable() {
        if (this._controller) return;
        if (PACKAGE_VERSION.split('.')[0] !== '51')
            throw new Error('Noise Cancellation requires GNOME Shell 51.');
        try {
            this._warnings = new Set();
            this._restoreRetries = 0;
            this._settings = this.getSettings();
            let processing;
            try { processing = processingSettings(this._settings.get_string('processing-settings')); }
            catch {
                processing = processingSettings();
                this._warnOnce('saved-processing', 'Invalid saved processing settings; using disabled defaults.');
            }
            // The controller owns audio state; the tile only requests actions and renders snapshots.
            this._controller = new Controller(new PipeWireBackend({previousShutdown: this._shutdown}), {
                preferred: this._settings.get_string('preferred-microphone'),
                processing,
                now: () => GLib.get_monotonic_time() / 1000,
                lastError: this._settings.get_string('last-error'),
                onFailure: value => {
                    if (!this._settings?.set_string('last-error', value))
                        this._warnOnce('last-error-save', 'Could not save the last problem.');
                },
                onPreference: value => {
                    if (!this._settings?.set_string('preferred-microphone', value))
                        this._warnOnce('preference-save', 'Could not save the microphone preference.');
                },
            });
            this._processingBinding = new ProcessingBinding(this._controller,
                () => this._settings.get_string('processing-settings'),
                message => this._warnOnce('processing-preferences', `Invalid processing preferences: ${message}`));
            this._settingsSignal = this._settings.connect('changed::processing-settings',
                () => this._processingBinding?.sync());
            this._indicator = new SystemIndicator();
            this._icon = this._indicator._addIndicator();
            this._icon.icon_name = 'audio-input-microphone-symbolic';
            this._icon.visible = false;
            this._toggle = new QuickMenuToggle({
                title: _('Noise Cancellation'),
                iconName: 'audio-input-microphone-symbolic',
                toggleMode: false,
            });
            this._indicator.quickSettingsItems.push(this._toggle);
            this._status = new PopupMenuItem('', {reactive: false, can_focus: false});
            this._status.label.add_style_class_name('noise-cancellation-status');
            this._status.label.clutter_text.set({
                line_wrap: true,
                line_wrap_mode: Pango.WrapMode.WORD_CHAR,
                ellipsize: Pango.EllipsizeMode.NONE,
            });
            this._toggle.menu.addMenuItem(this._status);
            this._toggle.menu.addMenuItem(new PopupSeparatorMenuItem(_('Physical microphone')));
            this._devices = new PopupMenuSection();
            this._toggle.menu.addMenuItem(this._devices);
            this._toggle.menu.addMenuItem(new PopupSeparatorMenuItem());
            this._toggle.menu.addAction(_('Settings…'), () => {
                panel.closeQuickSettings();
                this.openPreferences();
            });
            this._toggle.menu.addAction(_('Refresh microphones and dependencies'),
                () => this._refresh());
            this._toggleSignal = this._toggle.connect('clicked', () => {
                const c = this._controller;
                if (!c) return;
                if ([State.ACTIVE, State.STARTING].includes(c.state)) c.disable();
                else if (!c.selection || c.state === State.UNAVAILABLE)
                    this._toggle.menu.open();
                else void c.enable();
            });
            this._menuSignal = this._toggle.menu.connect('open-state-changed', (_menu, open) => {
                if (open) this._refresh();
            });
            this._unsubscribe = this._controller.subscribe(snapshot => this._render(snapshot));
            panel.statusArea.quickSettings.addExternalIndicator(this._indicator, 2);
            this._refresh();
        } catch (error) {
            this.disable();
            throw error;
        }
    }

    _warnOnce(category, message) {
        // Fixed call-site categories bound logging/history even if storage keeps failing.
        // Missing history also rejects callbacks that arrive after disable().
        if (!this._warnings || this._warnings.has(category)) return;
        this._warnings.add(category);
        console.warn(`[NoiseCancellation] ${message}`);
    }

    async _refresh() {
        const controller = this._controller;
        if (!controller) return;
        await controller.refresh();
        if (this._controller !== controller) return;
        if (!controller.snapshot().restorePending) {
            if (this._restoreTimer) GLib.source_remove(this._restoreTimer);
            this._restoreTimer = 0;
            return;
        }
        // Bounded read-only login discovery; never starts an audio session.
        const delays = [250, 750, 1500, 2500, 5000];
        if (!this._restoreTimer && this._restoreRetries < delays.length) {
            this._restoreTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delays[this._restoreRetries++], () => {
                this._restoreTimer = 0;
                if (this._controller === controller) void this._refresh();
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    _render(snapshot) {
        const {state, message, devices, selection, activity, lastError} = snapshot;
        if (!this._toggle) return;
        const failure = [State.ERROR, State.RECONFIGURATION_REQUIRED, State.UNAVAILABLE].includes(state)
            ? `${state}: ${message}` : null;
        if (failure && failure !== this._lastFailure)
            console.warn(`[NoiseCancellation] ${failure}`);
        this._lastFailure = failure;
        this._toggle.checked = state === State.ACTIVE;
        const subtitle = tileSubtitle(snapshot, _);
        this._toggle.subtitle = subtitle;
        this._toggle.accessible_name = `${_('Noise Cancellation')}: ${subtitle}`;
        // This extension icon is separate from GNOME's system microphone privacy indicator.
        this._icon.visible = state === State.ACTIVE && activity === 'processing';
        const header = selection?.name ?? _('No microphone selected');
        if (header !== this._headerTitle) {
            this._toggle.menu.setHeader('audio-input-microphone-symbolic', _('Noise Cancellation'), header);
            this._headerTitle = header;
        }
        this._status.label.text = lastError && [State.DISABLED, State.SETUP_REQUIRED].includes(state)
            ? `${message}\n${_('Last problem')}: ${lastError}` : message;
        const busy = [State.ACTIVE, State.STARTING, State.STOPPING].includes(state);
        const signature = JSON.stringify(devices.map(device => [device.key, device.name]));
        if (this._deviceSignature !== signature) {
            this._deviceSignature = signature;
            this._devices.removeAll();
            this._deviceItems = new Map();
            if (!devices.length) {
                this._devices.addMenuItem(new PopupMenuItem(_('No eligible microphones'),
                    {reactive: false, can_focus: false}));
            }
            for (const device of devices) {
                const item = new PopupMenuItem(device.name);
                item.connect('activate', () => this._controller?.select(device.key));
                this._deviceItems.set(device.key, item);
                this._devices.addMenuItem(item);
            }
        }
        // Selection emits synchronously inside PopupMenuItem.activate(). Do not
        // destroy that item before GNOME finishes handling its activation.
        for (const [key, item] of this._deviceItems ?? []) {
            item.setOrnament(key === selection?.key ? Ornament.CHECK : Ornament.NONE);
            item.setSensitive(!busy);
        }
    }

    disable() {
        const controller = this._controller;
        this._controller = null;
        // Termination must precede fallible signal/widget teardown. GNOME does
        // not await disable(), so retain only the asynchronous reap barrier.
        try {
            this._shutdown = Promise.all([this._shutdown, controller?.dispose()]).then(() => undefined);
        } catch (error) {
            this._shutdown = Promise.reject(error);
        }
        void this._shutdown.catch(() => console.error('[NoiseCancellation] Extension shutdown failed; audio restart blocked.'));
        let cleanupFailed = false;
        const release = action => {
            try { action(); }
            catch { cleanupFailed = true; }
        };
        release(() => { if (this._restoreTimer) GLib.source_remove(this._restoreTimer); });
        release(() => { if (this._settingsSignal) this._settings?.disconnect(this._settingsSignal); });
        release(() => { if (this._toggleSignal) this._toggle?.disconnect(this._toggleSignal); });
        release(() => { if (this._menuSignal) this._toggle?.menu.disconnect(this._menuSignal); });
        release(() => this._processingBinding?.dispose());
        release(() => this._unsubscribe?.());
        for (const item of this._indicator?.quickSettingsItems ?? []) release(() => item.destroy());
        release(() => this._indicator?.destroy());
        this._restoreTimer = this._restoreRetries = this._settingsSignal = 0;
        this._toggleSignal = this._menuSignal = 0;
        this._warnings?.clear();
        this._warnings = null;
        this._processingBinding = this._unsubscribe = null;
        this._indicator = this._toggle = this._devices = this._status = this._icon = null;
        this._deviceSignature = this._deviceItems = this._headerTitle = this._lastFailure = this._settings = null;
        if (cleanupFailed) console.error('[NoiseCancellation] Some desktop resources could not be released during shutdown.');
    }
}
