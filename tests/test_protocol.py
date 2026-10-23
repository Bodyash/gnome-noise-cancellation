"""Actual bundled Python worker: private pipes, synthetic checks, no graph."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'extension/worker/main.py'
ENV = {**os.environ, 'GST_REGISTRY': str(ROOT / 'build/gst-registry.bin'),
       'PIPEWIRE_REMOTE': 'noise-cancellation-nonexistent-test-server',
       'PIPEWIRE_RUNTIME_DIR': str(ROOT / 'build')}


class ProtocolTests(unittest.TestCase):
    def test_self_check_and_missing_python_bindings(self):
        for flags, ready in [([], True), (['-S'], False)]:
            result = subprocess.run([sys.executable, '-B', '-I', *flags, str(WORKER), '--check'],
                capture_output=True, env=ENV, timeout=3, check=True)
            report = json.loads(result.stdout)
            self.assertEqual(report['ready'], ready)
            if not ready: self.assertIn('manually', report['message'])

    def test_all_engine_check_is_structured_and_read_only(self):
        result = subprocess.run([sys.executable, '-B', '-I', str(WORKER), '--check-all'],
            capture_output=True, env=ENV, timeout=3, check=True)
        report = json.loads(result.stdout)
        self.assertTrue(report['ready'])
        self.assertEqual({item['id'] for item in report['backends']}, {'rnnoise', 'speex', 'webrtc'})
        self.assertTrue(all(item['ready'] for item in report['backends']))

    def test_protocol_errors_before_any_connection(self):
        cases = [b'not-json\n', b'{}\n', b'x' * 17000 + b'\n',
            json.dumps({'protocol': 1, 'session': 'test', 'command': 'publish'}).encode() + b'\n',
            json.dumps({'protocol': 999, 'session': 'test', 'command': 'prepare'}).encode() + b'\n',
            json.dumps({'protocol': 1, 'session': '../unsafe', 'command': 'prepare'}).encode() + b'\n',
            json.dumps({'protocol': 1, 'session': 'test', 'command': 'prepare', 'key': '{}', 'sourceId': 42}).encode() + b'\n']
        for request in cases:
            with self.subTest(request=request[:60]):
                with subprocess.Popen([sys.executable, '-B', '-I', str(WORKER), '--session'], env=ENV,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                    try:
                        child.stdin.write(request); child.stdin.flush()
                    except BrokenPipeError: pass
                    ready, _, _ = select.select([child.stdout], [], [], 3)
                    self.assertTrue(ready, 'Protocol fault must arrive before parent EOF')
                    line = child.stdout.readline()
                    out, errors = child.communicate(timeout=3)
                    self.assertNotEqual(child.returncode, 0)
                    self.assertEqual(json.loads(line)['event'], 'fault')
                    self.assertLess(len(line + out), 4096)
                    self.assertLess(len(errors), 16384)

    def test_stdin_eof_exits_without_resources(self):
        result = subprocess.run([sys.executable, '-B', '-I', str(WORKER), '--session'],
            input=b'', capture_output=True, env=ENV, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('revoked', json.loads(result.stdout)['message'])


if __name__ == '__main__': unittest.main()
