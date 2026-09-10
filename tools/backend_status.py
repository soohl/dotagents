"""Report exact source pins and local runtime identity without loading a model."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    candidates = json.loads((ROOT / 'backends/upstream-candidates.json').read_text())
    print('Upstream check:', candidates['checked'])
    for name in ('omlx', 'ds4'):
        pin = candidates[name]
        source = ROOT / 'backends' / name
        if not (source / '.git').exists():
            print(f'{name}: submodule missing; git submodule update --init --recursive')
            continue
        head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        changes = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True)
        print(f'{name}: HEAD {head}' + (' (modified)' if changes else ' (clean)'))
        print('  baseline:', pin['baseline'])
        print('  candidate:', pin['candidate'], '(not locally qualified)')
    qwen = candidates['ds4'].get('qwen_experiment')
    if qwen:
        print('DS4 Qwen experiment (unmerged):', qwen['revision'])
        source = ROOT / qwen['source_subdir']
        if (source / '.git').exists():
            print('  actual HEAD:', subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip())
        else:
            print('  isolated checkout missing; see README.md and backends/upstream-candidates.json')
    command = shutil.which('omlx')
    if command:
        version = subprocess.run([command, '--version'], text=True, capture_output=True)
        print('Installed oMLX:', version.stdout.strip() or version.stderr.strip())
        print('Runtime dependency and native-kernel baseline: backends/runtime-baseline.json')
    else:
        print('Installed oMLX: missing')


if __name__ == '__main__':
    main()
