"""Launch pinned native engines. Model pins and tuning live in config/."""
import hashlib
from runtime_config import WORKER_PORT, gateway_port, model_profiles
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def profiles():
    return model_profiles(ROOT)


def configured_models(config=None):
    """Keep the install catalog separate from the models this host enables."""
    config = profiles() if config is None else config
    keys = config.get('enabled_models', list(config['models']))
    if (not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys)
            or len(set(keys)) != len(keys) or any(k not in config['models'] for k in keys)
            or config['default_model'] not in keys):
        raise ValueError('enabled_models must contain known models and include default_model.')
    return {key: config['models'][key] for key in keys}


def model_profile(name):
    name = name or profiles()['default_model']
    for key, model in profiles()['models'].items():
        if name == key or name in model.get('aliases', []):
            return key, model
    raise ValueError(f'Unknown model: {name}. Use deepseek or qwen-omlx.')


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def check_revision(source, revision):
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != revision:
        raise ValueError('Engine revision differs from config/models.json. Restore the pinned source.')
    command(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'])


def check_artifact(artifact, checksum=False):
    path = ROOT / artifact['path']
    if not path.is_file() or path.stat().st_size != artifact['size']:
        raise ValueError(f'Missing or incomplete weights: {artifact["path"]}. Install the pinned artifacts from config/models.json.')
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


def integer_setting(environment, key, default, minimum, maximum):
    value = int(environment.get(key, default))
    if not minimum <= value <= maximum:
        raise ValueError(f'{key} must be between {minimum} and {maximum}.')
    return value


def server_args(key, model, environment):
    if model.get('engine') == 'omlx':
        from omlx_backend import server_args as omlx_args
        return omlx_args(model, environment)
    context = integer_setting(environment, 'LLM_CTX', model['context'], 2, 131072)
    port = integer_setting(environment, 'LLM_PORT', WORKER_PORT, 1, 65535)
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
    processes = subprocess.check_output(['ps', '-axo', 'command='], text=True)
    names = {'ds4', 'ds4-server', 'ds4-bench', 'omlx', 'omlx-server'}
    for line in processes.splitlines():
        try:
            args = shlex.split(line)
        except ValueError:
            continue
        if not args:
            continue
        executable = Path(args[0]).name
        running = executable in names
        if executable.lower().startswith('python') and len(args) > 1:
            running = Path(args[1]).name in names or args[1:3] == ['-m', 'omlx']
        if running:
            raise ValueError('An inference process is already running. Keep one large model loaded at a time.')


def compose(*args, **kwargs):
    return command(['docker', 'compose', '--env-file', str(ROOT / '.env'), *args],
                   cwd=ROOT, **kwargs)


def prerequisites():
    if platform.system() != 'Darwin':
        raise ValueError('DotAgents requires macOS.')
    for tool in ('git', 'docker', 'tailscale'):
        if not shutil.which(tool):
            raise ValueError(f'Install {tool} before running DotAgents.')
    if not (ROOT / '.env').is_file():
        raise ValueError('Copy config/env.example to .env and set the required values first.')
    compose('config', '--quiet')


def verify(*, full=True):
    from verification import verify as verify_installation
    return verify_installation(ROOT, full=full)


def start(initial=None, restart=False, dashboard=False):
    initial, _ = model_profile(initial)
    if initial not in configured_models():
        raise ValueError('The requested model is not enabled in config.yaml.')
    prerequisites()
    from gateway_service import GatewayService
    with GatewayService(ROOT) as service:
        owner = service.owner()
        if dashboard and owner and not owner['managed']:
            restart = True  # Replace this checkout's verified old foreground gateway.
        if owner and not restart:
            raise ValueError('This checkout\'s gateway is already running. '
                             'Check the owning dashboard before starting another gateway.')
        if not owner:
            ensure_idle([WORKER_PORT, gateway_port(ROOT)])
        selected = profiles()['models'][initial]
        for artifact in required_artifacts(selected):
            check_artifact(artifact)
        if selected.get('engine') == 'omlx':
            from omlx_backend import validate
            validate(selected)
        else:
            check_revision(ROOT / selected['source'], selected['revision'])
            if not os.access(ROOT / selected['source'] / 'ds4-server', os.X_OK):
                raise ValueError('Missing pinned native engine. Run ./run.sh --verify.')
        import harness_service
        if not harness_service.enabled(ROOT):
            raise ValueError('Missing Chat and Agent configuration in .local/harness/.')
        for role in ('chat', 'agent'):
            harness_service.workspace(root=ROOT, role=role)
        harness_service.write_endpoints(ROOT)
        import things_bridge
        if things_bridge.settings(ROOT).get(things_bridge.KEY):
            things_bridge.start(ROOT)
        from runtime_config import load
        if load(ROOT)['integrations']['calendar']['enabled']:
            import calendar_bridge
            calendar_bridge.start(ROOT)
        compose('up', '-d', '--wait', '--remove-orphans', '--no-build', '--pull', 'never')
        address = harness_service.BASE
        if restart:
            service.stop()
        from inference_gateway import ModelGateway, main as serve_gateway
        gateway = ModelGateway(initial)
        try:
            service.register()
        except BaseException:
            gateway.shutdown()
            raise
        print(f'Chat and Agent: {address}\nGateway: starting inference; closing this process stops its engine.', flush=True)
    try:
        serve_gateway(initial, gateway=gateway)
    finally:
        service.unregister()
