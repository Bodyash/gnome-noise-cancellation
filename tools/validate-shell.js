import Gio from 'gi://Gio';
import Shell from 'gi://Shell?version=51';

// Resolve a class symbol to load libshell's embedded resources, without starting
// a compositor, constructing Shell.Global or evaluating Shell UI modules.
if (!Shell.Global) throw new Error('Shell 51 typelib unavailable');
const [, bytes] = Gio.File.new_for_path('extension/extension.js').load_contents(null);
const extension = Reflect.parse(new TextDecoder().decode(bytes), {target: 'module'});
let count = 0;
function collectPattern(pattern, exported) {
    if (pattern.type === 'Identifier') exported.add(pattern.name);
    else if (pattern.type === 'ObjectPattern') {
        for (const property of pattern.properties) collectPattern(property.value, exported);
    } else throw new Error(`Unsupported export pattern: ${pattern.type}`);
}
for (const node of extension.body) {
    const uri = node.moduleRequest?.source?.value;
    if (node.type !== 'ImportDeclaration' || !uri?.startsWith('resource:///')) continue;
    const source = new TextDecoder().decode(Gio.resources_lookup_data(
        uri.slice('resource://'.length), Gio.ResourceLookupFlags.NONE).toArray());
    const ast = Reflect.parse(source, {target: 'module'});
    const exported = new Set();
    for (const entry of ast.body.filter(item => item.type === 'ExportDeclaration')) {
        if (entry.declaration?.id) exported.add(entry.declaration.id.name);
        for (const declaration of entry.declaration?.declarations ?? [])
            collectPattern(declaration.id, exported);
        for (const specifier of entry.specifiers ?? [])
            exported.add(specifier.name?.name ?? specifier.id?.name);
    }
    for (const specifier of node.specifiers) {
        if (!exported.has(specifier.id.name))
            throw new Error(`${specifier.id.name} is not exported by ${uri}`);
        count++;
    }
}
print(`${count} Shell imports verified against installed Shell 51 resources.`);
print('Static API check only: no UI construction or desktop activation.');
