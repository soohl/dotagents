"""Capture oMLX response timings without retaining chat content."""
import json
import re

from telemetry import number

MAX_EVENT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
EVENT_END = re.compile(rb'\r?\n\r?\n')


def request_usage(body):
    """Request a final usage event internally; preserve the client's preference."""
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get('stream') is not True:
        return body, False
    options = payload.get('stream_options')
    if options is None:
        options = {}
    if not isinstance(options, dict):
        return body, False
    hide = options.get('include_usage') is not True
    payload['stream_options'] = dict(options, include_usage=True)
    return json.dumps(payload).encode(), hide


def usage_metrics(usage):
    if not isinstance(usage, dict):
        return {}
    fields = {
        'prompt_tokens_per_second': 'prefill_tps',
        'generation_tokens_per_second': 'decode_tps',
        'prompt_eval_duration': 'prefill_seconds',
        'generation_duration': 'decode_seconds',
        'time_to_first_token': 'ttft_seconds',
        'total_time': 'request_seconds',
        'prompt_tokens': 'input_tokens',
        'completion_tokens': 'output_tokens',
    }
    metrics = {field: number(usage[key]) for key, field in fields.items()
               if key in usage and not isinstance(usage[key], bool)}
    metrics = {key: value for key, value in metrics.items() if value is not None}
    # Use engine durations and uncached counts if a rate field is absent.
    details = usage.get('prompt_tokens_details') or {}
    cached = details.get('cached_tokens', 0) if isinstance(details, dict) else None
    cached = None if isinstance(cached, bool) else number(cached)
    prompt, duration = metrics.get('input_tokens'), metrics.get('prefill_seconds')
    if prompt is not None and cached is not None and cached <= prompt:
        metrics.update(cached_tokens=cached, uncached_tokens=prompt-cached)
    if ('prefill_tps' not in metrics and duration and prompt is not None
            and cached is not None and cached <= prompt):
        rate = number((prompt - cached) / duration)
        if rate is not None:
            metrics['prefill_tps'] = rate
    tokens, duration = metrics.get('output_tokens'), metrics.get('decode_seconds')
    if 'decode_tps' not in metrics and duration and tokens is not None:
        rate = number(tokens / duration)
        if rate is not None:
            metrics['decode_tps'] = rate
    return metrics if 'prefill_tps' in metrics or 'decode_tps' in metrics else {}


class UsageRelay:
    """Relay bytes, with bounded buffers for JSON responses and SSE events."""

    def __init__(self, streaming, record, hide_usage=False):
        self.streaming, self.record, self.hide_usage = streaming, record, hide_usage
        self.pending = bytearray()
        self.oversized = False
        self.recorded = False

    def capture(self, payload):
        if not isinstance(payload, dict):
            return False
        usage = payload.get('usage')
        if not self.recorded:
            metrics = usage_metrics(usage)
            if metrics:
                self.record(metrics)
                self.recorded = True
        return isinstance(usage, dict) and payload.get('choices') == []

    def feed(self, chunk):
        if not self.streaming:
            if not self.oversized:
                self.pending.extend(chunk)
                if len(self.pending) > MAX_RESPONSE_BYTES:
                    self.pending.clear()
                    self.oversized = True
            return chunk
        self.pending.extend(chunk)
        output = bytearray()
        while match := EVENT_END.search(self.pending):
            event = bytes(self.pending[:match.end()])
            del self.pending[:match.end()]
            hide = False
            if not self.oversized and len(event) <= MAX_EVENT_BYTES:
                data = b'\n'.join(line[5:].lstrip(b' ') for line in event.splitlines()
                                  if line.startswith(b'data:'))
                try:
                    hide = self.capture(json.loads(data)) and self.hide_usage
                except (ValueError, UnicodeDecodeError):
                    pass
            self.oversized = False
            if not hide:
                output.extend(event)
        if len(self.pending) > MAX_EVENT_BYTES:
            # Relay a large event unchanged. Resume parsing at its boundary.
            output.extend(self.pending[:-3])
            del self.pending[:-3]
            self.oversized = True
        return bytes(output)

    def finish(self):
        if self.streaming:
            remainder = bytes(self.pending)
            self.pending.clear()
            return remainder
        if not self.oversized:
            try:
                self.capture(json.loads(self.pending))
            except (ValueError, UnicodeDecodeError):
                pass
        self.pending.clear()
        return b''
