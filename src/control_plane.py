"""Read local stack state and build explicit actions for the terminal UI.

Discovery reports observations. It does not enroll devices or expose APIs.
"""
from dataclasses import dataclass
import ipaddress
import json
from launcher import configured_models
from runtime_config import gateway_port, model_profiles
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import urllib.error
import urllib.request
import telemetry
from inference_observations import local_services, remote_services
from inference_speeds import local_metrics
import harness_service

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str


def catalog(root=ROOT):
    config = json.loads((root / 'config/stack.json').read_text())
    if config.get('schema_version') != 2:
        raise ValueError('Unsupported stack configuration version.')
    deployment = config['deployment']
    if (deployment['applications'] != 'docker' or deployment['router_database'] is not False
            or deployment['application_state'] != 'service_owned'
            or deployment['inference'] != {'mac-metal': 'native', 'linux-cuda': 'docker'}):
        raise ValueError('Invalid deployment or application state policy.')
    service_ids = set()
    compose_names = set()
    for service in config['services']:
        if (service['id'] in service_ids or service['runtime'] != 'docker'
                or service['status'] not in ('available', 'planned')):
            raise ValueError('Invalid or duplicate application service.')
        service_ids.add(service['id'])
        if name := service.get('compose_service'):
            if name in compose_names:
                raise ValueError('Duplicate Compose service mapping.')
            compose_names.add(name)
        if service['role'] == 'routing' and (service['state_owner'] != 'none' or service['retains']):
            raise ValueError('Routing services must not own application data.')
    ids = set()
    for model in config['models']:
        if model['id'] in ids or model['kind'] not in ('chat', 'image'):
            raise ValueError('Invalid or duplicate model profile.')
        ids.add(model['id'])
        if model['status'] not in ('available', 'planned'):
            raise ValueError('Invalid model capability status.')
        if model['runtime'] != deployment['inference'].get(model['platform']):
            raise ValueError('Inference runtime does not match the platform.')
    return config


def host_platform():
    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
        return 'mac-metal'
    if platform.system() == 'Linux':
        return 'linux-cuda'
    return 'client'


def model_rows(root=ROOT):
    pins = json.loads((root / 'config/models.json').read_text())['models']
    rows = []
    for model in catalog(root)['models']:
        row = dict(model, execution=model['runtime'], runtime='planned' if model['status'] == 'planned' else 'unknown',
                   disk_bytes=None, artifacts='Not installed')
        if ref := model.get('model_ref'):
            pin = pins[ref]
            row.update(context=pin['context'], endpoint=f'http://127.0.0.1:{gateway_port(root)}/v1')
            artifacts = [*pin['artifacts'], *([pin['vision_encoder']] if pin.get('vision_encoder') else [])]
            row['disk_bytes'] = sum(a['size'] for a in artifacts)
            complete = all((root / a['path']).is_file() and
                           (root / a['path']).stat().st_size == a['size'] for a in artifacts)
            row['artifacts'] = 'Present; hash not checked' if complete else 'Download required'
        rows.append(row)
    return rows


def configured_keys(path):
    """Read only key names. Never return credentials to diagnostics or widgets."""
    if not path.is_file():
        return set()
    keys = set()
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if match and match[2] not in ('', "''", '""') and not match[2].startswith('#'):
            keys.add(match[1])
    return keys


def capture(args, root=ROOT, timeout=5):
    """Bound read-only probes. Do not return error output that may contain secrets."""
    try:
        with subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, start_new_session=True) as process:
            try:
                output, _ = process.communicate(timeout=timeout)
            finally:
                # CLI plugins can keep pipes open after their leader exits.
                # The isolated group contains only this read-only probe.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return output if process.returncode == 0 else None


def compose_args(*args, root=ROOT):
    return ['docker', 'compose', '--env-file', str(root / '.env'), *args]


