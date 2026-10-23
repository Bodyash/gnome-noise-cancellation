import {ExtensionPreferences, gettext as _}
    from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';
import {PreferencesView} from './preferences/window.js';

export default class NoiseCancellationPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        window.set_default_size(640, 640);
        const view = new PreferencesView(this.getSettings(), {translate: _});
        window.add(view.page);
        window.connect('close-request', () => { view.dispose(); return false; });
    }
}
