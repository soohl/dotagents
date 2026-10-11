"""Speed plots retain measured rates through idle periods and keep identity."""
from datetime import datetime
import json
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from inference_speeds import SpeedHistory, health_metrics, log_timestamp, local_metrics


class SpeedTests(unittest.TestCase):
    def test_local_report_survives_noise_partial_append_and_repeated_monitor_polls(self):
        stamp = '2026-10-02T22:46:48.115+00:00'
        wall = datetime.fromisoformat(stamp).timestamp()
        record = stamp + ' dotagents-speed: ' + json.dumps(dict(prefill_tps=400, decode_tps=55,
                                                               prompt='private')) + '\n'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / '.local/inference-gateway'
            directory.mkdir(parents=True)
            log = directory / 'qwen-omlx.log'
            log.write_text(record + 'native log noise\n' * 200 + stamp + ' dotagents-speed: {')
            report = local_metrics(root, 'qwen-omlx')
            self.assertEqual(report, dict(prefill_tps=400, decode_tps=55, measured_at=wall))
            self.history.observe_local(report, wall=wall+300, clock=400)
            self.history.observe_local(report, wall=wall+301, clock=401)
            self.assertEqual(len(self.history.samples['prefill']), 1)
            self.assertEqual(self.history.chart_samples('prefill', 401)[-1][1], 400)
            self.assertEqual(self.history.value('decode', 401), 55)
            self.assertEqual(local_metrics(root, '../qwen-omlx'), {})
            self.assertEqual(local_metrics(root, 'missing'), {})

    def test_omlx_metrics_keep_original_time_and_hold_rates_after_idle_or_restart(self):
        wall = datetime.fromisoformat('2026-10-02T22:46:48.115+00:00').timestamp()
        line = '2026-10-02T22:46:48.115+00:00 dotagents-speed: ' + json.dumps(dict(
            prefill_tps=130.28, decode_tps=45.7, request_seconds=8.3, output_tokens=70,
            prompt='private', response='private'))
        self.history.feed_log(line, wall=wall + 1, clock=101)
        self.assertEqual(self.history.samples['prefill'][0][:2], (100, 130.28))
        self.assertEqual(self.history.value('decode', 400), 45.7)
        self.assertEqual(self.history.chart_samples('decode', 400)[-1][:2], (400, 45.7))
        self.assertNotIn('private', str(self.history.latest))
        self.history.feed_log(line, wall=wall + 300, clock=400)
        self.assertEqual(len(self.history.samples['decode']), 1)
        restarted = SpeedHistory()
        restarted.feed_log(line, wall=wall + 300, clock=400)
        self.assertEqual(restarted.samples['decode'][0][:2], (100, 45.7))
        self.assertEqual(restarted.chart_samples('prefill', 400)[-1][:2], (400, 130.28))

    def setUp(self):
        self.wall = datetime(2026, 10, 2, 18, 40, 0).timestamp()
        self.history = SpeedHistory()

    def feed(self, seconds, text):
        self.history.feed_log(f'1002 18:40:{seconds:02d} ds4-server: chat ctx=512..1927:1415 {text}',
                              wall=self.wall + seconds, clock=100 + seconds)

    def test_prefill_uses_uncached_average_when_first_chunk_rate_is_zero(self):
        self.feed(0, 'TOOLS prompt start')
        self.feed(2, 'TOOLS prefill chunk 1415/1415 (100.0%) chunk=0.00 t/s avg=722.97 t/s 1.957s')
        self.assertEqual(self.history.rate('prefill', 102), 722.97)
        self.assertEqual(self.history.latest['prefill_seconds'], 1.957)
        self.feed(3, 'gen=50 TOOLS decoding chunk=45.04 t/s avg=44.00 t/s 1.110s')
        self.assertEqual(self.history.rate('decode', 103), 45.04)
        self.assertEqual(self.history.latest['output_tokens'], 50)
        self.feed(4, 'gen=86 TOOLS finish=tool_calls 3.831s')
        self.assertEqual(self.history.latest['request_seconds'], 3.831)
        self.assertEqual(self.history.samples['decode'][-1][1], 45.04)
        measured = list(self.history.samples['decode'])
        self.assertEqual(self.history.chart_samples('decode', 250)[-1][:2], (250, 45.04))
        self.assertEqual(self.history.value('decode', 250), 45.04)
        self.assertEqual(list(self.history.samples['decode']), measured)
        self.feed(5, 'TOOLS prompt start')
        self.assertEqual(self.history.latest, {'prefill_tps': 722.97, 'decode_tps': 45.04})

    def test_historical_tails_retain_original_time_and_last_logged_rate(self):
        line = '1002 18:40:00 ds4-server: chat prefill chunk 10/10 avg=100.00 t/s 0.100s'
        self.history.feed_log(line, wall=self.wall + 30, clock=130)
        self.assertEqual(self.history.samples['prefill'][-1][0], 100)
        self.history.feed_log(line, wall=self.wall + 300, clock=400)
        self.assertEqual(len(self.history.samples['prefill']), 1)
        self.assertIsNone(self.history.rate('prefill', 400))
        self.history.feed_log('ds4-server: chat prefill chunk 10/10 avg=100.00 t/s 0.100s')
        self.assertEqual(len(self.history.samples['prefill']), 1)
        restarted = SpeedHistory()
        restarted.feed_log(line, wall=self.wall + 300, clock=400)
        self.assertEqual(restarted.samples['prefill'][-1][0], 100)
        self.assertEqual(restarted.value('prefill', 400), 100)
        self.assertEqual(restarted.chart_samples('prefill', 400)[-1][:2], (400, 100))

    def test_log_replay_cannot_clear_more_recent_request_details(self):
        self.feed(3, 'gen=50 TOOLS decoding chunk=45.04 t/s avg=44.00 t/s 1.110s')
        self.feed(0, 'TOOLS prompt start')
        self.assertEqual(self.history.latest['output_tokens'], 50)

    def test_year_boundary_uses_nearest_year_and_invalid_dates_are_ignored(self):
        now = datetime(2027, 1, 1, 0, 0, 5).timestamp()
        self.assertEqual(log_timestamp('1231 23:59:59 ds4-server:', now), now - 6)
        self.assertIsNone(log_timestamp('0230 23:59:59 ds4-server:', now))

    def test_remote_polls_do_not_repeat_completed_requests_and_disconnect_is_a_gap(self):
        metrics = {'decode_tps': 40, 'request_seconds': 2}
        self.history.observe(metrics, 100)
        self.assertEqual(self.history.latest, metrics)
        self.assertFalse(self.history.samples['decode'])
        self.history.observe(metrics, 103)
        self.assertFalse(self.history.samples['decode'])
        self.history.observe(metrics | {'request_seconds': 3}, 106)
        self.assertEqual(self.history.rate('decode', 106), 40)
        self.history.observe({}, 109, connected=False)
        self.assertIsNone(self.history.samples['decode'][-1][1])
        self.history.observe(metrics | {'request_seconds': 3}, 112)
        self.assertEqual(len(self.history.samples['decode']), 2)

    def test_timestamped_health_metrics_keep_original_time_and_deduplicate_polls(self):
        self.history.observe({'decode_tps': 40, 'age_seconds': 5}, 100)
        self.assertEqual(self.history.samples['decode'][0][:2], (95, 40))
        self.history.observe({'decode_tps': 40, 'age_seconds': 8}, 103)
        self.assertEqual(len(self.history.samples['decode']), 1)
        self.history.observe({'decode_tps': 40, 'age_seconds': 500}, 103)
        self.assertEqual(len(self.history.samples['decode']), 1)

    def test_old_remote_chat_report_restores_held_rates_after_dashboard_restart(self):
        self.history.observe({'prefill_tps': 1500, 'decode_tps': 100, 'age_seconds': 500}, 600)
        self.assertEqual(self.history.samples['prefill'][0][:2], (100, 1500))
        self.assertEqual(self.history.chart_samples('decode', 600), [(540, 100, {}), (600, 100, {})])
        self.history.observe({'prefill_tps': 1500, 'decode_tps': 100, 'age_seconds': 501}, 601)
        self.assertEqual(len(self.history.samples['decode']), 1)
        image = SpeedHistory()
        image.observe({'steps_per_second': 2, 'request_seconds': 10, 'age_seconds': 500}, 600)
        self.assertFalse(image.samples['steps'])

    def test_same_second_retains_logged_rates_and_ignores_clock_reading_jitter(self):
        self.feed(2, 'gen=50 TOOLS decoding chunk=45.04 t/s avg=44.00 t/s 1.110s')
        line = '1002 18:40:02 ds4-server: chat gen=55 TOOLS decoding chunk=46.00 t/s avg=44.00 t/s 1.120s'
        self.history.feed_log(line, wall=self.wall + 2.2, clock=102.19999)
        self.assertEqual(self.history.rate('decode', 102.3), 46)
        self.feed(2, 'gen=55 TOOLS finish=stop 1.200s')
        self.assertEqual(self.history.samples['decode'][-1][1], 46)
        self.feed(2, 'TOOLS prompt start')
        self.feed(2, 'gen=1 TOOLS decoding chunk=50.00 t/s avg=50.00 t/s 0.020s')
        self.assertEqual([value for _, value, _ in self.history.samples['decode']], [45.04, 46, 50])

    def test_held_rates_update_at_new_measurements_and_break_on_connection_failure(self):
        self.history.observe({'prefill_tps': 700, 'decode_tps': 40, 'age_seconds': 0}, 100)
        self.history.observe({'decode_tps': 50, 'age_seconds': 0}, 200)
        self.assertEqual(self.history.value('prefill', 250), 700)
        self.assertEqual([value for _, value, _ in self.history.chart_samples('decode', 250)],
                         [40, 40, 50, 50])
        self.history.observe({}, 260, connected=False)
        self.assertIsNone(self.history.chart_samples('decode', 300)[-1][1])
        self.assertEqual(self.history.value('decode', 300), 50)

    def test_image_timing_is_assigned_to_only_the_request_service(self):
        for references, service in [(0, 'Image generation'), (2, 'Image editing')]:
            health = {'worker': {'last_request': {'seconds': 10, 'steps': 20, 'references': references}}}
            metrics = health_metrics(health, service)
            self.assertEqual(metrics['steps_per_second'], 2)
            self.assertEqual(metrics['request_seconds'], 10)
            other = 'Image generation' if references else 'Image editing'
            self.assertEqual(health_metrics(health, other), {})
            self.assertEqual(health_metrics(health, 'Image')['steps_per_second'], 2)
            self.assertEqual(health_metrics(health, 'Image')['references'], references)

    def test_health_retains_only_finite_nonnegative_numeric_metrics(self):
        health = {'worker': {'last_request': dict(
            prefill_tps=float('nan'), decode_tps=-1, request_seconds='2.5',
            output_tokens=True, prompt='private', response='private')}}
        self.assertEqual(health_metrics(health, 'Chat'), {'request_seconds': 2.5})
        for malformed in ({}, {'worker': []}, {'worker': {'last_request': 'wrong'}}):
            self.assertEqual(health_metrics(malformed, 'Chat'), {})



if __name__ == '__main__':
    unittest.main()
