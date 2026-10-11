"""Verify the configured stack before opening the dashboard."""
import argparse
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='DotAgents starts only after readiness verification passes.')
    parser.add_argument('--verify', action='store_true', help='Run readiness checks and the full test suite without starting services')
    args = parser.parse_args(argv)
    from launcher import verify
    endpoints = verify(full=args.verify)
    if args.verify:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Run ./run.sh in a terminal.')
    from node_discovery import save
    from launcher import ROOT
    save(ROOT, endpoints)
    from tui import DotAgents
    app = DotAgents()
    app.verified = True
    app.run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
