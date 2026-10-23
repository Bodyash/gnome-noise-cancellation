"""Private source-only audio worker. Never installs dependencies or uses a shell."""
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

sys.dont_write_bytecode = True
# -I ignores user Python paths. Only our bundled sibling modules are added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle import Authorization, Session


def report_check(all_backends=False):
    try:
        from streaming import check_runtime
        result = check_runtime(all_backends)
    except Exception as error:
        result = {'protocol': 1, 'backend': 'rnnoise', 'ready': False,
            'message': f'Audio runtime check failed: {error}. Requires Python ctypes, PyGObject, '
                'GStreamer/PipeWire plugins, WirePlumber 0.5 introspection and RNNoise. Install missing distro packages manually.'}
    print(json.dumps(result, separators=(',', ':')))
    return 0  # bounded structured status; callers inspect ready and message


class Worker:
    def __init__(self):
        from streaming import GLib
        self.GLib = GLib
        self.loop = GLib.MainLoop()
        self.session = self.driver = self.registry = None
        self.token = ''
        self.processing = None
        self.closed = False
        self.failed = False
        self.stopping = False
        self.buffer = bytearray()
        self.started = self.last_control = self.last_tick = time.monotonic()
        self._watch_stop = threading.Event()
        # stdin/stdout carry private control messages; audio samples stay in GStreamer.
        os.set_blocking(0, False); os.set_blocking(1, False)
        self.stdin_source = GLib.io_add_watch(0, GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, self._input)
        self.timer = GLib.timeout_add(100, self._tick)
        self.signals = [GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._terminate)
                        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]]

    def emit(self, event, **fields):
        if event in ('prepared', 'active', 'activity'):
            fields['configuration'] = self.processing
        data = (json.dumps({'protocol': 1, 'session': self.token, 'event': event, **fields},
                           separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(data) > 4096: raise ValueError('Worker status exceeded its protocol bound.')
        if os.write(1, data) != len(data): raise RuntimeError('Controller status channel is unavailable.')
        if event == 'fault':
            self.failed = True
            self.GLib.idle_add(self._terminate)

    def fault(self, message):
        if self.closed: return
        if self.stopping:
            self._terminate()
            return
        if self.session: self.session.fault(message)
        else:
            self.failed = True
            try: self.emit('fault', message=str(message)[:400])
            finally: self._terminate()

    def _input(self, _fd, condition):
        try:
            data = os.read(0, 4096)
            if not data:
                self.fault('Controller channel closed; audio authorization revoked.')
                return self.GLib.SOURCE_REMOVE
            # Reads may split or combine JSON lines; retain only bounded incomplete requests.
            self.buffer.extend(data)
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                if len(line) > 16384 or b'\0' in line: raise ValueError('Invalid or excessive worker request.')
                request = json.loads(line.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number')))
                self.command(request)
                if self.closed or self.stopping: return self.GLib.SOURCE_REMOVE
            if len(self.buffer) > 16384: raise ValueError('Worker request exceeded its protocol bound.')
        except BlockingIOError:
            if condition & (self.GLib.IOCondition.HUP | self.GLib.IOCondition.ERR):
                self.fault('Controller channel failed.')
        except Exception as error:
            self.fault(str(error))
        return self.GLib.SOURCE_REMOVE if self.closed else self.GLib.SOURCE_CONTINUE

    def command(self, request):
        if not isinstance(request, dict) or type(request.get('protocol')) is not int or request['protocol'] != 1:
            raise ValueError('Incompatible worker protocol.')
        token, command = request.get('session'), request.get('command')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', token):
            raise ValueError('Invalid audio session token.')
        if self.token and token != self.token: raise ValueError('Stale worker session token.')
        # A worker accepts one authorization; subsequent commands can only supervise or publish it.
        if self.session is None:
            if command != 'prepare': raise ValueError('Explicit prepare authorization is required first.')
            authorization = Authorization(request.get('key'), request.get('sourceId'))
            from streaming import AudioDriver
            from routing import Registry
            self.token = token
            from engines import configuration
            config = configuration(request.get('processing', {}))
            self.processing = config
            self.driver = AudioDriver(self.fault, lambda: self.registry.changed(), config)
            self.session = Session(authorization, token, self.driver, self.emit)
            self.registry = Registry(token, self.session.update, self.session.removed, self.fault)
            self.driver.registry = self.registry
            self.registry.start()
        elif command == 'ping':
            self.emit('pong')
        elif command == 'publish':
            self.registry.tick()  # validate fresh registry data before publication
            self.session.publish()
        elif command == 'stop':
            if request.get('handoff') is not True or self.stopping:
                raise ValueError('Invalid or repeated user stop request.')
            self.stopping = True
            self.driver.revoke()
            # Freeze graph callbacks before expected capture/link removal.
            self.session.closed = True
            if self.session.active:
                self.driver.stop_for_handoff(self.session, self._terminate)
            else:
                self._terminate()
        else: raise ValueError('Unknown, repeated or out-of-order worker command.')
        self.last_control = time.monotonic()

    def _tick(self):
        if self.closed: return self.GLib.SOURCE_REMOVE
        self.last_tick = time.monotonic()
        try:
            if self.registry: self.registry.tick()
            if self.session and not self.session.active and self.last_tick - self.started > 8:
                self.fault(f'Audio startup timed out: {self.driver.stage}. The authorized graph was not ready.')
        except Exception as error: self.fault(str(error))
        return self.GLib.SOURCE_REMOVE if self.closed else self.GLib.SOURCE_CONTINUE

    def _watchdog(self):
        # Independent of the GLib control loop. A fully blocked Python interpreter
        # also blocks Python buffer production; already queued native audio is bounded.
        while not self._watch_stop.wait(0.1):
            now = time.monotonic()
            reason = None
            if now - self.last_control > 2.5: reason = 'Controller heartbeat lost.'
            elif now - self.last_tick > 1.5: reason = 'Audio control loop stopped responding.'
            driver = self.driver
            if driver and driver.deadline:
                if now >= driver.deadline: reason = reason or 'Audio graph supervision lease expired.'
                elif driver.capture_requested and driver.last_audio > 0 and now - driver.last_audio > 1.5:
                    reason = reason or 'Authorized microphone stopped delivering audio.'
            if reason:
                if driver: driver.revoke()
                try: self.emit('fault', message=f'{reason} Stage: {driver.stage if driver else "initialization"}')
                except Exception: pass  # a closed parent pipe must not delay termination
                os._exit(1)  # closes all owned PipeWire connections even if GLib teardown is blocked

    def _terminate(self):
        if self.closed: return self.GLib.SOURCE_REMOVE
        self.closed = True
        if self.driver: self.driver.revoke()
        if self.stopping and self.driver: self.driver.close()
        elif self.session: self.session.close()
        elif self.driver: self.driver.close()
        self.loop.quit()
        return self.GLib.SOURCE_REMOVE

    def run(self):
        threading.Thread(target=self._watchdog, daemon=True, name='audio-supervision').start()
        try: self.loop.run()
        finally:
            self._terminate()
            self._watch_stop.set()
            for ident in [self.stdin_source, self.timer, *self.signals]:
                source = self.GLib.MainContext.default().find_source_by_id(ident)
                if source: source.destroy()
        return 1 if self.failed else 0


def main():
    if sys.argv[1:] == ['--check']: return report_check()
    if sys.argv[1:] == ['--check-all']: return report_check(True)
    if sys.argv[1:] != ['--session']:
        print('Usage: main.py --check|--check-all|--session', file=sys.stderr)
        return 2
    # Linux parent-death termination, including a race between getppid and prctl.
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if parent <= 1 or libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        return 1
    try:
        return Worker().run()
    except Exception as error:
        try: print(json.dumps({'protocol': 1, 'session': '', 'event': 'fault', 'message': str(error)[:400]}), flush=True)
        except Exception: pass
        return 1


if __name__ == '__main__': raise SystemExit(main())
