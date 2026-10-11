"""Dashboard presentation helpers and device widgets."""
from dataclasses import dataclass
from enum import IntEnum
import re
from urllib.parse import urlsplit

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from charts import UsageChart, InferenceChart, MemoryCharts
from inference_speeds import WINDOW
from inference_view import endpoint_address
import telemetry


class InferenceColumn(IntEnum):
    CONNECTION = 0
    DEVICE = 1
    DEVICE_INFO = 2
    CAPABILITY = 3
    MODEL = 4
    ENGINE = 5
    STATE = 6


INFERENCE_COLUMNS = [('', 2), ('Device', 16), ('Device Info', 24),
                     ('Services', 12), ('Model', 12),
                     ('Engine', 15), ('State', 12)]


def stack_addresses(snapshot):
    """Use published application ports and configured inference API endpoints."""
    def published(name):
        return list(dict.fromkeys(address for container in snapshot.get('containers', [])
                                  if container['name'] == name and container.get('state') == 'running'
                                  for address in container.get('published', [])))
    inference = list(dict.fromkeys(endpoint_address(endpoint)
                                  for service in snapshot.get('inference_services', [])
                                  if service['role'] == 'service'
                                  for endpoint in service.get('endpoints', [])))
    browser = published('caddy') or [a for a in published('tailscale') if a.endswith(':3000')]
    return {'Gateway': browser,
            'Model APIs': [address for address in inference if address != '—'],
            'Tunnel': [address for address in published('tailscale') if address not in browser],
            'Authentication': published('authelia'),
            'Chat': published('chat') or browser,
            'Agent': published('agent') or browser, 'Image workspace': published('image-web')}


def grouped_addresses(addresses):
    """List each host once, with its ports; keep different hosts on separate lines."""
    hosts = {}
    for address in addresses:
        host, separator, port = address.rpartition(':')
        if separator and host and port.isdigit():
            hosts.setdefault(host, set()).add(port)
    return '\n'.join(f"{host}:{','.join(sorted(ports, key=int))}"
                     for host, ports in hosts.items()) or '—'


def docker_state(container):
    """Use process state when a running container has no health check."""
    state, health = container.get('state'), container.get('health')
    if state in ('exited', 'stopped', 'created', 'paused', 'disabled', 'removed'):
        return 'disabled'
    if state == 'running' and health in ('healthy', '', '—', None):
        return 'healthy'
    return 'unhealthy'


def model_api_counts(services):
    """Count configured APIs once, regardless of model or capability count."""
    endpoints, ready = set(), set()
    for service in services:
        if service['role'] != 'service':
            continue
        addresses = {url.rstrip('/') for url in service.get('endpoints', [])}
        endpoints.update(addresses)
        if service['state'] in ('ready', 'running'):
            ready.update(addresses)
    return len(ready), len(endpoints)


def stack_services(snapshot):
    """Summarize service availability separately from model residency."""
    checks = {check['name']: check for check in snapshot.get('checks', [])}
    rows = []
    for key, label in [('Stack tunnel', 'Tunnel'), ('Gateway', 'Gateway')]:
        check = checks.get(key, {'state': 'unknown', 'detail': 'No observation available.'})
        state = 'ready' if check['state'] in ('ready', 'healthy', 'running', 'enabled') else 'disabled'
        rows.append((label, state, f"{check['detail']} Observed state: {check['state']}."))
    services = [s for s in snapshot['inference_services'] if s['role'] == 'service']
    def inference_service(name, selected):
        ready = [s for s in selected if s['state'] in ('ready', 'running')]
        active = any('running' in s.get('model_activity', {}).values() or s['state'] == 'running'
                     or ((s.get('node') or {}).get('location') == 'local'
                         and any(m.get('activity') == 'running' and m.get('endpoint') in s.get('endpoints', [])
                                 for m in snapshot['models'])) for s in ready)
        state = 'running' if active else 'ready' if ready else 'disabled'
        available, total = model_api_counts(selected)
        detail = f'{available}/{total} APIs available. '
        detail += ' '.join(f"{s.get('id', s['name'])}: {s['state']}. {s['detail']}" for s in selected)
        return name, state, detail
    def application_service(name, compose_name, application):
        container = next((c for c in snapshot.get('containers', []) if c['name'] == compose_name), None)
        available = container is not None and docker_state(container) == 'healthy'
        detail = (f"Process: {container['state']}; health check: {container.get('health', 'none')}."
                  if container else 'No application container observed.')
        return name, 'ready' if available else 'disabled', (
            f'{application}. {detail} This row reports application availability. '
            'Model API availability and activity appear under Model APIs and Inference.')
    rows.append(application_service('Authentication', 'authelia',
                                    'Authelia shared remote browser login; local access needs no login'))
    harness = snapshot.get('harness', {})
    for role, label in [('chat', 'Chat'), ('agent', 'Agent')]:
        row = application_service(label, role, f'{label} has its own Harness instance and conversation history')
        if row[1] == 'ready' and harness.get('services', {}).get(role, {}).get('active', 0):
            row = (label, 'running', row[2])
        rows.append(row)
    rows.append(application_service('Image workspace', 'image-web', 'Image workspace'))
    rows.append(inference_service('Model APIs', services))
    return rows


