"""Join service observations to explicit model and device identities."""
import ipaddress
from urllib.parse import urlsplit


def endpoint_address(endpoint):
    """Show the configured destination and port without URL credentials or paths."""
    if not endpoint:
        return '—'
    try:
        url = urlsplit(endpoint)
        host = url.hostname
        port = url.port or {'http': 80, 'https': 443}.get(url.scheme)
        if not host or not port:
            return '—'
        return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    except ValueError:
        return '—'


def transport(endpoints, node):
    if node and node.get("location") == "local":
        return "Host"
    if not endpoints:
        return "Unknown" if node else "—"
    try:
        address = ipaddress.ip_address(urlsplit(endpoints[0]).hostname)
    except ValueError:
        return "Unknown"
    if address in ipaddress.ip_network("100.64.0.0/10") or address in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return "Tailscale"
    if address.is_loopback:
        return "Host"
    if address.is_private and not address.is_reserved:
        return "LAN"
    return "Unknown"


def bindings(snapshot):
    models = snapshot['models']
    catalog = {model['id']: model for model in models}
    peers = snapshot.get('devices', [])
    local = snapshot.get('local_node', {})
    local_name = (local.get('host_name') or local.get('name') or 'Local').split('.')[0]
    rows, used_peers = [], set()

    def destination(node):
        if node.get('location') == 'local':
            source = local.get('source') or 'Local'
            info = f"Local device: {local_name}. Topology source: {local.get('name', local_name)} ({source})."
            return local_name, source.title(), info, 'local'
        peer = next((peer for peer in peers if (node.get('address') and node['address'] in peer['address'].split(', '))
                     or node['label'] == peer['name']), None)
        if peer:
            used_peers.add(peer['name'])
        path = peer['connection'].split(' (')[0] if peer else 'Path unknown'
        detail = (f"{peer['name']} · {peer['address']} · {peer['connection']} · {peer['trust']}. "
                  if peer else f"Configured device: {node['label']} · {node.get('address', 'address unknown')}. ")
        return node['label'].split('.')[0], path.replace('Online; path unknown', 'Unknown'), detail, 'remote:' + (peer['name'] if peer else node['label'])

    for service in snapshot['inference_services']:
        if service['role'] != 'service':
            continue
        node = service.get('node')
        if not node or not service.get('endpoints'):
            continue
        device, path, device_detail, device_key = destination(node)
        service_id = service.get('id', service['name'])
        endpoints = service.get('endpoints', [])
        connection = transport(endpoints, node)
        connected = ('connected' if service['state'] in ('ready', 'running')
                     else 'disconnected' if endpoints else 'unknown')
        candidates = []
        for model_id in service.get('model_ids', []):
            profile = catalog.get(service.get('catalog_models', {}).get(model_id))
            model = profile if device_key == 'local' else None
            state = model.get('runtime', 'unknown') if model else service.get('model_states', {}).get(model_id, 'unknown')
            candidates.append((model_id, model, state))
        if not candidates:
            candidates = [('—', None, '')]
        for model_id, model, model_state in candidates:
            profile = model or catalog.get(service.get('catalog_models', {}).get(model_id))
            model_name = service.get('model_names', {}).get(model_id) or (profile.get('name') if profile else None) or model_id
            # Reachability belongs to Connection. State describes this model's work.
            activity = service.get('model_activity', {}).get(model_id)
            if model is not None:
                activity = model.get('activity')
            if activity not in ('running', 'loaded', 'unloaded'):
                activity = {'loaded': 'loaded', 'standby': 'unloaded'}.get(model_state, 'unknown')
            key = model['id'] if model else (service_id if model_id == '—' else service_id + ':' + model_id)
            engine = (service.get('model_engines', {}).get(model_id)
                      or (model.get('engine') if model else None) or service.get('engine') or '—')
            row_endpoints = [model['endpoint']] if model and model.get('endpoint') else endpoints
            detail = (f"{service['name']} · {service['state']} · {device_detail}"
                      f"Model: {model_name}. API/profile ID: {model_id} ({model_state or 'none confirmed'}). State: {activity}. "
                      f"{engine} · {service.get('execution') or 'No runtime'}. "
                      f"Endpoints: {', '.join(row_endpoints) or 'unassigned'}. {service['detail']} "
                      f"Connection: {connection} · {connected}. Remote API reachability is independent of worker state.")
            if model:
                detail += f" Weights: {model['artifacts']}."
                if not model.get('activity_observed', False):
                    detail += ' Active-request reporting requires the updated local gateway; restart that stack to enable it.'
                if model.get('context'):
                    detail += f" Context: {model['context']:,}."
            rows.append(dict(key=key, device=device, device_key=device_key, path=path,
                             sampled_at=service.get('sampled_at', snapshot.get('inference_sampled_at')),
                             connection=connection, connection_state=connected, service=service['name'],
                             model=model_id, model_name=model_name, model_state=model_state,
                             address=endpoint_address(row_endpoints[0] if row_endpoints else None),
                             engine=engine, execution=service.get('execution') or '',
                             state=activity if node else service['state'], detail=detail,
                             speed_metrics=(model.get('speed_metrics', {}) if model else
                                            service.get('model_metrics', {}).get(model_id, {}))))

    for peer in peers:
        if peer['name'] in used_peers:
            continue
        device, path, detail, device_key = destination({'label': peer['name'], 'location': 'remote',
                                          'address': peer['address'].split(', ')[0]})
        rows.append(dict(key='peer:' + peer['name'], device=device, device_key=device_key, path=path,
                         connection='Tailscale',
                         connection_state='disconnected' if path == 'Offline' else 'connected',
                         service='—', model='—', model_name='—', model_state='', engine='—', execution='',
                         address=peer['address'].split(', ')[0] or '—',
                         state='offline' if path == 'Offline' else 'no API',
                         detail=detail + 'No inference endpoint configured; remote hardware telemetry unavailable.'))
    groups = {}
    for row in rows:
        groups.setdefault(row['device_key'], []).append(row)
    # Keep every device contiguous, even when multiple endpoints were configured
    # in a different order. A short display name is never the grouping identity.
    connection_rank = {'connected': 0, 'disconnected': 1, 'unknown': 2}
    ordered = sorted(groups, key=lambda key: (
        min(connection_rank.get(row['connection_state'], 2) for row in groups[key]),
        0 if key == 'local' else 1))
    result = []
    for key in ordered:
        services = {}
        for row in groups[key]:
            services.setdefault(row['service'], []).append(row)
        group = []
        for models in sorted(services.values(), key=lambda rows: min(
                connection_rank.get(row['connection_state'], 2) for row in rows)):
            models.sort(key=lambda row: (connection_rank.get(row['connection_state'], 2),
                                        0 if row['model_state'] in ('loaded', 'running') else 1))
            for index, row in enumerate(models):
                row['service_display'] = row['service'] if index == 0 else ''
                group.append(row)
        for index, row in enumerate(group):
            row['device_display'] = row['device'] if index == 0 else ''
            result.append(row)
    return result
