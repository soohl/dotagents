"""Manage this checkout's gateway without a terminal or a multiplexer."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.request

import launcher
from runtime_config import gateway_port


class GatewayService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / '.local/inference-gateway'
        self.record = self.directory / 'gateway.json'
        self.log = self.directory / 'gateway.log'

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.control = (self.directory / 'control.lock').open('a+')
        fcntl.flock(self.control, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        self.control.close()

    def processes(self, pid=None):
        args = ['ps', '-p', str(pid)] if pid is not None else ['ps', '-ax']
        result = subprocess.run(args + ['-o', 'pid=,ppid=,uid=,stat=,lstart=,command='],
                                capture_output=True, text=True, check=False)
        if result.returncode and pid is None:
            raise ValueError('Cannot inspect gateway ownership.')
        rows = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 9)
            if len(fields) != 10 or 'Z' in fields[3]:
                continue
            try:
                rows.append(dict(pid=int(fields[0]), parent=int(fields[1]), uid=int(fields[2]),
                                 started=' '.join(fields[4:9]), command=shlex.split(fields[9])))
            except ValueError:
                continue
        return rows

    def is_gateway(self, process):
        args = list(process['command'])
        if process['uid'] != os.getuid() or not args or not Path(args.pop(0)).name.lower().startswith('python'):
            return False
        if args and args[0] == '-u':
            args.pop(0)
        if not args:
            return False
        script = args.pop(0)
        if script == str(self.root / 'src/dotagents.py'):
            if not args or args.pop(0) not in ('serve', 'serve-restart'):
                return False
        elif script not in (str(self.root / 'src/inference_gateway.py'),
                            str(self.root / 'src/dashboard_inference.py')):
            return False
        if not args:
            return True
        if len(args) != 1:
            return False
        try:
            launcher.model_profile(args[0])
        except ValueError:
            # Retired models still identify a gateway that this checkout owns.
            return args[0] in launcher.profiles().get('retired_model_aliases', [])
        return True

    @staticmethod
    def same_process(a, b):
        return all(a.get(key) == b.get(key) for key in ('pid', 'uid', 'started', 'command'))

    def owner(self):
        if self.record.exists():
            try:
                saved = json.loads(self.record.read_text())
                if not isinstance(saved, dict) or type(saved.get('pid')) is not int or saved['pid'] <= 0:
                    raise ValueError('Invalid gateway process record.')
            except (OSError, ValueError) as error:
                raise ValueError(f'Cannot read gateway ownership: {self.record}') from error
            live = self.processes(saved['pid'])
            if live and self.same_process(saved, live[0]):
                if not self.is_gateway(live[0]):
                    raise ValueError('The gateway record refers to another process. It was not stopped.')
                return dict(live[0], managed=True)
        # Adopt only the old gateway for this checkout. Its worker and lock
        # establish ownership; a matching command line alone is insufficient.
        lock_path = self.root / '.local/inference.lock'
        active_path = self.directory / 'active.json'
        if not lock_path.exists() or not active_path.exists():
            return None
        with lock_path.open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return None
            except BlockingIOError:
                pass
        try:
            active = json.loads(active_path.read_text())
            child_pid = active.get('pid')
            if type(child_pid) is not int or child_pid <= 0:
                return None
        except (OSError, ValueError, AttributeError):
            return None
        children = self.processes(child_pid)
        if not children or children[0]['uid'] != os.getuid():
            return None
        parents = self.processes(children[0]['parent'])
        if parents and parents[0]['pid'] != os.getpid() and self.is_gateway(parents[0]):
            return dict(parents[0], managed=False)
        return None

    def save(self, process):
        with tempfile.NamedTemporaryFile('w', dir=self.directory, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(process, output)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, self.record)
            finally:
                temporary.unlink(missing_ok=True)

    def status(self):
        owner = self.owner()
        result = dict(state='stopped', pid=None, model=None, engine=None, log=str(self.log))
        if owner is None:
            return result
        result.update(state='starting' if owner['managed'] else 'legacy', pid=owner['pid'])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f'http://127.0.0.1:{gateway_port(self.root)}/health', timeout=1) as response:
                health = json.load(response)
            current = health.get('active_model') if isinstance(health, dict) else None
            models = launcher.configured_models()
            if (isinstance(health, dict) and health.get('gateway') == 'ready'
                    and isinstance(current, str) and current in models):
                result.update(state='ready', model=current,
                              engine=models[current].get('engine', 'ds4'))
        except (OSError, ValueError):
            pass
        return result

    def register(self):
        process = self.processes(os.getpid())
        if not process or not self.is_gateway(process[0]):
            raise ValueError('Cannot register this gateway process.')
        self.save(process[0])

    def unregister(self):
        if self.record.exists():
            saved = json.loads(self.record.read_text())
            if saved.get('pid') == os.getpid():
                self.record.unlink(missing_ok=True)

    def stop(self, timeout=70):
        owner = self.owner()
        if owner is None:
            self.record.unlink(missing_ok=True)
            return False
        live = self.processes(owner['pid'])
        if not live or not self.same_process(owner, live[0]) or not self.is_gateway(live[0]):
            raise ValueError('Gateway ownership changed. No process was stopped.')
        try:
            os.kill(owner['pid'], signal.SIGTERM)
        except ProcessLookupError:
            self.record.unlink(missing_ok=True)
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            live = self.processes(owner['pid'])
            if not live or not self.same_process(owner, live[0]):
                self.record.unlink(missing_ok=True)
                return True
            time.sleep(.2)
        raise ValueError(f'Gateway {owner["pid"]} is still shutting down. '
                         'Wait for it to exit before starting another worker.')

    def logs(self):
        paths = [self.log, *(self.directory / f'{key}.log' for key in launcher.profiles()['models'])]
        os.execvp('tail', ['tail', '-n', '20', '-F', *(str(path) for path in paths)])
