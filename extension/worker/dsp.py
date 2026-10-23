"""Xiph RNNoise C API and bounded F32 framing; no device or GUI dependencies."""
import array
import ctypes
import ctypes.util
import math
import threading


class Processor:
    def __init__(self, library=None):
        self.frames = 0
        self._revoked = threading.Event()
        self._pending = array.array('f')
        self._state = None
        try:
            path = library or ctypes.util.find_library('rnnoise')
            if not path:
                raise RuntimeError('RNNoise runtime library is missing; install the distro runtime package manually.')
            self._lib = ctypes.CDLL(path)
            self._lib.rnnoise_get_frame_size.argtypes = []
            self._lib.rnnoise_get_frame_size.restype = ctypes.c_int
            self._lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            self._lib.rnnoise_create.restype = ctypes.c_void_p
            self._lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            self._lib.rnnoise_destroy.restype = None
            self._lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
            self._lib.rnnoise_process_frame.restype = ctypes.c_float
            if self._lib.rnnoise_get_frame_size() != 480:
                raise RuntimeError('RNNoise must support 480-sample frames.')
            self._state = self._lib.rnnoise_create(None)
            if not self._state:
                raise RuntimeError('RNNoise default model could not initialize.')
        except (OSError, AttributeError) as error:
            raise RuntimeError(f'RNNoise runtime API is unavailable: {error}') from error
        self._input = (ctypes.c_float * 480)()
        self._output = (ctypes.c_float * 480)()

    def revoke(self):
        self._revoked.set()

    @property
    def revoked(self):
        return self._revoked.is_set()

    def feed(self, data):
        if self.revoked or not self._state:
            raise ValueError('RNNoise processing session was permanently revoked.')
        try:
            if len(data) % 4 or len(data) > 131072:
                raise ValueError('Invalid or excessive F32 audio buffer.')
            block = array.array('f')
            block.frombytes(data)
            if any(not math.isfinite(value) for value in block):
                raise ValueError('Non-finite microphone sample.')
            self._pending.extend(block)
            result = array.array('f')
            offset = 0
            while len(self._pending) - offset >= 480:
                if self.revoked:
                    raise ValueError('RNNoise processing was revoked.')
                # RNNoise expects float samples at S16 amplitude, not normalized [-1, 1].
                for index in range(480):
                    self._input[index] = max(-1.0, min(1.0, self._pending[offset + index])) * 32768.0
                probability = self._lib.rnnoise_process_frame(self._state, self._output, self._input)
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError('RNNoise rejected its processing state.')
                for value in self._output:
                    if not math.isfinite(value):
                        raise ValueError('RNNoise produced a non-finite sample.')
                    result.append(max(-1.0, min(1.0, value / 32768.0)))
                offset += 480
                self.frames += 1
            del self._pending[:offset]
            if self.revoked:
                raise ValueError('RNNoise processing was revoked.')
            return result.tobytes()
        except Exception:
            self.revoke()
            raise

    def close(self):
        # Caller must first stop streaming; only the capture thread may call feed.
        self.revoke()
        if self._state:
            self._lib.rnnoise_destroy(self._state)
            self._state = None
        self._pending = array.array('f')

    def reset(self):
        # Only while capture is PAUSED, serialized with feed/close by the driver.
        # The permanent revocation event is never cleared by an idle cycle.
        if self.revoked or not self._state:
            raise ValueError('Cannot reset a revoked RNNoise session.')
        replacement = self._lib.rnnoise_create(None)
        if not replacement:
            self.revoke()
            raise RuntimeError('RNNoise could not reset after idle.')
        previous, self._state = self._state, replacement
        self._lib.rnnoise_destroy(previous)
        self._pending = array.array('f')
        # Keep frames cumulative so resume can require a fresh processed frame.

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