def inference_widths(width, rows=()):
    """Fit full labels and numeric readings; scroll when they cannot fit."""
    hidden = {InferenceColumn.DEVICE_INFO} if width < 190 else set()
    widths = []
    for index, (label, _) in enumerate(INFERENCE_COLUMNS):
        if index == InferenceColumn.CONNECTION:
            widths.append(2)
            continue
        if index in hidden:
            # Textual requires positive widths, including for blank columns.
            widths.append(1)
            continue
        minimum = Text(label).cell_len + 1
        minimum = max(minimum, max((display(cells[index]).cell_len + 1
                                    for cells in rows), default=minimum))
        widths.append(minimum)
    return widths, hidden


def speed_detail(row, history, now):
    image = row['service'] == 'Image'
    metrics = [('steps', 'Steps/s'), ('duration', 'Time s')] if image else [
        ('prefill', 'Prefill tok/s'), ('decode', 'Decode tok/s')]
    values = []
    for metric, label in metrics:
        value = history.value(metric, now)
        values.append(f'{label}: {value:.1f}' if value is not None else f'{label}: not reported')
    for field, label in [('prefill_seconds', 'Prefill s'), ('ttft_seconds', 'TTFT s'),
                         ('request_seconds', 'Request s'), ('output_tokens', 'Output tokens'),
                         ('input_tokens', 'Prompt tokens'), ('cached_tokens', 'Cached tokens'),
                         ('uncached_tokens', 'New prompt tokens')]:
        if field in history.latest:
            values.append(f'{label}: {history.latest[field]:.2f}')
    timing = ('oMLX rates update when a reply completes. Prefill timing runs through the first model token; '
              'decode timing starts after it.' if row.get('engine', '').lower() == 'omlx' else
              'Decode uses the latest engine chunk.')
    return (' Last measured: ' + ' · '.join(values) +
            f'. System speed charts cover {WINDOW} seconds with a separate scale for each metric. ' +
            ('Steps/s includes model loading and image encoding.' if image else
             'Prefill and decode hold the last measured rate until the next report. '
             'A connection failure leaves a gap. Prefill counts uncached prompt tokens. '
             + timing))


def plain_output(value):
    # Render subprocess text as text, never as terminal commands or Rich markup.
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', value)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value)


@dataclass(frozen=True)
class Status:
    label: str
    state: str = ''

    @property
    def animated(self):
        return (self.state or self.label).lower() in ('running', 'starting', 'stopping')

    def render(self, frame=0):
        state = (self.state or self.label).lower()
        if self.animated:
            marker, color = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[frame % 10], '#60a5fa'
        elif state in ('healthy', 'ready', 'connected', 'loaded', 'success', 'direct', 'relay', 'peer relay'):
            marker, color = '●', '#4ade80'
        elif state == 'disconnected':
            marker, color = '●', '#f87171'
        elif state in ('error', 'failed', 'unhealthy', 'degraded', 'missing'):
            marker, color = '●', '#facc15'
        else:
            marker, color = '●', '#94a3b8'
        text = Text(marker, style=color)
        text.append(' ' + plain_output(self.label))
        return text

    def __str__(self):
        return self.label


def display(value):
    if isinstance(value, Status):
        return value.render()
    if isinstance(value, Text):
        return value
    return Text(plain_output(str(value)))


def model_label(model):
    """Keep model family markers separate from health indicators."""
    label = Text()
    name = plain_output(model).replace('\n', ' ')
    if name.lower().rsplit('/', 1)[-1].startswith('qwen'):
        label.append('✦ ', style='#a78bfa')
    elif name.lower().rsplit('/', 1)[-1].startswith('deepseek'):
        label.append('✦ ', style='#60a5fa')
    label.append(name)
    return label


