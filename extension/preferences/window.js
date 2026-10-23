import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import {processingSettings, ENGINE_NAMES, checkedEngines} from '../core/settings.js';
import {capabilities} from '../audio/backend.js';
import {ProcessRunner} from '../audio/process.js';

/** GTK is confined to GNOME's preferences process; this view never starts audio. */
export class PreferencesView {
    constructor(settings, {translate = value => value, runner = new ProcessRunner(),
        detect = capabilities, checkOnOpen = true} = {}) {
        this._settings = settings;
        this._ = translate;
        this._runner = runner;
        this._detect = detect;
        this._alive = true;
        this._rendering = false;
        this._checking = false;
        this._checked = false;
        this._connections = [];
        this._engines = [];
        this._commonError = '';
        this._advice = {};
        this.rows = new Map();
        const _ = this._;
        this.page = new Adw.PreferencesPage({title: _('Processing'),
            icon_name: 'audio-input-microphone-symbolic'});
        const general = new Adw.PreferencesGroup({title: _('Processing'),
            description: _('Changes are saved automatically. Disable noise cancellation, then enable it to apply them.')});
        this.page.add(general);
        this._choice(general, 'backend', _('Backend'),
            Object.entries(ENGINE_NAMES).map(([value, label]) => [value, label]));
        this._spin(general, 'outputGainDb', _('Output level (dB)'),
            _('Adjusts the processed signal. Positive gain can distort loud speech.'), -12, 12);
        this.notice = new Adw.ActionRow({title: _('Saved settings could not be read'),
            subtitle: _('Choose an option to replace invalid settings with safe defaults.'), use_markup: false});
        general.add(this.notice);
        this._groups = new Map();
        for (const [id, title, description] of [
            ['rnnoise', 'RNNoise', _('Uses the installed pretrained model. No suppression-strength setting is available.')],
            ['speex', 'SpeexDSP', _('Stronger attenuation can affect quiet speech. Automatic gain is optional.')],
            ['webrtc', 'WebRTC', _('Higher suppression can reduce noise at the expense of speech quality.')],
        ]) {
            const group = new Adw.PreferencesGroup({title, description});
            this._groups.set(id, group); this.page.add(group);
        }
        this._spin(this._groups.get('speex'), 'speexAttenuation', _('Noise attenuation (dB)'),
            _('More negative values allow stronger noise suppression.'), -60, -5);
        this._switch(this._groups.get('speex'), 'speexAgc', _('Automatic gain'),
            _('Adjust speech level automatically; may amplify residual noise.'));
        this._choice(this._groups.get('speex'), 'speexTarget', _('Gain target'),
            [[4000, _('Quiet (−18 dBFS)')], [8000, _('Balanced (−12 dBFS)')],
                [12000, _('Louder (−9 dBFS)')], [16000, _('Loudest (−6 dBFS)')]]);
        this._choice(this._groups.get('webrtc'), 'webrtcLevel', _('Noise suppression'),
            [[0, _('Low')], [1, _('Moderate')], [2, _('High')], [3, _('Very high')]]);
        this._switch(this._groups.get('webrtc'), 'webrtcAgc', _('Automatic gain'),
            _('Adjust speech level automatically; may amplify residual noise.'));
        this._spin(this._groups.get('webrtc'), 'webrtcTarget', _('Gain target (dB below full scale)'),
            _('A larger value leaves more headroom.'), 0, 31);
        this._switch(this._groups.get('webrtc'), 'webrtcHighPass', _('High-pass filter'),
            _('Reduce low-frequency rumble.'));
        const dependencies = new Adw.PreferencesGroup({title: _('Dependencies')});
        this.page.add(dependencies);
        this.status = new Adw.ActionRow({title: _('Checking dependencies…'), use_markup: false,
            subtitle_lines: 0});
        this.checkButton = new Gtk.Button({label: _('Check again'), valign: Gtk.Align.CENTER});
        this.status.add_suffix(this.checkButton);
        dependencies.add(this.status);
        this._connect(this.checkButton, 'clicked', () => { void this.refresh(); });
        this._connect(settings, 'changed::processing-settings', () => this._render());
        this._render();
        if (checkOnOpen) void this.refresh();
    }

    _connect(object, signal, callback) {
        this._connections.push([object, object.connect(signal, callback)]);
    }

    _load() {
        try {
            const value = processingSettings(this._settings.get_string('processing-settings'));
            this.notice.visible = false;
            return value;
        } catch {
            this.notice.visible = true;
            return processingSettings();
        }
    }

