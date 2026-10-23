"""Allowlisted engines and immutable per-session options. No devices or downloads."""
import array
import ctypes
import ctypes.util
import math
import threading
from dsp import Processor as RNNoise

DEFAULTS = {'backend': 'rnnoise', 'outputGainDb': 0, 'speexAttenuation': -25,
    'speexAgc': False, 'speexTarget': 8000, 'webrtcLevel': 2,
    'webrtcAgc': False, 'webrtcHighPass': False, 'webrtcTarget': 3}


def configuration(value=None):
    if value is None: value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unknown processing settings.')
    config = {**DEFAULTS, **value}
    if config['backend'] not in ('rnnoise', 'speex', 'webrtc'):
        raise ValueError('Unknown noise suppression backend.')
    for key in ['speexAgc', 'webrtcAgc', 'webrtcHighPass']:
        if type(config[key]) is not bool: raise ValueError(f'Invalid {key}.')
    for key, low, high in [('outputGainDb', -12, 12), ('speexAttenuation', -60, -5),
                          ('speexTarget', 1000, 16000), ('webrtcLevel', 0, 3),
                          ('webrtcTarget', 0, 31)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f'Invalid {key}.')
    return config


# Adapt variable-sized capture blocks to the 480-sample frames shared by these engines.
class Framed:
    def __init__(self):
        self.frames = 0
        self._revoked = threading.Event()
        self._pending = array.array('f')
        self._state = None

    @property
    def revoked(self): return self._revoked.is_set()
    def revoke(self): self._revoked.set()

    def feed(self, data):
        if self.revoked or self._state is None: raise ValueError('Processing was permanently revoked.')
        try:
            if len(data) % 4 or len(data) > 131072: raise ValueError('Invalid or excessive F32 buffer.')
            values = array.array('f'); values.frombytes(data)
            if any(not math.isfinite(v) for v in values): raise ValueError('Non-finite microphone sample.')
            self._pending.extend(values)
            output = array.array('f')
            offset = 0
            while len(self._pending) - offset >= 480:
                if self.revoked: raise ValueError('Processing revoked.')
                frame = self._process(self._pending[offset:offset + 480])
                if len(frame) != 480 or any(not math.isfinite(v) for v in frame):
                    raise ValueError('Invalid processed frame.')
                output.extend(max(-1.0, min(1.0, v)) for v in frame)
                self.frames += 1
                offset += 480
            del self._pending[:offset]
            if self.revoked: raise ValueError('Processing revoked.')
            return output.tobytes()
        except Exception:
            self.revoke()
            raise

    def reset(self):
        if self.revoked or self._state is None: raise ValueError('Cannot reset revoked processing.')
        self._destroy()
        try: self._initialize()
        except Exception:
            self.revoke()
            raise
        self._pending = array.array('f')

    def close(self):
        self.revoke()
        self._destroy()
        self._pending = array.array('f')

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


class Speex(Framed):
    def __init__(self, config):
        super().__init__()
        self.config = config
        path = ctypes.util.find_library('speexdsp')
        if not path: raise RuntimeError('SpeexDSP is missing; install speexdsp / libspeexdsp1 manually.')
        try:
            self.lib = ctypes.CDLL(path)
            self.lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
            self.lib.speex_preprocess_state_init.restype = ctypes.c_void_p
            self.lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
            self.lib.speex_preprocess_state_destroy.restype = None
            self.lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            self.lib.speex_preprocess_ctl.restype = ctypes.c_int
            self.lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16)]
            self.lib.speex_preprocess_run.restype = ctypes.c_int
            self._initialize()
        except Exception:
            self._destroy()
            raise

    def _initialize(self):
        self._state = self.lib.speex_preprocess_state_init(480, 48000)
        if not self._state: raise RuntimeError('Cannot initialize SpeexDSP.')
        try:
            # ABI from speex_preprocess.h: AGC level (6) takes float; others int32.
            for request, value in [(0, ctypes.c_int(1)), (2, ctypes.c_int(self.config['speexAgc'])),
                                   (18, ctypes.c_int(self.config['speexAttenuation'])),
                                   (6, ctypes.c_float(self.config['speexTarget'])),
                                   (30, ctypes.c_int(12))]:
                if self.lib.speex_preprocess_ctl(self._state, request, ctypes.byref(value)) != 0:
                    raise RuntimeError('SpeexDSP rejected its processing settings.')
        except Exception:
            self._destroy()
            raise

    def _process(self, frame):
        data = (ctypes.c_int16 * 480)(*[max(-32768, min(32767, round(v * 32768))) for v in frame])
        self.lib.speex_preprocess_run(self._state, data)
        return [v / 32768 for v in data]

    def _destroy(self):
        if self._state:
            self.lib.speex_preprocess_state_destroy(self._state)
            self._state = None


