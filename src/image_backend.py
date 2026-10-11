"""Map the cook-4090 workspace to one approved OpenAI-compatible image API."""
import base64
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / 'config/image.json').read_text())


def backend_url():
    value = os.environ.get('IMAGE_API_BASE_URL', '').rstrip('/')
    url = urlsplit(value)
    try:
        address = ipaddress.ip_address(url.hostname or '')
        port = url.port
    except ValueError:
        raise ValueError('Configure an approved private image /v1 URL.') from None
    if (url.scheme not in ('http', 'https') or url.username or url.password or url.query
            or url.fragment or url.path != '/v1' or port == 0
            or not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10'))
            or address.is_unspecified or address.is_multicast):
        raise ValueError('Configure an approved private image /v1 URL.')
    return value


def error_detail(response):
    try:
        body = response.json()
        message = body.get('error', {}).get('message') or body.get('detail')
    except (ValueError, AttributeError):
        message = None
    return message if isinstance(message, str) else 'The image worker could not complete this request.'


async def upstream(method, target, body=b''):
    """Keep UI requests independent of remote model lifecycle and management."""
    base = backend_url()
    async with httpx.AsyncClient(timeout=3 if method == 'GET' else SETTINGS['request_timeout'],
                                 trust_env=False, follow_redirects=False) as client:
        if method == 'GET' and target == '/images/options':
            response = await client.get(base + '/models')
            if not response.is_success:
                return response
            served = {item['id'] for item in response.json()['data']}
            models = ([dict(id=SETTINGS['model'], label=SETTINGS['label'], sizes=SETTINGS['sizes'])]
                      if SETTINGS['model'] in served else [])
            return httpx.Response(200, json=dict(models=models, steps=SETTINGS['steps'],
                                                max_steps=SETTINGS['max_steps'],
                                                max_references=SETTINGS['max_references']))
        if method == 'GET' and target == '/health':
            response = await client.get(base.removesuffix('/v1') + '/metrics')
            if not response.is_success:
                return response
            report = response.json()
            worker = report['worker']
            busy = worker.get('status') in ('loading', 'generating')
            return httpx.Response(200, json=dict(busy=busy, queued=0, engine='diffusers',
                                                active_model=SETTINGS['label'] if worker.get('loaded') else None,
                                                loaded=worker.get('loaded') is True, progress=report.get('progress'),
                                                note=None if worker.get('loaded') or busy else
                                                'Image generation requires free GPU memory on the inference server.'))
        if method != 'POST' or target != '/images/generate':
            raise ValueError('Unsupported image operation.')
        payload = json.loads(body)
        references = payload.pop('references', [])
        if references:
            files = [('image[]', (f'reference-{index}.png', base64.b64decode(value, validate=True), 'image/png'))
                     for index, value in enumerate(references)]
            response = await client.post(base + '/images/edits', data={key: str(value) for key, value in payload.items()}, files=files)
        else:
            response = await client.post(base + '/images/generations', json=payload)
        if not response.is_success:
            return httpx.Response(response.status_code, json={'detail': error_detail(response)})
        try:
            image = response.json()['data'][0]['b64_json']
            if not isinstance(image, str) or not image:
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValueError('The image worker returned an invalid image response.') from None
        return httpx.Response(200, json={'image': image, 'status': 'done'})
