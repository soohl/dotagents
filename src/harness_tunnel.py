"""Restore this stack's authenticated browser tunnel."""
import subprocess


def compose(root, *args, capture=False):
    return subprocess.run(['docker', 'compose', '--env-file', str(root / '.env'), *args],
                          cwd=root, check=True, text=True, capture_output=capture)


def enable(root):
    compose(root, 'up', '-d', '--wait', 'tailscale', 'authelia', 'caddy')
    hostname = next(line.split('=', 1)[1].strip().strip('\"\'')
                    for line in (root / '.env').read_text().splitlines()
                    if line.startswith('FUNNEL_HOSTNAME='))
    node = compose(root, 'ps', '-q', 'tailscale', capture=True).stdout.strip()
    from harness_service import IMAGE
    def probe(port, path):
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'container:' + node,
                                 '--entrypoint', 'curl', IMAGE, '--max-time', '10',
                                 '--connect-to', f'{hostname}:443:127.0.0.1:{port}',
                                 '-H', 'Accept: text/html', '-sS', '-o', '/dev/null', '-w', '%{http_code}',
                                 f'https://{hostname}{path}'], capture_output=True, text=True, check=True)
        return result.stdout
    if (any(probe(8443, path) not in ('302', '303') for path in ('/', '/chat/', '/agent/', '/images/'))
            or probe(8443, '/_dotagents/status') != '403'):
        raise ValueError('The browser edge did not enforce authentication. Tunnel routes remain unchanged.')
    if (probe(9443, '/') != '200' or probe(9443, '/admin/') != '403'
            or probe(9443, '/api/authz/forward-auth') != '403'):
        raise ValueError('The identity TLS endpoint is unavailable. Tunnel routes remain unchanged.')
    compose(root, 'exec', '-T', 'tailscale', 'tailscale', 'funnel', '--bg', '--tcp=8443', 'tcp://127.0.0.1:9443')
    try:
        compose(root, 'exec', '-T', 'tailscale', 'tailscale', 'funnel', '--bg', '--tcp=443', 'tcp://127.0.0.1:8443')
    except subprocess.CalledProcessError:
        compose(root, 'exec', '-T', 'tailscale', 'tailscale', 'funnel', '--tcp=8443', 'off')
        raise
    print(f'Services: https://{hostname}/')
    print(f'Chat: https://{hostname}/chat/; Agent: https://{hostname}/agent/')
    print(f'Image: https://{hostname}/images/')


def disable(root):
    compose(root, 'exec', '-T', 'tailscale', 'tailscale', 'funnel', '--tcp=443', 'off')
    compose(root, 'exec', '-T', 'tailscale', 'tailscale', 'funnel', '--tcp=8443', 'off')
