"""GStreamer audio lives in the worker process, never GNOME Shell."""
import collections
import sys
import threading
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst
from engines import create_processor, check_engines
from lifecycle import OUTPUT_NAME

Gst.init(None)
CAPS = 'audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=1'


def element(name):
    result = Gst.ElementFactory.make(name)
    if result is None:
        raise RuntimeError(f'GStreamer element {name} is missing. Install its distro runtime plugin manually.')
    return result


def structure(values):
    result = Gst.Structure.new_empty('props')
    for key, value in values.items(): result.set_value(key, str(value))
    return result


def stream_properties(token, suffix):
    return structure({'node.name': OUTPUT_NAME if suffix == 'source' else f'noise_cancellation.{token}.{suffix}',
        'noise-cancellation.session': token,
        'node.description': 'Clean Microphone' if suffix == 'source' else 'Authorized microphone capture',
        'node.group': f'noise_cancellation.{token}', 'node.virtual': 'true',
        'node.autoconnect': 'false', 'node.dont-reconnect': 'true', 'node.dont-fallback': 'true',
        'node.pause-on-idle': 'true', 'node.always-process': 'false',
        'audio.rate': '48000', 'audio.channels': '1', 'audio.position': '[ MONO ]',
        'adapter.auto-port-config': '{ mode = dsp monitor = false control = false position = preserve }',
        'media.class': 'Audio/Source' if suffix == 'source' else 'Stream/Input/Audio'})


# Pin the authorized source; the registry creates its exact link separately from negotiation.
def make_capture(token, source_serial):
    pipeline = Gst.Pipeline.new(None)
    source, caps, sink = element('pipewiresrc'), element('capsfilter'), element('appsink')
    source.set_property('autoconnect', False)
    source.set_property('target-object', source_serial)
    source.set_property('stream-properties', stream_properties(token, 'capture'))
    source.set_property('resend-last', False)
    source.set_property('keepalive-time', 0)
    source.set_property('max-buffers', 8)
    if source.find_property('dont-reconnect'): source.set_property('dont-reconnect', True)
    if source.find_property('on-disconnect'): source.set_property('on-disconnect', 2)
    if source.find_property('use-bufferpool'): source.set_property('use-bufferpool', False)
    caps.set_property('caps', Gst.Caps.from_string(CAPS))
    sink.set_property('emit-signals', True)
    sink.set_property('max-buffers', 8)
    sink.set_property('drop', True)
    sink.set_property('sync', False)
    # Readiness is proven by the exact route and processed frames. Waiting for
    # sink preroll adds a second state-transition wait during capture suspension.
    sink.set_property('async', False)
    sink.set_property('enable-last-sample', False)
    for item in [source, caps, sink]: pipeline.add(item)
    if not source.link(caps) or not caps.link(sink):
        raise RuntimeError('Cannot link owned capture pipeline.')
    return pipeline, source, sink


def configure_output(pipeline, app, sink):
    # The provider sink drops buffers when no PW consumer is streaming. Without
    # Gst clock pacing, appsrc then spins at CPU speed instead of 100 frames/s.
    # A system clock also lets the independent source negotiate while idle.
    pipeline.use_clock(Gst.SystemClock.obtain())
    app.set_property('caps', Gst.Caps.from_string(CAPS))
    app.set_property('format', Gst.Format.TIME)
    app.set_property('is-live', True)
    app.set_property('block', False)
    app.set_property('max-bytes', 8 * 1920)
    app.set_property('max-buffers', 8)
    sink.set_property('sync', True)
    sink.set_property('enable-last-sample', False)


def make_output(token):
    pipeline = Gst.Pipeline.new(None)
    app, sink = element('appsrc'), element('pipewiresink')
    configure_output(pipeline, app, sink)
    sink.set_property('mode', 2)  # provide: export a source instead of rendering to a speaker
    sink.set_property('stream-properties', stream_properties(token, 'source'))
    for item in [app, sink]: pipeline.add(item)
    if not app.link(sink): raise RuntimeError('Cannot link filtered virtual microphone pipeline.')
    return pipeline, app, sink


