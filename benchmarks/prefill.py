#!/usr/bin/env python3
"""Model/backend prefill/TTFT experiment; start and stop only owned servers."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MATRIX = {
    'qwen': {'omlx': 'Qwen3.8-Flash-Next', 'ds4': 'Qwen3.8-Flash-Next-DS4'},
    'glm': {'omlx': 'GLM-5.3-Flash', 'ds4': 'GLM-5.3-Flash-DS4'},
    'deepseek-0731': {'ds4': 'DeepSeek-V4-Flash-0731', 'omlx': 'DeepSeek-V4-Flash-0731-MLX'},
    'deepseek-vision': {'ds4': 'DeepSeek-V4-Flash-Vision-Exp'},
}
PRIMARY_BACKENDS = ('ds4', 'omlx')
PROFILES = MATRIX['glm']


def default_backends(model):
    return [backend for backend in MATRIX[model] if backend in PRIMARY_BACKENDS]


def config(path):
    # Repository-owned shell config; use Bash to preserve quoted values.
    if not path.is_file():
        raise FileNotFoundError(path)
    command = 'set -ae; source "$1"; env -0'
    data = subprocess.check_output(['bash', '-c', command, 'config', str(path)])
    return dict(entry.decode().split('=', 1) for entry in data.split(b'\0') if entry)


def source_directory(backend, conf):
    if backend == 'ds4' and os.environ.get('LLM_DS4_MAIN_CANDIDATE') == '1':
        if conf.get('DS4_SOURCE_SUBDIR'):
            raise ValueError('Main candidate cannot replace a model-specific DS4 branch')
        candidate = config(ROOT / 'backends/config/ds4-main.conf')
        return ROOT / candidate['DS4_SOURCE_SUBDIR']
    return ROOT / conf.get('DS4_SOURCE_SUBDIR', 'backends/' + backend)


def preflight(backends, model='glm'):
    configs = {}
    for backend in backends:
        model_dir = ROOT / 'models' / MATRIX[model][backend]
        conf = config(model_dir / 'model.conf')
        key = conf['DEFAULT_VARIANT'].upper().replace('-', '_')
        if f'DOWNLOAD_{key}_FILE' in conf:
            path = model_dir / 'gguf' / conf[f'DOWNLOAD_{key}_FILE']
            if not path.is_file() or path.stat().st_size != int(conf[f'DOWNLOAD_{key}_SIZE']):
                raise SystemExit(f"Weights missing. Download explicitly with: ./run.sh download {conf['MODEL_ID']} {conf['DEFAULT_VARIANT']}")
        else:
            artifact = model_dir / conf.get('MODEL_STORAGE_DIR', 'gguf') / conf[f'DOWNLOAD_{key}_DIR']
            for line in (model_dir / conf[f'DOWNLOAD_{key}_MANIFEST']).read_text().splitlines():
                _, size, name = line.split(maxsplit=2)
                path = artifact / name
                if not path.is_file() or path.stat().st_size != int(size):
                    raise SystemExit(f'Artifact missing/wrong size: {path}')
        configs[backend] = conf
    return configs


def prompt(source, chars, trial, salt):
    nonce = hashlib.sha256(f'{salt}:{trial}:{chars}'.encode()).hexdigest()[:24]
    # Put the nonce first to prevent reuse of a previous user prefix.
    return (f'{nonce}\nRead this passage:\n{source[:chars]}\n\n'
            'Continue the passage in the same language. Output only the continuation.')


def completion(url, backend, text, generated, api_model='glm-5.3-flash'):
    payload = {'model': api_model, 'messages': [{'role': 'user', 'content': text}],
               'max_tokens': generated, 'temperature': 0, 'top_p': 1, 'seed': 42,
               'stream': True, 'stream_options': {'include_usage': True},
               'reasoning_effort': 'high'}
    if backend == 'omlx':
        payload['chat_template_kwargs'] = {'reasoning_effort': 'high'}
        if api_model.startswith('qwen'):
            payload['reasoning_effort'] = 'xhigh'
            payload['chat_template_kwargs'] = {'enable_thinking': True, 'reasoning_effort': 'xhigh'}
    else:
        payload['think'] = True
    if api_model.startswith('qwen'):
        payload['reasoning_effort'] = 'xhigh'
    request = urllib.request.Request(url + '/v1/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    start = time.monotonic()
    first = None
    usage = None
    output = []
    native_timings = None
    with urllib.request.urlopen(request, timeout=1800) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith('data: '):
                continue
            if line[6:] == '[DONE]':
                break
            event = json.loads(line[6:])
            if event.get('error'):
                raise RuntimeError(event['error'])
            if event.get('timings'):
                native_timings = event['timings']
            if event.get('usage'):
                usage = event['usage']
            for choice in event.get('choices', []):
                delta = choice.get('delta', {})
                text = delta.get('content') or delta.get('reasoning_content') or delta.get('reasoning') or ''
                if text:
                    if first is None:
                        first = time.monotonic()
                    output.append(text)
    elapsed = time.monotonic() - start
    if usage is None or first is None:
        raise RuntimeError('Response lacks usage or a content token')
    cached = usage.get('prompt_tokens_details', {}).get('cached_tokens',
                       usage.get('cached_tokens', 0))
    ttft = first - start
    return {'ttft_seconds': ttft, 'elapsed_seconds': elapsed, 'usage': usage,
            'cached_tokens': cached, 'prompt_tokens': usage['prompt_tokens'],
            'completion_tokens': usage['completion_tokens'],
            'effective_input_tokens_per_ttft_second': (usage['prompt_tokens'] - cached) / ttft,
            'native_timings': native_timings, 'output': ''.join(output)}


def wait_ready(process, url, api_model='glm-5.3-flash', discovery_id=None, discovery_name=None):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Owned model server exited; inspect server log')
        try:
            with urllib.request.urlopen(url + '/v1/models', timeout=5) as response:
                data = json.load(response)
            if any(item.get('id') == (discovery_id or api_model) and
                   (discovery_name is None or item.get('name') == discovery_name)
                   for item in data.get('data', [])):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise TimeoutError('Owned model server did not become ready')


def stop(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)


def write_report(directory, rows):
    lines = ['# Prefill measurements by model and backend', '',
             'Same user prompts; compare backend results within a model. Artifact precision/templates differ.',
             'TTFT includes request/template overhead and the first decode step.',
             'Input tokens / TTFT is an end-to-end rate, not native kernel prefill speed.',
             'Only fresh user prefixes are measured; cached template tokens are reported.', '',
             '| Model | Backend | Source characters | Trials | Median actual tokens | Median TTFT s | Median input/TTFT tok/s | Max cached tokens |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for model, backend, chars in sorted({(r.get('model', 'glm'), r['backend'], r['source_chars']) for r in rows}):
        group = [r for r in rows if r.get('model', 'glm') == model and r['backend'] == backend and r['source_chars'] == chars]
        med = lambda key: statistics.median(r[key] for r in group)
        lines.append(f"| {model} | {backend} | {chars} | {len(group)} | {med('prompt_tokens'):.0f} | "
                     f"{med('ttft_seconds'):.3f} | {med('effective_input_tokens_per_ttft_second'):.2f} | "
                     f"{max(r['cached_tokens'] for r in group)} |")
    (directory / 'REPORT.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', nargs='+', choices=MATRIX, default=['glm'])
    parser.add_argument('--backends', nargs='+', choices=PRIMARY_BACKENDS)
    parser.add_argument('--continue-on-error', action='store_true', help='record failed backend attempts and continue')
    parser.add_argument('--available', action='store_true', help='record missing/unsupported combinations and run available ones')
    parser.add_argument('--prompt-seed', default='cook-prefill-v1', help='fixed seed gives identical prompts across separate invocations')
    parser.add_argument('--chars', nargs='+', type=int, default=[32768, 131072])
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--generated', type=int, default=8)
    parser.add_argument('--context', type=int, default=65536)
    args = parser.parse_args()
    if not 2 <= args.context <= 65536 or not 0 < args.generated < args.context:
        parser.error('context must be 2..65536 tokens with room for the requested output')
    source = (ROOT / 'benchmarks/speed/promessi-sposi.txt').read_text()
    if args.trials < 1 or args.generated < 1 or any(c < 1 or c > len(source) for c in args.chars):
        parser.error('invalid trial/output count or source length')
    configs = {}
    skipped = []
    for model in args.models:
        for backend in args.backends or default_backends(model):
            try:
                if backend not in MATRIX[model]:
                    raise SystemExit(f'{model}/{backend}: unsupported in this matrix')
                conf = preflight([backend], model)[backend]
                configs[f'{model}/{backend}'] = conf
            except (SystemExit, FileNotFoundError) as error:
                if not args.available:
                    raise
                skipped.append({'model': model, 'backend': backend, 'reason': str(error)})
    if not configs:
        raise SystemExit('No runnable combinations: ' + json.dumps(skipped))
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    directory = ROOT / 'results' / ('prefill-' + stamp)
    directory.mkdir(parents=True)
    print(directory, flush=True)
    metadata = {'date': stamp, 'cache_policy': 'fresh user prefixes; oMLX cache disabled; DS4 disk checkpoints disabled', 'settings': vars(args), 'reasoning': 'Qwen xhigh; GLM/DeepSeek high (native controls)', 'skipped': skipped,
                'hardware': {'cpu': subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip(),
                             'memory_bytes': int(subprocess.check_output(['sysctl', '-n', 'hw.memsize']))},
                'conditions': {'power_thermal': 'not sampled', 'background_load': os.environ.get('LLM_BENCH_BACKGROUND', 'not independently verified')},
                'models': {b: {k: c[k] for k in ['MODEL_ID', 'TARGET_HF_REPO', 'TARGET_HF_REVISION', 'DEFAULT_VARIANT']} for b, c in configs.items()},
                'discovery_overrides': {key: {k: c[k] for k in ('READINESS_MODEL_ID', 'READINESS_MODEL_NAME') if k in c} for key, c in configs.items()},
                'model_config_sha256': {key: hashlib.sha256((ROOT / 'models' / MATRIX[key.split('/')[0]][key.split('/')[1]] / 'model.conf').read_bytes()).hexdigest() for key in configs},
                'overrides': {k: v for k, v in os.environ.items() if k in ['LLM_PREFILL_CHUNK', 'LLM_ALLOW_BACKEND_DRIFT', 'OMLX_COMMAND', 'LLM_DS4_MAIN_CANDIDATE']},
                'source_revisions': {key: subprocess.check_output(['git', '-C', str(source_directory(key.split('/')[1], c)), 'rev-parse', 'HEAD'], text=True).strip() for key, c in configs.items()},
                'runtime_baseline_sha256': hashlib.sha256((ROOT / 'backends/runtime-baseline.json').read_bytes()).hexdigest()}
    (directory / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    rows = []
    for trial in range(args.trials):
        # Alternate backend order by trial, retaining the same prompt bytes across backends.
        order = list(configs) if trial % 2 == 0 else list(reversed(configs))
        for key in order:
            model, backend = key.split('/')
            api_model = configs[key].get('API_MODEL_ID', configs[key]['MODEL_ID'])
            with tempfile.TemporaryDirectory(prefix='cook-prefill-') as runtime, \
                 (directory / f'{trial}-{model}-{backend}.log').open('w') as log:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                url = f'http://127.0.0.1:{port}'
                env = dict(os.environ, LLM_HOST='127.0.0.1', LLM_PORT=str(port),
                           LLM_CTX=str(args.context), LLM_MAX_TOKENS=str(args.generated),
                           LLM_RUNTIME_ROOT=runtime, LLM_DISABLE_PROMPT_CACHE='1',
                           LLM_SPECULATIVE='off')
                process = subprocess.Popen([str(ROOT / 'run.sh'), 'serve', configs[key]['MODEL_ID']],
                    env=env, stdout=log, stderr=log, start_new_session=True)
                try:
                    print(f'trial {trial + 1}: loading {model}/{backend}', flush=True)
                    wait_ready(process, url, api_model, configs[key].get('READINESS_MODEL_ID'),
                               configs[key].get('READINESS_MODEL_NAME'))
                    completion(url, backend, 'Reply with the word ready.', 16, api_model)
                    for chars in args.chars:
                        text = prompt(source, chars, trial, args.prompt_seed)
                        result = completion(url, backend, text, args.generated, api_model)
                        result.update(model=model, backend=backend, trial=trial + 1, source_chars=chars,
                                      prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
                        rows.append(result)
                        with (directory / 'results.jsonl').open('a') as handle:
                            handle.write(json.dumps(result) + '\n')
                        write_report(directory, rows)
                        print(f"{model}/{backend}: {result['prompt_tokens']} tokens, TTFT {result['ttft_seconds']:.3f}s, "
                              f"cached {result['cached_tokens']}", flush=True)
                except (RuntimeError, OSError, ValueError, TimeoutError) as error:
                    with (directory / 'failures.jsonl').open('a') as handle:
                        handle.write(json.dumps({'model': model, 'backend': backend, 'trial': trial + 1, 'error': str(error)}) + '\n')
                    if not args.continue_on_error:
                        raise
                    print(f'{key}: failed: {error}', flush=True)
                finally:
                    stop(process)
    return 1 if (directory / 'failures.jsonl').exists() else 0


if __name__ == '__main__':
    raise SystemExit(main())
