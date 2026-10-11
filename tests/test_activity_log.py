"""Keep mixed service output readable without trusting terminal markup."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from activity_log import normalize


class ActivityTests(unittest.TestCase):
    def test_docker_and_native_timestamps_do_not_repeat(self):
        record = normalize('inference/qwen-omlx',
            '2026-10-06T07:00:00.123456789Z 07:00:00.123 INFO: request complete')
        self.assertEqual(record.time.astimezone(timezone.utc).hour, 7)
        self.assertEqual(record.message, 'request complete')
        self.assertEqual(record.level, 'INFO')

    def test_json_worker_identity_and_errors_survive_normalization(self):
        line = json.dumps({'source': 'GPU/task-2', 'level': 'error', 'msg': 'output limit [bold]\u001b[31m'})
        record = normalize('agent', line)
        self.assertEqual(record.source, 'agent')
        self.assertEqual(record.level, 'ERR')
        self.assertEqual(record.message, 'output limit [bold]')
        self.assertEqual(normalize('chat', line).source, 'chat')

    def test_wrapped_messages_keep_a_shared_column(self):
        received = datetime(2026, 10, 6, 12, 34, 56).astimezone()
        for width in (36, 54, 90):
            console = Console(width=width, color_system=None)
            with console.capture() as output:
                console.print(normalize('agent', 'one\ntwo', received).render(width))
                console.print(normalize('inference/deepseek', 'three', received).render(width))
            lines = output.get().splitlines()
            positions = [next(line.index(word) for line in lines if word in line)
                         for word in ('one', 'two', 'three')]
            self.assertEqual(len(set(positions)), 1, lines)
            self.assertTrue(all(len(line) <= width for line in lines), lines)

    def test_loguru_prefix_and_json_details_remain_readable(self):
        record = normalize('chat', '2026-10-06 07:00:00.123 | WARNING  | request failed')
        self.assertEqual((record.level, record.message), ('WARN', 'request failed'))
        record = normalize('caddy', '{"level":"info","msg":"request","status":200}')
        self.assertIn('"status": 200', record.message)
