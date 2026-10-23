"""Default input handoff ordering; no desktop graph, config or Bluetooth access."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))
from handoff import DefaultHandoff, DefaultMetadata, default_name

CLEAN = 'noise_cancellation.clean_microphone'
CURRENT = 'default.audio.source'
CONFIGURED = 'default.configured.audio.source'


class Metadata:
    def __init__(self):
        self.values = {CURRENT: CLEAN, CONFIGURED: CLEAN}
        self.writes = []
    def read(self, key):
        name = self.values.get(key)
        return (json.dumps({'name': name}) if name is not None else None, 'Spa:String:JSON')
    def set(self, subject, key, kind, value):
        self.writes.append((subject, key, kind, json.loads(value)))
        self.values[key] = json.loads(value)['name']


class HandoffTests(unittest.TestCase):
    def test_signal_cache_retains_only_bounded_default_input_keys(self):
        view = DefaultMetadata(self)
        view.update(0, CURRENT, 'Spa:String:JSON', json.dumps({'name': CLEAN}))
        view.update(0, CONFIGURED, 'Spa:String:JSON', json.dumps({'name': CLEAN}))
        for index in range(1000): view.update(index, 'unrelated', 'string', 'x' * 5000)
        self.assertEqual(default_name(view, CURRENT), CLEAN)
        self.assertEqual(set(view.values), {CURRENT, CONFIGURED})
        view.update(0, CURRENT, 'Spa:String:JSON', 'x' * 10000)
        self.assertEqual(len(view.read(CURRENT)[0]), 4097)
        with self.assertRaises(ValueError): default_name(view, CURRENT)
        view.update(0, CURRENT, None, None)
        self.assertIsNone(default_name(view, CURRENT))
        view.update(0, None, None, None)
        self.assertEqual(view.values, {})

    def setUp(self):
        self.metadata = Metadata()
        self.syncs, self.finished = [], []
        self.now = 0
        self.valid = True
        self.handoff = DefaultHandoff(71, lambda _: 'alsa_input.usb' if self.valid else None,
            lambda: self.metadata, self.syncs.append, self.finished.append, clock=lambda: self.now)
        self.snapshot = {'objects': [{'id': 71}]}

    def test_capture_is_removed_before_default_change_and_output_waits_for_ack(self):
        self.handoff.poll(self.snapshot)
        self.assertEqual(self.metadata.writes, [])
        self.snapshot['objects'] = []
        self.handoff.poll(self.snapshot)
        self.assertEqual(self.metadata.writes, [(0, CURRENT, 'Spa:String:JSON', {'name': 'alsa_input.usb'})])
        self.assertEqual(self.metadata.values[CONFIGURED], CLEAN, 'Saved Clean preference must be preserved')
        self.assertEqual(self.finished, [])
        self.syncs[0](True)
        self.assertEqual(self.finished, [True])
        self.handoff.poll(self.snapshot)
        self.assertEqual(len(self.metadata.writes), 1)

    def test_default_following_consumers_can_move_before_clean_source_disappears(self):
        self.handoff.output_id = 90
        consumer = {'id': 91, 'kind': 'Link', 'state': 'active',
            'props': {'link.output.node': '90', 'link.input.node': '92'}}
        self.handoff.poll({'objects': [consumer]})
        self.syncs[0](True)
        self.assertEqual(self.finished, [], 'Metadata acknowledgment alone does not prove OBS left Clean')
        self.handoff.poll({'objects': [consumer]})
        self.assertEqual(self.finished, [])
        moved = {'id': 93, 'kind': 'Link', 'state': 'active',
            'props': {'link.output.node': '42', 'link.input.node': '92'}}
        self.handoff.poll({'objects': [moved]})
        self.assertEqual(self.finished, [True])

    def test_unrelated_default_and_pending_user_selection_are_never_overwritten(self):
        for key in (CURRENT, CONFIGURED):
            with self.subTest(key=key):
                self.setUp()
                self.metadata.values[key] = 'another_source'
                self.handoff.poll({'objects': []})
                self.assertEqual(self.metadata.writes, [])
                self.assertEqual(self.finished, [False])

    def test_identity_loss_missing_metadata_or_malformed_values_skip_handoff(self):
        self.valid = False
        self.handoff.poll({'objects': []})
        self.assertEqual(self.metadata.writes, [])
        self.assertEqual(self.finished, [False])
        self.setUp()
        self.metadata.values[CURRENT] = '\0invalid'
        self.handoff.poll({'objects': []})
        self.assertEqual(self.metadata.writes, [])
        self.assertEqual(self.finished, [False])
        self.setUp()
        self.handoff.metadata = lambda: None
        self.handoff.poll({'objects': []})
        self.assertEqual(self.finished, [False])

    def test_timeout_failure_close_and_late_ack_do_not_delay_or_repeat_cleanup(self):
        self.handoff.poll({'objects': []})
        self.now = 0.5
        self.handoff.poll({'objects': []})
        self.assertEqual(self.finished, [False])
        self.syncs[0](True)
        self.assertEqual(self.finished, [False])
        self.setUp()
        self.handoff.poll({'objects': []})
        self.handoff.close()
        self.syncs[0](True)
        self.assertEqual(self.finished, [])

    def test_rejected_write_or_sync_still_finishes_once(self):
        def fail(*_): raise RuntimeError('denied')
        self.metadata.set = fail
        self.handoff.poll({'objects': []})
        self.assertEqual(self.finished, [False])
        self.setUp()
        self.handoff.poll({'objects': []})
        self.syncs[0](False)
        self.assertEqual(self.finished, [False])

    def test_user_default_change_during_sync_is_preserved(self):
        self.handoff.poll({'objects': []})
        self.metadata.values[CURRENT] = 'another_source'
        self.syncs[0](True)
        self.assertEqual(self.metadata.values[CURRENT], 'another_source')
        self.assertEqual(len(self.metadata.writes), 1)
        self.assertEqual(self.finished, [False])
