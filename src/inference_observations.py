"""Read approved nodes and legacy endpoints. Never manage remote workers."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import urllib.parse
import urllib.request

from inference_speeds import health_metrics

NODE_PROTOCOLS = ('dotagents-node', 'sealedllm-node')


def service_names(endpoint):
    """Treat legacy generation/editing entries as one Image capability."""
    return list(dict.fromkeys('Image' if name in ('Image generation', 'Image editing') else name
                             for name in endpoint['services']))


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    request = urllib.request.Request(url, headers=headers or {})
    with opener.open(request, timeout=2) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('Endpoint response exceeds the observation limit')
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError('Expected an object from the endpoint')
    return result


def advertised_models(result):
    """Validate a bounded model list without substituting configured models."""
    models = result.get('data')
    if (not isinstance(models, list) or len(models) > 256 or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or not 0 < len(item['id']) <= 256
            or any(ord(c) < 32 for c in item['id']) for item in models)):
        raise ValueError('Invalid model catalog')
    return list({item['id']: item for item in models}.values())


def local_services(inference, models, pins):
    """Use loopback API advertisements; profiles only enrich matching records."""
    rows = []
    for key, endpoint in inference.get('endpoints', {}).items():
        base = endpoint['endpoint']
        service = dict(id='local:' + key, name='Chat', role='service',
                       state='unknown', node={'label': 'Local', 'location': 'local'},
                       endpoints=[base], engine='—', execution='native', model_ids=[],
                       model_names={}, model_engines={}, catalog_models={},
                       model_states={}, model_activity={}, detail='Local model catalog unavailable.')
        try:
            records = advertised_models(read_json(base + '/models'))
            service.update(state='ready', detail='Local API advertises these models. Residency uses gateway health.')
            for record in records:
                model_id = record['id']
                service['model_ids'].append(model_id)
                for field, target in [('name', 'model_names'), ('engine', 'model_engines')]:
                    value = record.get(field)
                    if isinstance(value, str) and 0 < len(value) <= 256 and all(ord(c) >= 32 for c in value):
                        service[target][model_id] = value
                profile = next((m for m in models if m.get('endpoint') == base
                                and pins.get(m.get('model_ref', m['id']), {}).get('api_model') == model_id), None)
                if profile:
                    service['catalog_models'][model_id] = profile['id']
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        rows.append(service)
    return rows


def load_endpoints(root):
    path = root / '.local/discovered-endpoints.json'
    if not path.exists():
        return []
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get('schema_version') != 1 or not isinstance(config.get('endpoints'), list):
        raise ValueError('Invalid local inference endpoint configuration')
    if len(config['endpoints']) > 16:
        raise ValueError('Configure at most 16 observed endpoints')
    ids = set()
    for endpoint in config['endpoints']:
        node = isinstance(endpoint, dict) and endpoint.get('protocol') in NODE_PROTOCOLS
        required = ('id', 'device', 'base_url') if node else ('id', 'device', 'base_url', 'engine', 'execution')
        if (not isinstance(endpoint, dict) or any(not isinstance(endpoint.get(key), str) or not endpoint[key]
                for key in required)
                or not node and not isinstance(endpoint.get('services'), list)
                or not isinstance(endpoint.get('note', ''), str)
                or not isinstance(endpoint.get('models', {}), dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in endpoint.get('models', {}).items())):
            raise ValueError('Invalid inference endpoint fields')
        url = urllib.parse.urlsplit(endpoint['base_url'])
        if (endpoint['id'] in ids or endpoint.get('approved') is not True
                or endpoint.get('access') != 'network'
                or url.scheme not in ('http', 'https') or not url.hostname
                or url.username or url.password or url.query or url.fragment
                or not url.path.endswith('/v1') or node and url.path != '/v1'):
            raise ValueError('Endpoints require explicit network trust and a /v1 URL without credentials')
        url.port
        if url.scheme == 'http':
            try:
                address = ipaddress.ip_address(url.hostname)
            except ValueError:
                raise ValueError('HTTP observations require an explicit LAN or Tailscale IP') from None
            if not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')):
                raise ValueError('Public plaintext inference endpoints are not supported')
        if not node and (not endpoint['services'] or any(name not in ('Chat', 'Image', 'Image generation', 'Image editing')
                                            for name in endpoint['services'])):
            raise ValueError('Unknown inference service')
        ids.add(endpoint['id'])
    return config['endpoints']


def node_models(endpoint, result=None):
    """Read model metadata from the approved node, without following advertised URLs."""
    if result is None:
        result = read_json(endpoint['base_url'] + '/models')
    # Existing independent servers keep their versioned wire format.
    if endpoint.get('protocol') == 'sealedllm-node':
        result = dict(result, dotagents=result.get('sealedllm', {}))
        if isinstance(result.get('data'), list):
            result['data'] = [dict(m, dotagents=m.get('sealedllm')) if isinstance(m, dict) else m
                              for m in result['data']]
    extension = result.get('dotagents', {})
    if extension.get('schema_version') != 1 or extension.get('protocol') != endpoint.get('protocol'):
        raise ValueError('Unsupported node catalog')
    models = result.get('data')
    if not isinstance(models, list) or len(models) > 256:
        raise ValueError('Invalid node model list')
    seen = set()
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get('dotagents'), dict):
            raise ValueError('Invalid node model')
        meta = model['dotagents']
        for value in (model.get('id'), meta.get('name'), meta.get('engine'), meta.get('execution')):
            if not isinstance(value, str) or not 0 < len(value) <= 256 or any(ord(c) < 32 for c in value):
                raise ValueError('Invalid model label')
        if (model['id'] in seen or meta.get('service') not in ('Chat', 'Image')
                or any(type(meta.get(k)) is not bool for k in ('installed', 'loadable', 'ready'))
                or meta.get('loaded') is not None and type(meta['loaded']) is not bool
                or meta.get('state') not in ('loaded', 'unloaded', 'running', 'unknown')
                or meta.get('state') in ('loaded', 'running') and meta.get('loaded') is not True
                or meta.get('state') == 'unloaded' and meta.get('loaded') is not False):
            raise ValueError('Invalid node model state')
        context = meta.get('context_window')
        if context is not None and (type(context) is not int or not 0 < context <= 2**30):
            raise ValueError('Invalid model context')
        seen.add(model['id'])
    return models


def observe_node(endpoint):
    base = dict(role='service', node={'label': endpoint['device'], 'location': 'remote',
                                    'address': urllib.parse.urlsplit(endpoint['base_url']).hostname},
                endpoints=[endpoint['base_url']], execution='docker', catalog_models={})
    try:
        models = node_models(endpoint)
        rows = []
        for model in models:
            key, meta = model['id'], model['dotagents']
            service = meta['service']
            metrics = health_metrics({'worker': {'last_request': meta.get('metrics', {})}}, service)
            detail = (f"Installed: {meta['installed']}. Available to load: {meta['loadable']}. "
                      f"Worker ready: {meta['ready']}. Model loading stays on the node. "
                      'API reachability does not imply model residency or free GPU capacity.')
            rows.append(dict(base, id=endpoint['id'] + ':' + service + ':' + key,
                             name=service, state='ready', engine=meta['engine'], execution=meta['execution'],
                             model_ids=[key], model_names={key: meta['name']},
                             model_states={key: 'loaded' if meta['loaded'] is True else
                                                'standby' if meta['loaded'] is False else 'unknown'},
                             model_activity={key: meta['state']}, model_metrics={key: metrics}, detail=detail))
        if rows:
            return rows
        detail = 'Node API responds. No models are registered.'
        state = 'ready'
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        detail = 'Node catalog unavailable or invalid; no model or engine confirmed.'
        state = 'unknown'
    return [dict(base, id=endpoint['id'], name='Node API', state=state, engine='—',
                 model_ids=[], model_states={}, model_activity={}, model_metrics={}, detail=detail)]


def chat_endpoints(root):
    """Resolve chat catalog metadata for application setup on approved nodes."""
    rows = []
    for endpoint in load_endpoints(root):
        if endpoint.get('protocol') in NODE_PROTOCOLS:
            models = [m for m in node_models(endpoint) if m['dotagents']['service'] == 'Chat'
                      and m['dotagents']['installed']]
            rows.append(dict(endpoint, services=['Chat'], engine='Node API',
                             models={m['id']: m['dotagents']['name'] for m in models},
                             contexts={m['id']: m['dotagents'].get('context_window') for m in models}))
        elif 'Chat' in endpoint['services']:
            rows.append(endpoint)
    return rows


def observe(endpoint):
    if endpoint.get('protocol') in NODE_PROTOCOLS:
        return observe_node(endpoint)
    base = endpoint['base_url']
    services = service_names(endpoint)
    model_ids, model_states, model_activity, model_metrics = [], {}, {}, {}
    state, detail = 'unknown', 'Endpoint unavailable; no served model confirmed.'
    try:
        result = read_json(base + '/models')
        models = advertised_models(result)
        model_ids = list(dict.fromkeys(item['id'] for item in models))
        model_states = {key: 'unknown' for key in model_ids}
        state = 'ready'
        detail = 'API responds. Model residency and activity are not reported by the standard model catalog.'
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    if state == 'ready' and endpoint.get('health') in ('dotagents-image', 'dotagents-chat',
                                                    'sealedllm-image', 'sealedllm-chat', 'strata'):
        try:
            result = read_json(base.removesuffix('/v1') + '/health')
            strata = endpoint.get('health') == 'strata'
            if strata and result.get('service') != 'strata':
                raise ValueError('Expected Strata health')
            worker = result.get('worker', {})
            if strata:
                worker = {'loaded': result.get('loaded')}
            if result.get('status') == 'ok' and result.get('model') in model_ids:
                model_metrics = {name: {result['model']: health_metrics(result, name)}
                                 for name in services}
                loaded = worker.get('loaded')
                phase = worker.get('status')
                model_states[result['model']] = ('loaded' if loaded is True else
                                                 'standby' if loaded is False else 'unknown')
                model_activity[result['model']] = ('running' if phase == 'generating' and loaded is True else
                                                   'loaded' if loaded is True else
                                                   'unloaded' if loaded is False else 'unknown')
                detail = (f'Worker phase: {phase}. Model residency and inference activity reported by the worker. '
                          'Running means active inference, not allocated VRAM. Free GPU capacity is not verified.')
                if strata:
                    detail = ('Strata reports model residency. Active requests and inference rates are not reported. '
                              'Free GPU capacity is not verified.')
        except (OSError, ValueError, AttributeError, TypeError):
            detail += ' Worker health unavailable.'
    detail += ' ' + endpoint.get('note', '')
    return [dict(id=endpoint['id'] + ':' + name, name=name, role='service', state=state,
                 node={'label': endpoint['device'], 'location': 'remote',
                       'address': urllib.parse.urlsplit(base).hostname},
                 endpoints=[base], engine=endpoint['engine'], execution=endpoint['execution'],
                 model_ids=model_ids, model_states=model_states, model_activity=model_activity,
                 model_metrics=model_metrics.get(name, {}),
                 catalog_models=endpoint.get('models', {}), detail=detail.strip())
            for name in services]


def remote_services(root):
    endpoints = load_endpoints(root)
    if not endpoints:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [row for rows in pool.map(observe, endpoints) for row in rows]