    // Save choices only; the Shell controller applies them after the current session stops.
    _write(key, value) {
        if (!this._alive || this._rendering) return;
        try {
            const next = processingSettings({...this._load(), [key]: value});
            if (!this._settings.set_string('processing-settings', JSON.stringify(next)))
                throw new Error(this._('Could not save preferences.'));
        } catch (error) {
            this.notice.visible = true;
            this.notice.subtitle = String(error.message ?? error).slice(0, 300);
        }
    }

    _choice(group, key, title, options) {
        const row = new Adw.ComboRow({title, use_markup: false});
        const spec = {row, options, values: options.map(([value]) => value)};
        this.rows.set(key, spec);
        group.add(row);
        this._connect(row, 'notify::selected', () => {
            if (row.selected < spec.values.length) this._write(key, spec.values[row.selected]);
        });
    }

    _spin(group, key, title, subtitle, lower, upper) {
        const row = new Adw.SpinRow({title, subtitle, use_markup: false, digits: 0,
            adjustment: new Gtk.Adjustment({lower, upper, step_increment: 1, page_increment: 3})});
        this.rows.set(key, {row}); group.add(row);
        this._connect(row, 'notify::value', () => this._write(key, Math.round(row.value)));
    }

    _switch(group, key, title, subtitle) {
        const row = new Adw.SwitchRow({title, subtitle, use_markup: false});
        this.rows.set(key, {row}); group.add(row);
        this._connect(row, 'notify::active', () => this._write(key, row.active));
    }

    _render() {
        if (!this._alive) return;
        this._rendering = true;
        try {
            const config = this._load();
            for (const [key, spec] of this.rows) {
                if (spec.options) {
                    const values = spec.options.map(([value]) => value);
                    const labels = spec.options.map(([value, label]) => label +
                        (key === 'backend' && this._engines.find(engine => engine.id === value)?.ready === false
                            ? ` · ${this._('Unavailable')}` : ''));
                    if (!values.includes(config[key])) {
                        values.push(config[key]); labels.push(`${this._('Custom')}: ${config[key]}`);
                    }
                    spec.values = values;
                    // Keep native popovers open while updating selection. Models
                    // change only when labels/availability actually change.
                    if (JSON.stringify(spec.labels) !== JSON.stringify(labels)) {
                        spec.row.model = Gtk.StringList.new(labels); spec.labels = labels;
                    }
                    spec.row.selected = values.indexOf(config[key]);
                } else if (typeof config[key] === 'boolean') spec.row.active = config[key];
                else spec.row.value = config[key];
            }
            for (const [id, group] of this._groups) group.visible = id === config.backend;
            this.rows.get('speexTarget').row.sensitive = config.speexAgc;
            this.rows.get('webrtcTarget').row.sensitive = config.webrtcAgc;
            const selected = this._engines.find(engine => engine.id === config.backend);
            this.status.title = this._checking ? this._('Checking dependencies…') : this._commonError
                ? this._('Dependencies unavailable') : selected?.ready ? this._('Backend ready')
                : this._checked ? this._('Backend unavailable') : this._('Dependencies not checked');
            this.status.subtitle = this._commonError || (selected?.ready
                ? `${ENGINE_NAMES[config.backend]} — ${this._('ready for the next explicit enable.')}`
                : [selected?.message, this._advice[config.backend]?.installCommand,
                    this._('Install missing dependencies manually, then check again.')].filter(Boolean).join('\n'));
            this.checkButton.sensitive = !this._checking;
        } finally { this._rendering = false; }
    }

    // Probe libraries/plugins without opening a microphone or creating a PipeWire graph.
    async refresh() {
        if (!this._alive || this._checking) return;
        this._checking = true; this._render();
        try {
            const detected = this._detect();
            this._advice = detected.runtimeLibraries ?? {};
            if (!detected.worker || detected.issues.length)
                throw new Error(detected.issues.join(' ') || this._('Python audio worker is unavailable.'));
            const {stdout} = await this._runner.run([...detected.worker, '--check-all'],
                {timeoutMs: 3000, maxBytes: 16384});
            if (!this._alive) return;
            this._engines = checkedEngines(JSON.parse(stdout));
            this._commonError = '';
        } catch (error) {
            if (!this._alive) return;
            this._engines = [];
            this._commonError = String(error.message ?? error).replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 800);
        } finally {
            this._checking = false;
            this._checked = true;
            if (this._alive) this._render();
        }
    }

    dispose() {
        if (!this._alive) return;
        this._alive = false;
        this._runner.dispose();
        for (const [object, id] of this._connections) object.disconnect(id);
        this._connections = [];
        this._settings = null;
    }
}
