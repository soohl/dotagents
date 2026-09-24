"""Small DS4 launcher. Model pins and tuning live in config/models.json."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def profiles():
    return json.loads((ROOT / 'config/models.json').read_text())


def model_profile(name):
    for key, model in profiles()['models'].items():
        if name == key or name in model.get('aliases', []):
            return key, model
    raise ValueError(f'Unknown model: {name}. Use qwen or deepseek.')


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def check_revision(source, revision):
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != revision:
        raise ValueError('DS4 revision differs from config/models.json; run setup for this model.')
    command(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'])


def setup(model):
    if platform.system() != 'Darwin':
        raise ValueError('DS4 Metal requires macOS.')
    source = ROOT / model['source']
    if not (ROOT / 'backends/ds4/.git').exists():
        command(['git', '-C', str(ROOT), 'submodule', 'update', '--init', 'backends/ds4'])
    check_revision(source, model['revision'])
    command(['make', '-C', str(source), '-j', '8', 'ds4', 'ds4-server'])


def check_artifact(artifact, checksum=False):
    path = ROOT / artifact['path']
    if not path.is_file() or path.stat().st_size != artifact['size']:
        raise ValueError(f'Missing or incomplete weights: {artifact["path"]}. Run download for this model.')
    if checksum:
        digest = hashlib.sha256()
        with path.open('rb') as file:
            while block := file.read(16 * 1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != artifact['sha256']:
            raise ValueError(f'Weight checksum mismatch: {artifact["path"]}')
    return path


def required_artifacts(model):
    return [*model['artifacts'], *([model['vision_encoder']] if model.get('vision_encoder') else [])]


def download(model):
    hf = shutil.which('hf')
    if not hf:
        raise ValueError('Install the Hugging Face CLI before downloading weights.')
    for artifact in required_artifacts(model):
        path = ROOT / artifact['path']
        try:
            check_artifact(artifact, checksum=True)
        except ValueError:
            path.parent.mkdir(parents=True, exist_ok=True)
            args = [hf, 'download', artifact.get('repository', model['repository']), path.name,
                    '--revision', artifact.get('revision', model['weights_revision']),
                    '--local-dir', str(path.parent)]
            if path.exists():
                args.append('--force-download')
            command(args)
            check_artifact(artifact, checksum=True)
    print('Weight sizes and SHA-256 checksums verified.')


def integer_setting(environment, key, default, minimum, maximum):
    value = int(environment.get(key, default))
    if not minimum <= value <= maximum:
        raise ValueError(f'{key} must be between {minimum} and {maximum}.')
    return value


def server_args(key, model, environment):
    context = integer_setting(environment, 'LLM_CTX', model['context'], 2, 65536)
    port = integer_setting(environment, 'LLM_PORT', model['port'], 1, 65535)
    chunk = integer_setting(environment, 'LLM_PREFILL_CHUNK', model['prefill_chunk'], 1, 65536)
    args = [str(ROOT / model['source'] / 'ds4-server'), '--model', str(check_artifact(model['artifacts'][0])),
            '--metal', '--host', '127.0.0.1', '--port', str(port), '--ctx', str(context),
            '--prefill-chunk', str(chunk)]
    if model.get('vision_encoder'):
        args += ['--vision', str(check_artifact(model['vision_encoder']))]
    if environment.get('LLM_DISABLE_PROMPT_CACHE') != '1':
        runtime = Path(environment.get('LLM_RUNTIME_ROOT', str(ROOT / '.local/runtime' / key)))
        cache = runtime / 'ds4' / model['cache_directory']
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache.chmod(0o700)
        budget = integer_setting(environment, 'LLM_KV_DISK_SPACE_MB', model['cache_space_mb'], 0, 1048576)
        args += ['--kv-disk-dir', str(cache), '--kv-disk-space-mb', str(budget),
                 '--kv-cache-cold-max-tokens', str(context),
                 '--kv-cache-boundary-align-tokens', str(model['cache_alignment']),
                 '--kv-cache-reject-different-quant']
    return args


def ensure_idle(ports):
    for port in set(ports):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError(f'Port {port} is already in use. Stop its server yourself before loading a model.')
    # Include manually launched servers on other ports. Never stop their processes.
    processes = subprocess.check_output(['ps', '-axo', 'comm='], text=True)
    if any(Path(line.strip()).name in ('ds4', 'ds4-server', 'ds4-bench') for line in processes.splitlines()):
        raise ValueError('A DS4 process is already running. Keep one large model loaded at a time.')


def serve(key, model):
    if platform.system() != 'Darwin':
        raise ValueError('DS4 Metal requires macOS.')
    source = ROOT / model['source']
    check_revision(source, model['revision'])
    if not os.access(source / 'ds4-server', os.X_OK):
        raise ValueError(f'DS4 is not built. Run ./run.sh setup {key}.')
    os.umask(0o077)
    lock_path = ROOT / '.local/inference.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise ValueError('Another model owns the inference lock.') from None
    port = integer_setting(os.environ, 'LLM_PORT', model['port'], 1, 65535)
    ensure_idle([port, *(m['port'] for m in profiles()['models'].values())])
    args = server_args(key, model, os.environ)
    environment = dict(os.environ)
    environment.pop('DS4_METAL_Q8_MV_NSG', None)
    environment.pop('DS4_METAL_Q8_MV_ROWS', None)
    environment.setdefault('DS4_METAL_MODEL_UNTRACKED', '1')
    environment.update(model.get('environment', {}))
    os.set_inheritable(lock, True)
    os.chdir(source)
    os.execve(args[0], args, environment)


def main():
    parser = argparse.ArgumentParser(description='DeepSeek V4 Flash and Qwen on native DS4 Metal.')
    parser.add_argument('action', nargs='?', default='help',
                        choices=['help', 'list', 'downloaded', 'setup', 'download', 'verify', 'serve', 'ui'])
    parser.add_argument('model', nargs='?')
    args = parser.parse_args()
    if args.action == 'help':
        parser.print_help()
        print('\n./run.sh setup|download|verify|serve qwen|deepseek\n'
              './run.sh gateway qwen|deepseek\n./run.sh ui\n./run.sh check')
    elif args.action in ('list', 'downloaded'):
        for key, model in profiles()['models'].items():
            try:
                for artifact in required_artifacts(model):
                    check_artifact(artifact)
                status = 'present (size checked)'
            except ValueError:
                status = 'missing or incomplete'
            print(f'{key:10} {model["name"]:26} port {model["port"]}  {status}')
    elif args.action == 'ui':
        if not (ROOT / '.env').is_file():
            raise ValueError('Copy config/env.example to .env and set the required values first.')
        os.chdir(ROOT)
        os.execvp('docker', ['docker', 'compose', '--env-file', str(ROOT / '.env'), 'up', '-d'])
    else:
        if not args.model:
            parser.error('Select qwen or deepseek.')
        key, model = model_profile(args.model)
        if args.action == 'serve':
            serve(key, model)
        elif args.action == 'setup':
            setup(model)
        elif args.action == 'download':
            download(model)
        elif args.action == 'verify':
            for artifact in required_artifacts(model):
                check_artifact(artifact, checksum=True)
            print('Weight sizes and SHA-256 checksums verified.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
