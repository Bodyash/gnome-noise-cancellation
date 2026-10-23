"""One private synthetic session per process, matching the runtime boundary."""
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extension/worker'))


def main():
    if os.environ.get('NOISE_CANCELLATION_PRIVATE_VOLUME_TEST') != '1':
        raise SystemExit('Use tools/test-volume.py --pulse to create a private server.')
    from streaming import AudioDriver, Gst, GLib, make_capture, make_output
    from routing import Registry
    from lifecycle import Session
    from volume import Volume
    failures = []
    token = 'pulse-probe'
    driver = AudioDriver(failures.append, lambda: None)
    registry = Registry(token, lambda _: None, lambda _: None, failures.append)
    driver.registry = registry
    registry.start()
    context = GLib.MainContext.default()

    def objects(): return registry.snapshot()['objects'] if registry._ready else []
    def node(name): return next((o for o in objects() if o['kind'] == 'Node' and
        o['props'].get('node.name') == name), None)
    def wait(predicate):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            for _ in range(20):
                if not context.pending(): break
                context.iteration(False)
            if failures: raise AssertionError(failures)
            driver.renew()
            if predicate(): return
            time.sleep(0.005)
        raise AssertionError(f'Private worker startup timed out at {driver.stage}.')

    try:
        wait(lambda: node('routing_probe.physical'))
        physical = node('routing_probe.physical')
        capture, _source, frames = make_capture(token, physical['props']['object.serial'])
        driver.capture = capture
        frames.connect('new-sample', driver._sample)
        driver._watch_bus(capture)
        driver.request_capture(True)
        wait(lambda: node(f'noise_cancellation.{token}.capture'))
        capture_id = node(f'noise_cancellation.{token}.capture')['id']
        def ports():
            values = [next((o['id'] for o in objects() if o['kind'] == 'Port' and
                o['props'].get('node.id') == str(ident) and
                o['props'].get('port.direction') == direction), None)
                for ident, direction in [(physical['id'], 'out'), (capture_id, 'in')]]
            return values if all(v is not None for v in values) else None
        wait(ports)
        registry.create_link(*ports())
        wait(lambda: driver.capture_running and driver.fresh_capture)
        output, app, _sink = make_output(token, Volume((1,), False))
        driver.output = output
        app.connect('need-data', driver._need_data)
        driver._watch_bus(output)
        driver._start_pipeline(output, 'output')
        wait(lambda: node('noise_cancellation.clean_microphone'))
        def emit(_event, **fields): print(fields['state'], flush=True)
        control = SimpleNamespace(driver=driver, capture_id=capture_id,
            output_id=node('noise_cancellation.clean_microphone')['id'], activity=None,
            _switch_started=None, emit=emit, proof=lambda _published: {},
            handoff_source=lambda _snapshot: 'routing_probe.physical')
        loop = GLib.MainLoop()
        stopping = False
        def tick():
            try:
                if not stopping:
                    driver.renew()
                    links = [o for o in objects() if o['kind'] == 'Link' and
                        o['props'].get('link.input.node') == str(capture_id)]
                    Session._demand(control, objects(), links[0]['state'] if links else 'missing')
                registry.tick()
                if failures: raise AssertionError(failures)
            except Exception as error:
                failures.append(str(error)); loop.quit()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        def stopped():
            driver.close()
            loop.quit()
        def command(_fd, condition):
            nonlocal stopping
            request = os.read(0, 32)
            if request != b'stop\n' or condition & GLib.IOCondition.HUP:
                failures.append('Private test controller closed unexpectedly.'); loop.quit()
            elif not stopping:
                stopping = True
                driver.stop_for_handoff(control, stopped)
            return GLib.SOURCE_REMOVE
        timer = GLib.timeout_add(20, tick)
        watch = GLib.io_add_watch(0, GLib.IOCondition.IN | GLib.IOCondition.HUP, command)
        try: loop.run()
        finally:
            for ident in [timer, watch]:
                source = context.find_source_by_id(ident)
                if source: source.destroy()
        if failures: raise AssertionError(failures)
    finally: driver.close()


if __name__ == '__main__': main()
