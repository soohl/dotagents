"""Read inference rates from engine logs and optional worker health timings."""
from collections import deque
from datetime import datetime
import json
import re
import time

from telemetry import number

WINDOW = 60
RATE = r'(\d+(?:\.\d+)?)'
LOG_TAIL_BYTES = 64 * 1024


def local_metrics(root, model):
    """Read the latest numeric oMLX record independently of Activity readers."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', model):
        return {}
    try:
        with (root / '.local/inference-gateway' / f'{model}.log').open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - LOG_TAIL_BYTES))
            tail = stream.read(LOG_TAIL_BYTES)
    except OSError:
        return {}
    lines = tail.splitlines()
    if tail and not tail.endswith(b'\n'):
        lines = lines[:-1]
    for raw in reversed(lines):
        line = raw.decode('utf-8', errors='replace').replace(' sealedllm-speed: ', ' dotagents-speed: ', 1)
        if ' dotagents-speed: ' not in line:
            continue
        timestamp = log_timestamp(line)
        try:
            result = json.loads(line.split(' dotagents-speed: ', 1)[1])
        except ValueError:
            continue
        metrics = health_metrics({'worker': {'last_request': result}}, 'Chat')
        if timestamp is not None and ('prefill_tps' in metrics or 'decode_tps' in metrics):
            return metrics | {'measured_at': timestamp}
    return {}


def log_timestamp(line, now=None):
    """Read ISO timestamps or DS4 host timestamps without a year."""
    line = line.replace(' sealedllm-speed: ', ' dotagents-speed: ', 1)
    now = time.time() if now is None else now
    if ' dotagents-speed: ' in line:
        try:
            return datetime.fromisoformat(line.split(' ', 1)[0]).timestamp()
        except ValueError:
            return None
    match = re.match(r'(\d{4} \d{2}:\d{2}:\d{2}) ', line)
    if not match:
        return None
    year = datetime.fromtimestamp(now).year
    candidates = []
    for candidate in (year - 1, year, year + 1):
        try:
            candidates.append(datetime.strptime(f'{candidate} {match[1]}', '%Y %m%d %H:%M:%S').timestamp())
        except ValueError:
            continue
    return min(candidates, key=lambda value: abs(value - now)) if candidates else None


class SpeedHistory:
    """Keep bounded samples in memory. Replayed logs keep their original time."""

    def __init__(self):
        self.samples = {key: deque(maxlen=120) for key in ('prefill', 'decode', 'steps', 'duration')}
        self.latest = {}
        self.remote_signature = None
        self.log_time = None
        self.log_offset = None

    def sample(self, metric, timestamp, value):
        samples = self.samples[metric]
        value = number(value)
        if samples and timestamp < samples[-1][0]:
            return
        if samples and samples[-1][:2] == (timestamp, value):
            return
        samples.append((timestamp, value, {}))
        if metric in ('prefill', 'decode') and value is not None:
            self.latest[metric + '_tps'] = value

    def chart_samples(self, metric, now):
        """Hold chat rates for display without adding synthetic measurements."""
        samples = self.samples[metric]
        if metric not in ('prefill', 'decode'):
            return [sample for sample in samples if sample[0] <= now] + [(now, None, {})]
        start, held, output = now - WINDOW, None, []
        for timestamp, value, _ in samples:
            if timestamp > now:
                break
            if timestamp < start:
                held = value
                continue
            if not output and held is not None:
                output.append((start, held, {}))
            if held is not None:
                output.append((timestamp, held, {}))
            output.append((timestamp, value, {}))
            held = value
        if not output and held is not None:
            output.append((start, held, {}))
        output.append((now, held, {}))
        return output

    def feed_log(self, line, wall=None, clock=None):
        line = line.replace(' sealedllm-speed: ', ' dotagents-speed: ', 1)
        wall = time.time() if wall is None else wall
        clock = time.monotonic() if clock is None else clock
        timestamp = log_timestamp(line, wall)
        # Replayed logs retain their original time. Tooltips identify held
        # values as the last measurement, including after a dashboard restart.
        if timestamp is None or timestamp > wall:
            return
        if 'ds4-server:' not in line and ' dotagents-speed: ' not in line:
            return
        if ' dotagents-speed: ' in line:
            try:
                result = json.loads(line.split(' dotagents-speed: ', 1)[1])
            except ValueError:
                return
            if isinstance(result, dict):
                self.observe_local(result | {'measured_at': timestamp}, wall, clock)
            return
        if self.log_time is not None and timestamp < self.log_time:
            return
        self.log_time = timestamp
        if self.log_offset is None:
            self.log_offset = clock - wall
        timestamp += self.log_offset
        if ' prompt start' in line:
            self.latest = {key: value for key, value in self.latest.items()
                           if key in ('prefill_tps', 'decode_tps')}
        match = re.search(r' prefill chunk .* avg=' + RATE + r' t/s ' + RATE + r's$', line)
        if match:
            self.sample('prefill', timestamp, match[1])
            self.latest['prefill_seconds'] = float(match[2])
        match = re.search(r' gen=(\d+).* decoding chunk=' + RATE + r' t/s avg=' + RATE + r' t/s ' + RATE + r's$', line)
        if match:
            self.sample('decode', timestamp, match[2])
            self.latest.update(output_tokens=int(match[1]), decode_seconds=float(match[4]))
        match = re.search(r' gen=(\d+).* finish=\S+ ' + RATE + r's$', line)
        if match:
            self.latest.update(output_tokens=int(match[1]), request_seconds=float(match[2]))

    def observe_local(self, report, wall=None, clock=None):
        """Restore timestamped local rates, including reports older than the window."""
        wall = time.time() if wall is None else wall
        clock = time.monotonic() if clock is None else clock
        timestamp = number(report.get('measured_at'))
        metrics = health_metrics({'worker': {'last_request': report}}, 'Chat')
        if (timestamp is None or timestamp > wall or not metrics
                or self.log_time is not None and timestamp < self.log_time):
            return
        self.log_time = timestamp
        if self.log_offset is None:
            self.log_offset = clock - wall
        timestamp += self.log_offset
        self.latest = {key: value for key, value in self.latest.items()
                       if key in ('prefill_tps', 'decode_tps')} | metrics
        for metric in ('prefill', 'decode'):
            if metric + '_tps' in metrics:
                self.sample(metric, timestamp, metrics[metric + '_tps'])

    def observe(self, metrics, timestamp, connected=True):
        """Plot changed reports once; polling the same request adds no samples."""
        if not connected:
            for metric in self.samples:
                self.sample(metric, timestamp, None)
            return
        if not metrics:
            return
        signature = tuple(sorted((key, value) for key, value in metrics.items() if key != 'age_seconds'))
        if signature == self.remote_signature:
            return
        first = self.remote_signature is None
        self.remote_signature = signature
        self.latest = {key: value for key, value in self.latest.items()
                       if key in ('prefill_tps', 'decode_tps')} | metrics
        # A health report without a timestamp can describe an old request.
        # Establish a baseline first; plot only subsequent changed reports.
        if first and 'age_seconds' not in metrics:
            return
        age = metrics.get('age_seconds', 0)
        if age < 0:
            return
        for metric, field in [('prefill', 'prefill_tps'), ('decode', 'decode_tps'),
                              ('steps', 'steps_per_second'), ('duration', 'request_seconds')]:
            if metric in ('prefill', 'decode') and field not in metrics:
                continue
            if metric not in ('prefill', 'decode') and age > WINDOW:
                continue
            self.sample(metric, timestamp - age, metrics.get(field))
            if metric not in ('prefill', 'decode'):
                # Image summaries remain completed-request points.
                self.sample(metric, timestamp - age, None)

    def value(self, metric, now):
        rate = self.rate(metric, now)
        field = {'prefill': 'prefill_tps', 'decode': 'decode_tps',
                 'steps': 'steps_per_second', 'duration': 'request_seconds'}[metric]
        return self.latest.get(field) if rate is None else rate

    def rate(self, metric, now):
        # Keep the last measured rate visible as a labelled historical value.
        return next((value for timestamp, value, _ in reversed(self.samples[metric])
                     if timestamp >= now - WINDOW and value is not None), None)


def health_metrics(result, service):
    """Accept numeric timing fields only; never retain prompts or responses."""
    worker = result.get('worker')
    if not isinstance(worker, dict):
        return {}
    request = worker.get('last_request')
    if not isinstance(request, dict):
        return {}
    fields = ('prefill_tps', 'decode_tps', 'prefill_seconds', 'decode_seconds',
              'request_seconds', 'ttft_seconds', 'input_tokens', 'output_tokens',
              'cached_tokens', 'uncached_tokens',
              'steps_per_second', 'steps', 'age_seconds', 'references')
    metrics = {key: number(request[key]) for key in fields if key in request
               and not isinstance(request[key], bool)}
    metrics = {key: value for key, value in metrics.items() if value is not None}
    if service in ('Image', 'Image generation', 'Image editing'):
        references = number(request.get('references'))
        if references is None or (service != 'Image' and (references > 0) != (service == 'Image editing')):
            return {}
        duration, steps = number(request.get('seconds')), number(request.get('steps'))
        if duration is not None:
            metrics['request_seconds'] = duration
        if duration and steps is not None:
            metrics['steps_per_second'] = steps / duration
    return metrics
