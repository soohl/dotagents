"""Check the live local service dashboard and save screenshots under .local."""
import base64
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from harness_service import IMAGE
from harness_tunnel import compose


def main():
    container = compose(ROOT, 'ps', '-q', 'caddy', capture=True).stdout.strip()
    network = 'container:' + container
    result = subprocess.run(['docker', 'run', '--rm', '-i', '--network', network,
                             '--entrypoint', 'node', IMAGE, '-'],
                            input=(ROOT / 'tests/dashboard_smoke.cjs').read_text(),
                            text=True, capture_output=True, timeout=120)
    directory = ROOT / '.local/dashboard-check'
    directory.mkdir(parents=True, exist_ok=True)
    for line in result.stdout.splitlines():
        if line.startswith('SCREENSHOT '):
            _, name, image = line.split(' ', 2)
            (directory / (name + '.png')).write_bytes(base64.b64decode(image))
        else:
            print(line)
    print(result.stderr, end='')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
