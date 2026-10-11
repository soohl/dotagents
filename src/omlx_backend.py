"""Prepare and launch the optional, pinned Qwen oMLX worker."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import launcher
from runtime_config import WORKER_PORT

VALIDATION_TIMEOUT = 15


def probe(args, label):
    try:
        return subprocess.check_output(args, text=True, timeout=VALIDATION_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f'oMLX {label} check timed out after {VALIDATION_TIMEOUT} seconds.') from None


def settings():
    return json.loads((launcher.ROOT / 'config/omlx.json').read_text())


def validate(model, weights=True):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('oMLX requires an Apple Silicon Mac.')
    config = settings()
    source = launcher.ROOT / model['source']
    launcher.check_revision(source, model['revision'])
    executable = launcher.ROOT / config['executable']
    if not executable.is_file():
        raise ValueError('Missing pinned oMLX executable. Check config/omlx.json.')
    version = probe([str(executable), '--version'], 'version').strip()
    if version != config['version']:
        raise ValueError('oMLX version differs from config/omlx.json. Install the pinned engine from config/omlx.json.')
    native = json.loads(probe([
        str(launcher.ROOT / config['python']), '-c',
        'import json; from omlx.custom_kernels import native_kernel_status; '
        'print(json.dumps({name: item["available"] for name, item in native_kernel_status().items()}))'],
        'native kernels'))
    if not native or not all(native.values()):
        raise ValueError('oMLX compiled kernels are unavailable. Install the pinned engine from config/omlx.json.')
    if weights:
        for artifact in model['artifacts']:
            launcher.check_artifact(artifact)


def setup(model):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('oMLX requires an Apple Silicon Mac.')
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('Install uv before setting up oMLX.')
    source = launcher.ROOT / model['source']
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        launcher.command(['git', 'clone', launcher.profiles()['engines']['omlx']['repository'], str(source)])
        launcher.command(['git', '-C', str(source), 'checkout', '--detach', model['revision']])
    launcher.check_revision(source, model['revision'])
    if sys.version_info[:2] != (3, 11):
        raise ValueError('The pinned oMLX wheel requires the project Python 3.11 environment.')
    artifact = settings()['wheel']
    wheel = launcher.ROOT / artifact['path']
    try:
        launcher.check_artifact(artifact, checksum=True)
    except ValueError:
        wheel.parent.mkdir(parents=True, exist_ok=True)
        temporary = wheel.with_suffix('.download')
        for attempt in range(3):
            offset = temporary.stat().st_size if temporary.exists() else 0
            request = urllib.request.Request(artifact['url'], headers={'Range': f'bytes={offset}-'} if offset else {})
            try:
                with urllib.request.urlopen(request, timeout=30) as source_file:
                    mode = 'ab' if offset and source_file.status == 206 else 'wb'
                    with temporary.open(mode) as output:
                        shutil.copyfileobj(source_file, output)
                os.replace(temporary, wheel)
                break
            except OSError:
                if attempt == 2:
                    raise
                print('Retrying the resumable oMLX wheel download.', flush=True)
        launcher.check_artifact(artifact, checksum=True)
    launcher.command([uv, 'pip', 'install', '--python', str(launcher.ROOT / settings()['python']),
                      '--reinstall-package', 'omlx',
                      '-c', str(launcher.ROOT / 'config/tui-requirements.txt'), str(wheel)],
                     cwd=launcher.ROOT)
    validate(model, weights=False)
    print('Installed pinned oMLX with custom kernels. DS4 and the system oMLX installation are unchanged.')


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(value, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def server_args(model, environment):
    validate(model)
    config = settings()
    base = launcher.ROOT / config['base_path']
    private_json(base / 'settings.json', config['runtime_settings'])
    private_json(base / 'model_settings.json', config['settings'])
    environment.pop('OMLX_API_KEY', None)
    environment['OMLX_NO_HF_CACHE'] = '1'
    port = launcher.integer_setting(environment, 'LLM_PORT', WORKER_PORT, 1, 65535)
    return [str(launcher.ROOT / config['executable']), 'serve',
            '--base-path', str(base), '--model-dir', str(launcher.ROOT / config['model_root']),
            '--host', '127.0.0.1', '--port', str(port), '--no-hf-cache',
            '--max-concurrent-requests', str(config['max_concurrent_requests']),
            '--memory-guard', config['memory_guard'], '--hot-cache-max-size', config['hot_cache_max_size'],
            '--paged-ssd-cache-dir', str(base / 'cache'),
            '--paged-ssd-cache-max-size', config['ssd_cache_max_size'], '--log-level', 'info']


def headers():
    return {}
