"""Read-only startup checks. No installers, service starts, or saved success flags."""
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import launcher
from runtime_config import activate, load, model_profiles


def run(root, args, *, timeout=30):
    # Compose output can contain secrets. Keep it out of diagnostics, including errors.
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('Command timed out: ' + Path(args[0]).name) from None
    if result.returncode:
        raise ValueError('Command failed: ' + ' '.join(args[:2]) + '. Check the local configuration.')
    return result.stdout


def host(root):
    if platform.system() != 'Darwin':
        raise ValueError('The host must run macOS.')
    missing = [name for name in ('tailscale', 'docker', 'git') if not shutil.which(name)]
    if missing:
        raise ValueError('Missing host tools: ' + ', '.join(missing))
    # Installation is required; a peer, tunnel, and particular tailnet are not.
    run(root, ['tailscale', 'version'])


def dependencies(root):
    if sys.prefix == sys.base_prefix:
        raise ValueError('Use the project .venv.')
    failures = []
    for name, expected in re.findall(r'^([\w.-]+)==([^\s;\\]+)',
                                     (root / 'config/tui-requirements.txt').read_text(), re.M):
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            failures.append(name + '==' + expected)
    if failures:
        raise ValueError('Install the pinned TUI requirements: ' + ', '.join(failures))
    # Metadata alone does not prove that native modules can be imported.
    run(root, [sys.executable, '-c', 'import textual, rich; from cryptography.hazmat.primitives.kdf.argon2 import Argon2id'])


