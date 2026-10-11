"""Image workspace adapter requests use only the configured inference API."""
import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import image_backend as backend


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_and_editing_use_their_openai_routes(self):
        calls = []
        def reply(request):
            calls.append(request)
            return httpx.Response(200, json={'data': [{'b64_json': 'cG5n'}]})
        client_type = httpx.AsyncClient
        def client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            return client_type(transport=httpx.MockTransport(reply), **kwargs)
        with patch.dict(os.environ, IMAGE_API_BASE_URL='http://100.64.0.2:8002/v1'), \
                patch.object(backend.httpx, 'AsyncClient', side_effect=client):
            payload = dict(model='qwen-image-2.1', prompt='A tree', size='1024x1024', steps=25, seed=42,
                           references=[])
            result = await backend.upstream('POST', '/images/generate', json.dumps(payload).encode())
            self.assertEqual(result.json(), {'image': 'cG5n', 'status': 'done'})
            self.assertEqual(calls[0].url.path, '/v1/images/generations')
            self.assertNotIn('references', json.loads(calls[0].content))
            payload['references'] = [base64.b64encode(b'reference').decode()]
            await backend.upstream('POST', '/images/generate', json.dumps(payload).encode())
            self.assertEqual(calls[1].url.path, '/v1/images/edits')
            self.assertIn(b'name="image[]"', calls[1].content)
            self.assertIn(b'reference', calls[1].content)
            self.assertNotIn(b'name="mask"', calls[1].content)

    async def test_health_options_and_busy_errors_match_the_workspace_contract(self):
        def reply(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [{'id': 'qwen-image-2.1'}]})
            if request.url.path == '/metrics':
                return httpx.Response(200, json={'worker': {'loaded': False, 'status': 'idle'}, 'progress': None})
            return httpx.Response(503, json={'error': {'message': 'Release the chat worker before image inference.'}})
        client_type = httpx.AsyncClient
        with patch.dict(os.environ, IMAGE_API_BASE_URL='http://100.64.0.2:8002/v1'), \
                patch.object(backend.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(reply), **kw)):
            options = (await backend.upstream('GET', '/images/options')).json()
            self.assertEqual(options['models'][0]['id'], 'qwen-image-2.1')
            self.assertEqual(options['max_steps'], 50)
            health = (await backend.upstream('GET', '/health')).json()
            self.assertFalse(health['loaded'])
            self.assertFalse(health['busy'])
            self.assertIsNone(health['active_model'])
            result = await backend.upstream('POST', '/images/generate', b'{"references": []}')
            self.assertEqual(result.status_code, 503)
            self.assertIn('Release the chat worker', result.json()['detail'])

    def test_upstream_requires_an_explicit_private_endpoint_without_credentials(self):
        for value in ('', 'http://8.8.8.8/v1', 'http://example.org/v1',
                      'http://user:password@100.64.0.2/v1', 'http://100.64.0.2/v1?secret=x',
                      'http://0.0.0.0/v1', 'http://100.64.0.2/admin'):
            with self.subTest(value=value), patch.dict(os.environ, IMAGE_API_BASE_URL=value), self.assertRaises(ValueError):
                backend.backend_url()