def service_rows(root=ROOT):
    """Observe configured services, including stopped or removed containers."""
    if not (root / '.env').is_file():
        return [{'name': 'Stack', 'state': 'not configured', 'health': 'Set up .env'}]
    declared = capture(compose_args('config', '--services', root=root), root)
    names = declared.splitlines() if declared is not None else []
    output = capture(compose_args('ps', '--all', '--format', 'json', root=root), root)
    unknown = [{'name': name, 'state': 'unknown', 'health': 'Probe unavailable'}
               for name in names or ['Stack']]
    if output is None:
        return unknown
    try:
        try:
            entries = json.loads(output) if output.strip() else []
        except ValueError:
            entries = [json.loads(line) for line in output.splitlines() if line.strip()]
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or any(not isinstance(e, dict) or
                                               not isinstance(e.get('Service'), str) for e in entries):
            return unknown
    except ValueError:
        return unknown
    rows = [{'name': e['Service'], 'state': str(e.get('State') or 'unknown'),
             'container_name': str(e.get('Name') or ''),
             'health': str(e.get('Health') or '—'),
             'container_id': str(e.get('ID') or ''),
             'published': published_addresses(e.get('Publishers'))} for e in entries]
    observed = {row['name'] for row in rows}
    rows.extend({'name': name, 'state': 'not created', 'health': '—'}
                for name in names if name not in observed)
    return sorted(rows, key=lambda row: row['name']) or unknown


def published_addresses(publishers):
    addresses = []
    for publisher in publishers if isinstance(publishers, list) else []:
        if not isinstance(publisher, dict) or publisher.get('Protocol') != 'tcp':
            continue
        try:
            host = ipaddress.ip_address(publisher.get('URL'))
            port = publisher.get('PublishedPort')
            if type(port) is not int or not 1 <= port <= 65535:
                continue
            addresses.append(f'[{host}]:{port}' if host.version == 6 else f'{host}:{port}')
        except ValueError:
            continue
    return sorted(set(addresses))


def stack_tunnel(containers, root=ROOT):
    """Observe the dedicated browser tunnel separately from the host peer client."""
    node = next((row for row in containers if row['name'] == 'tailscale'), None)
    if node and node['state'] == 'running':
        output = capture(compose_args('exec', '-T', 'tailscale', 'tailscale',
                                      'serve', 'status', '--json', root=root), root)
        try:
            routes = json.loads(output)
            if isinstance(routes, dict):
                enabled = bool(routes.get('TCP') or routes.get('Web') or routes.get('Foreground'))
                return Check('Stack tunnel', 'enabled' if enabled else 'disabled',
                             'Dedicated stack Serve/Funnel routes are ' + ('configured' if enabled else 'off'))
        except (ValueError, TypeError):
            pass
        return Check('Stack tunnel', 'unknown', 'Cannot read dedicated stack Serve/Funnel routes')
    if any(row['state'] == 'unknown' for row in containers):
        return Check('Stack tunnel', 'unknown', 'Container observation is unavailable')
    return Check('Stack tunnel', 'disabled', 'Dedicated stack tunnel is stopped or not configured; host peer networking is separate')


def application_rows(containers, root=ROOT):
    """Join deployment intent to observations without promoting planned services.

    Container health is process evidence only. Data ownership comes from the
    service contract, never from inspecting application files or credentials.
    """
    observed = {row['name']: row for row in containers}
    rows, mapped = [], set()
    for service in catalog(root)['services']:
        name = service.get('compose_service')
        container = observed.get(name)
        if name:
            mapped.add(name)
        state = 'planned' if service['status'] == 'planned' else 'not configured'
        detail = service['detail']
        if container and service['status'] == 'available':
            state = container_state(container)
            detail += f" Container: {container['state']}; health: {container['health']}."
        elif service['status'] == 'available':
            detail += ' No matching service observation; availability is unverified.'
            state = 'unknown'
        published = container.get('published', []) if container else []
        if published:
            detail += ' Published addresses: ' + ', '.join(published) + '.'
        rows.append(dict(service, state=state, detail=detail, published=published))
    for container in containers:
        if container['name'] not in mapped:
            rows.append({'id': 'compose:' + container['name'], 'name': container['name'],
                         'role': 'observed', 'runtime': 'docker', 'status': 'observed',
                         'state': container_state(container), 'state_owner': 'unverified', 'retains': [],
                         'detail': f"Compose observation: {container['state']}; health: {container['health']}. "
                                   'Application data ownership has not been cataloged.'})
    return rows