def check_runtime(all_backends=False):
    if sys.byteorder != 'little':
        raise RuntimeError('This worker currently supports little-endian audio only.')
    for name in ['pipewiresrc', 'pipewiresink', 'appsink', 'appsrc', 'capsfilter']:
        element(name)
    # Property compatibility is checked without moving either pipeline out of NULL.
    capture, _, _ = make_capture('check', '0')
    output, _, _ = make_output('check')
    capture.set_state(Gst.State.NULL); output.set_state(Gst.State.NULL)
    from routing import Registry
    registry = Registry('check', lambda _: None, lambda _: None, lambda _: None)
    registry.close()  # validate the installed Wp constructor/signal ABI without connecting
    checks = check_engines() if all_backends else None
    if not all_backends:
        with create_processor() as processor:
            if len(processor.feed(bytes(1920))) != 1920:
                raise RuntimeError('RNNoise self-check did not produce a filtered frame.')
    return {'protocol': 1, 'ready': True, 'rate': 48000,
            'channels': 1, 'frameSize': 480, 'worker': 'python', 'gstreamer': Gst.version_string(),
            **({'backends': checks} if checks is not None else {'backend': 'rnnoise'})}


class AudioDriver:
    def __init__(self, fault, changed, config=None):
        self.processor = create_processor(config)
        self.fault, self.changed = fault, changed
        self.capture = self.output = self.registry = None
        self._buses = []
        self._queue = collections.deque(maxlen=8)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self.deadline = self.last_audio = 0.0
        self._position = 0
        self._callback_faulted = False
        self._transitions = set()
        self.stage = 'initializing'
        # Queue access, capture requests and native DSP state use separate locks.
        self._switch_lock = threading.Lock()
        self._switch_changed = threading.Event()
        self._dsp_lock = threading.Lock()
        self._capture_thread = None
        self.capture_requested = self._capture_current = None
        self.resume_baseline = 0
        self._capture_generation = 0
        self.fresh_capture = False
        self._delivery = True
        self._delivery_generation = 0
        self._first_sample = True

    @property
    def frames(self): return self.processor.frames

    @property
    def transitions_pending(self): return bool(self._transitions)

    def _start_pipeline(self, pipeline, label):
        if self._stopped.is_set(): raise ValueError('Audio startup was cancelled.')
        if label == 'capture':
            self.request_capture(True)
            return
        self.stage = f'waiting for {label} negotiation'
        def run():
            try:
                if self._stopped.is_set(): return
                # pipewiresrc's READY->PAUSED waits for negotiation. The GLib/Wp
                # thread must remain free to create the exact link it needs.
                result = pipeline.set_state(Gst.State.PLAYING)
                if self._stopped.is_set():
                    # Cancellation may have won the state lock before this
                    # thread entered PLAYING. Undo any late native transition.
                    pipeline.set_state(Gst.State.NULL)
                else:
                    if result == Gst.StateChangeReturn.FAILURE:
                        self._error(f'Cannot start {label} pipeline. Check the audio runtime logs.')
                    else:
                        self.stage = f'{label} running'
                        GLib.idle_add(lambda: (self.changed(), GLib.SOURCE_REMOVE)[1]
                            if not self._stopped.is_set() else GLib.SOURCE_REMOVE)
            except Exception as error:
                if not self._stopped.is_set(): self._error(f'{label} startup failed: {str(error)[:200]}')
            finally:
                self._transitions.discard(threading.current_thread())
        thread = threading.Thread(target=run, name=f'audio-{label}-startup', daemon=True)
        self._transitions.add(thread)
        thread.start()

    @property
    def capture_running(self):
        return self.capture_requested is True and self._capture_current is True and self._capture_thread is None

    @property
    def capture_idle(self):
        return self.capture_requested is False and self._capture_current is False and self._capture_thread is None

    def set_delivery(self, enabled):
        if enabled and (self._stopped.is_set() or self.processor.revoked):
            raise ValueError('Cannot deliver audio after revocation.')
        with self._lock:
            self._delivery = enabled
            if not enabled:
                self._delivery_generation += 1
                self._queue.clear()

    def request_capture(self, enabled):
        if self._stopped.is_set() or self.processor.revoked:
            raise ValueError('Capture authorization was revoked.')
        with self._switch_lock:
            if self.capture_requested == enabled: return
            # Do not let the watchdog mistake the old idle timestamp for a stall
            # before the resume thread has a chance to run.
            if enabled:
                self.last_audio = 0.0
                self.resume_baseline = self.frames
            with self._lock:
                self.capture_requested = enabled
                self._capture_generation += 1
                self.fresh_capture = False
            self._switch_changed.set()
            self.set_delivery(False)
            if self._capture_thread is not None: return
            thread = threading.Thread(target=self._capture_switch, daemon=True, name='audio-capture-state')
            self._capture_thread = thread
            self._transitions.add(thread)
        thread.start()

    def _capture_switch(self):
        try:
            while not self._stopped.is_set() and not self.processor.revoked:
                with self._switch_lock:
                    target = self.capture_requested
                    self._switch_changed.clear()
                    if self._stopped.is_set() or self.processor.revoked: return
                    if target == self._capture_current:
                        self._capture_thread = None
                        return
                if not target:
                    # Consumer links can briefly disappear while an application
                    # changes devices. Output is already gated. Settle for a
                    # bounded 200 ms instead of churning native state changes.
                    self.stage = 'waiting for stable idle'
                    if self._switch_changed.wait(0.2): continue
                    with self._switch_lock:
                        if self.capture_requested is not False: continue
                    if self._stopped.is_set() or self.processor.revoked: return
                self.stage = 'setting capture PLAYING' if target else 'setting capture PAUSED'
                if target:
                    self.resume_baseline = self.frames
                    self.last_audio = 0.0
                    self._first_sample = True
                state = Gst.State.PLAYING if target else Gst.State.PAUSED
                result = self.capture.set_state(state)
                if self._stopped.is_set():
                    self.capture.set_state(Gst.State.NULL)
                    return
                if result == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError('Capture state change failed.')
                if result == Gst.StateChangeReturn.ASYNC:
                    self.stage = 'waiting for capture PLAYING' if target else 'waiting for capture PAUSED'
                    result, current, _pending = self.capture.get_state(2 * Gst.SECOND)
                    if result == Gst.StateChangeReturn.FAILURE or current != state:
                        raise RuntimeError('Capture state change did not complete.')
                if not target:
                    # PAUSED corks the native PipeWire capture stream. Reset
                    # history and flush queued output without removing its node.
                    self.stage = 'resetting processing after pause'
                    with self._dsp_lock:
                        if not self._stopped.is_set() and not self.processor.revoked: self.processor.reset()
                    self.set_delivery(False)
                    if self.output and not self._stopped.is_set():
                        self.stage = 'flushing virtual output (start)'
                        if not self.output.send_event(Gst.Event.new_flush_start()):
                            raise RuntimeError('Cannot begin flushing filtered output on idle.')
                        self.stage = 'flushing virtual output (stop)'
                        if not self.output.send_event(Gst.Event.new_flush_stop(False)):
                            raise RuntimeError('Cannot flush filtered output on idle.')
                with self._switch_lock: self._capture_current = target
                self.stage = 'capture running' if target else 'capture paused'
                GLib.idle_add(lambda: (self.changed(), GLib.SOURCE_REMOVE)[1]
                    if not self._stopped.is_set() else GLib.SOURCE_REMOVE)
        except Exception as error:
            if not self._stopped.is_set(): self._error(f'Capture idle/resume failed: {str(error)[:200]}')
        finally:
            with self._switch_lock:
                if self._capture_thread is threading.current_thread(): self._capture_thread = None
            self._transitions.discard(threading.current_thread())

    # Registry validation renews this lease; streaming callbacks only check its expiry.
    def renew(self):
        if self.processor.revoked: raise ValueError('Audio processing authorization was revoked.')
        self.deadline = time.monotonic() + 1.5

    def _valid(self):
        if self._stopped.is_set() or self.processor.revoked: return False
        if self.deadline and time.monotonic() >= self.deadline:
            self._error('Audio graph supervision timed out. Select the microphone again.')
            return False
        return self.deadline > 0

    def _error(self, message):
        self.revoke()
        if not self._callback_faulted:
            self._callback_faulted = True
            GLib.idle_add(lambda: (self.fault(message), GLib.SOURCE_REMOVE)[1])

    def _watch_bus(self, pipeline):
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        def message(_bus, msg):
            if self._stopped.is_set(): return
            if msg.type == Gst.MessageType.ERROR:
                error, _debug = msg.parse_error()
                self._error(f'GStreamer audio pipeline failed: {error.message[:240]}')
            elif msg.type == Gst.MessageType.EOS:
                self._error('Microphone stream ended. Select the microphone again.')
        ident = bus.connect('message', message)
        self._buses.append((bus, ident))

    def start_capture(self, authorization, token):
        if self.capture is not None: raise ValueError('Capture startup cannot be repeated.')
        self.capture, _source, sink = make_capture(token, authorization.key[1])
        self._watch_bus(self.capture)
        sink.connect('new-sample', self._sample)
        self._start_pipeline(self.capture, 'capture')

    def _sample(self, sink):
        try:
            # A new-sample signal can already be queued for Python when another
            # thread moves appsink to PAUSED. pull-sample then waits for PLAYING,
            # while set_state(PAUSED) may be waiting for this callback to finish.
            # A missing sample during pause/flush is expected; real EOS is handled
            # by the bus watch. Never wait for a future state from a callback.
            with self._lock:
                generation = self._delivery_generation
                # Input freshness and permission to enqueue output have independent generations.
                capture_generation = self._capture_generation
            sample = sink.emit('try-pull-sample', 0)
            if sample is None: return Gst.FlowReturn.OK
            if not self.capture_requested or not self._valid(): return Gst.FlowReturn.OK
            caps = sample.get_caps().get_structure(0)
            if caps.get_value('format') != 'F32LE' or caps.get_value('rate') != 48000 or caps.get_value('channels') != 1:
                raise ValueError('Unexpected capture format; microphone reconfiguration required.')
            buf = sample.get_buffer()
            if buf.get_size() > 131072:
                raise ValueError('Capture buffer exceeds the bounded processing limit.')
            if not self._first_sample and buf.has_flags(Gst.BufferFlags.DISCONT):
                raise ValueError('Capture discontinuity; microphone reconfiguration required.')
            self._first_sample = False
            with self._dsp_lock:
                filtered = self.processor.feed(buf.extract_dup(0, buf.get_size()))
            self.last_audio = time.monotonic()
            with self._lock:
                # The callback can span an entire suspend/resume cycle. A bool
                # alone would admit its old frame after delivery reopens.
                current_capture = (self.capture_requested and
                    capture_generation == self._capture_generation and self._valid())
                # A completed old callback can advance frame counts without proving fresh resume audio.
                if filtered and current_capture: self.fresh_capture = True
                if (self.output and self._delivery and current_capture and
                        generation == self._delivery_generation):
                    for offset in range(0, len(filtered), 1920):
                        self._queue.append(filtered[offset:offset + 1920])
            return Gst.FlowReturn.OK
        except Exception as error:
            self._error(str(error))
            return Gst.FlowReturn.ERROR

    def start_output(self, token):
        if self.output is not None: raise ValueError('Output publication cannot be repeated.')
        if not self._valid() or self.frames < 1:
            raise ValueError('No verified filtered input is ready for publication.')
        self.output, app, _sink = make_output(token)
        self._watch_bus(self.output)
        app.connect('need-data', self._need_data)
        self._start_pipeline(self.output, 'output')

    def _need_data(self, app, _length):
        if self._stopped.is_set(): return
        try:
            # Keep output clocked while idle or underrunning; only filtered frames replace silence.
            data = bytes(1920)
            with self._lock:
                generation = self._delivery_generation
                if self._delivery and self._valid() and self._queue:
                    data = self._queue.popleft()
            if not self._delivery or generation != self._delivery_generation or not self._valid():
                data = bytes(1920)
            if app.get_property('current-level-bytes') >= 8 * 1920:
                raise ValueError('Filtered output queue exceeded its bound.')
            buf = Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)
            buf.pts = self._position * Gst.SECOND // 48000
            buf.duration = 480 * Gst.SECOND // 48000
            self._position += 480
            if app.emit('push-buffer', buf) not in [Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING]:
                raise ValueError('Filtered output stopped accepting audio.')
        except Exception as error: self._error(str(error))

    def create_link(self, source, capture): self.registry.create_link(source, capture)
    def owns_link(self, ident): return self.registry is not None and self.registry.link_id == ident

    def revoke(self):
        self.processor.revoke()
        self._switch_changed.set()

    def close(self):
        if self._stopped.is_set(): return
        self.revoke()
        self._stopped.set()
        self.stage = 'stopping audio'
        for pipeline in [self.output, self.capture]:
            if pipeline: pipeline.set_state(Gst.State.NULL)
        for bus, ident in self._buses:
            bus.disconnect(ident); bus.remove_signal_watch()
        self._buses.clear()
        if self.registry: self.registry.close()
        # NULL synchronizes streaming callbacks before releasing the C state.
        with self._dsp_lock: self.processor.close()
