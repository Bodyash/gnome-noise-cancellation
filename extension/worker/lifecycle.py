"""Device authorization and graph supervision, independent of GStreamer/GI."""
import json
import os
import re
import time

OUTPUT_NAME = 'noise_cancellation.clean_microphone'

STABLE = ['device.serial', 'device.bus-path', 'api.bluez5.address', 'api.alsa.path',
          'device.vendor.id', 'device.product.id']


def number(value, maximum=0xffffffff - 1):
    if isinstance(value, bool) or not re.fullmatch(r'0|[1-9][0-9]*', str(value)):
        raise ValueError('Invalid graph identity number.')
    result = int(value)
    if result > maximum:
        raise ValueError('Graph identity number exceeds its range.')
    return result


# Key layout: server cookie, node serial, device serial, node name, API, stable property pairs.
class Authorization:
    def __init__(self, key, source_id):
        if not isinstance(key, str) or len(key.encode()) > 8192:
            raise ValueError('Invalid microphone authorization.')
        self.key = json.loads(key)
        self.source_id = number(source_id)
        k = self.key
        if not isinstance(k, list) or len(k) != 6:
            raise ValueError('Invalid microphone identity key.')
        for value in k[:3]:
            if not isinstance(value, str):
                raise ValueError('Identity serial must be a string.')
            number(value, 2**53 - 1)
        number(k[0], 0xffffffff)
        if not isinstance(k[3], str) or not k[3] or len(k[3]) > 1024 or '\0' in k[3] or re.search(
                r'monitor|clean_microphone|noise_cancellation', k[3], re.I) or k[4] not in ['alsa', 'bluez5']:
            raise ValueError('Selected source is not an eligible physical microphone.')
        if not isinstance(k[5], list) or len(k[5]) != len(STABLE):
            raise ValueError('Physical device identity is incomplete.')
        for name, pair in zip(STABLE, k[5]):
            if not isinstance(pair, list) or len(pair) != 2 or pair[0] != name or (
                pair[1] is not None and (type(pair[1]) not in [str, int] or
                len(str(pair[1])) > 1024 or '\0' in str(pair[1]))):
                raise ValueError('Malformed stable device property.')
        if not any(isinstance(pair[1], str) and pair[1] for pair in k[5][:3]):
            raise ValueError('No stable physical device identity.')

    def validate(self, snapshot):
        objects = {o['id']: o for o in snapshot['objects']}
        node = objects.get(self.source_id, {})
        p = node.get('props', {})
        device_id = number(p.get('device.id', 'invalid'))
        device = objects.get(device_id, {})
        d = device.get('props', {})
        k = self.key
        if (str(snapshot['cookie']) != k[0] or node.get('kind') != 'Node' or
            device.get('kind') != 'Device' or str(p.get('object.serial')) != k[1] or
            str(d.get('object.serial')) != k[2] or p.get('node.name') != k[3] or
            d.get('device.api') != k[4] or p.get('media.class') != 'Audio/Source' or
            str(p.get('node.virtual', '')).lower() in ['true', '1'] or
            str(p.get('stream.monitor', '')).lower() in ['true', '1']):
            raise ValueError('Configured microphone disappeared or changed identity.')
        for name, expected in k[5]:
            actual = d.get(name, p.get(name))
            if (actual is None) != (expected is None) or (actual is not None and str(actual) != str(expected)):
                raise ValueError('Configured physical device identity changed.')
        return device_id


def wireplumber(objects, api, pinned=None):
    """Find relevant service providers, not ephemeral media/export connections."""
    providers, legacy = {}, {}
    service = 'api.bluez' if api == 'bluez5' else 'api.alsa'
    for obj in objects:
        props = obj['props']
        # Secondary contexts can inherit session.services from the primary.
        # They host media objects, not the session-manager control connection.
        if (str(props.get('wireplumber.client-context', '')).lower() in ['true', '1'] or
            re.search(r'\[(?:export|client)\]$', props.get('application.name', ''))):
            continue
        if obj['kind'] != 'Client' or not (
            props.get('application.process.binary') == 'wireplumber' or
            props.get('application.name') == 'WirePlumber' or
            props.get('application.name', '').startswith('WirePlumber [')):
            continue
        services = props.get('session.services')
        if services is not None:
            # WirePlumber's SPA-JSON array contains unquoted service names.
            if not isinstance(services, str) or len(services) > 4096:
                continue
            tokens = set(re.findall(r'[A-Za-z0-9_.-]+', services))
            if service in tokens or any(s.startswith('policy.linking.') for s in tokens):
                providers[obj['id']] = props.get('object.serial')
        elif props.get('application.name') == 'WirePlumber':
            legacy[obj['id']] = props.get('object.serial')
    # Older configurations may disable service announcements. Only a unique,
    # exactly named primary client is acceptable as a conservative fallback.
    if pinned is not None:
        # Additional connections are not a restart. Only the initially pinned
        # providers must remain present, unchanged and relevant.
        return {ident: serial for ident, serial in (providers | legacy).items() if ident in pinned}
    return providers or (legacy if len(legacy) == 1 else {})


