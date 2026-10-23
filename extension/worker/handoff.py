"""Bounded tile-off handoff of the current input; never changes saved defaults."""
import json
import time

from lifecycle import OUTPUT_NAME

CURRENT = 'default.audio.source'
CONFIGURED = 'default.configured.audio.source'


def default_name(metadata, key):
    value, kind = metadata.read(key)
    if value is None: return None
    if kind != 'Spa:String:JSON' or not isinstance(value, str) or len(value) > 4096:
        raise ValueError('Invalid default input metadata.')
    data = json.loads(value)
    name = data.get('name') if isinstance(data, dict) else None
    if not isinstance(name, str) or not 1 <= len(name) <= 1024 or '\0' in name:
        raise ValueError('Invalid default input name.')
    return name


class DefaultMetadata:
    """Cache only two keys from GI's changed signal, including the initial replay.

    Wp 0.5 Metadata.find's out-string ownership annotation is unsafe through GI;
    MetadataItem iteration also lacks a usable Python value type. Signals avoid
    both paths without adding bindings or invoking an external control tool.
    """
    def __init__(self, target):
        self.target = target
        self.ready = False
        self.values = {}

    def update(self, subject, key, kind, value):
        if subject != 0: return
        if key is None:
            self.values.clear()
        elif key in (CURRENT, CONFIGURED):
            if value is None: self.values.pop(key, None)
            else: self.values[key] = (value[:4097], kind[:64] if isinstance(kind, str) else None)

    def read(self, key): return self.values.get(key, (None, None))

    def set(self, subject, key, kind, value): self.target.set(subject, key, kind, value)


class DefaultHandoff:
    def __init__(self, capture_id, source, metadata, sync, done, clock=time.monotonic, output_id=None):
        self.capture_id, self.source, self.metadata = capture_id, source, metadata
        self.output_id = output_id
        self.sync, self.done, self.clock = sync, done, clock
        self.deadline = clock() + 0.4
        self.closed = self.waiting = False
        self.acknowledged = False
        self._snapshot = None
        self.target = None

    def poll(self, snapshot):
        if self.closed: return
        self._snapshot = snapshot
        if self.clock() >= self.deadline:
            self._finish(False)
            return
        if self.waiting:
            if not self.acknowledged: return
            try:
                if (self.source(snapshot) != self.target or
                        default_name(self.metadata(), CURRENT) != self.target):
                    self._finish(False)
                elif not any(o.get('kind') == 'Link' and o.get('state') not in ('error', 'unlinked') and
                        o.get('props', {}).get('link.output.node') == str(self.output_id)
                        for o in snapshot['objects']):
                    self._finish(True)
            except Exception: self._finish(False)
            return
        if any(o['id'] == self.capture_id for o in snapshot['objects']): return
        try:
            target = self.source(snapshot)
            metadata = self.metadata()
            if (target is None or metadata is None or default_name(metadata, CURRENT) != OUTPUT_NAME or
                    default_name(metadata, CONFIGURED) not in (None, OUTPUT_NAME)):
                self._finish(False)
                return
            self.waiting = True
            self.target = target
            # Only the effective default changes. WirePlumber retains the user's
            # configured Clean preference and restores it on the next enable.
            metadata.set(0, CURRENT, 'Spa:String:JSON', json.dumps({'name': target}))

            def acknowledged(success):
                if self.closed: return
                try:
                    success = (success and self.metadata() is metadata and
                        default_name(metadata, CURRENT) == target)
                except Exception: success = False
                if not success: self._finish(False)
                else:
                    self.acknowledged = True
                    self.poll(self._snapshot)
            self.sync(acknowledged)
        except Exception:
            self._finish(False)  # Default-input convenience must never prevent cleanup.

    def _finish(self, success):
        if self.closed: return
        self.close()
        self.done(success)

    def close(self):
        self.closed = True
