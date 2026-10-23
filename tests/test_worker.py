"""Source-only worker tests; synthetic audio and fake graph, never the desktop."""
import array
import copy
import ctypes
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))
from dsp import Processor
from lifecycle import Authorization, Session


KEY = ['10', '100', '88', 'alsa_input.usb', 'alsa', [
    ['device.serial', 'USB-123'], ['device.bus-path', None], ['api.bluez5.address', None],
    ['api.alsa.path', None], ['device.vendor.id', None], ['device.product.id', None]]]


def graph():
    return {'cookie': 10, 'objects': [
        {'id': 30, 'kind': 'Device', 'props': {'object.serial': '88', 'device.api': 'alsa', 'device.serial': 'USB-123'}},
        {'id': 42, 'kind': 'Node', 'props': {'object.serial': '100', 'device.id': '30',
            'media.class': 'Audio/Source', 'node.name': 'alsa_input.usb'}},
        {'id': 50, 'kind': 'Client', 'props': {'object.serial': '200', 'application.name': 'WirePlumber'}},
        {'id': 60, 'kind': 'Port', 'props': {'object.serial': '300', 'node.id': '42',
            'port.direction': 'out', 'audio.channel': 'MONO'}},
    ]}


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.frames = 0
        self.revoked = False
        self.capture_requested = self.capture_running = True
        self.capture_idle = False
        self.fresh_capture = True  # Fake frame updates complete synchronously.
        self.resume_baseline = 0
        self.volume_ready = True
        self.volume_calls = []
    def sync_volume(self, source, output=None):
        self.volume_calls.append((source, output))
        return self.volume_ready
    def start_capture(self, authorization, token): self.calls.append('capture')
    def create_link(self, source, capture): self.calls.append(('link', source, capture))
    def start_output(self, token): self.calls.append('output')
    def renew(self): self.calls.append('renew')
    def revoke(self): self.revoked = True
    def close(self): self.calls.append('close')
    def owns_link(self, ident): return ident == 80
    def request_capture(self, enabled):
        if self.revoked: raise ValueError('Revoked')
        if self.capture_requested == enabled: return
        self.capture_requested = self.capture_running = enabled
        self.capture_idle = not enabled
        if enabled: self.resume_baseline = self.frames
        self.calls.append(('capture-enabled', enabled))
    def set_delivery(self, enabled): self.delivery = enabled


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.events = []
        self.session = Session(Authorization(json.dumps(KEY), 42), 'test', self.driver,
                               lambda event, **fields: self.events.append((event, fields)), pid=123)
        self.snapshot = graph()

    def route(self, frames=1):
        self.session.update(self.snapshot)
        self.snapshot['objects'] += [
            {'id': 70, 'kind': 'Client', 'props': {'object.serial': '301', 'application.process.id': '123'}},
            {'id': 71, 'kind': 'Node', 'props': {'object.serial': '302', 'node.name': 'noise_cancellation.test.capture', 'client.id': '70'}},
            {'id': 72, 'kind': 'Port', 'props': {'object.serial': '303', 'node.id': '71', 'port.direction': 'in', 'audio.channel': 'MONO'}},
        ]
        self.session.update(self.snapshot)
        self.snapshot['objects'].append({'id': 80, 'kind': 'Link', 'state': 'active', 'props': {
            'object.serial': '304', 'link.output.node': '42', 'link.output.port': '60',
            'link.input.node': '71', 'link.input.port': '72', 'noise-cancellation.session': 'test'}})
        self.driver.frames = frames
        self.session.update(self.snapshot)

    def published(self):
        self.route()
        self.session.publish()
        self.snapshot['objects'].append({'id': 90, 'kind': 'Node', 'props': {
            'object.serial': '305', 'node.name': 'noise_cancellation.clean_microphone',
            'noise-cancellation.session': 'test', 'client.id': '70', 'media.class': 'Audio/Source'}})
        self.session.update(self.snapshot)

    def consumer(self, ident=91):
        self.snapshot['objects'].extend([
            {'id': ident, 'kind': 'Node', 'props': {'object.serial': str(ident + 500),
                'media.class': 'Stream/Input/Audio', 'node.name': 'obs.capture'}},
            {'id': ident + 1, 'kind': 'Link', 'state': 'active', 'props': {
                'object.serial': str(ident + 600), 'link.output.node': '90',
                'link.input.node': str(ident)}},
        ])

    def test_volume_readiness_gates_capture_and_publication(self):
        self.driver.volume_ready = False
        self.session.update(self.snapshot)
        self.assertNotIn('capture', self.driver.calls)
        self.driver.volume_ready = True
        self.route()
        self.session.publish()
        self.snapshot['objects'].append({'id': 90, 'kind': 'Node', 'props': {
            'object.serial': '305', 'node.name': 'noise_cancellation.clean_microphone',
            'noise-cancellation.session': 'test', 'client.id': '70', 'media.class': 'Audio/Source'}})
        self.driver.volume_ready = False
        self.session.update(self.snapshot)
        self.assertFalse(self.session.active)
        self.assertFalse(self.driver.delivery)
        self.driver.volume_ready = True
        self.session.update(self.snapshot)
        self.assertTrue(self.session.active)
        self.assertEqual(self.driver.volume_calls[-1], (42, 90))

    def test_volume_updates_stop_before_identity_loss_or_after_close(self):
        self.published()
        count = len(self.driver.volume_calls)
        self.snapshot['objects'][1]['props']['object.serial'] = '999'
        self.session.update(self.snapshot)
        self.session.update(self.snapshot)
        self.assertTrue(self.session.closed)
        self.assertEqual(len(self.driver.volume_calls), count)

    def test_processed_frame_count_alone_cannot_prove_fresh_resume_audio(self):
        self.published()
        self.consumer()
        self.session.update(self.snapshot)
        self.driver.frames += 1
        self.driver.fresh_capture = False
        self.session.update(self.snapshot)
        self.assertEqual(self.session.activity, 'resuming')
        self.assertFalse(self.driver.delivery)
        self.driver.fresh_capture = True
        self.session.update(self.snapshot)
        self.assertEqual(self.session.activity, 'processing')
        self.assertTrue(self.driver.delivery)

    def test_idle_source_stays_published_and_system_default_is_not_demand(self):
        self.published()
        self.assertFalse(self.driver.capture_requested)
        self.assertFalse(self.driver.delivery)
        self.assertTrue(self.session.active)
        self.assertEqual(self.events[-1][0], 'activity')
        self.assertEqual(self.events[-1][1]['state'], 'idle')
        self.snapshot['defaultSource'] = 'noise_cancellation.clean_microphone'
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.capture_requested)
        self.assertNotIn('close', self.driver.calls)

    def test_consumers_resume_only_verified_fresh_processing_then_suspend(self):
        self.published(); self.consumer()
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.capture_requested)
        self.assertFalse(self.driver.delivery)
        self.assertEqual(self.events[-1][1]['state'], 'resuming')
        self.driver.frames += 1
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.delivery)
        self.assertEqual(self.events[-1][1]['state'], 'processing')
        self.snapshot['objects'] = [o for o in self.snapshot['objects'] if o['id'] not in [91, 92]]
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.capture_requested)
        self.assertEqual(self.events[-1][1]['state'], 'idle')

    def test_multiple_consumers_keep_capture_until_the_last_disconnects(self):
        self.published(); self.consumer(); self.consumer(93)
        self.session.update(self.snapshot); self.driver.frames += 1
        self.session.update(self.snapshot)
        self.snapshot['objects'] = [o for o in self.snapshot['objects'] if o['id'] not in [91, 92]]
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.capture_requested)
        self.assertTrue(self.driver.delivery)

    def test_paused_consumer_does_not_capture_and_uncork_resumes(self):
        self.published(); self.consumer()
        consumer_link = next(o for o in self.snapshot['objects'] if o['id'] == 92)
        consumer_link['state'] = 'paused'
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.capture_requested)
        consumer_link['state'] = 'active'
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.capture_requested)
        consumer_link['state'] = 'paused'
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.capture_requested)

    def test_intentional_idle_allows_paused_link_but_not_removed_link(self):
        self.published()
        link = next(o for o in self.snapshot['objects'] if o['id'] == 80)
        link['state'] = 'paused'
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.revoked)
        self.session.removed(80)
        self.assertTrue(self.driver.revoked)

    def test_idle_unplug_never_resumes_when_consumer_or_microphone_returns(self):
        self.published(); self.session.removed(42)
        self.consumer(); self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)
        self.assertFalse(self.driver.capture_requested)
        self.assertNotIn(('capture-enabled', True), self.driver.calls)

    def test_idle_still_rejects_foreign_input_and_virtual_source_loss(self):
        for change in ['foreign-input', 'output-removed']:
            with self.subTest(change=change):
                self.setUp(); self.published()
                if change == 'foreign-input':
                    extra = copy.deepcopy(next(o for o in self.snapshot['objects'] if o['id'] == 80))
                    extra['id'] = 99; extra['props']['link.output.node'] = '999'
                    self.snapshot['objects'].append(extra)
                    self.session.update(self.snapshot)
                else: self.session.removed(90)
                self.assertTrue(self.driver.revoked)

    def test_resume_waits_for_active_link_and_transition_timeout_fails_closed(self):
        self.published(); self.consumer()
        link = next(o for o in self.snapshot['objects'] if o['id'] == 80)
        link['state'] = 'paused'
        with patch('lifecycle.time.monotonic', return_value=1): self.session.update(self.snapshot)
        self.driver.frames += 1
        with patch('lifecycle.time.monotonic', return_value=2): self.session.update(self.snapshot)
        self.assertFalse(self.driver.delivery)
        self.assertFalse(self.driver.revoked)
        with patch('lifecycle.time.monotonic', return_value=10): self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)
        message = self.events[-1][1]['message']
        self.assertIn('timed out', message)
        self.assertIn('link=paused', message)
        self.assertIn('fresh-frame=True', message)

    def test_consumer_churn_cannot_extend_a_blocked_transition_indefinitely(self):
        self.published(); self.consumer()
        self.driver.request_capture = lambda enabled: setattr(self.driver, 'capture_requested', enabled)
        self.driver.capture_running = self.driver.capture_idle = False
        link = next(o for o in self.snapshot['objects'] if o['id'] == 92)
        with patch('lifecycle.time.monotonic', return_value=1): self.session.update(self.snapshot)
        link['state'] = 'paused'
        with patch('lifecycle.time.monotonic', return_value=4): self.session.update(self.snapshot)
        link['state'] = 'active'
        with patch('lifecycle.time.monotonic', return_value=10): self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)

    def test_active_link_without_processed_frames_cannot_prepare(self):
        self.route(frames=0)
        self.assertEqual(self.events, [])
        with self.assertRaises(ValueError): self.session.publish()
        self.driver.frames = 1
        self.session.update(self.snapshot)
        self.assertEqual(self.events[0][0], 'prepared')

    def test_no_publication_before_real_processing_and_route(self):
        self.session.update(self.snapshot)
        self.assertEqual(self.events, [])
        with self.assertRaises(ValueError): self.session.publish()

    def test_prepare_publish_cleanup(self):
        self.route()
        self.assertEqual(self.events[0][0], 'prepared')
        self.assertNotIn('output', self.driver.calls)
        self.session.publish()
        self.snapshot['objects'].append({'id': 90, 'kind': 'Node', 'props': {
            'object.serial': '305', 'node.name': 'noise_cancellation.clean_microphone', 'client.id': '70',
            'noise-cancellation.session': 'test',
            'media.class': 'Audio/Source'}})
        self.session.update(self.snapshot)
        self.assertIn('active', [event for event, _ in self.events])
        self.assertEqual(self.events[-1][1]['captureLinkId'], 80)
        self.session.close(); self.session.close()
        self.assertTrue(self.driver.revoked)
        self.assertEqual(self.driver.calls.count('close'), 1)

    def test_invalidated_identity_never_resumes(self):
        self.route()
        self.snapshot['objects'][0]['props']['device.serial'] = 'replacement'
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)
        before = copy.deepcopy(self.events)
        self.snapshot = graph()
        self.session.update(self.snapshot)
        with self.assertRaises(ValueError): self.session.publish()
        self.assertEqual(self.events, before)

    def test_required_objects_disappearing_revoke(self):
        for ident in [30, 42, 50, 60, 70, 71, 72, 80]:
            with self.subTest(ident=ident):
                self.setUp(); self.route()
                self.session.removed(ident)
                self.assertTrue(self.driver.revoked)
                self.assertEqual(self.events[-1][0], 'fault')

    def test_server_restart_revoke(self):
        self.route(); self.snapshot['cookie'] = 11; self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)

    def test_unrelated_bluetooth_manager_connections_do_not_revoke_usb(self):
        self.route()
        auxiliary = {'id': 95, 'kind': 'Client', 'props': {
            'object.serial': '900', 'application.name': 'WirePlumber [export]',
            'application.process.binary': 'wireplumber'}}
        self.snapshot['objects'].append(auxiliary)
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.revoked)
        self.session.removed(95)
        self.snapshot['objects'].remove(auxiliary)
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.revoked)

    def test_already_connected_bluetooth_manager_is_not_pinned_for_usb(self):
        main_services = '[audio, api.alsa, policy.linking.standard]'
        for name, services, flag in [
            ('WirePlumber', '[bluetooth.audio, api.bluez]', None),
            # A clone may inherit the primary's service-announcement properties.
            ('WirePlumber [export]', main_services, None),
            ('WirePlumber [client]', main_services, 'true'),
            ('Custom auxiliary connection', main_services, 'true'),
        ]:
            with self.subTest(name=name):
                self.setUp()
                self.snapshot['objects'][2]['props']['session.services'] = main_services
                self.snapshot['objects'].append({'id': 95, 'kind': 'Client', 'props': {
                    'object.serial': '900', 'application.name': name,
                    'application.process.binary': 'wireplumber',
                    'session.services': services, 'wireplumber.client-context': flag}})
                self.route()
                self.session.removed(95)
                self.snapshot['objects'] = [o for o in self.snapshot['objects'] if o['id'] != 95]
                self.session.update(self.snapshot)
                self.assertFalse(self.driver.revoked)

    def test_extra_exactly_named_legacy_client_does_not_replace_pinned_primary(self):
        self.route()
        self.snapshot['objects'].append({'id': 95, 'kind': 'Client', 'props': {
            'object.serial': '900', 'application.name': 'WirePlumber',
            'application.process.binary': 'wireplumber'}})
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.revoked)
        self.session.removed(95)
        self.assertFalse(self.driver.revoked)

    def test_ambiguous_legacy_managers_cannot_start_capture(self):
        self.snapshot['objects'].append({'id': 95, 'kind': 'Client', 'props': {
            'object.serial': '900', 'application.name': 'WirePlumber'}})
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)
        self.assertNotIn('capture', self.driver.calls)

    def test_unrelated_devices_and_default_metadata_do_not_change_authorization(self):
        self.route()
        self.snapshot['objects'].extend([
            {'id': 96, 'kind': 'Device', 'props': {'object.serial': '901', 'device.api': 'bluez5'}},
            {'id': 97, 'kind': 'Node', 'props': {'object.serial': '902', 'device.id': '96',
                'media.class': 'Audio/Source', 'node.name': 'bluez_input.headset'}},
        ])
        self.session.update(self.snapshot)
        self.session.removed(97); self.session.removed(96)
        self.snapshot['objects'] = [o for o in self.snapshot['objects'] if o['id'] not in [96, 97]]
        self.session.update(self.snapshot)
        self.assertFalse(self.driver.revoked)

    def test_relevant_manager_replacement_or_loss_still_revokes(self):
        for change in ['replace', 'remove', 'lose-service']:
            with self.subTest(change=change):
                self.setUp()
                self.snapshot['objects'][2]['props']['session.services'] = '[api.alsa, policy.linking.standard]'
                self.route()
                if change == 'replace': self.snapshot['objects'][2]['props']['object.serial'] = '201'
                if change == 'remove': self.snapshot['objects'].pop(2)
                if change == 'lose-service':
                    self.snapshot['objects'][2]['props']['session.services'] = '[bluetooth.audio]'
                self.session.update(self.snapshot)
                self.assertTrue(self.driver.revoked)

    def test_foreign_capture_link_revoke(self):
        self.route()
        extra = copy.deepcopy(self.snapshot['objects'][-1]); extra['id'] = 81
        extra['props']['link.output.node'] = '999'
        self.snapshot['objects'].append(extra)
        self.session.update(self.snapshot)
        self.assertTrue(self.driver.revoked)

    def test_inactive_link_revoke(self):
        self.route(); self.snapshot['objects'][-1]['state'] = 'paused'
        self.session.update(self.snapshot); self.assertTrue(self.driver.revoked)

    def test_node_incarnation_change_revoke(self):
        self.route(); self.snapshot['objects'][1]['props']['object.serial'] = '101'
        self.session.update(self.snapshot); self.assertTrue(self.driver.revoked)

    def test_parent_or_pipeline_failure_revoke(self):
        self.route(); self.session.fault('Controller lost')
        self.assertTrue(self.driver.revoked)

    def test_strict_authorization(self):
        for key in ['{}', 'null', json.dumps(KEY).replace('USB-123', '\\u0000')]:
            with self.subTest(key=key), self.assertRaises(ValueError): Authorization(key, 42)
        with self.assertRaises(ValueError): Authorization(json.dumps(KEY), True)


