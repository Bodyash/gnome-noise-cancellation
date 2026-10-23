"""Opt-in private Pulse/WirePlumber regression; synthetic audio only."""
import array
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


def probe():
    from streaming import Gst, GLib
    from routing import Registry
    failures = []
    observer = Registry('pulse-observer', lambda _: None, lambda _: None, failures.append)
    observer.start()
    context = GLib.MainContext.default()
    child = None
    activity, pending = '', b''

    def snapshot(): return observer.snapshot()['objects'] if observer._ready else []
    def node(name): return next((o for o in snapshot() if o['kind'] == 'Node' and
        o['props'].get('node.name') == name), None)
    def pump():
        nonlocal activity, pending
        for _ in range(20):
            if not context.pending(): break
            context.iteration(False)
        if failures: raise AssertionError(failures)
        if child:
            try: data = os.read(child.stdout.fileno(), 4096)
            except BlockingIOError: data = b''
            pending += data
            if len(pending) > 4096: raise AssertionError('Excessive private worker status.')
            while b'\n' in pending:
                line, _, pending = pending.partition(b'\n')
                activity = line.decode('ascii')
            if child.poll() not in (None, 0): raise AssertionError('Private worker crashed.')
    def wait(predicate, message):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            pump()
            if predicate(): return
            time.sleep(0.005)
        print('Stalled private graph:', [(o['id'], o['kind'], o.get('state'),
            o['props'].get('node.name'), o['props'].get('link.output.node'),
            o['props'].get('link.input.node')) for o in snapshot() if o['kind'] in ['Node', 'Link']], flush=True)
        raise AssertionError(f'{message} Last activity: {activity}.')
    def start(pipeline):
        threading.Thread(target=lambda: pipeline.set_state(Gst.State.PLAYING), daemon=True).start()
    def pactl(*args):
        subprocess.run(['pactl', *args], check=True, timeout=3, stdout=subprocess.DEVNULL)
    recorder = Gst.parse_launch('pulsesrc name=pulse ! '
        'audio/x-raw,format=F32LE,rate=48000,channels=1 ! '
        'appsink name=recorded sync=false async=false max-buffers=8 drop=true')
    sink = recorder.get_by_name('recorded')
    recorder.get_by_name('pulse').set_property('client-name', 'Synthetic OBS default-input test')
    def audible():
        sample = sink.emit('try-pull-sample', 0)
        if sample is None: return False
        buffer = sample.get_buffer()
        data = array.array('f', buffer.extract_dup(0, buffer.get_size()))
        return any(abs(value) > 0.0001 for value in data)
    try:
        wait(lambda: node('routing_probe.physical'), 'Native synthetic physical source missing.')
        pactl('set-default-source', 'routing_probe.physical')
        start(recorder)
        wait(audible, 'Default Pulse recorder did not receive the synthetic physical source.')
        for cycle in range(8):
            activity, pending = '', b''
            child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('routing_worker.py'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            os.set_blocking(child.stdout.fileno(), False)
            wait(lambda: node('noise_cancellation.clean_microphone'), 'Clean did not register.')
            if cycle == 0: pactl('set-default-source', 'noise_cancellation.clean_microphone')
            wait(lambda: activity == 'processing' and audible(),
                'Default Pulse recorder stayed silent after enabling Clean.')
            recorder.set_state(Gst.State.PAUSED)
            wait(lambda: activity == 'idle', 'Corked Pulse recorder did not let physical capture idle.')
            start(recorder)
            wait(lambda: activity == 'processing' and audible(), 'Pulse recorder stayed silent after uncork.')
            child.stdin.write(b'stop\n'); child.stdin.flush()
            wait(lambda: child.poll() is not None, 'Private handoff worker did not exit.')
            child.wait(timeout=1)
            child.stdin.close(); child.stdout.close(); child = None
            wait(lambda: node('noise_cancellation.clean_microphone') is None, 'Stopped source remained registered.')
            wait(audible, 'Default Pulse recorder stayed silent after returning to physical.')
        print('Private default-input test passed: 8 fresh workers, enable/handoff and idle/resume cycles '
            'with one live Pulse recorder.')
    finally:
        if child:
            child.terminate()
            try: child.wait(timeout=1)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=1)
            child.stdin.close(); child.stdout.close()
        recorder.set_state(Gst.State.NULL)
        observer.close()
