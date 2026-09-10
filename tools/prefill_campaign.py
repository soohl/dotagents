#!/usr/bin/env python3
"""Wait for in-progress artifacts, verify them, then run the full prefill matrix."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prefill', ROOT / 'benchmarks/prefill.py')
prefill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefill)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(16 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def artifacts():
    for model, profiles in prefill.MATRIX.items():
        for backend in prefill.default_backends(model):
            name = profiles[backend]
            directory = ROOT / 'models' / name
            conf = prefill.config(directory / 'model.conf')
            key = conf['DEFAULT_VARIANT'].upper().replace('-', '_')
            root = directory / conf.get('MODEL_STORAGE_DIR', 'gguf')
            if f'DOWNLOAD_{key}_FILE' in conf:
                yield root / conf[f'DOWNLOAD_{key}_FILE'], int(conf[f'DOWNLOAD_{key}_SIZE']), conf[f'DOWNLOAD_{key}_SHA256']
            else:
                root /= conf[f'DOWNLOAD_{key}_DIR']
                for line in (directory / conf[f'DOWNLOAD_{key}_MANIFEST']).read_text().splitlines():
                    sha, size, name = line.split(maxsplit=2)
                    yield root / name, int(size), sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout-hours', type=float, default=24)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout_hours * 3600
    targets = list(artifacts())
    while True:
        missing = []
        for path, size, sha in targets:
            partial = path.with_name(path.name + '.part')
            if not path.exists() and partial.exists() and partial.stat().st_size == size:
                print(f'Verifying completed partial: {partial}', flush=True)
                if digest(partial) != sha:
                    raise RuntimeError(f'SHA-256 mismatch: {partial}')
                partial.rename(path)
            if not path.is_file() or path.stat().st_size != size:
                missing.append(str(path.relative_to(ROOT)))
        if not missing:
            break
        print(json.dumps({'waiting_for': missing, 'time': time.time()}), flush=True)
        if time.monotonic() >= deadline:
            raise TimeoutError('Artifacts still incomplete; inspect results/downloads logs. No benchmark started.')
        time.sleep(30)
    # Verify every artifact before an unattended performance run, including existing weights.
    for path, _, sha in targets:
        print(f'Verifying: {path}', flush=True)
        if digest(path) != sha:
            raise RuntimeError(f'SHA-256 mismatch: {path}')
    print('All artifacts verified; starting three-trial matrix.', flush=True)
    return subprocess.call([str(ROOT / 'run.sh'), 'prefill', '--models', *prefill.MATRIX,
                            '--continue-on-error', '--trials', '3'])


if __name__ == '__main__':
    raise SystemExit(main())
