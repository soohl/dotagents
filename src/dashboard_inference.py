"""Foreground gateway child owned and stopped by the dashboard."""
import os
import signal
import subprocess
import sys
import threading

import launcher


def watch_dashboard(parent, done, stop_timeout=5):
    # The dashboard creates this session. Close the owned group if the terminal
    # kills the dashboard before Textual can run its shutdown hook.
    while not done.wait(.5):
        if parent <= 1 or os.getppid() != parent:
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGTERM)
                if not done.wait(stop_timeout):
                    os.killpg(os.getpgrp(), signal.SIGKILL)
            else:
                os.kill(os.getpid(), signal.SIGTERM)
            return


if __name__ == '__main__':
    parent = os.getppid()
    done = threading.Event()
    threading.Thread(target=watch_dashboard, args=(parent, done), daemon=True).start()
    try:
        launcher.start(initial=sys.argv[1] if len(sys.argv) > 1 else None, dashboard=True)
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Gateway error: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        done.set()
