"""Explicit, read-only metrics peers. No discovery, worker control, or persistence."""
import ipaddress
import json
import math
import time
from urllib.parse import urlsplit

from inference_observations import NODE_PROTOCOLS, read_json, load_endpoints as inference_endpoints

POLL_SECONDS = 1


def load_endpoints(root):
    path = root / '.local/metrics-endpoints.json'
    config = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'endpoints': []}
    if not isinstance(config, dict) or config.get('schema_version') != 1:
        raise ValueError('Invalid metrics configuration')
    endpoints = config.get('endpoints')
    if isinstance(endpoints, list):
        nodes = [dict(id=e['id'], device=e['device'], url=e['base_url'] + '/system',
                      approved=True, access='network') for e in inference_endpoints(root)
                 if e.get('protocol') in NODE_PROTOCOLS]
        devices = {e['device'] for e in nodes}
        endpoints = [e for e in endpoints if not isinstance(e, dict) or e.get('device') not in devices] + nodes
    if not isinstance(endpoints, list) or len(endpoints) > 16:
        raise ValueError('Configure at most 16 metrics peers')
    seen = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or any(not isinstance(endpoint.get(k), str)
                or not 0 < len(endpoint[k]) <= 256 for k in ('id', 'device', 'url')):
            raise ValueError('Invalid metrics peer')
        url = urlsplit(endpoint['url'])
        if (endpoint['id'] in seen or endpoint.get('approved') is not True
                or endpoint.get('access') != 'network' or url.scheme not in ('http', 'https')
                or not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path != '/v1/system'):
            raise ValueError('Metrics require explicit network trust and a /v1/system URL')
        if url.scheme == 'http':
            address = ipaddress.ip_address(url.hostname)
            if not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')):
                raise ValueError('Public plaintext metrics are not supported')
        url.port  # Reject malformed ports at configuration time.
        seen.add(endpoint['id'])
    return endpoints


def number(value, limit=2**63):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not 0 <= value <= limit or not math.isfinite(value)):
        raise ValueError('Invalid hardware counter')
    return value


def text(value):
    if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid hardware label')
    return value


def capacity(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid hardware capacity')
    used, total = value.get('used'), value.get('total')
    used = number(used) if used is not None else None
    total = number(total) if total is not None else None
    if used is not None and total is not None and used > total:
        raise ValueError('Hardware usage exceeds capacity')
    return {'used': used, 'total': total}


def validate(sample, now):
    if not isinstance(sample, dict) or sample.get('schema_version') != 1 or sample.get('scope') != 'host':
        raise ValueError('Unsupported host metrics schema')
    instance = text(sample.get('instance_id'))
    sequence = number(sample.get('sample_id'))
    age = number(sample.get('sample_age_seconds'), 12)
    if not instance or not isinstance(sequence, int) or sequence < 1:
        raise ValueError('Invalid sample identity')
    raw = sample.get('hardware')
    if not isinstance(raw, dict):
        raise ValueError('Missing host hardware')
    ticks = raw.get('cpu_ticks')
    if ticks is not None:
        if not isinstance(ticks, dict):
            raise ValueError('Invalid CPU counters')
        ticks = {k: number(ticks.get(k)) for k in ('busy', 'total')}
        if ticks['busy'] > ticks['total']:
            raise ValueError('Invalid CPU totals')
    gpus = raw.get('gpus')
    if not isinstance(gpus, list) or len(gpus) > 32:
        raise ValueError('Invalid GPU counters')
    normalized = []
    for gpu in gpus:
        data = capacity(gpu)
        utilization = gpu.get('utilization')
        normalized.append(dict(data, id=text(gpu.get('id')), name=text(gpu.get('name')),
                               utilization=number(utilization, 100) if utilization is not None else None))
        for field in ('power_watts', 'power_limit_watts'):
            value = gpu.get(field)
            normalized[-1][field] = number(value) if value is not None else None
    cores = raw.get('cores')
    return dict(sampled_at=now - age, instance_id=instance, sample_id=sequence,
                chip=text(raw.get('chip')), os=text(raw.get('os')),
                cores=number(cores, 65536) if cores is not None else None,
                cpu_ticks=ticks, memory=capacity(raw.get('memory')), disk=capacity(raw.get('disk')),
                gpus=normalized)


class Peer:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.identity = None
        self.failures = 0
        self.next_poll = 0
        self.advanced_at = None

    def poll(self, clock=time.monotonic, fetch=read_json):
        now = clock()
        if now < self.next_poll:
            return None
        try:
            sample = validate(fetch(self.endpoint['url']), clock())
            identity = sample['instance_id'], sample['sample_id']
            if self.identity and identity[0] == self.identity[0]:
                if identity[1] < self.identity[1]:
                    raise ValueError('Metrics sample moved backwards')
                if identity[1] == self.identity[1]:
                    if now - self.advanced_at > 12:
                        raise ValueError('Metrics sample did not advance')
                    self.next_poll = now + POLL_SECONDS
                    return None  # The exporter can sample slower than the dashboard.
            # Use receiver time for graph ordering across exporter restarts.
            sample['sampled_at'] = clock()
            sample['reset'] = self.identity is None or identity[0] != self.identity[0]
            self.identity = identity
            self.advanced_at = now
            self.failures = 0
            self.next_poll = now + POLL_SECONDS
            return {'state': 'connected', 'hardware': sample, 'sampled_at': sample['sampled_at']}
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.failures += 1
            self.next_poll = now + min(60, POLL_SECONDS * 2 ** min(self.failures - 1, 6))
            return {'state': 'disconnected', 'sampled_at': clock(), 'detail': str(error)}
