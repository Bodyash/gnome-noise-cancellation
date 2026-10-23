"""Opt-in synthetic volume test on an owned, hardware-free PipeWire server.

Never connects to the desktop server or loads devices. --pulse additionally runs
WirePlumber's policy-only profile and a private Pulse server/recording stream.
Requires existing PipeWire/GStreamer/Wp runtimes and permission to bind a private
Unix socket. All configuration, logs and sockets stay under build/.
"""
import array
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

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
    { name = libpipewire-module-metadata }
]
context.objects = [
    { factory = spa-node-factory args = {
        factory.name = support.node.driver node.name = Driver priority.driver = 200000
    } }
    { factory = metadata args = { metadata.name = default } }
]
'''
CLIENT_CONFIG = '''
context.properties = { mem.allow-mlock = false }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-metadata }
]
'''


def probe_idle_negotiation(registry, wait, node, start):
    """Real source with a consumer before its first buffer, then idle arrivals."""
    from streaming import AudioDriver, Gst, make_capture, make_output
    from routing import Wp, FEATURES
    from volume import Volume

    for cycle in range(3):
        failures, owned_links, consumers = [], [], []
        driver = AudioDriver(failures.append, registry.changed)
        capture = Gst.parse_launch('audiotestsrc is-live=true samplesperbuffer=480 freq=160 volume=0.2 ! '
            'audio/x-raw,format=F32LE,rate=48000,channels=1 ! '
            'appsink name=frames emit-signals=true sync=false async=false')
        driver.capture = capture
        capture.get_by_name('frames').connect('new-sample', driver._sample)
        output, app, _sink = make_output(f'idle-{cycle}', Volume((0.125,), False))
        driver.output = output
        first_buffer, allow_buffer = threading.Event(), threading.Event()
        def need_data(source, length):
            if not first_buffer.is_set():
                first_buffer.set()
                allow_buffer.wait(5)
            driver._need_data(source, length)
        app.connect('need-data', need_data)
        driver._watch_bus(capture)
        driver._watch_bus(output)

        def checked(predicate):
            if failures: raise AssertionError(failures)
            driver.renew()
            return predicate()

        def connect(source_id, target_name):
            def endpoints():
                target = node(target_name)
                if target is None: return None
                objects = registry.snapshot()['objects']
                ports = [next((o['id'] for o in objects if o['kind'] == 'Port' and
                    o['props'].get('node.id') == str(ident) and
                    o['props'].get('port.direction') == direction), None)
                    for ident, direction in [(source_id, 'out'), (target['id'], 'in')]]
                return ports if all(p is not None for p in ports) else None
            wait(lambda: checked(endpoints), 'Synthetic link ports did not register.')
            source_port, input_port = endpoints()
            props = Wp.Properties.new_empty()
            for key, value in {'link.output.port': source_port, 'link.input.port': input_port,
                    'object.linger': 'false', 'link.passive': 'false'}.items(): props.set(key, str(value))
            link = Wp.Link.new_from_factory(registry.core, 'link-factory', props)
            owned_links.append(link)
            link.activate(FEATURES, None, lambda obj, result: obj.activate_finish(result))
            return link

        try:
            driver.renew()
            driver.request_capture(True)
            driver._start_pipeline(output, 'output')
            wait(lambda: checked(lambda: driver.capture_running and driver.fresh_capture and
                node('noise_cancellation.clean_microphone') and first_buffer.is_set()),
                'Startup did not reach real filtered frames and first output request.')
            clean = node('noise_cancellation.clean_microphone')
            _result, current, pending = output.get_state(100 * Gst.MSECOND)
            assert current == Gst.State.PLAYING, f'Provider waiting for first-buffer preroll: {current}, {pending}'
            for arrival in range(3):
                if arrival:
                    driver.request_capture(False)
                    wait(lambda: checked(lambda: driver.capture_idle), 'Synthetic capture did not idle.')
                token = f'consumer-{cycle}-{arrival}'
                consumer, _source, sink = make_capture(token, clean['props']['object.serial'])
                consumers.append(consumer)
                start(consumer)

                link = connect(clean['id'], f'noise_cancellation.{token}.capture')
                allow_buffer.set()
                samples = []

                def receive():
                    sample = sink.emit('try-pull-sample', 0)
                    if sample is None: return False
                    data = array.array('f')
                    buffer = sample.get_buffer()
                    data.frombytes(buffer.extract_dup(0, buffer.get_size()))
                    samples.append(max(abs(value) for value in data))
                    return True

                wait(lambda: checked(receive), f'Late consumer stuck without idle silence; stage={driver.stage}.')
                assert samples[-1] == 0, 'Idle source leaked nonzero samples.'
                before = driver.frames
                driver.request_capture(True)
                wait(lambda: checked(lambda: driver.capture_running and driver.fresh_capture and driver.frames > before),
                    'Capture did not produce fresh resume frames.')
                driver.set_delivery(True)
                wait(lambda: checked(lambda: receive() and samples[-1] > 0.0001),
                    'Late consumer never received actual nonzero filtered audio after resume.')
                consumer.set_state(Gst.State.NULL)
                link.request_destroy()
            assert not failures, failures
        finally:
            allow_buffer.set()
            driver.close()
            for consumer in consumers: consumer.set_state(Gst.State.NULL)
            for link in owned_links:
                try: link.request_destroy()
                except Exception: pass
        wait(lambda: node('noise_cancellation.clean_microphone') is None, 'Stopped probe source remained registered.')


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
        # A synthetic default-input policy: test real metadata acknowledgment and
        # native node-removal ordering, without running desktop/Bluetooth policy.
        from handoff import CURRENT, CONFIGURED, default_name
        wait(lambda: registry._default_metadata() is not None, 'Private default metadata did not appear.')
        metadata = registry._default_metadata()
        clean_name = 'noise_cancellation.clean_microphone'
        metadata.set(0, CONFIGURED, 'Spa:String:JSON', '{"name":"' + clean_name + '"}')
        metadata.set(0, CURRENT, 'Spa:String:JSON', '{"name":"' + clean_name + '"}')
        wait(lambda: default_name(metadata, CURRENT) == clean_name, 'Private default input was not set.')
        observer = Registry('handoff-observer', lambda _: None, lambda _: None, faults.append)
        try:
            observer.start()
            wait(lambda: observer._default_metadata() is not None, 'Initial metadata replay did not finish.')
            assert default_name(observer._default_metadata(), CURRENT) == clean_name
            assert default_name(observer._default_metadata(), CONFIGURED) == clean_name
        finally: observer.close()
        capture_id = node('noise_cancellation.volume-probe.capture')['id']
        capture.set_state(Gst.State.NULL)
        completed = []
        registry.begin_handoff(SimpleNamespace(capture_id=capture_id, output_id=clean_id,
            handoff_source=lambda _snapshot: 'volume_probe.physical'), completed.append)
        wait(lambda: (registry.tick(), bool(completed))[1], 'Native handoff timed out.')
        assert completed == [True], 'Current default did not acknowledge the selected source.'
        assert node(clean_name), 'Clean source vanished before the handoff acknowledgment.'
        assert default_name(metadata, CURRENT) == 'volume_probe.physical'
        assert default_name(metadata, CONFIGURED) == clean_name, 'Saved Clean preference was changed.'
        output.set_state(Gst.State.NULL)
        wait(lambda: node(clean_name) is None, 'Clean source did not disappear after the handoff.')
        probe_idle_negotiation(registry, wait, node, start)
        print('Private native test passed: volume/mute, unity gain, input handoff, '
            'early consumers and nonzero filtered audio after idle/resume.')
    finally:
        registry.close()
        for pipeline in reversed(pipelines): pipeline.set_state(Gst.State.NULL)


def main():
    if sys.argv[1:] == ['--pulse-probe']:
        if os.environ.get('NOISE_CANCELLATION_PRIVATE_VOLUME_TEST') != '1':
            raise SystemExit('Use the parent runner to create an isolated server.')
        sys.path.insert(0, str(ROOT / 'extension/worker'))
        import faulthandler
        faulthandler.enable()
        from routing_probe import probe as pulse_probe
        pulse_probe()
        return
    if sys.argv[1:] == ['--probe']:
        if os.environ.get('NOISE_CANCELLATION_PRIVATE_VOLUME_TEST') != '1':
            raise SystemExit('Use the parent runner to create an isolated server.')
        import faulthandler
        faulthandler.dump_traceback_later(15)
        probe()
        faulthandler.cancel_dump_traceback_later()
        return
    policy = sys.argv[1:] == ['--pulse']
    if sys.argv[1:] and not policy: raise SystemExit('Usage: python3 -B tools/test-volume.py [--pulse]')
    program = shutil.which('pipewire')
    if not program: raise SystemExit('Blocked: existing pipewire executable is required.')
    if policy:
        missing = [name for name in ['pipewire-pulse', 'wireplumber', 'pactl'] if not shutil.which(name)]
        if missing: raise SystemExit('Blocked: existing test runtimes required: ' + ', '.join(missing))
    (ROOT / 'build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='volume-', dir=ROOT / 'build') as directory:
        root = Path(directory)
        config = root / 'private.conf'
        server_config = CONFIG
        if policy:
            server_config = server_config.replace('audio.convert.* =',
                'audiotestsrc = audiotestsrc/libspa-audiotestsrc audio.convert.* =')
            server_config = server_config.replace('{ factory = metadata args = { metadata.name = default } }', '''
    { factory = adapter args = {
        factory.name = audiotestsrc node.name = routing_probe.physical
        node.description = "Synthetic physical microphone" media.class = Audio/Source
        priority.driver = 2000
        audio.channels = 1 audio.rate = 48000 audio.position = [ MONO ]
        node.param.Props = { live = true frequency = 160 volume = 0.2 }
    } }
