"""Worker stop dispatch and cancellation without private pipes or a real graph."""
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('worker_main', ROOT / 'extension/worker/main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WorkerStopTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.pending = [], []
        self.worker = object.__new__(module.Worker)
        worker = self.worker
        worker.closed = worker.stopping = worker.failed = False
        worker.token = 'test'
        worker.last_control = time.monotonic()
        worker.session = SimpleNamespace(active=True, closed=False)
        worker.driver = SimpleNamespace(revoke=lambda: self.calls.append('revoke'),
            close=lambda: self.calls.append('close'),
            stop_for_handoff=lambda session, done: self.pending.append((session, done)))
        worker.loop = SimpleNamespace(quit=lambda: self.calls.append('quit'))
        worker.GLib = SimpleNamespace(SOURCE_REMOVE=False)

    def request(self, **fields):
        return {'protocol': 1, 'session': 'test', 'command': 'stop', 'handoff': True, **fields}

    def test_explicit_stop_revokes_and_freezes_callbacks_before_async_handoff(self):
        self.worker.command(self.request())
        self.assertEqual(self.calls, ['revoke'])
        self.assertTrue(self.worker.session.closed)
        self.assertTrue(self.worker.stopping)
        self.assertFalse(self.worker.closed)
        self.assertEqual(len(self.pending), 1)
        self.pending[0][1]()
        self.assertEqual(self.calls, ['revoke', 'revoke', 'close', 'quit'])
        self.pending[0][1]()
        self.assertEqual(self.calls.count('close'), 1)

    def test_signal_or_fault_during_handoff_immediately_finishes_cleanup(self):
        for fault in (False, True):
            self.setUp()
            self.worker.command(self.request())
            if fault: self.worker.fault('control channel failed')
            else: self.worker._terminate()
            self.assertTrue(self.worker.closed)
            self.pending[0][1]()
            self.assertEqual(self.calls.count('close'), 1)

    def test_unpublished_session_does_not_attempt_default_changes(self):
        self.worker.session.active = False
        self.worker.command(self.request())
        self.assertEqual(self.pending, [])
        self.assertTrue(self.worker.closed)

    def test_stop_requires_session_token_and_explicit_handoff_boolean(self):
        for fields in ({'handoff': False}, {'handoff': 1}, {'session': 'other'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.worker.command(self.request(**fields))
        self.assertEqual(self.calls, [])
        self.worker.command(self.request())
        with self.assertRaises(ValueError): self.worker.command(self.request())
