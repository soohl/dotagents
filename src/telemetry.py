"""Read hardware counters without elevated privileges or remote execution."""
import csv
import ctypes
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import time


def cpu_ticks():
    """Return cumulative busy/total ticks; callers compute interval utilization."""
    try:
        if platform.system() == 'Darwin':
            lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            lib.mach_host_self.restype = ctypes.c_uint
            lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int,
                                           ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
            lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
            host = lib.mach_host_self()
            try:
                ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
                if lib.host_statistics(host, 3, ticks, ctypes.byref(count)) != 0 or count.value != 4:
                    return None
                return {'busy': ticks[0] + ticks[1] + ticks[3], 'total': sum(ticks)}
            finally:
                lib.mach_port_deallocate(ctypes.c_uint.in_dll(lib, 'mach_task_self_').value, host)
        if platform.system() == 'Linux':
            return linux_cpu_ticks(Path('/proc/stat').read_text())
    except (OSError, ValueError, AttributeError):
        pass
    return None


def linux_cpu_ticks(output):
    fields = output.splitlines()[0].split() if output else []
    if not fields or fields[0] != 'cpu' or len(fields) < 5:
        return None
    try:
        # Guest ticks are already included in user/nice; do not count them twice.
        ticks = [int(value) for value in fields[1:9]]
    except ValueError:
        return None
    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    return {'busy': sum(ticks) - idle, 'total': sum(ticks)}


def cpu_usage(previous, current):
    if previous is None or current is None:
        return None
    elapsed = current['total'] - previous['total']
    busy = current['busy'] - previous['busy']
    if elapsed <= 0 or busy < 0 or busy > elapsed:
        return None
    return busy / elapsed


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def mac_memory(output):
    """Count resident anonymous, wired, and compressed pages; exclude file cache."""
    page = re.search(r'page size of (\d+) bytes', output or '')
    fields = dict(re.findall(r'^([^:\n]+):\s+(\d+)\.', output or '', re.M))
    names = ('Anonymous pages', 'Pages wired down', 'Pages occupied by compressor')
    if not page or not all(name in fields for name in names):
        return None
    return int(page[1]) * sum(int(fields[name]) for name in names)


def mac_gpus(output, total):
    try:
        entries = plistlib.loads((output or '').encode())
    except (ValueError, plistlib.InvalidFileException):
        return []
    rows = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict):
            continue
        stats = entry.get('PerformanceStatistics', {})
        if not isinstance(stats, dict):
            stats = {}
        rows.append({'id': f'metal-{index}', 'name': str(entry.get('model', 'Apple GPU')),
                     'used': number(stats.get('In use system memory')), 'total': total,
                     'utilization': number(stats.get('Device Utilization %')), 'shared': True})
    return rows


def nvidia_gpus(output):
    rows = []
    for fields in csv.reader((output or '').splitlines(), skipinitialspace=True):
        if len(fields) not in (5, 7):
            continue
        uuid, name, used, total, busy = fields[:5]
        used, total = number(used), number(total)
        rows.append({'id': uuid, 'name': name,
                     'used': used * 2**20 if used is not None else None,
                     'total': total * 2**20 if total is not None else None,
                     'utilization': number(busy), 'shared': False})
        if len(fields) == 7:
            rows[-1].update(power_watts=number(fields[5]), power_limit_watts=number(fields[6]))
    return rows


@lru_cache(maxsize=1)
def mac_model(root, capture):
    """Read the product name once; never infer a product from the CPU alone."""
    try:
        data = json.loads(capture(['system_profiler', 'SPHardwareDataType', '-json'], root) or '{}')
        return data['SPHardwareDataType'][0].get('machine_name', '')
    except (ValueError, KeyError, IndexError, TypeError):
        return ''


def inference_summary(reading, cpu, available=True):
    """Device identity plus CPU, RAM and GPU-memory percentages."""
    if not reading:
        return ('—',) * 4
    shared = reading.get('memory', {}).get('shared', False)
    gpus = reading.get('gpus', [])
    if shared:
        label = ' '.join(filter(None, (reading.get('model'), reading.get('chip', '').removeprefix('Apple '))))
    else:
        label = ', '.join(dict.fromkeys(g['name'].replace('NVIDIA GeForce ', 'NVIDIA ') for g in gpus))
    label = label or reading.get('chip') or '—'
    if not available:
        return label, '—', '—', '—'
    memory = reading.get('memory', {})
    ram = memory.get('used') / memory['total'] if memory.get('used') is not None and memory.get('total') else None
    # Apple GPU allocation shares RAM. Multiple discrete GPUs use a weighted total.
    vram = None
    if gpus and all(g.get('used') is not None and g.get('total') for g in gpus):
        total = memory.get('total') if shared else sum(g['total'] for g in gpus)
        if total:
            vram = sum(g['used'] for g in gpus) / total
    return (label, *(f'{value:.0%}' if value is not None else '—' for value in (cpu, ram, vram)))


def read(root, capture):
    system = platform.system()
    total = used = None
    chip = platform.processor() or platform.machine()
    cores = os.cpu_count()
    gpus = []
    if system == 'Darwin':
        values = (capture(['sysctl', '-n', 'machdep.cpu.brand_string', 'hw.logicalcpu',
                           'hw.memsize'], root) or '').splitlines()
        if len(values) == 3:
            chip, cores, total = values[0], number(values[1]), number(values[2])
        used = mac_memory(capture(['vm_stat'], root))
        gpus = mac_gpus(capture(['ioreg', '-a', '-r', '-d', '1', '-c', 'AGXAccelerator'], root), total)
    elif system == 'Linux':
        try:
            fields = dict(re.findall(r'^(\w+):\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M))
            total = int(fields['MemTotal']) * 1024
            used = total - int(fields['MemAvailable']) * 1024
            cpu = re.search(r'^model name\s*:\s*(.+)', Path('/proc/cpuinfo').read_text(), re.M)
            if cpu:
                chip = cpu[1]
        except (OSError, KeyError, ValueError):
            pass
        gpus = nvidia_gpus(capture(['nvidia-smi',
            '--query-gpu=uuid,name,memory.used,memory.total,utilization.gpu,power.draw,power.limit',
            '--format=csv,noheader,nounits'], root))
    disk = shutil.disk_usage(root)
    cpu = cpu_ticks()
    return {'sampled_at': time.monotonic(), 'cpu_ticks': cpu, 'chip': chip, 'cores': cores,
            'model': mac_model(root, capture) if system == 'Darwin' else '',
            'os': f'{system} {platform.release()} / {platform.machine()}',
            'memory': {'used': used, 'total': total, 'shared': system == 'Darwin' and platform.machine() == 'arm64'},
            'gpus': gpus, 'disk': {'used': disk.used, 'total': disk.total}}
