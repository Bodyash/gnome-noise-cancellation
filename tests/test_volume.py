"""Volume proxy regressions; no desktop server or microphone is used."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))
from volume import VolumeBinding, Volume


def level(value, mute=False):
    return Volume((value,), mute)


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.binding = VolumeBinding()

    def initialized(self, source=None):
        source = source or level(0.125)  # GNOME's 50% uses cubic scaling.
        self.assertEqual(self.binding.update(source, level(1), 0), [('clean', source)])
        self.assertEqual(self.binding.update(source, source, 0.1), [])
        self.assertTrue(self.binding.ready)
        return source

    def test_initial_source_wins_over_restored_clean_volume_without_writing_hardware(self):
        source = level(0.125, True)
        self.assertEqual(self.binding.update(source, level(1), 0), [('clean', source)])
        self.assertFalse(self.binding.ready)
        self.assertEqual(self.binding.update(source, level(0.9), 0.1), [])
        self.assertEqual(self.binding.update(source, source, 0.2), [])
        self.assertTrue(self.binding.ready)

    def test_clean_80_percent_changes_source_and_ack_does_not_loop(self):
        source = self.initialized()
        louder = level(0.8 ** 3)
        self.assertEqual(self.binding.update(source, louder, 0.2), [('source', louder)])
        self.assertEqual(self.binding.update(louder, louder, 0.3), [])
        for index in range(20):
            self.assertEqual(self.binding.update(louder, louder, 0.4 + index / 100), [])

    def test_physical_edits_mute_and_zero_are_bidirectional(self):
        source = self.initialized()
        muted = level(source.channels[0], True)
        self.assertEqual(self.binding.update(muted, source, 0.2), [('clean', muted)])
        self.binding.update(muted, muted, 0.3)
        zero = level(0, True)
        self.assertEqual(self.binding.update(muted, zero, 0.4), [('source', zero)])
        self.binding.update(zero, zero, 0.5)
        unmuted = level(0, False)
        self.assertEqual(self.binding.update(zero, unmuted, 0.6), [('source', unmuted)])

    def test_channel_balance_is_preserved(self):
        source = Volume((0.125, 0.0625), False)
        self.assertEqual(self.binding.update(source, level(1), 0), [('clean', level(0.125))])
        self.binding.update(source, level(0.125), 0.1)
        self.assertEqual(self.binding.update(source, level(0.512), 0.2),
                         [('source', Volume((0.512, 0.256), False))])

    def test_rapid_clean_edits_ignore_late_source_acknowledgments(self):
        source = self.initialized()
        a, b = level(0.3), level(0.5)
        self.binding.update(source, a, 0.2)
        self.assertEqual(self.binding.update(source, b, 0.21), [])
        self.assertEqual(self.binding.update(a, b, 0.3), [('source', b)])
        self.assertEqual(self.binding.update(b, b, 0.4), [])

    def test_hardware_rounding_is_reflected_in_clean_volume(self):
        source = self.initialized()
        requested, actual = level(0.512), level(0.51)
        self.binding.update(source, requested, 0.2)
        self.assertEqual(self.binding.update(actual, requested, 0.3), [('clean', actual)])
        self.assertEqual(self.binding.update(actual, actual, 0.4), [])

    def test_source_wins_simultaneous_edits(self):
        self.initialized()
        self.assertEqual(self.binding.update(level(0.2), level(0.7), 0.2), [('clean', level(0.2))])

    def test_rapid_physical_edits_coalesce_without_echoing_into_hardware(self):
        source = self.initialized()
        a, b = level(0.3), level(0.5)
        self.assertEqual(self.binding.update(a, source, 0.2), [('clean', a)])
        self.assertEqual(self.binding.update(b, source, 0.21), [])
        self.assertEqual(self.binding.update(b, a, 0.3), [('clean', b)])
        self.assertEqual(self.binding.update(b, b, 0.4), [])

    def test_late_virtual_mirror_cannot_overwrite_a_newer_clean_edit(self):
        source = self.initialized()
        a, b = level(0.3), level(0.5)
        self.binding.update(a, source, 0.2)
        self.assertEqual(self.binding.update(a, b, 0.21), [('source', b)])
        self.assertEqual(self.binding.update(a, a, 0.22), [])
        self.assertEqual(self.binding.update(b, a, 0.3), [('clean', b)])
        self.assertEqual(self.binding.update(b, b, 0.4), [])

    def test_startup_rechecks_source_if_it_changed_before_initial_ack(self):
        self.binding.update(level(0.125), level(1), 0)
        self.assertEqual(self.binding.update(level(0.5), level(0.125), 0.1), [('clean', level(0.5))])
        self.assertFalse(self.binding.ready)
        self.binding.update(level(0.5), level(0.5), 0.2)
        self.assertTrue(self.binding.ready)

    def test_zero_can_be_raised_without_division_by_zero(self):
        source = Volume((0, 0), False)
        self.binding.update(source, level(0), 0)
        self.assertEqual(self.binding.update(source, level(0.5), 0.1),
                         [('source', Volume((0.5, 0.5), False))])

    def test_failed_write_times_out_and_close_permanently_stops_writes(self):
        source = self.initialized()
        self.binding.update(source, level(0.5), 0.2)
        with self.assertRaisesRegex(ValueError, 'volume.*timed out'):
            self.binding.update(source, level(0.5), 2.3)
        self.binding.close()
        self.assertEqual(self.binding.update(level(0.8), level(0.9), 3), [])

    def test_initial_and_mirror_acknowledgments_also_have_deadlines(self):
        self.binding.update(level(0.125), level(1), 0)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.binding.update(level(0.125), level(1), 2.1)
        self.binding = VolumeBinding()
        source = self.initialized()
        self.binding.update(level(0.3), source, 0.2)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.binding.update(level(0.3), source, 2.3)

    def test_many_pending_slider_edits_coalesce_to_one_latest_request(self):
        source = self.initialized()
        first = level(0.2)
        self.binding.update(source, first, 0.2)
        for index in range(500):
            latest = level(0.3 + index / 1000)
            self.assertEqual(self.binding.update(source, latest, 0.3 + index / 1000), [])
        self.assertEqual(self.binding.update(first, latest, 1), [('source', latest)])
        self.assertEqual(self.binding.update(latest, latest, 1.1), [])

    def test_malformed_and_unbounded_levels_are_rejected(self):
        for channels, mute in [((), False), ((float('nan'),), False), ((-1,), False),
                               ((11,), False), ((True,), False), ((1,) * 65, False), ((1,), 1)]:
            with self.subTest(channels=channels, mute=mute), self.assertRaises(ValueError):
                Volume(channels, mute)
