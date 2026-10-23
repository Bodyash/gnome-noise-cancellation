"""WirePlumber's distro mixer API, scoped to the worker's authorized session."""
import time

import gi
gi.require_version('Wp', '0.5')
from gi.repository import Gio, GLib, Wp
from volume import Volume, VolumeBinding


class Mixer:
    def __init__(self, core, changed, fault, signal):
        if Wp.base_dirs_find_file(Wp.BaseDirsFlags.MODULE, None,
                'libwireplumber-module-mixer-api.so') is None:
            raise RuntimeError('WirePlumber mixer API module is missing. Check the installed WirePlumber runtime packages.')
        self.core, self.changed, self.fault, self.signal = core, changed, fault, signal
        self.plugin = None
        self.closed = False
        self.cancel = Gio.Cancellable()
        self.binding = VolumeBinding()
        self.source_id = self.output_id = None
        self._seen = set()
        self._waiting = None

    def start(self):
        self.core.load_component('libwireplumber-module-mixer-api', 'module', None,
            None, self.cancel, self._loaded)

    def _loaded(self, core, result, *_args):
        if self.closed: return
        try:
            core.load_component_finish(result)
            self.plugin = Wp.Plugin.find(core, 'mixer-api')
            if self.plugin is None: raise ValueError('WirePlumber mixer API is unavailable.')
            self.plugin.set_property('scale', 0)  # SPA linear amplitudes; never GNOME cubic percentages.
            self.signal(self.plugin, 'changed', lambda _plugin, ident:
                self.changed() if ident in (self.source_id, self.output_id) else None)
            self.changed()
        except Exception:
            self.fault('Cannot load the WirePlumber mixer API. Check the installed WirePlumber runtime packages.')

    def read(self, ident):
        if self.closed or self.plugin is None: return None
        result = self.plugin.emit('get-volume', ident)
        if result is None:
            if ident in self._seen: raise ValueError('Authorized microphone volume controls disappeared.')
            return None
        data = result.unpack()
        channels = data.get('channelVolumes')
        if not isinstance(channels, dict) or len(channels) > 64 or data.get('id') != ident:
            raise ValueError('Malformed microphone mixer state.')
        if not channels:
            if ident in self._seen: raise ValueError('Authorized microphone volume controls disappeared.')
            return None
        if set(channels) != {str(i) for i in range(len(channels))}:
            raise ValueError('Malformed microphone mixer channels.')
        value = Volume(tuple(channels[str(i)]['volume'] for i in range(len(channels))), data.get('mute'))
        self._seen.add(ident)
        return value

    def initial_volume(self):
        value = self.read(self.source_id)
        if value is None: raise ValueError('Selected microphone volume is not ready.')
        return value

    def sync(self, source_id, output_id=None):
        if self.closed: return False
        if self.source_id is not None and self.source_id != source_id:
            raise ValueError('Cannot rebind microphone volume within an authorized session.')
        if self.output_id is not None and output_id != self.output_id:
            raise ValueError('Cannot replace the owned virtual microphone volume control.')
        self.source_id, self.output_id = source_id, output_id
        source = self.read(source_id)
        clean = self.read(output_id) if output_id is not None else None
        now = time.monotonic()
        if source is None or (output_id is not None and clean is None):
            if self._waiting is None: self._waiting = now
            if now - self._waiting > 2:
                raise ValueError('Microphone volume controls timed out. Check the WirePlumber runtime.')
            return False
        self._waiting = None
        # The lifecycle calls this only after checking current identities/providers
        # and (for the published source) the owned exact capture route.
        for side, value in self.binding.update(source, clean, now):
            ident = source_id if side == 'source' else output_id
            channels = {str(i): GLib.Variant('a{sv}', {'volume': GLib.Variant('d', float(v))})
                        for i, v in enumerate(value.channels)}
            request = GLib.Variant('a{sv}', {'mute': GLib.Variant('b', value.mute),
                'channelVolumes': GLib.Variant('a{sv}', channels)})
            if not self.plugin.emit('set-volume', ident, request):
                raise ValueError('Cannot synchronize the authorized microphone volume.')
        return output_id is None or self.binding.ready

    def close(self):
        if self.closed: return
        self.closed = True
        self.binding.close()
        self.cancel.cancel()
        if self.plugin: self.plugin.deactivate(Wp.PluginFeatures.ENABLED)
        self.plugin = None
        self._seen.clear()
