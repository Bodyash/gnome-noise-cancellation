import Gio from 'gi://Gio';

// Execute the actual unload method with fake Shell objects. Resource imports
// require a compositor, so remove only import declarations from the parsed source.
// This fixture is development-only and never enters the extension ZIP.
const [, bytes] = Gio.File.new_for_path('extension/extension.js').load_contents(null);
const source = new TextDecoder().decode(bytes);
const imports = Reflect.parse(source, {target: 'module'}).body
    .filter(node => node.type === 'ImportDeclaration').map(node => node.loc);
const isolated = source.split('\n').map((line, index) =>
    imports.some(loc => index + 1 >= loc.start.line && index + 1 <= loc.end.line) ? '' : line)
    .join('\n').replace('export default class NoiseCancellationExtension', 'return class NoiseCancellationExtension');
function assert(value, message) { if (!value) throw new Error(message); }
function fixture() {
    const events = [], errors = [], warnings = [];
    const Extension = new Function('Extension', 'GLib', 'console', isolated)(class {},
        {source_remove: () => events.push('timer')},
        {error: message => errors.push(message), warn: message => warnings.push(message)});
    const view = new Extension();
    view._controller = {dispose() { events.push('terminate'); return Promise.resolve(); }};
    view._restoreTimer = 10;
    view._settingsSignal = 20;
    view._toggleSignal = 21;
    view._menuSignal = 22;
    view._warnings = new Set();
    view._settings = {disconnect() { events.push('settings'); }};
    view._processingBinding = {dispose() { events.push('binding'); }};
    view._unsubscribe = () => events.push('unsubscribe');
    view._toggle = {disconnect(id) { assert(id === 21, 'Wrong tile signal ID'); events.push('toggle-signal'); },
        menu: {disconnect(id) { assert(id === 22, 'Wrong menu signal ID'); events.push('menu-signal'); }},
        destroy() { events.push('tile'); }};
    view._indicator = {quickSettingsItems: [view._toggle],
        destroy() { events.push('indicator'); }};
    return {view, events, errors, warnings};
}
let cases = 0;
{
    const {view, events} = fixture();
    let finish;
    view._controller.dispose = () => { events.push('terminate'); return new Promise(resolve => { finish = resolve; }); };
    view.disable();
    assert(events[0] === 'terminate', 'Worker termination must precede fallible desktop cleanup');
    assert(!view._controller && !view._indicator && !view._restoreTimer && !view._settingsSignal,
        'Unload retained desktop resources');
    assert(!view._toggleSignal && !view._menuSignal && !view._warnings,
        'Unload retained signal IDs or diagnostic history');
    for (const signal of ['toggle-signal', 'menu-signal'])
        assert(events.includes(signal) && events.indexOf(signal) < events.indexOf('tile'),
            'Tile/menu signal must disconnect before actor destruction');
    let completed = false;
    view._shutdown.then(() => { completed = true; });
    await Promise.resolve(); assert(!completed, 'Unload lost asynchronous reaping');
    finish(); await view._shutdown;
    view.disable(); await view._shutdown;
    assert(events.filter(event => event === 'terminate').length === 1, 'Repeated unload terminated twice');
    assert(events.filter(event => event.endsWith('-signal')).length === 2, 'Repeated unload disconnected twice');
    print('ok - actual extension unload terminates before UI cleanup and retains only the reap barrier'); cases++;
}
for (const failure of ['settings', 'tile', 'toggle', 'menu']) {
    const {view, events, errors} = fixture();
    if (failure === 'settings') view._settings.disconnect = () => { throw new Error('Broken signal'); };
    else if (failure === 'tile') view._toggle.destroy = () => { throw new Error('Broken actor'); };
    else if (failure === 'toggle') view._toggle.disconnect = () => { throw new Error('Broken tile signal'); };
    else view._toggle.menu.disconnect = () => { throw new Error('Broken menu signal'); };
    view.disable(); await view._shutdown;
    assert(events.includes('terminate') && events.includes('indicator') && !view._indicator && !view._settings,
        'A desktop cleanup exception prevented complete worker/UI teardown');
    assert(errors.length === 1, 'Cleanup failure must produce one bounded diagnostic');
    print(`ok - ${failure} cleanup failure cannot skip worker termination or remaining teardown`); cases++;
}
{
    const {view, events, errors} = fixture();
    view._controller.dispose = () => { throw new Error('Cannot confirm termination'); };
    view.disable();
    let rejected = false;
    try { await view._shutdown; } catch { rejected = true; }
    assert(rejected && !view._controller && events.includes('indicator') && errors.length === 1,
        'Failed shutdown lost its re-enable barrier or prevented UI cleanup');
    print('ok - failed extension shutdown retains a rejected barrier and still releases UI'); cases++;
}
{
    const {view, warnings} = fixture();
    for (let i = 0; i < 20; i++) view._warnOnce('preference-save', 'Could not save preference.');
    view._warnOnce('processing-settings', 'Invalid settings.');
    assert(warnings.length === 2 && warnings.every(value => value.startsWith('[NoiseCancellation]')),
        'Repeated diagnostics must be bounded per category and retain the log prefix');
    print('ok - repeated warnings emit once per category per extension lifecycle'); cases++;
}
{
    const {view, warnings} = fixture();
    view._warnOnce('preference-save', 'Before unload.');
    view.disable(); await view._shutdown;
    view._warnOnce('preference-save', 'Late callback.');
    assert(warnings.length === 1, 'A late callback logged after extension unload');
    print('ok - unload releases diagnostic history and rejects late warnings'); cases++;
}
print(`${cases}/${cases} extension unload wiring cases passed`);
