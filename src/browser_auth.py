"""Manage the Authelia account file and local enrollment notifications."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
import fcntl
import getpass
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from harness_tunnel import compose


SECRET_KEYS = ('AUTHELIA_SESSION_SECRET', 'AUTHELIA_STORAGE_ENCRYPTION_KEY',
               'AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET')


def settings(root):
    values = {}
    for line in (root / '.env').read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator and re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            parsed = shlex.split(value, comments=True)
            values[key.strip()] = parsed[0] if parsed else ''
    return values


def save_settings(root, updates):
    path = root / '.env'
    lines = path.read_text().splitlines()
    remaining = dict(updates)
    for index, line in enumerate(lines):
        key = line.partition('=')[0].strip()
        if key in remaining:
            lines[index] = f'{key}={remaining.pop(key)}'
    lines.extend(f'{key}={value}' for key, value in remaining.items())
    descriptor, name = tempfile.mkstemp(prefix='.env.', dir=root)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def data_command(root, script, data=None):
    result = subprocess.run([
        'docker', 'compose', '--env-file', str(root / '.env'), 'run', '--rm', '-T',
        '--no-deps', '--entrypoint', 'sh', 'authelia', '-c', script,
    ], cwd=root, input=data, text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Cannot access Authelia data. Check Docker and the private browser account configuration.')
    return result.stdout


def read_users(root):
    return json.loads(data_command(root, 'if [ -f /data/users.json ]; then cat /data/users.json; '
                                  'else printf \'{"users":{}}\'; fi'))


def write_users(root, document):
    data_command(root, 'umask 077; cat > /data/.users.tmp && mv /data/.users.tmp /data/users.json',
                 json.dumps(document, indent=2) + '\n')


@contextmanager
def user_lock(root):
    directory = root / '.local'
    directory.mkdir(exist_ok=True)
    with (directory / 'authelia-users.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def password_hash(secret):
    return Argon2id(salt=secrets.token_bytes(16), length=32, iterations=3,
                    lanes=4, memory_cost=65536).derive_phc_encoded(secret.encode())


def setup(root, username=None):
    values = settings(root)
    username = username or values.get('HARNESS_LOGIN_USERNAME')
    if not username or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', username):
        raise ValueError('Use a browser login ID with 3–64 lowercase letters, digits, dots, underscores, or hyphens.')
    if 'config/compose.harness-remote.yaml' not in values.get('COMPOSE_FILE', ''):
        raise ValueError('Add config/compose.harness-remote.yaml to COMPOSE_FILE first.')
    updates = {key: secrets.token_urlsafe(64) for key in SECRET_KEYS if not values.get(key)}
    updates['HARNESS_LOGIN_USERNAME'] = username
    save_settings(root, updates)
    compose(root, 'run', '--rm', '--no-deps', '--user', '0', '--cap-add', 'CHOWN',
            '--cap-add', 'FOWNER', '--entrypoint', 'sh', 'authelia', '-c',
            'chown 1000:1000 /data && chmod 700 /data', capture=True)
    created = False
    with user_lock(root):
        document = read_users(root)
        if username not in document['users']:
            document['users'][username] = {
                'displayname': username, 'password': password_hash(secrets.token_urlsafe(64)),
                'email': values.get('WEBUI_ADMIN_EMAIL') or username + '@dotagents.invalid',
                'groups': ['browser'],
            }
            write_users(root, document)
            created = True
    compose(root, 'up', '-d', '--wait', 'authelia')
    print(f'Login ID: {username}')
    print('Set the password from a local terminal.' if created else
          'Existing password and enrolled security keys are preserved.')


def password(root):
    if not sys.stdin.isatty():
        raise ValueError('Password changes require a local terminal.')
    username = settings(root).get('HARNESS_LOGIN_USERNAME')
    if not username:
        raise ValueError('Configure the private browser account first.')
    print(f'Set the password for {username}. Use at least 12 characters.')
    secret = getpass.getpass('New password: ')
    if len(secret) < 12 or username.lower() in secret.lower():
        raise ValueError('Use at least 12 characters and omit the login ID.')
    if secret != getpass.getpass('Repeat password: '):
        raise ValueError('The passwords do not match. No password was changed.')
    digest = password_hash(secret)
    with user_lock(root):
        document = read_users(root)
        if username not in document['users']:
            raise ValueError('Configure the private browser account first.')
        document['users'][username]['password'] = digest
        write_users(root, document)
    # Memory-backed sessions must not survive a local password reset.
    compose(root, 'restart', 'authelia', capture=True)
    compose(root, 'up', '-d', '--wait', 'authelia', capture=True)
    print('Password saved. Sessions cleared. Enrolled security keys are preserved.')


def verification_code(body, email):
    recipient = re.search(r'^Recipient: (.+)$', body, re.MULTILINE)
    issued = re.search(r'^Date: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', body, re.MULTILINE)
    code = re.search(r'-{40}\s+([A-Z0-9]{8})\s+-{40}', body)
    # Authelia 4.39 formats mail.Address as {displayname address}.
    address = (recipient[1].rsplit(' ', 1)[-1].removesuffix('}')
               if recipient and recipient[1].startswith('{') else
               parseaddr(recipient[1])[1] if recipient else '')
    if address != email or not issued or not code:
        raise ValueError('Request security key registration in the browser first.')
    timestamp = datetime.strptime(issued[1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if age < -30 or age >= 300:
        raise ValueError('The verification code expired. Request a new code in the browser.')
    return code[1]


def notification(root):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Enrollment codes require a local terminal. Do not share the code.')
    username = settings(root).get('HARNESS_LOGIN_USERNAME')
    users = read_users(root)['users']
    if username not in users:
        raise ValueError('The login ID is not configured.')
    body = data_command(root, 'test ! -f /data/notifications.txt || cat /data/notifications.txt')
    code = verification_code(body, users[username]['email'])
    print('Enter this verification code in your browser. It expires five minutes after the request.')
    print(code)
