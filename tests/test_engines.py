"""Synthetic backend contracts; never connect to a desktop audio server."""
import array
import ctypes
import math
import os
import json
import shutil
import subprocess
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))
os.environ['GST_REGISTRY'] = str(Path(__file__).resolve().parents[1] / 'build/gst-registry.bin')
from engines import configuration, create_processor, check_engines


class EngineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('gjs'), 'GJS is not installed; cross-language defaults cannot be checked')
    def test_js_and_python_defaults_match(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['gjs', '-m', 'tests/settings.js', '--json'], cwd=root,
                                capture_output=True, check=True, timeout=3)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), configuration())

    def test_speex_controls_reach_the_native_api(self):
        with create_processor({'backend': 'speex', 'speexAttenuation': -40, 'speexAgc': True}) as output:
            processor = output.processor
            for request, expected in [(19, -40), (3, 1), (31, 12)]:
                value = ctypes.c_int()
                self.assertEqual(processor.lib.speex_preprocess_ctl(
                    processor._state, request, ctypes.byref(value)), 0)
                self.assertEqual(value.value, expected)

    def test_webrtc_controls_keep_suppression_on_and_echo_off(self):
        with create_processor({'backend': 'webrtc', 'webrtcLevel': 3,
                               'webrtcAgc': True, 'webrtcHighPass': True}) as output:
            dsp = output.processor.dsp
            for name, value in [('noise-suppression', True), ('echo-cancel', False),
                                ('gain-control', True), ('high-pass-filter', True),
                                ('noise-suppression-level', 3)]:
                self.assertEqual(dsp.get_property(name), value)

    def test_configuration_rejects_unknown_and_unsafe_values(self):
        for value in [[], {'backend': 'raw'}, {'speexAgc': 1}, {'outputGainDb': 100},
                      {'webrtcLevel': -1}, {'speexAttenuation': 0}, {'echoCancel': True},
                      {'outputGainDb': float('nan')}, {'speexTarget': 0}]:
            with self.subTest(value=value), self.assertRaises(ValueError): configuration(value)

    def test_all_engines_frame_reset_revoke_and_cleanup(self):
        for backend in ['rnnoise', 'speex', 'webrtc']:
            with self.subTest(backend=backend), create_processor({'backend': backend}) as processor:
                self.assertEqual(processor.feed(bytes(479 * 4)), b'')
                self.assertEqual(len(processor.feed(bytes(4))), 1920)
                self.assertEqual(processor.frames, 1)
                processor.feed(bytes(100)); processor.reset()
                self.assertEqual(processor.feed(bytes(4)), b'')
                processor.revoke()
                with self.assertRaises(ValueError): processor.reset()
                with self.assertRaises(ValueError): processor.feed(bytes(1920))
                processor.close(); processor.close()

    def test_synthetic_settings_and_finite_output(self):
        cases = [{'backend': 'rnnoise', 'outputGainDb': 6},
                 {'backend': 'speex', 'speexAgc': True, 'speexTarget': 12000,
                  'speexAttenuation': -40},
                 *[{'backend': 'webrtc', 'webrtcLevel': level, 'webrtcAgc': True,
                    'webrtcHighPass': True, 'webrtcTarget': 6} for level in range(4)]]
        data = array.array('f', [0.1 * math.sin(i / 20) for i in range(1440)]).tobytes()
        for config in cases:
            with self.subTest(config=config), create_processor(config) as processor:
                out = array.array('f'); out.frombytes(processor.feed(data))
                self.assertEqual(len(out), 1440)
                self.assertTrue(all(math.isfinite(v) and abs(v) <= 1 for v in out))
                self.assertEqual(processor.frames, 3)

    def test_bad_audio_permanently_revokes_every_engine(self):
        for backend in ['rnnoise', 'speex', 'webrtc']:
            for data in [b'x', array.array('f', [float('nan')]).tobytes(), bytes(131076)]:
                with self.subTest(backend=backend), create_processor({'backend': backend}) as processor:
                    with self.assertRaises(ValueError): processor.feed(data)
                    self.assertTrue(processor.revoked)

    def test_missing_engine_is_independent_and_no_fallback(self):
        original = ctypes.util.find_library
        with patch('ctypes.util.find_library', side_effect=lambda name: None if name == 'rnnoise' else original(name)):
            checks = check_engines()
            self.assertFalse(next(c for c in checks if c['id'] == 'rnnoise')['ready'])
            self.assertTrue(next(c for c in checks if c['id'] == 'speex')['ready'])
            with self.assertRaises(RuntimeError): create_processor({'backend': 'rnnoise'})
        with patch('engines.WebRTC', side_effect=RuntimeError('WebRTC missing')):
            checks = check_engines()
            self.assertFalse(next(c for c in checks if c['id'] == 'webrtc')['ready'])
            self.assertTrue(next(c for c in checks if c['id'] == 'rnnoise')['ready'])
        with patch('ctypes.util.find_library', return_value=None):
            checks = check_engines()
            self.assertTrue(next(c for c in checks if c['id'] == 'webrtc')['ready'])
            self.assertFalse(next(c for c in checks if c['id'] == 'speex')['ready'])


if __name__ == '__main__': unittest.main()
