"""Bounded, GI-independent decisions for one authorized microphone volume proxy."""
from dataclasses import dataclass
import math


@dataclass(frozen=True, eq=False)
class Volume:
    channels: tuple
    mute: bool

    def __post_init__(self):
        if (not isinstance(self.channels, tuple) or not 1 <= len(self.channels) <= 64 or
                type(self.mute) is not bool or any(type(v) not in (int, float) or
                not math.isfinite(v) or not 0 <= v <= 10 for v in self.channels)):
            raise ValueError('Invalid or unsupported microphone volume.')

    def __eq__(self, other):
        return (isinstance(other, Volume) and self.mute == other.mute and
            len(self.channels) == len(other.channels) and all(
                math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-7)
                for a, b in zip(self.channels, other.channels)))

    def mono(self):
        return Volume((max(self.channels),), self.mute)

    def follow(self, clean):
        peak, target = max(self.channels), clean.channels[0]
        return Volume(tuple(v / peak * target if peak else target for v in self.channels), clean.mute)


class VolumeBinding:
    """Source wins startup/simultaneous changes; acknowledgments never feed back.

    Writes are acknowledged by observed levels, with a two-second deadline.
    Rapid clean-slider edits coalesce while the physical write is outstanding,
    so hardware rounding of an older request cannot overwrite a newer gesture.
    """
    def __init__(self):
        self.ready = self.closed = False
        self._source = self._clean = None
        self._pending = None
        self._queued = None
        self._obsolete_clean = None

    def _write(self, side, value, now):
        self._pending = (side, value, now)
        return [(side, value)]

    def update(self, source, clean, now):
        if self.closed: return []
        if self._pending and now - self._pending[2] > 2:
            raise ValueError('Microphone volume synchronization timed out; select the microphone again.')
        if clean is None:
            self._source = source
            return []
        if len(clean.channels) != 1:
            raise ValueError('Clean Microphone volume must be mono.')
        previous_source, previous_clean = self._source, self._clean
        self._source, self._clean = source, clean
        if not self.ready:
            # Never copy a restored/default virtual volume into physical hardware.
            if self._pending:
                if clean != self._pending[1]: return []
                self._pending = None
            if clean != source.mono(): return self._write('clean', source.mono(), now)
            self.ready = True
            return []
        source_changed = source != previous_source
        clean_changed = clean != previous_clean
        if self._pending and self._pending[0] == 'source':
            if clean_changed and clean != self._obsolete_clean: self._queued = clean
            # Accept hardware quantization as the actual new level. One physical
            # request at a time makes this acknowledgment unambiguous.
            if not source_changed and source != self._pending[1]: return []
            self._pending = None
            self._obsolete_clean = None
            if self._queued is not None:
                desired, self._queued = self._queued, None
                target = source.follow(desired)
                if target != source: return self._write('source', target, now)
            if clean != source.mono(): return self._write('clean', source.mono(), now)
            return []
        if self._pending:  # virtual write: coalesce physical changes until its echo
            if source_changed: self._queued = source.mono()
            if clean == self._pending[1]:
                self._pending = None
                desired, self._queued = self._queued, None
                if desired is not None and clean != desired: return self._write('clean', desired, now)
                return []
            if not clean_changed: return []
            # A newer explicit virtual edit supersedes the outstanding mirror.
            self._obsolete_clean = self._pending[1]
            self._pending = None
            self._queued = None
        if source_changed:
            if clean != source.mono(): return self._write('clean', source.mono(), now)
        elif clean_changed:
            target = source.follow(clean)
            if target != source: return self._write('source', target, now)
        return []

    def close(self):
        self.closed = True
        self._pending = self._queued = None