class DspTests(unittest.TestCase):
    def test_idle_reset_discards_partial_audio_and_never_reopens_revocation(self):
        with Processor() as processor:
            processor.feed(array.array('f', [0.1] * 479).tobytes())
            processor.reset()
            self.assertEqual(processor.feed(bytes(4)), b'')
            processor.revoke()
            with self.assertRaises(ValueError): processor.reset()

    def test_normalized_samples_match_direct_c_reference(self):
        with Processor() as p:
            reference = p._lib.rnnoise_create(None)
            try:
                source = (ctypes.c_float * 480)(*[0.125 * 32768] * 480)
                expected = (ctypes.c_float * 480)()
                p._lib.rnnoise_process_frame(reference, expected, source)
                output = array.array('f')
                output.frombytes(p.feed(array.array('f', [0.125] * 480).tobytes()))
                for actual, value in zip(output, expected):
                    self.assertAlmostEqual(actual, max(-1, min(1, value / 32768)), places=6)
            finally: p._lib.rnnoise_destroy(reference)

    def test_real_model_split_frames_gate_and_revoke(self):
        with Processor() as p:
            self.assertEqual(p.feed(array.array('f', [0.05] * 479).tobytes()), b'')
            self.assertEqual(len(p.feed(array.array('f', [0.05]).tobytes())), 1920)
            self.assertEqual(p.frames, 1)
            p.revoke()
            with self.assertRaises(ValueError): p.feed(bytes(1920))

    def test_nonfinite_and_bad_buffers_fail_closed(self):
        for data in [b'x', array.array('f', [float('nan')]).tobytes(), bytes(131076)]:
            with self.subTest(size=len(data)), Processor() as p:
                with self.assertRaises(ValueError): p.feed(data)
                with self.assertRaises(ValueError): p.feed(bytes(1920))

    def test_missing_library_has_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, 'RNNoise'):
            Processor(library='/nonexistent-rnnoise-library.so')


if __name__ == '__main__': unittest.main()
