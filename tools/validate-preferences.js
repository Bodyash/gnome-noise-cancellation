import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import Gio from 'gi://Gio';

// Introspection only: no Gtk.init(), display connection, window or desktop writes.
for (const [type, properties] of [
    [Adw.PreferencesPage, ['title', 'icon-name']],
    [Adw.PreferencesGroup, ['title', 'description', 'visible']],
    [Adw.ActionRow, ['title', 'subtitle', 'use-markup', 'subtitle-lines']],
    [Adw.ComboRow, ['selected', 'model', 'use-markup']],
    [Adw.SpinRow, ['value', 'adjustment', 'digits', 'use-markup']],
    [Adw.SwitchRow, ['active', 'use-markup']],
    [Gtk.Button, ['label', 'valign', 'sensitive']],
    [Gtk.Adjustment, ['lower', 'upper', 'step-increment', 'page-increment']],
]) {
    const available = new Set(type.list_properties().map(property => property.name));
    for (const property of properties)
        if (!available.has(property)) throw new Error(`${type.name} lacks ${property}`);
}
for (const [type, method] of [[Adw.PreferencesWindow, 'set_default_size'],
    [Adw.PreferencesWindow, 'add'], [Adw.PreferencesPage, 'add'],
    [Adw.PreferencesGroup, 'add'], [Adw.ActionRow, 'add_suffix']])
    if (typeof type.prototype[method] !== 'function') throw new Error(`${type.name} lacks ${method}`);
if (typeof Gtk.StringList.new !== 'function') throw new Error('Gtk.StringList constructor missing');
const bundle = Gio.Resource.load('/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource');
const source = new TextDecoder().decode(bundle.lookup_data(
    '/org/gnome/Shell/Extensions/js/extensions/prefs.js', 0).toArray());
for (const symbol of ['ExtensionPreferences', 'gettext'])
    if (!source.includes(symbol)) throw new Error(`GNOME preferences resource lacks ${symbol}`);
print('GTK4/libadwaita preferences properties and methods verified from installed introspection.');
print('GNOME preferences host resource is present. No display or UI construction.');
