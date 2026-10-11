"""Check the current stack's service and Image storage boundaries."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ContainerBoundaryTests(unittest.TestCase):
    def compose(self, images=False):
        command = ['docker', 'compose', '--env-file', '/dev/null',
                   '-f', str(ROOT / 'config/compose.harness.yaml')]
        if images:
            command += ['-f', str(ROOT / 'config/compose.images.yaml')]
        environment = dict(os.environ, DOTAGENTS_REMOTE_IMAGE_URL='http://100.64.0.2:8002/v1')
        result = subprocess.run(command + ['config', '--format', 'json'], cwd=ROOT,
                                env=environment, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_host_stack_has_only_chat_agent_and_gateway(self):
        services = self.compose()['services']
        self.assertEqual(set(services), {'chat', 'agent', 'caddy'})
        for service in services.values():
            self.assertTrue(all(port['host_ip'] == '127.0.0.1' for port in service.get('ports', [])))

    def test_image_workspace_has_loopback_access_and_owns_its_history(self):
        config = self.compose(images=True)
        app = config['services']['image-web']
        self.assertEqual([(p['host_ip'], p['published']) for p in app['ports']], [('127.0.0.1', '3001')])
        self.assertTrue(app['read_only'])
        self.assertEqual(app['cap_drop'], ['ALL'])
        self.assertEqual(app['environment']['IMAGE_API_BASE_URL'], 'http://100.64.0.2:8002/v1')
        self.assertEqual(len(app['volumes']), 1)
        self.assertEqual(app['volumes'][0]['type'], 'volume')
        self.assertEqual(app['volumes'][0]['target'], '/data/images')
        self.assertNotIn('devices', app)