def container_state(container):
    if container['state'] == 'running' and container['health'] in ('healthy', 'unhealthy', 'starting'):
        return container['health']
    return container['state']


def tailscale_status(root=ROOT):
    candidates = []
    if shutil.which('tailscale'):
        candidates.append(('host', ['tailscale', 'status', '--json']))
    if shutil.which('docker') and (root / '.env').is_file():
        candidates.append(('container', compose_args('exec', '-T', 'tailscale',
                                                    'tailscale', 'status', '--json', root=root)))
    for source, args in candidates:
        output = capture(args, root)
        if output is not None:
            try:
                status = json.loads(output)
                if not isinstance(status, dict):
                    continue
                if status.get('BackendState') == 'Running':
                    return source, status
            except ValueError:
                continue
    return None, {}


def device_rows(status):
    rows = []
    for peer in status.get('Peer', {}).values():
        if not peer.get('Online'):
            connection = 'Offline'
        elif peer.get('Active') and peer.get('PeerRelay'):
            connection = 'Peer relay (last reported)'
        elif peer.get('Active') and peer.get('CurAddr'):
            connection = 'Direct (last reported)'
        elif peer.get('Active') and peer.get('Relay'):
            connection = 'Relay (last reported)'
        else:
            connection = 'Online; path unknown'
        rows.append({
            'name': peer.get('DNSName', '').rstrip('.') or peer.get('HostName', 'Unknown'),
            'os': peer.get('OS', 'Unknown'),
            'address': ', '.join(peer.get('TailscaleIPs', [])),
            'connection': connection,
            'trust': 'Discovered; not enrolled',
        })
    return sorted(rows, key=lambda p: p['name'])


def inference_status(root=ROOT):
    pins = configured_models(model_profiles(root))
    port = gateway_port(root)
    observation = dict(state='offline', active_model=None,
                       endpoint=f'http://127.0.0.1:{port}/v1')
    detail = 'Gateway not running'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=0.5) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('gateway') != 'ready':
            observation['state'] = 'unknown'
            detail = 'Another service occupies the gateway port'
        else:
            active = result.get('active_model')
            if active is not None and (not isinstance(active, str) or active not in pins):
                observation['state'] = 'unknown'
                detail = 'Gateway reported an unknown model'
            else:
                generating = result.get('generating_model')
                observation.update(state='ready', active_model=active,
                                   generating_model=generating if generating == active else None,
                                   activity_observed='generating_model' in result)
                detail = 'Gateway ready; active model: ' + (active or 'idle')
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return dict(state=observation['state'], detail=detail,
                endpoints={key: dict(observation) for key in pins})


def gateway_status(root=ROOT):
    return inference_status(root)['detail']


def observe_inference(root, models, *, include_remote=True):
    """Refresh inference without repeating Docker, disk, or network discovery."""
    import time
    sampled_at = time.monotonic()
    inference = inference_status(root)
    models = [dict(model) for model in models]
    for model in models:
        endpoint = inference['endpoints'].get(model['id'], {})
        model['runtime'] = ('planned' if model['status'] == 'planned' else
                            'unknown' if inference['state'] == 'unknown' else
                            'loaded' if endpoint.get('active_model') == model['id'] else
                            'standby' if endpoint.get('state') == 'ready' else 'unknown')
        model['activity'] = ('running' if model['runtime'] == 'loaded'
                             and endpoint.get('generating_model') == model['id'] else
                             'loaded' if model['runtime'] == 'loaded' else
                             'unloaded' if model['runtime'] == 'standby' else 'unknown')
        model['activity_observed'] = endpoint.get('activity_observed', False)
        if model.get('engine') == 'omlx':
            model['speed_metrics'] = local_metrics(root, model.get('model_ref', model['id']))
    pins = json.loads((root / 'config/models.json').read_text())['models']
    services = local_services(inference, models, pins)
    for service in services:
        service['sampled_at'] = sampled_at
    remote = remote_services(root) if include_remote else []
    return dict(inference=inference, models=models, inference_services=services + remote,
                **({'harness': harness_service.observe(root)} if include_remote else {}),
                inference_sampled_at=sampled_at)