''')
        config.write_text(server_config)
        (root / 'client.conf').write_text(CLIENT_CONFIG)
        env = {**os.environ, 'PIPEWIRE_RUNTIME_DIR': directory, 'PIPEWIRE_REMOTE': 'pw',
            'PIPEWIRE_CONFIG_DIR': directory, 'PIPEWIRE_CONFIG_PREFIX': '',
            'PIPEWIRE_CONFIG_NAME': 'client.conf', 'XDG_CONFIG_HOME': directory,
            'XDG_STATE_HOME': str(root / 'state'), 'XDG_RUNTIME_DIR': directory,
            'GST_REGISTRY': str(ROOT / 'build/gst-registry.bin'), 'PYTHONDONTWRITEBYTECODE': '1',
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path={root}/no-bus',
            'DISPLAY': '', 'WAYLAND_DISPLAY': '',
            'NOISE_CANCELLATION_PRIVATE_VOLUME_TEST': '1'}
        children = []
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
                if policy:
                    pulse_socket = root / 'pulse-native'
                    env['PULSE_SERVER'] = f'unix:{pulse_socket}'
                    (root / 'pulse.conf').write_text(CLIENT_CONFIG.replace(']\n',
                        '{ name = libpipewire-module-protocol-pulse args = { '
                        f'server.address = [ "unix:{pulse_socket}" ] }} }} ]\n'))
                    children.append(subprocess.Popen(['wireplumber', '--profile=policy'],
                        env={key: value for key, value in env.items() if not key.startswith('PIPEWIRE_CONFIG_')},
                        stdout=log, stderr=log))
                    children.append(subprocess.Popen(['pipewire-pulse', '-c', 'pulse.conf'],
                        env={**env, 'PIPEWIRE_CONFIG_NAME': 'pulse.conf'}, stdout=log, stderr=log))
                    deadline = time.monotonic() + 3
                    while not pulse_socket.exists() and time.monotonic() < deadline: time.sleep(0.01)
                    if not pulse_socket.exists():
                        log.seek(0); print(log.read(12000), file=sys.stderr)
                        raise RuntimeError('Private Pulse server did not start.')
                try:
                    with subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                            '--pulse-probe' if policy else '--probe'], env=env, start_new_session=True) as probe_child:
                        try:
                            result = probe_child.wait(timeout=55)
                            if result: raise subprocess.CalledProcessError(result, probe_child.args)
                        finally:
                            # Includes fixture workers if the probe itself hung/crashed.
                            try: os.killpg(probe_child.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                    log.seek(0)
                    if any('node_set_param(' in line and 'failed' in line for line in log):
                        raise AssertionError('Private server rejected an audio adapter format/port change.')
                except Exception:
                    log.seek(0); print(log.read(12000), file=sys.stderr)
                    raise
            finally:
                for child in reversed(children):
                    child.terminate()
                    try: child.wait(timeout=3)
                    except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
                daemon.terminate()
                try: daemon.wait(timeout=3)
                except subprocess.TimeoutExpired: daemon.kill(); daemon.wait(timeout=3)


if __name__ == '__main__': main()
