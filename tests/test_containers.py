"""Check container exposure and relay boundaries without a running daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerBoundaryTests(unittest.TestCase):
    def compose(self, remote=False, search=False, browser=False):
        if not shutil.which('docker'):
            self.skipTest('Docker Compose is not installed')
        probe = subprocess.run(['docker', 'compose', 'version'], capture_output=True)
        if probe.returncode:
            self.skipTest('Docker Compose is not installed')
        environment = dict(os.environ,
                           WEBUI_SECRET_KEY='test-only-secret',
                           WEBUI_ADMIN_EMAIL='test@example.invalid',
                           WEBUI_ADMIN_PASSWORD='test-only-password',
                           WEBUI_HOSTNAME='ai.example.invalid',
                           FUNNEL_HOSTNAME='cook-studio.example.ts.net',
                           POCKET_ID_ENCRYPTION_KEY='test-only-pocket-key',
                           OAUTH_CLIENT_ID='test-only-client',
                           OAUTH_CLIENT_SECRET='test-only-client-secret',
                           BRAVE_SEARCH_API_KEY='test-only-brave-key',
                           BROWSER_API_KEY='test-only-browser-key',
                           BROWSER_USER_ID='test-only-user',
                           COMPOSE_PROJECT_NAME='cook-studio')
        command = ['docker', 'compose', '--env-file', '/dev/null',
                   '-f', str(ROOT / 'config/compose.yaml')]
        if remote:
            command += ['-f', str(ROOT / 'config/compose.remote.yaml')]
            environment.update(WEBUI_PASSWORD_LOGIN_ENABLED='false',
                               WEBUI_OAUTH_MERGE_EMAIL='false')
        if search:
            command += ['-f', str(ROOT / 'config/compose.search.yaml')]
        if browser:
            command += ['-f', str(ROOT / 'config/compose.browser.yaml')]
        result = subprocess.run(
            command + ['config', '--format', 'json', '--no-env-resolution'],
            cwd=ROOT, env=environment, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_local_exposure_and_native_inference(self):
        config = self.compose()
        services = config['services']
        self.assertEqual(set(services), {'caddy', 'open-webui'})
        self.assertFalse(services['open-webui'].get('ports'))
        ports = services['caddy']['ports']
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]['host_ip'], '127.0.0.1')
        env = services['open-webui']['environment']
        self.assertEqual(env['OPENAI_API_BASE_URLS'].split(';'), [
            f'http://host.docker.internal:{port}/v1' for port in (8000, 8001)])
        configured = json.loads(env['OPENAI_API_CONFIGS'])
        chat_header = {'X-Cook-Studio-Chat-Id': '{{CHAT_ID}}'}
        self.assertEqual(configured, {
            '0': {'enable': True, 'headers': chat_header},
            '1': {'enable': True, 'headers': chat_header},
        })
        self.assertEqual(env['USER_PERMISSIONS_CHAT_FILE_UPLOAD'], 'true')
        self.assertEqual(env['BYPASS_EMBEDDING_AND_RETRIEVAL'], 'true')
        self.assertEqual(env['RAG_FILE_MAX_SIZE'], '20')
        self.assertEqual(env['ENABLE_CONTEXT_COMPACTION'], 'true')
        self.assertEqual(env['CONTEXT_COMPACTION_TOKEN_THRESHOLD'], '48000')
        self.assertEqual(env['CONTEXT_COMPACTION_RETENTION_PERCENTAGE'], '40')
        meter, = [volume for volume in services['open-webui']['volumes']
                  if volume['target'] == '/app/build/static/loader.js']
        self.assertEqual(meter['source'], str(ROOT / 'src/context_meter.js'))
        self.assertTrue(meter['read_only'])
        boundary = (ROOT / 'config/chat-only.caddy').read_text()
        self.assertNotIn('audio|files|images', boundary)
        self.assertIn('audio|images|retrieval', boundary)
        self.assertEqual(env['WEBUI_AUTH'], 'true')
        self.assertEqual(env['ENABLE_SIGNUP'], 'false')
        self.assertEqual(env['BYPASS_MODEL_ACCESS_CONTROL'], 'true')
        for service in services.values():
            self.assertIn('@sha256:', service['image'])
            for volume in service.get('volumes', []):
                self.assertNotIn('docker.sock', volume['target'])
                self.assertNotIn('/models', volume['target'])

    def test_funnel_keeps_tls_and_networking_at_home(self):
        services = self.compose(remote=True)['services']
        self.assertEqual(set(services), {'caddy', 'open-webui', 'tailscale', 'pocket-id'})
        for name, service in services.items():
            self.assertFalse(service.get('ports'))
            self.assertFalse(service.get('privileged'))
            self.assertEqual(service.get('cap_add', []),
                             ['NET_BIND_SERVICE'] if name == 'caddy' else [])
            self.assertIn('@sha256:', service['image'])
        self.assertEqual(services['tailscale']['cap_drop'], ['ALL'])
        self.assertEqual(services['caddy']['cap_drop'], ['ALL'])
        self.assertEqual(services['open-webui']['cap_drop'], ['ALL'])
        self.assertIn('--tun=userspace-networking', services['tailscale']['command'])
        self.assertEqual(services['caddy']['network_mode'], 'service:tailscale')
        self.assertTrue(services['caddy']['depends_on']['tailscale']['restart'])
        env = services['open-webui']['environment']
        self.assertEqual(env['WEBUI_URL'], 'https://cook-studio.example.ts.net')
        self.assertEqual(env['WEBUI_SESSION_COOKIE_SECURE'], 'true')
        tls = (ROOT / 'config/Caddyfile.funnel').read_text()
        self.assertIn('bind 127.0.0.1', tls)
        self.assertIn('protocols tls1.3', tls)
        for service in services.values():
            for volume in service.get('volumes', []):
                if volume['type'] == 'bind':
                    self.assertTrue(Path(volume['source']).exists(), volume['source'])
        mounts = services['caddy']['volumes']
        socket_mount = next(v for v in mounts if v['target'] == '/var/run/tailscale')
        self.assertTrue(socket_mount['read_only'])

    def test_passkey_disables_password_api_and_public_enrollment(self):
        services = self.compose(remote=True)['services']
        self.assertEqual(set(services), {'caddy', 'open-webui', 'tailscale', 'pocket-id'})
        for service in services.values():
            self.assertFalse(service.get('ports'))
        identity = services['pocket-id']
        self.assertEqual(identity['user'], '1000:1000')
        self.assertTrue(identity['read_only'])
        self.assertEqual(identity['cap_drop'], ['ALL'])
        self.assertIn('@sha256:', identity['image'])
        env = identity['environment']
        self.assertEqual(env['ALLOW_USER_SIGNUPS'], 'disabled')
        self.assertEqual(env['WEBAUTHN_USER_VERIFICATION'], 'required')
        self.assertEqual(env['EMAIL_ONE_TIME_ACCESS_AS_UNAUTHENTICATED_ENABLED'], 'false')
        self.assertNotIn('STATIC_API_KEY', env)
        env = services['open-webui']['environment']
        self.assertEqual(env['ENABLE_LOGIN_FORM'], 'false')
        self.assertEqual(env['ENABLE_PASSWORD_AUTH'], 'false')
        self.assertEqual(env['ENABLE_OAUTH_SIGNUP'], 'false')
        self.assertEqual(env['OAUTH_MERGE_ACCOUNTS_BY_EMAIL'], 'false')
        self.assertEqual(env['OAUTH_CODE_CHALLENGE_METHOD'], 'S256')
        self.assertEqual(env['WEBUI_AUTH_COOKIE_SECURE'], 'true')

    def test_search_preserves_passkeys_and_other_feature_boundaries(self):
        services = self.compose(remote=True, search=True)['services']
        ui = services['open-webui']
        env = ui['environment']
        for key in ('ENABLE_WEB_SEARCH', 'USER_PERMISSIONS_FEATURES_WEB_SEARCH',
                    'BYPASS_WEB_SEARCH_WEB_LOADER',
                    'BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL'):
            self.assertEqual(env[key], 'true')
        for key in ('ENABLE_PASSWORD_AUTH', 'ENABLE_SIGNUP', 'ENABLE_PLUGINS',
                    'ENABLE_CODE_EXECUTION', 'ENABLE_IMAGE_GENERATION',
                    'ENABLE_LOCAL_WEB_FETCH'):
            self.assertEqual(env[key], 'false')
        self.assertEqual(env['WEB_SEARCH_ENGINE'], 'brave_llm_context')
        self.assertEqual(json.loads(env['DEFAULT_MODEL_PARAMS'])['function_calling'], 'native')
        defaults = json.loads(env['DEFAULT_MODEL_METADATA'])
        self.assertEqual(defaults['defaultFeatureIds'], ['web_search'])
        self.assertTrue(defaults['capabilities']['web_search'])
        self.assertTrue(defaults['builtinTools']['web_search'])
        self.assertFalse(defaults['builtinTools']['knowledge'])
        self.assertEqual(env['BRAVE_SEARCH_CONTEXT_TOKENS'], '4096')
        self.assertEqual(env['WEB_SEARCH_CONCURRENT_REQUESTS'], '1')
        self.assertEqual(env['BRAVE_SEARCH_API_KEY'], 'test-only-brave-key')
        self.assertEqual(env['OAUTH_CLIENT_SECRET'], 'test-only-client-secret')
        self.assertEqual(services['pocket-id']['environment']['ENCRYPTION_KEY'],
                         'test-only-pocket-key')
        for name, service in services.items():
            self.assertFalse(service.get('env_file'))
            assigned = service.get('environment', {})
            self.assertNotIn('CF_API_TOKEN', assigned)
            if name != 'open-webui':
                self.assertNotIn('BRAVE_SEARCH_API_KEY', assigned)
                self.assertNotIn('OAUTH_CLIENT_SECRET', assigned)
            if name != 'pocket-id':
                self.assertNotIn('ENCRYPTION_KEY', assigned)
        self.assertFalse(ui.get('ports'))

    def test_browser_has_only_controlled_egress_and_account_access(self):
        config = self.compose(remote=True, search=True, browser=True)
        services = config['services']
        browser = services['browser']
        self.assertEqual(set(browser['networks']), {'browser-private'})
        self.assertTrue(config['networks']['browser-private']['internal'])
        self.assertFalse(browser.get('ports'))
        self.assertFalse(browser.get('volumes'))
        self.assertTrue(browser['read_only'])
        self.assertEqual(browser['user'], '1000:1000')
        self.assertEqual(browser['cap_drop'], ['ALL'])
        self.assertFalse(services['browser-egress'].get('environment'))
        self.assertEqual(set(services['browser-egress']['networks']),
                         {'browser-private', 'browser-public'})
        env = services['open-webui']['environment']
        self.assertEqual(json.loads(env['DEFAULT_MODEL_PARAMS'])['function_calling'], 'native')
        self.assertIn('Brave Search', json.loads(env['DEFAULT_MODEL_PARAMS'])['system'])
        self.assertIn('browser tools', json.loads(env['DEFAULT_MODEL_PARAMS'])['system'])
        self.assertIn('Do not use fetch_url', json.loads(env['DEFAULT_MODEL_PARAMS'])['system'])
        defaults = json.loads(env['DEFAULT_MODEL_METADATA'])
        self.assertEqual(defaults['defaultFeatureIds'], ['web_search'])
        self.assertEqual(defaults['toolIds'], ['server:mcp:browser'])
        connection, = json.loads(env['TOOL_SERVER_CONNECTIONS'])
        self.assertEqual(connection['type'], 'mcp')
        self.assertEqual(connection['url'], 'http://browser:8080/mcp')
        self.assertEqual(connection['config']['access_grants'], [
            {'principal_type': 'user', 'principal_id': 'test-only-user', 'permission': 'read'}])
        for service in services.values():
            self.assertFalse(service.get('ports'))
        for key in ('ENABLE_PASSWORD_AUTH', 'ENABLE_PLUGINS', 'ENABLE_CODE_EXECUTION',
                    'USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS'):
            self.assertEqual(env[key], 'false')



if __name__ == '__main__':
    unittest.main()
