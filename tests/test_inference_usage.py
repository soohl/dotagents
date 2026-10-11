"""Check response timing capture, stream boundaries, and chat byte preservation."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from inference_usage import UsageRelay, request_usage, usage_metrics

USAGE = dict(prompt_tokens=100, completion_tokens=50,
             prompt_tokens_details={'cached_tokens': 20},
             prompt_eval_duration=.4, generation_duration=2,
             prompt_tokens_per_second=200, generation_tokens_per_second=25,
             total_time=2.4, time_to_first_token=.4)


class UsageTests(unittest.TestCase):
    def test_invalid_stream_options_are_forwarded_unchanged_for_upstream_validation(self):
        for options in ([], False, 'invalid'):
            with self.subTest(options=options):
                original = json.dumps(dict(stream=True, stream_options=options)).encode()
                self.assertEqual(request_usage(original), (original, False))

    def test_stream_metrics_preserve_client_usage_preference_and_response_bytes(self):
        token = b'data: {"choices":[{"delta":{"content":"private reply"}}]}\r\n\r\n'
        usage = ('data: ' + json.dumps({'choices': [], 'usage': USAGE}) + '\n\n').encode()
        done = b'data: [DONE]\n\n'
        original = token + usage + done
        for include in (None, False, True):
            with self.subTest(include=include):
                payload = dict(model='qwen-omlx', messages=[], stream=True)
                if include is not None:
                    payload['stream_options'] = {'include_usage': include, 'other': 'preserved'}
                upstream, hide = request_usage(json.dumps(payload).encode())
                self.assertTrue(json.loads(upstream)['stream_options']['include_usage'])
                if include is not None:
                    self.assertEqual(json.loads(upstream)['stream_options']['other'], 'preserved')
                reports = []
                relay = UsageRelay(True, reports.append, hide)
                # Fragment every byte, including CRLF boundaries and usage JSON.
                response = b''.join(relay.feed(bytes([byte])) for byte in original) + relay.finish()
                self.assertEqual(response, original if include is True else token + done)
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]['prefill_tps'], 200)
                self.assertEqual(reports[0]['decode_tps'], 25)
                self.assertNotIn('private reply', str(reports))

    def test_regular_json_response_is_unchanged_and_records_only_numeric_usage(self):
        original = json.dumps({'choices': [{'message': {'content': 'private'}}],
                               'usage': USAGE | {'secret': 'private'}}).encode()
        reports = []
        relay = UsageRelay(False, reports.append)
        response = relay.feed(original[:31]) + relay.feed(original[31:]) + relay.finish()
        self.assertEqual(response, original)
        self.assertEqual(reports[0]['output_tokens'], 50)
        self.assertNotIn('private', str(reports))
        self.assertFalse(relay.pending)

    def test_rate_fallback_counts_only_uncached_tokens_and_requires_real_duration(self):
        metrics = usage_metrics(dict(prompt_tokens=100, completion_tokens=50,
                                    prompt_tokens_details={'cached_tokens': 80},
                                    prompt_eval_duration=.5, generation_duration=2))
        self.assertEqual(metrics['prefill_tps'], 40)
        self.assertEqual(metrics['decode_tps'], 25)
        self.assertEqual(metrics['cached_tokens'], 80)
        self.assertEqual(metrics['uncached_tokens'], 20)
        self.assertEqual(usage_metrics(dict(prompt_tokens=100, completion_tokens=50,
                                          total_time=3, time_to_first_token=1)), {})
        self.assertEqual(usage_metrics(dict(prompt_tokens_per_second=float('nan'),
                                          generation_tokens_per_second=-1)), {})
        self.assertEqual(usage_metrics(dict(generation_tokens_per_second=True)), {})
        cached = USAGE | {'prompt_tokens_per_second': 0, 'prompt_tokens_details': {'cached_tokens': 100}}
        self.assertEqual(usage_metrics(cached)['prefill_tps'], 0)

    def test_large_events_and_malformed_stream_data_do_not_damage_the_reply(self):
        reports = []
        relay = UsageRelay(True, reports.append, hide_usage=True)
        prefix = b'data: ' + b'x' * 100 + b'\n'
        usage = ('data: ' + json.dumps({'choices': [], 'usage': USAGE}) + '\n\n').encode()
        with patch('inference_usage.MAX_EVENT_BYTES', 40):
            response = relay.feed(prefix)
        response += relay.feed(b'\n' + usage + b'data: [DONE]\n\n') + relay.finish()
        self.assertEqual(response, prefix + b'\ndata: [DONE]\n\n')
        self.assertEqual(len(reports), 1)
        for streaming, original in [(False, b'{broken'), (True, b'data: {broken\n\n')]:
            reports = []
            relay = UsageRelay(streaming, reports.append)
            self.assertEqual(relay.feed(original) + relay.finish(), original)
            self.assertFalse(reports)

    def test_incomplete_stream_never_invents_a_measurement(self):
        reports = []
        relay = UsageRelay(True, reports.append)
        original = b'data: {"choices":[],"usage":'
        self.assertEqual(relay.feed(original) + relay.finish(), original)
        self.assertFalse(reports)


if __name__ == '__main__':
    unittest.main()
