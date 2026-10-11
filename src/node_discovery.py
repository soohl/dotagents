"""Find gateway APIs on online Tailscale peers at one configured port."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
import subprocess
import tempfile

from inference_observations import NODE_PROTOCOLS, advertised_models, node_models, read_json

from runtime_config import gateway_port


def candidates(status, port):
    peers = status.get('Peer', {})
    if not isinstance(peers, dict):
        raise ValueError('Invalid Tailscale peer list.')
    self_addresses = set((status.get('Self') or {}).get('TailscaleIPs', []))
    seen = set()
    for peer in peers.values():
        if not isinstance(peer, dict) or peer.get('Online') is not True:
            continue
        for value in peer.get('TailscaleIPs', []):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if (value in self_addresses or value in seen or not (
                    address in ipaddress.ip_network('100.64.0.0/10') if address.version == 4 else
                    address in ipaddress.ip_network('fd7a:115c:a1e0::/48'))):
                continue
            seen.add(value)
            host = '[' + value + ']' if address.version == 6 else value
            name = peer.get('HostName') or peer.get('DNSName') or 'Tailscale node'
            if not isinstance(name, str) or not 0 < len(name) <= 256 or any(ord(c) < 32 for c in name):
                name = 'Tailscale node'
            name = name.rstrip('.')
            yield dict(id='tailscale-' + address.packed.hex(), device=name,
                       base_url=f'http://{host}:{port}/v1', approved=True, access='network')
            break


def probe(endpoint):
    try:
        document = read_json(endpoint['base_url'] + '/models')
        models = advertised_models(document)
        for key in ('dotagents', 'sealedllm'):
            extension = document.get(key)
            if isinstance(extension, dict) and extension.get('protocol') in NODE_PROTOCOLS:
                if extension.get('schema_version') != 1:
                    return None
                node = dict(endpoint, protocol=extension['protocol'])
                records = node_models(node, document)
                return dict(node, services=list(dict.fromkeys(m['dotagents']['service'] for m in records)))
        # A standard catalog confirms a chat API, not a particular GPU or engine.
        return dict(endpoint, protocol='openai', engine='OpenAI-compatible', execution='remote',
                    services=['Chat'], models={m['id']: m['id'] for m in models})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def discover(root):
    port = gateway_port(root)
    try:
        result = subprocess.run(['tailscale', 'status', '--json'], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise ValueError('Tailscale status is unavailable.')
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            raise ValueError('Invalid Tailscale status.')
        if status.get('BackendState') != 'Running':
            print('Tailscale is not connected; using the host only.', flush=True)
            return []
        peers = list(candidates(status, port))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print('Tailscale discovery is unavailable; using the host only.', flush=True)
        return []
    print(f'Probing {len(peers)} online Tailscale peers on gateway port {port}…', flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        endpoints = [endpoint for endpoint in pool.map(probe, peers) if endpoint is not None]
    if len(endpoints) > 16:
        raise ValueError('More than 16 gateways responded. Limit access with Tailscale policy.')
    print(f'Found {len(endpoints)} gateway APIs.', flush=True)
    return endpoints


def save(root, endpoints):
    """Replace ephemeral discovery results after verification succeeds."""
    directory = root / '.local'
    directory.mkdir(exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.discovered-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(schema_version=1, endpoints=endpoints), stream, indent=2)
            stream.write('\n')
        os.replace(name, directory / 'discovered-endpoints.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)