class WebRTC(Framed):
    def __init__(self, config):
        super().__init__()
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self.Gst, self.config = Gst, config
        Gst.init(None)
        self._initialize()

    def _initialize(self):
        Gst = self.Gst
        pipeline = Gst.Pipeline.new(None)
        self._state = pipeline
        try:
            self.app = Gst.ElementFactory.make('appsrc')
            self.dsp = Gst.ElementFactory.make('webrtcdsp')
            self.sink = Gst.ElementFactory.make('appsink')
            if any(v is None for v in [self.app, self.dsp, self.sink]):
                raise RuntimeError('GStreamer webrtcdsp is missing; install the distro bad plugins package manually.')
            self.app.set_property('caps', Gst.Caps.from_string(
                'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1'))
            self.app.set_property('format', Gst.Format.TIME)
            self.app.set_property('max-bytes', 8 * 960)
            self.app.set_property('max-buffers', 8)
            self.app.set_property('block', False)
            self.sink.set_property('sync', False)
            self.sink.set_property('async', False)
            self.sink.set_property('max-buffers', 8)
            self.sink.set_property('enable-last-sample', False)
            # No synchronized playback reference is routed, so echo cancellation stays disabled.
            for key, value in {'echo-cancel': False, 'noise-suppression': True,
                    'noise-suppression-level': self.config['webrtcLevel'],
                    'gain-control': self.config['webrtcAgc'], 'gain-control-mode': 1,
                    'target-level-dbfs': self.config['webrtcTarget'], 'limiter': True,
                    'high-pass-filter': self.config['webrtcHighPass'], 'voice-detection': False}.items():
                self.dsp.set_property(key, value)
            for item in [self.app, self.dsp, self.sink]: pipeline.add(item)
            if not self.app.link(self.dsp) or not self.dsp.link(self.sink):
                raise RuntimeError('Cannot link the WebRTC processor.')
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('Cannot start the WebRTC processor.')
            self.position = 0
        except Exception:
            self._destroy()
            raise

    def _process(self, frame):
        Gst = self.Gst
        data = array.array('h', [max(-32768, min(32767, round(v * 32768))) for v in frame]).tobytes()
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self.position * Gst.SECOND // 48000
        buf.duration = Gst.SECOND // 100
        self.position += 480
        if self.app.emit('push-buffer', buf) != Gst.FlowReturn.OK:
            raise RuntimeError('WebRTC stopped accepting frames.')
        sample = self.sink.emit('try-pull-sample', Gst.SECOND // 2)
        if sample is None: raise RuntimeError('WebRTC processing timed out.')
        processed = sample.get_buffer()
        if processed.get_size() != 960: raise RuntimeError('WebRTC returned an invalid frame size.')
        values = array.array('h'); values.frombytes(processed.extract_dup(0, 960))
        return [v / 32768 for v in values]

    def _destroy(self):
        if self._state is not None:
            self._state.set_state(self.Gst.State.NULL)
            self._state = None


class OutputLevel:
    """Gain only on already filtered samples; never mixes an unfiltered input."""
    def __init__(self, processor, db):
        self.processor, self.gain = processor, 10 ** (db / 20)
    @property
    def frames(self): return self.processor.frames
    @property
    def revoked(self): return self.processor.revoked
    def revoke(self): self.processor.revoke()
    def reset(self): self.processor.reset()
    def close(self): self.processor.close()
    def feed(self, data):
        output = self.processor.feed(data)
        if self.gain == 1: return output
        values = array.array('f'); values.frombytes(output)
        output = array.array('f', [max(-1, min(1, v * self.gain)) for v in values]).tobytes()
        if self.revoked: raise ValueError('Processing revoked during output gain.')
        return output
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


# Construct only the explicitly selected engine; initialization failure never selects a fallback.
def create_processor(value=None):
    config = configuration(value)
    cls = {'rnnoise': RNNoise, 'speex': Speex, 'webrtc': WebRTC}[config['backend']]
    processor = cls() if cls is RNNoise else cls(config)
    return OutputLevel(processor, config['outputGainDb'])


def check_engines():
    result = []
    for backend in ['rnnoise', 'speex', 'webrtc']:
        try:
            with create_processor({'backend': backend}) as processor:
                if len(processor.feed(bytes(1920))) != 1920:
                    raise RuntimeError('No processed frame produced.')
            result.append({'id': backend, 'ready': True})
        except Exception as error:
            result.append({'id': backend, 'ready': False, 'message': str(error)[:400]})
    return result