def applications(root):
    if load(root)['applications']['images'] and not os.environ.get('DOTAGENTS_REMOTE_IMAGE_URL'):
        raise ValueError('No Image API was discovered. Start an Image gateway or disable applications.images in config.yaml.')
    environment = root / '.env'
    if not environment.is_file():
        raise ValueError('Copy config/env.example to .env and configure the host stack.')
    if environment.stat().st_mode & 0o077:
        raise ValueError('Restrict .env permissions with chmod 600 .env.')
    run(root, ['docker', 'info', '--format', '{{.ServerVersion}}'])
    compose = ['docker', 'compose', '--env-file', str(environment)]
    config = json.loads(run(root, [*compose, 'config', '--format', 'json']))
    services = config.get('services', {})
    if not {'chat', 'agent', 'caddy'} <= services.keys():
        raise ValueError('Enable the host stack from config/compose.harness.yaml.')
    problems = []
    images = set()
    for name, service in services.items():
        if not service.get('image'):
            problems.append(name + ': missing image')
        else:
            images.add(service['image'])
        for port in service.get('ports', []):
            if port.get('host_ip') not in ('127.0.0.1', '::1'):
                problems.append(name + ': published ports must use loopback')
        for volume in service.get('volumes', []):
            if volume.get('type') == 'bind' and not Path(volume['source']).exists():
                problems.append(name + ': missing bind source')
    for role in ('chat', 'agent'):
        expected = root / 'sessions' / role
        for target, source in (('/data', expected), ('/workspace', expected / 'workspace')):
            mounts = [v for v in services[role].get('volumes', []) if v.get('target') == target]
            if (len(mounts) != 1 or mounts[0].get('type') != 'bind'
                    or Path(mounts[0]['source']) != source or source.resolve() != source
                    or not source.is_dir()):
                problems.append(role + ': invalid data or workspace mount')
        path = root / '.local/harness' / (role + '-profile.json')
        if not path.is_file():
            problems.append(role + ': missing private Harness profile')
        elif not isinstance(json.loads(path.read_text()), list):
            problems.append(role + ': invalid private Harness profile')
    for image in sorted(images):
        try:
            run(root, ['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])
        except ValueError:
            problems.append('A configured application image is missing; build or pull it first.')
    # Remote browser access is checked only when its overlay is explicitly enabled.
    if 'authelia' in services:
        env = services['authelia'].get('environment', {})
        if not env.get('FUNNEL_HOSTNAME') or any(len(env.get(key, '') or '') < 32 for key in (
                'AUTHELIA_SESSION_SECRET', 'AUTHELIA_STORAGE_ENCRYPTION_KEY',
                'AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET')):
            problems.append('Remote browser configuration is incomplete.')
    if problems:
        raise ValueError('; '.join(dict.fromkeys(problems)))
    calendar_integration(root)


def calendar_integration(root):
    config = load(root)['integrations']['calendar']
    if not config['enabled']:
        return
    from calendar_bridge import command
    executable = command(root)[0]
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('Install uv to verify the Calendar MCP environment.')
    run(root, [uv, 'sync', '--locked', '--check', '--offline', '--directory',
               str(root / 'mcp/calendar-mcp')])
    output = run(root, [executable, '-c',
        'import asyncio,json; from calendar_mcp.backend import CalendarBackend; '
        'print(json.dumps(asyncio.run(CalendarBackend().call("health"))))'])
    health = json.loads(output)
    if health.get('readable') is not True:
        raise ValueError('Calendar MCP needs Full Access for the host application in macOS '
                         'Privacy & Security > Calendars; its tools remain read-only. '
                         'Alternatively disable integrations.calendar.enabled in config.yaml.')


def engine(root, model):
    if platform.machine() != 'arm64':
        raise ValueError('The configured native engine requires Apple Silicon.')
    if model.get('engine') == 'omlx':
        from omlx_backend import validate
        validate(model, weights=False)
    else:
        launcher.check_revision(root / model['source'], model['revision'])
        if not os.access(root / model['source'] / 'ds4-server', os.X_OK):
            raise ValueError('Build the pinned DS4 engine before starting DotAgents.')
    for artifact in launcher.required_artifacts(model):
        launcher.check_artifact(artifact)


def project(root):
    node = shutil.which('node')
    if not node:
        raise ValueError('Full verification requires Node.js for the Harness JavaScript tests. Install Node.js and retry ./run.sh --verify.')
    run(root, [node, '--version'])
    run(root, ['bash', '-n', str(root / 'run.sh')])
    if load(root)['integrations']['calendar']['enabled']:
        from calendar_bridge import command
        run(root, [command(root)[0], '-m', 'pytest', '-q',
                   str(root / 'mcp/calendar-mcp/tests')], timeout=60)
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                            cwd=root, timeout=180)
    if result.returncode:
        raise ValueError('Project tests failed.')


def verify(root, *, full=True):
    failures = []
    discovered = []

    def check(label, action):
        print('Checking ' + label + '…', flush=True)
        try:
            action()
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            # CalledProcessError may contain arguments; commands here never contain secrets.
            failures.append(label)
            print('FAIL: ' + label + ': ' + str(error), flush=True)
        else:
            print('PASS: ' + label, flush=True)

    check('runtime configuration', lambda: activate(root))
    for label, action in (('host', host), ('Python dependencies', dependencies)):
        check(label, lambda action=action: action(root))
    def discovery():
        from node_discovery import discover
        discovered.extend(discover(root))
    check('Tailscale gateway discovery', discovery)
    image = next((e['base_url'] for e in discovered if 'Image' in e.get('services', [])), '')
    os.environ['DOTAGENTS_REMOTE_IMAGE_URL'] = image
    check('applications', lambda: applications(root))
    models = {}

    def configuration():
        from control_plane import catalog
        catalog(root)
        models.update(launcher.configured_models(model_profiles(root)))
    check('model configuration', configuration)
    for name, model in models.items():
        check('engine and weights: ' + name, lambda model=model: engine(root, model))
    if full:
        check('project checks', lambda: project(root))
    if failures:
        raise ValueError('Verification failed. Startup is blocked. Failed checks: ' + ', '.join(failures))
    print('Full verification passed.' if full else 'Startup readiness checks passed.', flush=True)
    return discovered
