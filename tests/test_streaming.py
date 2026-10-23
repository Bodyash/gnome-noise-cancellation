"""Real GStreamer synthetic tests, no PipeWire server or physical input."""
import array
import math
import os
from pathlib import Path
import sys
import time
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['GST_REGISTRY'] = str(ROOT / 'build/gst-registry.bin')
sys.path.insert(0, str(ROOT / 'extension/worker'))
from streaming import AudioDriver, check_runtime, make_capture, make_output, configure_output, Gst, GLib
from dsp import Processor


class StreamingTests(unittest.TestCase):
    def test_brief_consumer_churn_does_not_pause_and_restart_native_capture(self):
        states = []
        paused = threading.Event()
        class Pipeline:
            def set_state(self, state):
                states.append(state)
                if state == Gst.State.PAUSED: paused.set()
                return Gst.StateChangeReturn.SUCCESS
        driver = AudioDriver(lambda _: None, lambda: None)
        driver.capture = Pipeline()
        try:
            driver.request_capture(True)
            deadline = time.monotonic() + 0.5
            while not driver.capture_running and time.monotonic() < deadline: time.sleep(0.002)
            self.assertTrue(driver.capture_running)
            driver.request_capture(False)
            self.assertFalse(driver._delivery)
            self.assertFalse(paused.wait(0.04), 'A brief consumer gap immediately churned the native pipeline')
            driver.request_capture(True)
            deadline = time.monotonic() + 0.5
            while not driver.capture_running and time.monotonic() < deadline: time.sleep(0.002)
            self.assertTrue(driver.capture_running)
            self.assertEqual(states, [Gst.State.PLAYING])
            self.assertFalse(driver._delivery, 'Return of demand still needs fresh routing/frame validation')
        finally: driver.close()

    def test_revocation_wakes_idle_settling_and_never_resumes_capture(self):
        states = []
        class Pipeline:
            def set_state(self, state):
                states.append(state)
                return Gst.StateChangeReturn.SUCCESS
        driver = AudioDriver(lambda _: None, lambda: None)
        driver.capture = Pipeline()
        try:
            driver.request_capture(True)
            deadline = time.monotonic() + 0.5
            while not driver.capture_running and time.monotonic() < deadline: time.sleep(0.002)
            self.assertTrue(driver.capture_running)
            driver.request_capture(False)
            deadline = time.monotonic() + 0.5
            while driver.stage != 'waiting for stable idle' and time.monotonic() < deadline: time.sleep(0.002)
            self.assertEqual(driver.stage, 'waiting for stable idle')
            thread = driver._capture_thread
            driver.revoke()
            thread.join(0.1)
            self.assertFalse(thread.is_alive(), 'Revocation did not wake the idle wait')
            self.assertEqual(states, [Gst.State.PLAYING])
            with self.assertRaises(ValueError): driver.request_capture(True)
            with self.assertRaises(ValueError): driver.set_delivery(True)
        finally: driver.close()

    def test_frame_from_before_delivery_gate_cycle_cannot_enter_output(self):
        entered, release = threading.Event(), threading.Event()
        driver = AudioDriver(lambda _: None, lambda: None)
        original = driver.processor
        class SlowProcessor:
            def __getattr__(self, name): return getattr(original, name)
            def feed(self, data):
                entered.set(); release.wait(0.5)
                return original.feed(data)
        driver.processor = SlowProcessor()
        class Pipeline:
            def set_state(self, _state): return Gst.StateChangeReturn.SUCCESS
        driver.capture = Pipeline()
        driver.capture_requested = driver._capture_current = True
        driver.output = object()
        driver.renew(); driver.set_delivery(True)
        data = array.array('f', [0.2] * 480).tobytes()
        buffer = Gst.Buffer.new_allocate(None, len(data), None); buffer.fill(0, data)
        sample = Gst.Sample.new(buffer, Gst.Caps.from_string(
            'audio/x-raw,format=F32LE,rate=48000,channels=1'), None, None)
        class Sink:
            def emit(self, *_args): return sample
        callback = threading.Thread(target=lambda: driver._sample(Sink()), daemon=True)
        try:
            callback.start(); self.assertTrue(entered.wait(0.2))
            driver.request_capture(False)
            driver.request_capture(True)
            driver.set_delivery(True)
            release.set(); callback.join(1)
            self.assertFalse(callback.is_alive())
            self.assertEqual(len(driver._queue), 0, 'A frame from a previous gate cycle leaked into the fresh output queue')
            self.assertFalse(driver.processor.revoked)
            self.assertFalse(driver.fresh_capture, 'A stale callback counted as fresh resume evidence')
            self.assertEqual(driver._sample(Sink()), Gst.FlowReturn.OK)
            self.assertTrue(driver.fresh_capture, 'A fresh callback failed to establish resume evidence')
        finally:
            release.set(); callback.join(1)
            driver.output = None
            driver.close()

    def test_late_sample_callback_cannot_block_after_sink_has_paused(self):
        # appsink's pull-sample waits for PLAYING. A callback already emitted
        # before pause may enter Python only after the sink changes state.
        for backend in ['rnnoise', 'speex', 'webrtc']:
            with self.subTest(backend=backend):
                driver = AudioDriver(lambda _: None, lambda: None, {'backend': backend})
                sink = Gst.ElementFactory.make('appsink')
                sink.set_property('async', False)
                sink.set_state(Gst.State.PAUSED)
                finished = threading.Event()
                results = []
                def late_callback():
                    try: results.append(driver._sample(sink))
                    finally: finished.set()
                driver.capture_requested = False
                callback = threading.Thread(target=late_callback, daemon=True)
                try:
                    callback.start()
                    self.assertTrue(finished.wait(0.2), 'Late sample callback blocked waiting for PLAYING during pause')
                    self.assertEqual(results, [Gst.FlowReturn.OK])
                    self.assertEqual(driver.frames, 0)
                    self.assertFalse(driver.processor.revoked)
                finally:
                    sink.set_state(Gst.State.NULL)
                    callback.join(1)
                    driver.close()

    def test_real_eos_still_revokes_after_empty_nonblocking_sample(self):
        failures = []
        driver = AudioDriver(failures.append, lambda: None)
        capture = Gst.Pipeline.new(None)
        driver.capture = capture
        sink = Gst.ElementFactory.make('appsink')
        sink.set_property('async', False)
        sink.set_state(Gst.State.PAUSED)
        try:
            driver.capture_requested = False
            self.assertEqual(driver._sample(sink), Gst.FlowReturn.OK)
            self.assertFalse(driver.processor.revoked)
            driver._watch_bus(capture)
            bus = capture.get_bus()
            bus.set_flushing(False)
            self.assertTrue(bus.post(Gst.Message.new_eos(capture)))
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 0.5
            while not failures and time.monotonic() < deadline:
                while context.pending(): context.iteration(False)
                time.sleep(0.005)
            self.assertTrue(driver.processor.revoked)
            self.assertTrue(failures and 'stream ended' in failures[0])
            with self.assertRaises(ValueError): driver.request_capture(True)
        finally:
            sink.set_state(Gst.State.NULL)
            driver.close()

    def test_capture_sink_does_not_wait_for_preroll_during_idle(self):
        capture, _source, sink = make_capture('check', '0')
        try:
            self.assertFalse(sink.get_property('async'),
                'Capture readiness comes from routing/fresh frames, not appsink preroll')
        finally: capture.set_state(Gst.State.NULL)

    def test_output_stays_clock_paced_when_sink_discards_buffers(self):
        driver = AudioDriver(lambda _: None, lambda: None)
        pipeline = Gst.Pipeline.new(None)
        app, sink = Gst.ElementFactory.make('appsrc'), Gst.ElementFactory.make('fakesink')
        configure_output(pipeline, app, sink)
        sink.set_property('async', False)
        pipeline.add(app); pipeline.add(sink); self.assertTrue(app.link(sink))
        driver.output = pipeline
        calls = []
        def need_data(source, length):
            calls.append(time.monotonic())
            driver._need_data(source, length)
        app.connect('need-data', need_data)
        try:
            driver.renew(); driver.set_delivery(False)
            pipeline.set_state(Gst.State.PLAYING)
            time.sleep(0.12)
            self.assertGreater(len(calls), 0)
            self.assertLessEqual(len(calls), 25, 'Idle output is spinning instead of clock paced')
            self.assertFalse(driver.processor.revoked)
        finally: driver.close()
    def test_real_synthetic_capture_stops_in_idle_and_resumes_without_audio_server(self):
        for backend in ['rnnoise', 'speex', 'webrtc']:
            with self.subTest(backend=backend): self._synthetic_idle_resume(backend)

    def _synthetic_idle_resume(self, backend):
        driver = AudioDriver(lambda message: failures.append(message), lambda: None, {'backend': backend})
        failures = []
        pipeline = Gst.parse_launch('audiotestsrc is-live=true samplesperbuffer=1024 freq=160 volume=0.2 ! '
            'audio/x-raw,format=F32LE,rate=48000,channels=1 ! appsink name=frames emit-signals=true sync=false async=false')
        output = Gst.Pipeline.new(None)
        app, target = Gst.ElementFactory.make('appsrc'), Gst.ElementFactory.make('fakesink')
        configure_output(output, app, target)
        target.set_property('async', False)
        target.set_property('signal-handoffs', True)
        delivered = []
        def delivered_sample(_target, buffer, _pad):
            samples = array.array('f')
            samples.frombytes(buffer.extract_dup(0, buffer.get_size()))
            delivered.append(max(abs(value) for value in samples))
        target.connect('handoff', delivered_sample)
        output.add(app); output.add(target); self.assertTrue(app.link(target))
        app.connect('need-data', driver._need_data)
        driver.capture, driver.output = pipeline, output
        pipeline.get_by_name('frames').connect('new-sample', driver._sample)
        def wait(predicate):
            until = time.monotonic() + 1
            while not predicate() and time.monotonic() < until:
                driver.renew(); time.sleep(0.005)
            self.assertTrue(predicate(), failures)
        try:
            output.set_state(Gst.State.PLAYING)
            driver.renew(); driver.request_capture(True)
            wait(lambda: driver.frames >= 3 and driver.capture_running and driver.fresh_capture)
            driver.set_delivery(True)
            wait(lambda: delivered and max(delivered) > 0.0001)
            before_flush = len(delivered)
            driver.request_capture(False)
            wait(lambda: driver.capture_idle)
            count = driver.frames
            time.sleep(0.03)
            self.assertEqual(driver.frames, count)
            self.assertFalse(driver.processor.processor._pending)
            wait(lambda: len(delivered) > before_flush + 12)
            self.assertEqual(delivered[-3:], [0, 0, 0])
            driver.renew(); driver.request_capture(True)
            wait(lambda: driver.frames > count and driver.capture_running and driver.fresh_capture)
            self.assertFalse(driver._delivery)
            delivered.clear()
            driver.set_delivery(True)
            wait(lambda: delivered and max(delivered) > 0.0001)
            self.assertFalse(failures)
        finally: driver.close()

    def test_capture_idle_resume_serializes_switches_clears_audio_and_keeps_output(self):
        states, events = [], []
        class Pipeline:
            def set_state(self, state): states.append(state); return Gst.StateChangeReturn.SUCCESS
        class Output:
            def send_event(self, event): events.append(event.type); return True
            def set_state(self, _state): return Gst.StateChangeReturn.SUCCESS
        driver = AudioDriver(lambda _: None, lambda: None)
        driver.capture, driver.output = Pipeline(), Output()
        def wait():
            deadline = time.monotonic() + 1
            while driver.transitions_pending and time.monotonic() < deadline: time.sleep(0.005)
            self.assertFalse(driver.transitions_pending)
        try:
            driver.request_capture(True); wait()
            driver.processor.feed(bytes(1920))
            driver._queue.append(bytes([1]) * 1920)
            driver.request_capture(False)
            self.assertFalse(driver._delivery)
            wait()
            self.assertTrue(driver.capture_idle)
            self.assertFalse(driver._queue)
            self.assertEqual(events, [Gst.EventType.FLUSH_START, Gst.EventType.FLUSH_STOP])
            driver.last_audio = time.monotonic() - 120
            driver.request_capture(True); wait()
            self.assertEqual(driver.last_audio, 0)
            self.assertTrue(driver.capture_running)
            self.assertFalse(driver._delivery)  # registry must prove fresh processing first
            self.assertEqual(driver.resume_baseline, driver.frames)
            self.assertEqual(states, [Gst.State.PLAYING, Gst.State.PAUSED, Gst.State.PLAYING])
            driver.revoke()
            with self.assertRaises(ValueError): driver.request_capture(True)
        finally: driver.close()

    def test_rapid_capture_requests_coalesce_behind_one_transition(self):
        entered, release = threading.Event(), threading.Event()
        states = []
        class Pipeline:
            def set_state(self, state):
                states.append(state)
                if state == Gst.State.PLAYING:
                    entered.set(); release.wait(0.5)
                return Gst.StateChangeReturn.SUCCESS
        driver = AudioDriver(lambda _: None, lambda: None)
        driver.capture = Pipeline()
        try:
            driver.request_capture(True); self.assertTrue(entered.wait(0.2))
            driver.processor.feed(bytes(1920))
            for _ in range(50): driver.request_capture(False); driver.request_capture(True)
            self.assertEqual(driver.resume_baseline, driver.frames)
            driver.request_capture(False)
            self.assertEqual(len(driver._transitions), 1)
            release.set()
            deadline = time.monotonic() + 0.5
            while driver.transitions_pending and time.monotonic() < deadline: time.sleep(0.005)
            self.assertTrue(driver.capture_idle)
            self.assertEqual(states, [Gst.State.PLAYING, Gst.State.PAUSED])
        finally: release.set(); driver.close()

    def test_cancelled_start_cleans_up_its_late_transition(self):
        entered, release = threading.Event(), threading.Event()
        states = []
        class Pipeline:
            def set_state(self, state):
                states.append(state)
                if state == Gst.State.PLAYING:
                    entered.set()
                    release.wait(0.5)
                return Gst.StateChangeReturn.SUCCESS
        changes = []
        driver = AudioDriver(lambda _: None, lambda: changes.append('changed'))
        pipeline = Pipeline()
        driver.capture = pipeline
        try:
            driver._start_pipeline(pipeline, 'capture')
            self.assertTrue(entered.wait(0.2))
            driver.close()
            release.set()
            deadline = time.monotonic() + 0.5
            while driver.transitions_pending and time.monotonic() < deadline: time.sleep(0.005)
            self.assertFalse(driver.transitions_pending)
            self.assertEqual(states[-1], Gst.State.NULL)
            self.assertEqual(states.count(Gst.State.NULL), 2)
            self.assertEqual(changes, [])
            self.assertTrue(driver.processor.revoked)
        finally:
            release.set(); driver.close()

    def test_capture_start_does_not_block_graph_linking_or_control_loop(self):
        entered, linked = threading.Event(), threading.Event()
        class Pipeline:
            def set_state(self, state):
                if state == Gst.State.PLAYING:
                    entered.set()
                    if not linked.wait(0.5): raise RuntimeError('No graph-link event could run')
                return Gst.StateChangeReturn.SUCCESS
        class Sink:
            def connect(self, *_args): pass
        failures = []
        driver = AudioDriver(failures.append, lambda: None)
        authorization = type('Authorization', (), {'key': ['10', '100']})()
        try:
            with patch('streaming.make_capture', return_value=(Pipeline(), None, Sink())), \
                 patch.object(driver, '_watch_bus'):
                driver.start_capture(authorization, 'test')
                self.assertTrue(entered.wait(0.2))
                self.assertFalse(failures)
                # This represents the GLib registry callback that must run while
                # pipewiresrc waits for its explicit link/format negotiation.
                linked.set()
                deadline = time.monotonic() + 0.5
                while driver.transitions_pending and time.monotonic() < deadline: time.sleep(0.005)
                self.assertFalse(driver.transitions_pending)
                self.assertFalse(driver.processor.revoked)
        finally:
            linked.set(); driver.close()

    def test_complete_runtime_check_is_read_only(self):
        result = check_runtime()
        self.assertTrue(result['ready'], result)
        self.assertEqual(result['frameSize'], 480)

    def test_capture_and_output_are_explicit_and_bounded(self):
        capture, source, sink = make_capture('test', '100')
        output, app, target = make_output('test')
        try:
            self.assertFalse(source.get_property('autoconnect'))
            self.assertFalse(source.get_property('resend-last'))
            self.assertEqual(source.get_property('target-object'), '100')
            props = source.get_property('stream-properties')
            self.assertEqual(props.get_value('node.dont-reconnect'), 'true')
            self.assertEqual(props.get_value('node.autoconnect'), 'false')
            self.assertLessEqual(sink.get_property('max-buffers'), 8)
            self.assertEqual(target.get_property('mode').value_nick, 'provide')
            self.assertTrue(target.get_property('sync'))
            self.assertEqual(target.get_property('stream-properties').get_value('media.class'), 'Audio/Source')
            output_props = target.get_property('stream-properties')
            self.assertEqual(output_props.get_value('node.name'), 'noise_cancellation.clean_microphone')
            self.assertEqual(output_props.get_value('noise-cancellation.session'), 'test')
            self.assertEqual(output_props.get_value('node.pause-on-idle'), 'true')
            self.assertLessEqual(app.get_property('max-bytes'), 15360)
        finally:
            capture.set_state(Gst.State.NULL); output.set_state(Gst.State.NULL)

    def test_real_resampling_and_rnnoise_without_audio_server(self):
        pipeline = Gst.parse_launch('audiotestsrc num-buffers=20 samplesperbuffer=441 wave=sine ! '
            'audio/x-raw,rate=44100,channels=1 ! audioconvert ! audioresample ! '
            'audio/x-raw,format=F32LE,rate=48000,channels=1 ! appsink name=frames sync=false')
        sink = pipeline.get_by_name('frames')
        total = 0
        try:
            pipeline.set_state(Gst.State.PLAYING)
            with Processor() as processor:
                while True:
                    sample = sink.emit('try-pull-sample', 2 * Gst.SECOND)
                    if sample is None: break
                    caps = sample.get_caps().get_structure(0)
                    self.assertEqual(caps.get_value('rate'), 48000)
                    buf = sample.get_buffer()
                    filtered = processor.feed(buf.extract_dup(0, buf.get_size()))
                    values = array.array('f'); values.frombytes(filtered)
                    self.assertTrue(all(math.isfinite(v) and abs(v) <= 1 for v in values))
                    total += len(values)
                self.assertGreaterEqual(processor.frames, 19)
            self.assertGreater(total, 9000)
            self.assertTrue(sink.get_property('eos'))
        finally: pipeline.set_state(Gst.State.NULL)

    def test_expired_supervision_produces_silence_and_cannot_reopen(self):
        class Sink:
            def __init__(self): self.buffers = []
            def get_property(self, _name): return 0
            def emit(self, _name, buf): self.buffers.append(buf); return Gst.FlowReturn.OK
        driver, sink = AudioDriver(lambda _: None, lambda: None), Sink()
        try:
            driver.renew()
            driver._queue.append(array.array('f', [0.25] * 480).tobytes())
            driver._need_data(sink, 1920)
            self.assertNotEqual(sink.buffers[-1].extract_dup(0, 1920), bytes(1920))
            driver._queue.append(array.array('f', [0.25] * 480).tobytes())
            driver.deadline = time.monotonic() - 1
            driver._need_data(sink, 1920)
            self.assertEqual(sink.buffers[-1].extract_dup(0, 1920), bytes(1920))
            self.assertTrue(driver.processor.revoked)
            with self.assertRaises(ValueError): driver.renew()
        finally: driver.close()

    def test_registry_api_can_be_constructed_without_connecting(self):
        from routing import Registry
        registry = Registry('offline', lambda _: None, lambda _: None, lambda _: None)
        registry.close(); registry.close()


if __name__ == '__main__': unittest.main()