def engine_label(engine):
    name = {'ds4': 'DS4', 'omlx': 'oMLX', 'exllamav3': 'ExLlamaV3',
            'diffusers': 'Diffusers'}.get(engine.lower(), engine)
    return Text(plain_output(name).replace('\n', ' '))


class ObservationTable(DataTable):
    """Refit columns after the table receives its final layout width."""

    def on_resize(self):
        self.app.call_after_refresh(self.app.resize_snapshot)


def hardware_label(reading):
    """Use GPU/chip, then CPU count, on every device card."""
    gpus = reading.get('gpus', [])
    if reading.get('memory', {}).get('shared'):
        device = reading['chip']
    else:
        device = ', '.join(gpu['name'].removeprefix('NVIDIA GeForce ').removeprefix('NVIDIA ')
                           for gpu in gpus) or reading['chip']
    cores = reading.get('cores')
    label = device + (f' · {int(cores)} CPUs' if cores else '')
    if gpus and any('power_watts' in gpu or 'power_limit_watts' in gpu for gpu in gpus):
        def watts(field):
            values = [gpu.get(field) for gpu in gpus]
            return f'{sum(values):.0f}' if all(value is not None for value in values) else '—'
        label += f" · Power {watts('power_watts')}/{watts('power_limit_watts')} W"
    return label


class RemoteHardware(Vertical):
    def __init__(self, endpoint):
        super().__init__(classes='hardware-card remote-hardware')
        self.endpoint = endpoint
        self.previous_cpu_ticks = None
        self.reading = None
        self.cpu = None
        self.connected = False

    def compose(self):
        yield Static(display(Status('Node · ' + self.endpoint['device'], 'unknown')), classes='hardware-title')
        yield Static('Connecting…', classes='hardware-description', markup=False)
        yield InferenceChart(device_key='remote:' + self.endpoint['device'],
                             endpoint_host=urlsplit(self.endpoint['url']).hostname)
        with MemoryCharts():
            yield UsageChart('RAM', '#4ade80', max_gap=12)
            yield UsageChart('VRAM', '#c084fc', max_gap=12, classes='gpu-memory')

    def show_reading(self, result):
        self.connected = result['state'] == 'connected'
        self.query_one('.hardware-title', Static).update(display(Status('Node · ' + self.endpoint['device'], result['state'])))
        charts = list(self.query(UsageChart))
        info = self.query_one('.hardware-description', Static)
        if result['state'] != 'connected':
            self.previous_cpu_ticks = None
            info.update('Metrics unavailable · retrying')
            info.tooltip = display(result.get('detail', ''))
            for chart in charts:
                chart.sample(result['sampled_at'], None, 'Unavailable')
            return
        reading = result['hardware']
        timestamp = reading['sampled_at']
        if reading['reset']:
            self.previous_cpu_ticks = None
        cpu = telemetry.cpu_usage(self.previous_cpu_ticks, reading['cpu_ticks'])
        self.reading, self.cpu = reading, cpu
        self.previous_cpu_ticks = reading['cpu_ticks']
        info.update(display(hardware_label(reading)))
        disk = reading['disk']
        disk_info = (f"Disk {disk['used'] / 2**30:.1f}/{disk['total'] / 2**30:.1f} GiB"
                     if disk['used'] is not None and disk['total'] else 'Disk unavailable')
        info.tooltip = display(f"{hardware_label(reading)} · {reading['chip']} · {reading['os']} · {disk_info}. "
                               'Power is GPU board draw / configured power limit in watts.')
        # This card summarizes GPU memory. Individual GPU counters stay in the tooltip.
        gpus = reading['gpus']
        gpu_capacity = ({'used': sum(g['used'] for g in gpus), 'total': sum(g['total'] for g in gpus)}
                        if gpus and all(g['used'] is not None and g['total'] for g in gpus)
                        else {'used': None, 'total': None})
        for chart, metric in zip(charts, (reading['memory'], gpu_capacity)):
            used, total = metric['used'], metric['total']
            ratio = used / total if used is not None and total else None
            detail = f'{used / 2**30:.1f}/{total / 2**30:.0f} GiB' if ratio is not None else 'Unavailable'
            chart.sample(timestamp, ratio, detail)
            chart.tooltip = display(detail)
        charts[1].tooltip = display(' · '.join(
            f"{g['name']}: {g['used'] / 2**30:.1f}/{g['total'] / 2**30:.1f} GiB · GPU {g['utilization']}%"
            for g in gpus if g['used'] is not None and g['total']))
