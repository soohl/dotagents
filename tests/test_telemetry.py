"""Hardware counters preserve units, missing data, and shared memory semantics."""
import plistlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import telemetry
import control_plane


class TelemetryTests(unittest.TestCase):
    def test_device_usage_keeps_shared_memory_and_missing_readings_distinct(self):
        reading = {'model': 'Mac Studio', 'chip': 'Apple M3 Ultra',
                   'memory': {'used': 50, 'total': 100, 'shared': True},
                   'gpus': [{'name': 'Apple GPU', 'used': 20, 'total': 100}]}
        self.assertEqual(telemetry.inference_summary(reading, .25),
                         ('Mac Studio M3 Ultra', '25%', '50%', '20%'))
        self.assertEqual(telemetry.inference_summary(reading, .25, False),
                         ('Mac Studio M3 Ultra', '—', '—', '—'))
        reading['memory']['shared'] = False
        reading['gpus'] = [{'name': 'NVIDIA GeForce Example GPU', 'used': 18, 'total': 24}]
        self.assertEqual(telemetry.inference_summary(reading, None),
                         ('NVIDIA Example GPU', '—', '50%', '75%'))
        reading['gpus'][0]['used'] = None
        self.assertEqual(telemetry.inference_summary(reading, 0)[-1], '—')
        self.assertEqual(telemetry.inference_summary(None, None), ('—',) * 4)

    def test_cpu_utilization_uses_counter_deltas_and_rejects_resets(self):
        first = {'busy': 100, 'total': 200}
        second = {'busy': 125, 'total': 300}
        self.assertEqual(telemetry.cpu_usage(first, second), .25)
        self.assertIsNone(telemetry.cpu_usage(None, first))
        self.assertIsNone(telemetry.cpu_usage(second, first))
        self.assertIsNone(telemetry.cpu_usage(first, first))
        self.assertIsNone(telemetry.cpu_usage(first, None))

    def test_linux_cpu_excludes_iowait_and_does_not_double_count_guest(self):
        ticks = telemetry.linux_cpu_ticks('cpu 100 10 20 200 30 5 5 10 50 5\ncpu0 1 2 3 4\n')
        self.assertEqual(ticks, {'busy': 150, 'total': 380})
        self.assertIsNone(telemetry.linux_cpu_ticks('cpu broken'))

    def test_mac_resident_memory_excludes_file_cache_and_compressor_original_size(self):
        sample = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Anonymous pages: 100.
Pages wired down: 200.
Pages occupied by compressor: 20.
Pages stored in compressor: 900.
File-backed pages: 9999.
'''
        self.assertEqual(telemetry.mac_memory(sample), 320 * 16384)
        self.assertIsNone(telemetry.mac_memory('Pages free: 0.'))

    def test_gpu_shared_memory_is_not_added_to_ram(self):
        sample = plistlib.dumps([{'model': 'Apple GPU', 'PerformanceStatistics': {
            'In use system memory': 4 * 2**30, 'Device Utilization %': 30}}]).decode()
        gpu = telemetry.mac_gpus(sample, 8 * 2**30)[0]
        self.assertTrue(gpu['shared'])
        self.assertEqual(gpu['used'], 4 * 2**30)
        self.assertEqual(gpu['total'], 8 * 2**30)
        self.assertEqual(telemetry.mac_gpus('not a plist', 1), [])
        missing = plistlib.dumps([{'model': 'Apple GPU'}]).decode()
        self.assertIsNone(telemetry.mac_gpus(missing, 1)[0]['used'])

    def test_nvidia_memory_uses_mib_and_keeps_unknown_values(self):
        gpus = telemetry.nvidia_gpus('GPU-one, NVIDIA Example GPU, 8192, 24576, 50\n'
                                    'GPU-two, NVIDIA Example GPU, N/A, 24576, [Not Supported]\n')
        self.assertEqual(gpus[0]['used'], 8 * 2**30)
        self.assertEqual(gpus[0]['total'], 24 * 2**30)
        self.assertFalse(gpus[0]['shared'])
        self.assertIsNone(gpus[1]['used'])
        self.assertIsNone(gpus[1]['utilization'])
        for value in ('nan', '-1', 'inf', None):
            self.assertIsNone(telemetry.number(value))
