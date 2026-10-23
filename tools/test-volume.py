"""Opt-in synthetic volume test on an owned, hardware-free PipeWire server.

Never connects to the desktop server, starts WirePlumber policy, or loads devices.
Requires existing PipeWire/GStreamer/Wp runtimes and permission to bind a private
Unix socket. All configuration, logs and sockets stay under build/.
"""
import array
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CONFIG = '''
context.properties = { core.daemon = true core.name = pw mem.allow-mlock = false }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-access args = { access.force = unrestricted } }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-link-factory }
]
context.objects = [
    { factory = spa-node-factory args = { factory.name = support.node.driver node.name = Driver } }
]
'''
CLIENT_CONFIG = '''
context.properties = { mem.allow-mlock = false }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
]
'''


def probe():
    # Parent sets every endpoint to its freshly created private server.
    sys.path.insert(0, str(ROOT / 'extension/worker'))
    from streaming import Gst, GLib, structure, make_capture, make_output
    from routing import Registry
    faults, snapshots, pipelines = [], [], []
    registry = Registry('volume-probe', snapshots.append, lambda _: None, faults.append)
    registry.start()
    context = GLib.MainContext.default()

    def wait(predicate, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for _ in range(20):
                if not context.pending(): break
                context.iteration(False)
            if faults: raise AssertionError(faults)
            if predicate(): return
            time.sleep(0.005)
        raise AssertionError(message)

    def node(name):
        return next((o for o in (snapshots[-1]['objects'] if snapshots else [])
            if o['kind'] == 'Node' and o['props'].get('node.name') == name), None)

    def start(pipeline):
        pipelines.append(pipeline)
        threading.Thread(target=lambda: pipeline.set_state(Gst.State.PLAYING), daemon=True).start()

    def set_level(ident, value, mute=False):
        request = GLib.Variant('a{sv}', {'volume': GLib.Variant('d', value),
            'mute': GLib.Variant('b', mute)})
        assert registry.mixer.plugin.emit('set-volume', ident, request)

    try:
        physical = Gst.parse_launch('audiotestsrc is-live=true wave=sine ! '
            'audio/x-raw,format=F32LE,rate=48000,channels=1 ! pipewiresink name=source mode=provide')
        physical.get_by_name('source').set_property('stream-properties', structure({
            'node.name': 'volume_probe.physical', 'media.class': 'Audio/Source', 'node.virtual': 'true',
            'node.autoconnect': 'false', 'audio.position': '[ MONO ]',
            'adapter.auto-port-config': '{ mode = dsp monitor = false control = false position = preserve }'}))
        start(physical)
        wait(lambda: node('volume_probe.physical') and registry.mixer.plugin is not None,
             'Synthetic source/mixer did not register.')
        source_id = node('volume_probe.physical')['id']
        wait(lambda: registry.mixer.read(source_id) is not None, 'Source volume not ready.')
        set_level(source_id, 0.125)
        wait(lambda: abs(registry.mixer.read(source_id).channels[0] - 0.125) < 1e-5, 'Source write not acknowledged.')
        registry.mixer.sync(source_id)
        output, app, _sink = make_output('volume-probe', registry.mixer.initial_volume())
        position = 0
        signal = array.array('f', [0.2 * math.sin(2 * math.pi * i / 48) for i in range(480)]).tobytes()

        def push(_app, _size):
            nonlocal position
            buffer = Gst.Buffer.new_allocate(None, len(signal), None)
            buffer.fill(0, signal)
            buffer.pts = position * Gst.SECOND // 48000
            buffer.duration = 480 * Gst.SECOND // 48000
            position += 480
            app.emit('push-buffer', buffer)

        app.connect('need-data', push)
        start(output)
        wait(lambda: node('noise_cancellation.clean_microphone'), 'Clean source did not register.')
        clean = node('noise_cancellation.clean_microphone')
        clean_id = clean['id']
        wait(lambda: registry.mixer.read(clean_id) is not None, 'Clean volume not ready.')
        assert abs(registry.mixer.read(clean_id).channels[0] - 0.125) < 1e-5, 'Clean source did not start at 50%.'

        def sync(): return registry.mixer.sync(source_id, clean_id)

        wait(sync, 'Initial clean volume was not acknowledged.')
        assert abs(registry.mixer.read(clean_id).channels[0] - 0.125) < 1e-5
        capture, _source, sink = make_capture('volume-probe', clean['props']['object.serial'])
        start(capture)

        def ports():
            objects = snapshots[-1]['objects']
            capture_node = node('noise_cancellation.volume-probe.capture')
            if capture_node is None: return None
            output_port = next((o['id'] for o in objects if o['kind'] == 'Port' and
                o['props'].get('node.id') == str(clean_id) and o['props'].get('port.direction') == 'out'), None)
            input_port = next((o['id'] for o in objects if o['kind'] == 'Port' and
                o['props'].get('node.id') == str(capture_node['id']) and o['props'].get('port.direction') == 'in'), None)
            return (output_port, input_port) if output_port is not None and input_port is not None else None

        wait(ports, 'Synthetic consumer ports did not register.')
        registry.create_link(*ports())

        def amplitude():
            samples = []
            until = time.monotonic() + 0.15
            while time.monotonic() < until:
                sample = sink.emit('try-pull-sample', 0)
                if sample:
                    data = array.array('f')
                    buffer = sample.get_buffer()
                    data.frombytes(buffer.extract_dup(0, buffer.get_size()))
                    samples.extend(data)
                sync()
                for _ in range(10):
                    if not context.pending(): break
                    context.iteration(False)
                time.sleep(0.002)
            return max((abs(v) for v in samples), default=0)

        wait(lambda: amplitude() > 0.19, 'Native output was silent or attenuated twice at 50%.')
        set_level(clean_id, 0.512)
        wait(lambda: (sync() and abs(registry.mixer.read(source_id).channels[0] - 0.512) < 1e-5),
             'Clean 80% did not propagate to the synthetic physical source.')
        assert 0.19 < amplitude() < 0.21, 'Virtual volume applied a second gain.'
        set_level(source_id, 0.027, True)
        wait(lambda: (sync() and registry.mixer.read(clean_id).mute and
            abs(registry.mixer.read(clean_id).channels[0] - 0.027) < 1e-5), 'Physical volume/mute did not mirror.')
        amplitude()  # drain already queued audio after mute
        assert amplitude() == 0, 'Muted virtual source delivered nonzero audio.'
        print('Private native volume test passed: 50% initialization, 80% source control, unity output gain, mute.')
    finally:
        registry.close()
        for pipeline in reversed(pipelines): pipeline.set_state(Gst.State.NULL)


def main():
    if sys.argv[1:] == ['--probe']:
        if os.environ.get('NOISE_CANCELLATION_PRIVATE_VOLUME_TEST') != '1':
            raise SystemExit('Use the parent runner to create an isolated server.')
        probe()
        return
    if sys.argv[1:]: raise SystemExit('Usage: python3 -B tools/test-volume.py')
    program = shutil.which('pipewire')
    if not program: raise SystemExit('Blocked: existing pipewire executable is required.')
    (ROOT / 'build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='volume-', dir=ROOT / 'build') as directory:
        root = Path(directory)
        config = root / 'private.conf'
        config.write_text(CONFIG)
        (root / 'client.conf').write_text(CLIENT_CONFIG)
        env = {**os.environ, 'PIPEWIRE_RUNTIME_DIR': directory, 'PIPEWIRE_REMOTE': 'pw',
            'PIPEWIRE_CONFIG_DIR': directory, 'PIPEWIRE_CONFIG_PREFIX': '',
            'PIPEWIRE_CONFIG_NAME': 'client.conf', 'XDG_CONFIG_HOME': directory,
            'GST_REGISTRY': str(ROOT / 'build/gst-registry.bin'), 'PYTHONDONTWRITEBYTECODE': '1',
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path={root}/no-bus',
            'NOISE_CANCELLATION_PRIVATE_VOLUME_TEST': '1'}
        with (root / 'server.log').open('w+') as log:
            daemon = subprocess.Popen([program, '-c', 'private.conf'],
                env={**env, 'PIPEWIRE_CONFIG_NAME': 'private.conf'}, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 3
                while not (root / 'pw').exists() and daemon.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not (root / 'pw').exists():
                    log.seek(0)
                    raise SystemExit('Blocked: private PipeWire socket could not start.\n' + log.read(4000))
                subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--probe'],
                    env=env, check=True, timeout=35)
            finally:
                daemon.terminate()
                try: daemon.wait(timeout=3)
                except subprocess.TimeoutExpired: daemon.kill(); daemon.wait(timeout=3)


if __name__ == '__main__': main()