class Session:
    def __init__(self, authorization, token, driver, emit, pid=None):
        self.authorization, self.token, self.driver, self.emit = authorization, token, driver, emit
        self.pid = os.getpid() if pid is None else pid
        self.closed = self.prepared = self.publishing = self.active = False
        self.snapshot = None
        self._started = False
        self._link_requested = False
        self._pins = {}
        self._wp = None
        self.capture_id = self.link_id = self.source_port = self.capture_port = None
        self.output_id = None
        self.activity = 'processing'
        self._switch_started = None

    # PipeWire may reuse numeric IDs; each pinned object must retain its original serial.
    def _pin(self, obj):
        serial = str(obj['props'].get('object.serial', ''))
        number(serial, 2**53 - 1)
        previous = self._pins.setdefault(obj['id'], serial)
        if previous != serial:
            raise ValueError('An authorized graph object changed incarnation.')

    def _own_node(self, objects, suffix):
        name = OUTPUT_NAME if suffix == 'source' else f'noise_cancellation.{self.token}.{suffix}'
        candidates = [o for o in objects if o['kind'] == 'Node' and o['props'].get('node.name') == name]
        if len(candidates) > 1:
            raise ValueError('Ambiguous owned audio node.')
        if not candidates:
            return None
        node = candidates[0]
        if suffix == 'source' and node['props'].get('noise-cancellation.session') != self.token:
            raise ValueError('The virtual microphone belongs to another session.')
        # Match the owning worker process as well as the session-specific node identity.
        client_id = number(node['props'].get('client.id', 'invalid'))
        client = next((o for o in objects if o['id'] == client_id and o['kind'] == 'Client'), None)
        if not client or str(client['props'].get('application.process.id')) != str(self.pid):
            raise ValueError('Audio node is not owned by this worker.')
        self._pin(client); self._pin(node)
        return node

    def _port(self, objects, node_id, direction):
        ports = [o for o in objects if o['kind'] == 'Port' and
            o['props'].get('node.id') == str(node_id) and o['props'].get('port.direction') == direction and
            str(o['props'].get('port.monitor', '')).lower() not in ['true', '1']]
        for channel in ['MONO', 'FL']:
            candidates = [o for o in ports if o['props'].get('audio.channel') == channel]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise ValueError('Ambiguous physical microphone channels.')
        if len(ports) > 1:
            raise ValueError('Unsupported ambiguous microphone channel layout.')
        return ports[0] if ports else None

    # Advance startup as graph objects appear; incomplete negotiation waits, identity loss revokes.
    def update(self, snapshot):
        if self.closed:
            return
        try:
            self.snapshot = snapshot
            objects = snapshot['objects']
            if len(objects) > 4096 or len({o['id'] for o in objects}) != len(objects):
                raise ValueError('Excessive or malformed graph snapshot.')
            device_id = self.authorization.validate(snapshot)
            indexed = {o['id']: o for o in objects}
            for ident, serial in self._pins.items():
                if ident not in indexed or str(indexed[ident]['props'].get('object.serial')) != serial:
                    raise ValueError('A required audio graph object disappeared or changed.')
            self._pin(indexed[self.authorization.source_id]); self._pin(indexed[device_id])
            wp = wireplumber(objects, self.authorization.key[4], self._wp)
            if not wp or len(wp) > 8 or any(not value for value in wp.values()):
                raise ValueError('Cannot establish WirePlumber session identity.')
            if self._wp is None:
                self._wp = wp
                for ident in wp: self._pin(indexed[ident])
            if any(wp.get(ident) != serial for ident, serial in self._wp.items()):
                raise ValueError('The microphone\'s WirePlumber service disappeared or changed.')
            if not self._started:
                if not self.driver.sync_volume(self.authorization.source_id): return
                self._started = True
                self.driver.start_capture(self.authorization, self.token)
                return
            capture = self._own_node(objects, 'capture')
            if capture is None:
                return
            self.capture_id = capture['id']
            source = self._port(objects, self.authorization.source_id, 'out')
            target = self._port(objects, self.capture_id, 'in')
            if not source or not target:
                return
            self._pin(source); self._pin(target)
            self.source_port, self.capture_port = source['id'], target['id']
            incoming = [o for o in objects if o['kind'] == 'Link' and
                o['props'].get('link.input.node') == str(self.capture_id)]
            if not self._link_requested:
                if incoming:
                    raise ValueError('Unexpected capture connection before explicit linking.')
                self._link_requested = True
                self.driver.create_link(self.source_port, self.capture_port)
                return
            if not incoming:
                if self.prepared: raise ValueError('Authorized capture link disappeared.')
                return
            if len(incoming) != 1:
                raise ValueError('More than one input is connected to the capture stream.')
            link = incoming[0]; props = link['props']
            expected = {'link.output.node': str(self.authorization.source_id),
                'link.output.port': str(self.source_port), 'link.input.port': str(self.capture_port),
                'noise-cancellation.session': self.token}
            if any(props.get(k) != v for k, v in expected.items()) or link['state'] in ['error', 'unlinked']:
                raise ValueError('Unauthorized or failed physical microphone connection.')
            self._pin(link); self.link_id = link['id']
            if link['state'] != 'active':
                if self.prepared and (not self.active or self.activity == 'processing'):
                    raise ValueError('Authorized capture link became inactive.')
                if not self.active: return
            if not self.driver.owns_link(self.link_id):
                return
            # Only the fully checked physical route may renew the output authorization lease.
            self.driver.renew()
            if (not self.prepared and self.driver.frames > 0 and self.driver.capture_running and
                    self.driver.fresh_capture):
                self.prepared = True
                self.emit('prepared', **self.proof(False))
            if self.publishing:
                output = self._own_node(objects, 'source')
                if self.active and (not output or output['props'].get('media.class') != 'Audio/Source'):
                    raise ValueError('The owned virtual microphone changed or disappeared.')
                if output and output['props'].get('media.class') == 'Audio/Source':
                    self.output_id = output['id']
                    if not self.driver.sync_volume(self.authorization.source_id, self.output_id):
                        self.driver.set_delivery(False)
                        return
                    if not self.active:
                        self.active = True
                        self.emit('active', **self.proof(True), outputId=self.output_id)
                    self._demand(objects, link['state'])
        except Exception as error:
            self.fault(str(error))

    def _demand(self, objects, link_state):
        # A default-source preference is metadata, not a recording connection.
        # The independent output pipeline can negotiate/serve silence while
        # physical capture sleeps. Only ACTIVE links demand the real input;
        # paused/corked consumers do not need capture to bootstrap their graph.
        nodes = {str(o['id']) for o in objects if o['kind'] == 'Node'}
        demand = any(o['kind'] == 'Link' and o.get('state') == 'active' and
            o['props'].get('link.output.node') == str(self.output_id) and
            o['props'].get('link.input.node') in nodes for o in objects)
        self.driver.request_capture(demand)
        if not demand:
            self.driver.set_delivery(False)
            state = 'idle' if self.driver.capture_idle else 'suspending'
        elif (self.driver.capture_running and self.driver.fresh_capture and link_state == 'active' and
              self.driver.frames > self.driver.resume_baseline):
            self.driver.set_delivery(True)
            state = 'processing'
        else:
            self.driver.set_delivery(False)
            state = 'resuming'
        if state != self.activity:
            self.activity = state
            if state not in ['resuming', 'suspending']:
                self._switch_started = None
            elif self._switch_started is None:
                self._switch_started = time.monotonic()
            self.emit('activity', **{**self.proof(True), 'processing': state == 'processing'},
                state=state, outputId=self.output_id, resumeBaseline=self.driver.resume_baseline)
        if self._switch_started is not None and time.monotonic() - self._switch_started > 8:
            stage = str(getattr(self.driver, 'stage', 'unknown native stage'))[:100]
            raise ValueError(f'Microphone {state} timed out: {stage}; '
                f'capture-running={self.driver.capture_running}, link={link_state}, '
                f'fresh-frame={self.driver.fresh_capture and self.driver.frames > self.driver.resume_baseline}.')

    def proof(self, published):
        return {'sourceId': self.authorization.source_id, 'sourceSerial': self.authorization.key[1],
            'cookie': int(self.authorization.key[0]), 'processing': True, 'published': published,
            'rate': 48000, 'channels': 1, 'processedFrames': self.driver.frames, 'captureLinkId': self.link_id}

    def publish(self):
        if self.closed or not self.prepared or self.publishing:
            raise ValueError('Output publication requires a fresh prepared processing session.')
        self.update(self.snapshot)
        if self.closed:
            raise ValueError('Microphone authorization was revoked.')
        self.publishing = True
        self.driver.start_output(self.token)

    def removed(self, ident):
        if ident in self._pins:
            if self._wp and ident in self._wp:
                reason = 'The microphone\'s WirePlumber service disappeared.'
            elif ident == self.authorization.source_id:
                reason = 'The selected microphone disappeared.'
            else:
                reason = 'A required microphone or processing graph object disappeared.'
            self.fault(reason)

    # Closing is permanent for this session; neither hotplug nor consumer demand can reopen it.
    def fault(self, message):
        if self.closed:
            return
        self.closed = True
        self.driver.revoke()
        try: self.emit('fault', message=message[:400])
        finally: self.driver.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.driver.revoke()
            self.driver.close()
