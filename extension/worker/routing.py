"""WirePlumber introspection: monitor incarnations and own one explicit link."""
import gi
gi.require_version('Wp', '0.5')
from gi.repository import GLib, GObject, Wp
from lifecycle import STABLE
from mixer import Mixer
from handoff import DefaultHandoff, DefaultMetadata

KEYS = STABLE + ['object.serial', 'device.api', 'device.id', 'media.class', 'node.name',
    'node.virtual', 'stream.monitor', 'application.name', 'application.process.binary',
    'application.process.id', 'node.id', 'client.id', 'port.direction', 'port.monitor',
    'audio.channel', 'noise-cancellation.session', 'session.services', 'wireplumber.client-context']
FEATURES = Wp.ProxyFeatures.PROXY_FEATURE_BOUND | Wp.ProxyFeatures.PIPEWIRE_OBJECT_FEATURE_INFO


class Registry:
    def __init__(self, token, update, removed, fault):
        Wp.init(Wp.InitFlags.PIPEWIRE | Wp.InitFlags.SPA_TYPES)
        self.token, self.update, self.removed, self.fault = token, update, removed, fault
        self._closed = self._ready = False
        self._objects, self._ids, self._signals = {}, {}, []
        self._pending = 0
        self.link = None
        self.link_id = None
        self.handoff = None
        self._metadata_views = {}
        props = Wp.Properties.new_empty()
        props.set('application.name', 'Noise Cancellation supervisor')
        self.core = Wp.Core.new(None, None, props)
        self.mixer = Mixer(self.core, self.changed, self._failure, self._signal)
        self.om = Wp.ObjectManager.new()
        for cls in [Wp.Node, Wp.Device, Wp.Client, Wp.Port, Wp.Link]:
            self.om.add_interest_full(Wp.ObjectInterest.new_type(cls.__gtype__))
            self.om.request_object_features(cls.__gtype__, FEATURES)
        self._signal(self.om, 'object-added', self._added)
        self._signal(self.om, 'object-removed', self._removed)
        self._signal(self.om, 'objects-changed', lambda *_: self.changed())
        self._signal(self.om, 'installed', self._installed)
        self._signal(self.core, 'disconnected', lambda *_: self._failure('PipeWire disconnected; select the microphone again.'))
        self.core.install_object_manager(self.om)
        # Attach the metadata listener before requesting BOUND/DATA. The initial
        # server replay can otherwise arrive before an object-added callback.
        self._metadata_manager = Wp.ObjectManager.new()
        self._metadata_manager.add_interest_full(Wp.ObjectInterest.new_type(Wp.Metadata.__gtype__))
        self._signal(self._metadata_manager, 'object-added', self._metadata_added)
        self._signal(self._metadata_manager, 'object-removed', self._metadata_removed)
        self.core.install_object_manager(self._metadata_manager)

    def _signal(self, obj, name, callback):
        ident = GObject.Object.connect(obj, name, callback)
        self._signals.append((obj, ident))

    def start(self):
        self.core.activate(Wp.CoreFeatures.CONNECTED, None, self._connected)

    def _connected(self, core, result, *_args):
        if self._closed: return
        try:
            core.activate_finish(result)
            self.mixer.start()
        except Exception as error: self._failure('Cannot connect to the user PipeWire server.', error)

    # A server round trip avoids treating the initial partial registry as a complete graph.
    def _installed(self, *_args):
        def synced(core, result, *_data):
            if self._closed: return
            try:
                core.sync_finish(result)
                self._ready = True
                self.changed()
            except Exception as error: self._failure('Cannot establish an initial PipeWire graph snapshot.', error)
        self.core.sync(None, synced)

    def _added(self, _om, obj):
        if self._closed: return
        ident = obj.get_bound_id()
        if ident >= 0xffffffff or ident in self._objects or len(self._objects) >= 4096:
            self._failure('Ambiguous or excessive PipeWire registry objects.')
            return
        self._objects[ident], self._ids[obj] = obj, ident
        self._signal(obj, 'notify::properties', lambda *_: self.changed())
        if isinstance(obj, (Wp.Link, Wp.Node)): self._signal(obj, 'state-changed', lambda *_: self.changed())
        self.changed()

    def _removed(self, _om, obj):
        if self._closed: return
        ident = self._ids.pop(obj, None)
        if ident is not None:
            self._objects.pop(ident, None)
            self.removed(ident)  # revoke before a coalesced snapshot can hide removal/reappearance
        remaining = []
        for target, handler in self._signals:
            if target == obj:
                if GObject.Object.handler_is_connected(target, handler):
                    GObject.Object.disconnect(target, handler)
            else: remaining.append((target, handler))
        self._signals = remaining
        self.changed()

    # Coalesce registry bursts; removal callbacks still revoke before this deferred snapshot.
    def changed(self):
        if not self._closed and self._ready and not self._pending:
            def run():
                self._pending = 0
                if not self._closed:
                    try:
                        snapshot = self.snapshot()
                        self.update(snapshot)
                        if self.handoff and not self._closed: self.handoff.poll(snapshot)
                    except Exception as error: self._failure('Cannot validate the current audio graph.', error)
                return GLib.SOURCE_REMOVE
            self._pending = GLib.idle_add(run)

    def snapshot(self):
        objects = []
        for ident, obj in self._objects.items():
            kind = next(name for cls, name in [(Wp.Node, 'Node'), (Wp.Device, 'Device'),
                (Wp.Client, 'Client'), (Wp.Port, 'Port'), (Wp.Link, 'Link')] if isinstance(obj, cls))
            properties = Wp.PipewireObject.get_properties(obj)
            global_properties = obj.get_global_properties()
            props = {}
            for key in KEYS:
                value = properties.get(key)
                if value is None: value = global_properties.get(key)
                if value is not None: props[key] = value
            entry = {'id': ident, 'kind': kind, 'props': props}
            if isinstance(obj, Wp.Node): entry['state'] = obj.get_property('state').value_nick
            if isinstance(obj, Wp.Link):
                state, _error = obj.get_state()
                entry['state'] = state.value_nick
                endpoints = obj.get_linked_object_ids()
                for key, value in zip(['link.output.node', 'link.output.port', 'link.input.node', 'link.input.port'], endpoints):
                    props[key] = str(value)
            objects.append(entry)
        return {'cookie': self.core.get_remote_cookie(), 'objects': objects}

    def create_link(self, source, capture):
        if self._closed or self.link is not None:
            raise ValueError('Owned capture link creation was cancelled or repeated.')
        props = Wp.Properties.new_empty()
        # Non-lingering ownership lets connection teardown remove the link even after forced exit.
        for key, value in {'link.output.port': source, 'link.input.port': capture,
            'object.linger': 'false', 'link.passive': 'false', 'noise-cancellation.session': self.token}.items():
            props.set(key, str(value))
        self.link = Wp.Link.new_from_factory(self.core, 'link-factory', props)
        if self.link is None: raise RuntimeError('Cannot create the explicit microphone link.')
        def activated(link, result, *_args):
            if self._closed: return
            try:
                link.activate_finish(result)
                self.link_id = link.get_bound_id()
                self.changed()
            except Exception as error: self._failure('Explicit microphone connection failed.', error)
        self.link.activate(FEATURES, None, activated)

    def tick(self):
        if self._ready and not self._closed:
            try:
                snapshot = self.snapshot()
                self.update(snapshot)
                if self.handoff and not self._closed: self.handoff.poll(snapshot)
            except Exception as error: self._failure('Audio graph validation failed.', error)

    def _default_metadata(self):
        candidates = list(self._metadata_views.values())
        return candidates[0] if len(candidates) == 1 and candidates[0].ready else None

    def _metadata_added(self, _om, obj):
        if (self._closed or obj.get_global_properties().get('metadata.name') != 'default' or
                len(self._metadata_views) >= 32): return
        view = self._metadata_views[obj] = DefaultMetadata(obj)
        self._signal(obj, 'changed', lambda _obj, subject, key, kind, value:
            view.update(subject, key, kind, value))
        def activated(metadata, result, *_args):
            if self._closed or self._metadata_views.get(metadata) is not view: return
            try:
                metadata.activate_finish(result)
                view.ready = True
                self.changed()
            except Exception: pass  # Optional handoff remains unavailable; cleanup still works.
        obj.activate(Wp.ProxyFeatures.PROXY_FEATURE_BOUND | Wp.MetadataFeatures.DATA, None, activated)

    def _metadata_removed(self, _om, obj):
        self._metadata_views.pop(obj, None)
        remaining = []
        for target, handler in self._signals:
            if target == obj:
                if GObject.Object.handler_is_connected(target, handler): GObject.Object.disconnect(target, handler)
            else: remaining.append((target, handler))
        self._signals = remaining

    def begin_handoff(self, session, done):
        if self._closed: done(False); return
        def sync(callback):
            def synced(core, result, *_args):
                if self._closed: return
                try: success = core.sync_finish(result)
                except Exception: success = False
                callback(success)
            self.core.sync(None, synced)
        self.handoff = DefaultHandoff(session.capture_id, session.handoff_source,
            self._default_metadata, sync, done, output_id=session.output_id)
        self.handoff.poll(self.snapshot())

    def _failure(self, message, error=None):
        if error is not None:
            detail = ''.join(c for c in str(error) if c.isprintable())[:160]
            message += f' {type(error).__name__}: {detail}'
        if not self._closed: self.fault(message)

    def close(self):
        if self._closed: return
        self._closed = True
        if self.handoff: self.handoff.close()
        if self._pending: GLib.source_remove(self._pending); self._pending = 0
        for obj, ident in self._signals:
            if GObject.Object.handler_is_connected(obj, ident): GObject.Object.disconnect(obj, ident)
        self._signals.clear()
        self.mixer.close()
        if self.link:
            try: self.link.request_destroy()
            except Exception: pass  # connection teardown also releases the non-lingering link
            self.link = None
        self.core.disconnect()
        self._metadata_views.clear()
        self._objects.clear(); self._ids.clear()