def snapshot(root=ROOT, include_hardware=True, inference_observation=None, include_remote=True):
    checks = []
    hardware = f'{platform.system()} / {platform.machine()}'
    checks.append(Check('Host', 'ready' if host_platform() == 'mac-metal' else 'observed', hardware))
    if platform.system() == 'Darwin':
        memory = capture(['sysctl', '-n', 'hw.memsize'], root)
        if memory and memory.strip().isdigit():
            checks.append(Check('Host memory', 'observed', f'{int(memory) / 2**30:.1f} GiB total'))
    elif platform.system() == 'Linux':
        try:
            memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
            checks.append(Check('Host memory', 'observed', f'{memory / 2**30:.1f} GiB total'))
        except (OSError, ValueError):
            pass
        gpu = capture(['nvidia-smi', '--query-gpu=name,memory.total',
                       '--format=csv,noheader,nounits'], root)
        checks.append(Check('NVIDIA GPUs', 'observed' if gpu else 'missing',
                            gpu.strip() if gpu else 'No NVIDIA driver report available'))
    disk = shutil.disk_usage(root)
    checks.append(Check('Model storage', 'observed', f'{disk.free / 2**30:.1f} GiB free'))
    tools = [name for name in ('git', 'make', 'hf', 'docker', 'tailscale', 'uv') if shutil.which(name)]
    checks.append(Check('Tools', 'observed', ', '.join(tools) or 'No stack tools found'))
    docker = capture(['docker', 'info', '--format', '{{.ServerVersion}}'], root)
    checks.append(Check('Docker', 'ready' if docker else 'missing',
                        'Daemon available' if docker else 'Start or install Docker'))
    keys = configured_keys(root / '.env')
    configured = (root / '.env').is_file()
    checks.append(Check('Stack configuration', 'configured' if configured else 'missing',
                        'Private environment file present' if configured else 'Configure the root .env'))
    source, status = tailscale_status(root)
    checks.append(Check('Tailscale', 'connected' if source else 'missing',
                        f'Connected through {source}; discovery does not grant access' if source else
                        'No connected host or existing container found'))
    observations = (inference_observation if inference_observation is not None
                    else observe_inference(root, model_rows(root), include_remote=include_remote))
    inference = observations['inference']
    checks.append(Check('Inference', inference['state'], inference['detail']))
    identity_keys = {'AUTHELIA_SESSION_SECRET', 'AUTHELIA_STORAGE_ENCRYPTION_KEY',
                     'AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET'}
    checks.append(Check('Browser password and 2FA', 'configured' if identity_keys <= keys else 'missing',
                        'Settings present; enforcement not verified' if identity_keys <= keys else
                        'Check the private browser account configuration'))
    containers = service_rows(root)
    checks.append(stack_tunnel(containers, root))
    local = status.get('Self') or {}
    result = {'checks': [c.__dict__ for c in checks], 'devices': device_rows(status),
            'local_node': {'name': local.get('HostName') or platform.node(),
                           'host_name': platform.node(),
                           'address': ', '.join(local.get('TailscaleIPs', [])),
                           'source': source, 'connected': bool(source)},
            **({'hardware': telemetry.read(root, capture)} if include_hardware else {}), **observations,
            'containers': containers, 'applications': application_rows(containers, root),
            'platform': host_platform()}
    return result
