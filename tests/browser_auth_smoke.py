"""Check public browser authentication with a disposable account and virtual key."""
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import browser_auth as auth
from harness_service import IMAGE


def main():
    username = 'auth-check-' + secrets.token_hex(6)
    password = secrets.token_urlsafe(30)
    with auth.user_lock(ROOT):
        users = auth.read_users(ROOT)
        users['users'][username] = {'displayname': username, 'email': username + '@example.invalid',
                                   'password': auth.password_hash(password), 'groups': ['browser']}
        auth.write_users(ROOT, users)
    try:
        process = subprocess.Popen([
            'docker', 'run', '--rm', '--name', username, '-i', '--entrypoint', 'node', IMAGE, '-e',
            (ROOT / 'tests/browser_auth_smoke.cjs').read_text(),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps({'origin': 'https://' + auth.settings(ROOT)['FUNNEL_HOSTNAME'],
                                       'username': username, 'password': password}) + '\n')
        process.stdin.flush()
        for line in process.stdout:
            if line.strip() == 'NEED_CODE':
                body = auth.data_command(ROOT, 'cat /data/notifications.txt')
                code = auth.verification_code(body, username + '@example.invalid')
                process.stdin.write(json.dumps({'code': code}) + '\n')
                process.stdin.flush()
            else:
                print(line, end='', flush=True)
        return process.wait(timeout=180)
    finally:
        subprocess.run(['docker', 'rm', '-f', username], capture_output=True, timeout=15)
        with auth.user_lock(ROOT):
            users = auth.read_users(ROOT)
            users['users'].pop(username, None)
            auth.write_users(ROOT, users)
        auth.compose(ROOT, 'exec', '-T', 'authelia', 'authelia', 'storage', 'user', 'webauthn',
                     'delete', username, '--all', '--config=/config/configuration.yml',
                     '--config.experimental.filters=template', capture=True)
        print('Temporary authentication test account and security keys removed.')


if __name__ == '__main__':
    sys.exit(main())
