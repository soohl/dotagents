import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'benchmarks'))
import tuning


class TuningIsolationTests(unittest.TestCase):
    def test_wrapper_applies_overrides_only_to_temporary_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root/'model_settings.json'
            settings.write_text(json.dumps({'models':{'test':{'max_context_window':65536,'mtp_enabled':False}}}))
            fake = root/'python'
            fake.write_text('#!/bin/sh\nexit 0\n')
            fake.chmod(0o755)
            subprocess.run([sys.executable,str(ROOT/'tools/omlx_experiment.py'),'serve'],check=True,
                           env=dict(os.environ,COOK_OMLX_PYTHON=str(fake),OMLX_BASE_PATH=str(root),
                                    COOK_OMLX_SETTINGS='{"prefill_step_size":512}'),capture_output=True)
            self.assertEqual(json.loads(settings.read_text())['models']['test'],
                             {'max_context_window':65536,'mtp_enabled':False,'prefill_step_size':512})
            self.assertFalse(settings.with_suffix('.experiment.tmp').exists())

    def test_version_probe_does_not_create_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory)/'python'
            fake.write_text('#!/bin/sh\nexit 0\n')
            fake.chmod(0o755)
            subprocess.run([sys.executable,str(ROOT/'tools/omlx_experiment.py'),'--version'],check=True,
                           env=dict(os.environ,COOK_OMLX_PYTHON=str(fake),OMLX_BASE_PATH=directory),capture_output=True)
            self.assertFalse((Path(directory)/'model_settings.json').exists())

    def test_plan_has_unique_names_and_pins(self):
        plan=json.loads((ROOT/'benchmarks/tuning-plan.json').read_text())
        names=[c['name'] for c in plan['candidates']]
        self.assertEqual(len(names),len(set(names)))
        for candidate in plan['candidates']:
            runtime=plan['runtimes'][candidate['runtime']]
            self.assertRegex(runtime['revision'],r'^[0-9a-f]{40}$')
            self.assertIn(candidate['backend'],('omlx','ds4'))

    def test_special_token_collapse_is_not_normal_output(self):
        self.assertTrue(tuning.degenerate_output('<｜begin▁of▁sentence｜>'*8))
        self.assertFalse(tuning.degenerate_output('The user wants the three registry codes.'))

    def test_short_retrieval_pass_does_not_clear_degenerate_long_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'quality.jsonl').write_text(json.dumps({'candidate':'test','passed':True})+'\n')
            row={'candidate':'test','source_chars':131072,'ttft_seconds':80,
                 'effective_input_tokens_per_ttft_second':500,'output':'<｜begin▁of▁sentence｜>'*8}
            tuning.report(root,[row])
            self.assertIn('DISQUALIFIED',(root/'REPORT.md').read_text())

    def test_failed_quality_disqualifies_normal_timing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'quality.jsonl').write_text(json.dumps({'candidate':'test','passed':False})+'\n')
            row={'candidate':'test','source_chars':131072,'ttft_seconds':80,
                 'effective_input_tokens_per_ttft_second':500,'output':'A plausible answer.'}
            tuning.report(root,[row])
            self.assertIn('DISQUALIFIED',(root/'REPORT.md').read_text())
