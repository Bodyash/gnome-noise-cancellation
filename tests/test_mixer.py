"""Real GLib variant marshalling with a fake mixer, never a server connection."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))
from mixer import Mixer, GLib


def state(ident, value, mute=False):
    return GLib.Variant('a{sv}', {'id': GLib.Variant('u', ident), 'mute': GLib.Variant('b', mute),
        'channelVolumes': GLib.Variant('a{sv}', {'0': GLib.Variant('a{sv}', {
            'volume': GLib.Variant('d', value)})})})


class Plugin:
    def __init__(self):
        self.values = {42: state(42, 0.125), 90: state(90, 1)}
        self.writes = []
        self.accept = True
    def emit(self, action, ident, request=None):
        if action == 'get-volume': return self.values.get(ident)
        self.writes.append((ident, request.unpack()))
        return self.accept
    def deactivate(self, _features): pass


class MixerTests(unittest.TestCase):
    def setUp(self):
        self.mixer = Mixer(None, lambda: None, lambda _: None, lambda *_: None)
        self.plugin = self.mixer.plugin = Plugin()

    def tearDown(self): self.mixer.close()

    def test_linear_variants_are_scoped_to_source_and_owned_output(self):
        self.assertTrue(self.mixer.sync(42))
        self.assertEqual(self.mixer.initial_volume().channels, (0.125,))
        self.assertFalse(self.mixer.sync(42, 90))
        self.assertEqual(self.plugin.writes, [(90, {'mute': False,
            'channelVolumes': {'0': {'volume': 0.125}}})])
        self.plugin.values[90] = state(90, 0.125)
        self.assertTrue(self.mixer.sync(42, 90))
        self.plugin.values[90] = state(90, 0.512, True)
        self.assertTrue(self.mixer.sync(42, 90))
        self.assertEqual(self.plugin.writes[-1], (42, {'mute': True,
            'channelVolumes': {'0': {'volume': 0.512}}}))
        with self.assertRaises(ValueError): self.mixer.sync(43, 90)
        with self.assertRaises(ValueError): self.mixer.sync(42, 91)

    def test_rejected_writes_and_lost_controls_fail_closed(self):
        self.plugin.accept = False
        with self.assertRaisesRegex(ValueError, 'Cannot synchronize'): self.mixer.sync(42, 90)
        self.plugin.values.pop(42)
        with self.assertRaisesRegex(ValueError, 'disappeared'): self.mixer.sync(42, 90)

    def test_close_cancels_loading_and_late_callbacks_without_writes(self):
        self.mixer.close()
        self.assertTrue(self.mixer.cancel.is_cancelled())
        self.mixer._loaded(None, None)
        self.assertFalse(self.mixer.sync(42, 90))
        self.assertEqual(self.plugin.writes, [])

    def test_malformed_native_channels_are_rejected(self):
        self.plugin.values[42] = state(43, 0.5)
        with self.assertRaisesRegex(ValueError, 'Malformed'): self.mixer.sync(42)

    def test_missing_distro_mixer_module_has_actionable_error_without_connecting(self):
        with patch('mixer.Wp.base_dirs_find_file', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'WirePlumber mixer API module is missing'):
                Mixer(None, lambda: None, lambda _: None, lambda *_: None)

    def test_load_failure_is_actionable_and_success_only_schedules_revalidation(self):
        faults, changed, callbacks = [], [], []
        self.mixer.fault = faults.append
        self.mixer.changed = lambda: changed.append(True)
        class Core:
            def load_component_finish(self, _result): raise RuntimeError('missing library')
        self.mixer._loaded(Core(), None)
        self.assertEqual(len(faults), 1)
        self.assertIn('WirePlumber runtime packages', faults[0])
        class LoadedCore:
            def load_component_finish(self, _result): return True
        self.plugin.set_property = lambda *_: None
        self.mixer.signal = lambda _obj, _name, callback: callbacks.append(callback)
        with patch('mixer.Wp.Plugin.find', return_value=self.plugin):
            self.mixer._loaded(LoadedCore(), None)
        self.assertEqual(changed, [True])
        self.mixer.source_id = 42
        callbacks[0](self.plugin, 1000)
        self.assertEqual(changed, [True])
        callbacks[0](self.plugin, 42)
        self.assertEqual(changed, [True, True])
        self.assertEqual(self.plugin.writes, [])
