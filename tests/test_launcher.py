"""Offline checks for DS4 model pins, launch arguments, and ownership guards."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cook_studio', ROOT / 'src/cook_studio.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class LauncherTests(unittest.TestCase):
    def test_discovery_from_another_directory(self):
        result = subprocess.run([str(ROOT / 'run.sh'), 'list'], cwd='/tmp',
                                capture_output=True, text=True, check=True)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertIn('Qwen 3.8 Flash Next', result.stdout)
        self.assertIn('DeepSeek V4 Flash', result.stdout)

    def test_only_supported_models_and_pinned_artifacts(self):
        config = runner.profiles()
        self.assertEqual(set(config['models']), {'qwen', 'deepseek'})
        self.assertEqual(config['models']['qwen']['api_model'], 'qwen3.8-flash-next')
        self.assertEqual(config['models']['deepseek']['api_model'], 'deepseek-v4-flash')
        for model in config['models'].values():
            self.assertRegex(model['revision'], r'^[0-9a-f]{40}$')
            self.assertRegex(model['weights_revision'], r'^[0-9a-f]{40}$')
            self.assertEqual(model['context'], 65536)
            for artifact in runner.required_artifacts(model):
                self.assertRegex(artifact['sha256'], r'^[0-9a-f]{64}$')
                self.assertGreater(artifact['size'], 0)
                self.assertTrue(artifact['path'].startswith('models/'))
                if 'revision' in artifact:
                    self.assertRegex(artifact['revision'], r'^[0-9a-f]{40}$')
        with self.assertRaises(ValueError):
            runner.model_profile('glm')

    def test_backend_gitlink_matches_profile(self):
        entry = subprocess.check_output(['git', 'ls-files', '-s', 'backends/ds4'],
                                        cwd=ROOT, text=True).split()
        for model in runner.profiles()['models'].values():
            self.assertEqual(model['source'], 'backends/ds4')
            self.assertEqual(entry[:2], ['160000', model['revision']])
        self.assertNotIn('omlx', (ROOT / '.gitmodules').read_text())

    def test_incomplete_or_corrupt_weights_fail(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runner, 'ROOT', Path(folder)):
            path = Path(folder) / 'weights.gguf'
            artifact = {'path':path.name, 'size':4, 'sha256':hashlib.sha256(b'good').hexdigest()}
            with self.assertRaises(ValueError):
                runner.check_artifact(artifact)
            path.write_bytes(b'bad')
            with self.assertRaises(ValueError):
                runner.check_artifact(artifact)
            path.write_bytes(b'evil')
            with self.assertRaises(ValueError):
                runner.check_artifact(artifact, checksum=True)
            path.write_bytes(b'good')
            self.assertEqual(runner.check_artifact(artifact, checksum=True), path)

    def test_qwen_native_weights_cache_and_arguments(self):
        model = copy.deepcopy(runner.profiles()['models']['qwen'])
        self.assertEqual(len(model['artifacts']), 1)
        self.assertNotIn('DS4_QWEN4_PLE_PREFETCH_FULL', model.get('environment', {}))
        with tempfile.TemporaryDirectory() as folder, patch.object(runner, 'ROOT', Path(folder)):
            for index, artifact in enumerate(runner.required_artifacts(model)):
                artifact.update(path=f'{index}.gguf', size=1)
                (Path(folder) / artifact['path']).write_bytes(b'x')
            args = runner.server_args('qwen', model, {})
            self.assertEqual(args[args.index('--vision')+1], str(Path(folder) / '1.gguf'))
            self.assertNotIn('--ple', args)
            self.assertEqual(args[args.index('--port')+1], '8000')
            self.assertEqual(args[args.index('--host')+1], '127.0.0.1')
            self.assertEqual(args[args.index('--prefill-chunk')+1], '8192')
            self.assertNotIn('--dspark', args)
            self.assertNotIn('--batched-session', args)
            self.assertEqual(args[args.index('--kv-cache-cold-max-tokens')+1], '65536')
            self.assertEqual(args[args.index('--kv-cache-boundary-align-tokens')+1], '256')
            self.assertIn('--kv-cache-reject-different-quant', args)
            cache = Path(args[args.index('--kv-disk-dir')+1])
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            args = runner.server_args('qwen', model, {'LLM_DISABLE_PROMPT_CACHE':'1'})
            self.assertNotIn('--kv-disk-dir', args)
            (Path(folder) / model['artifacts'][0]['path']).unlink()
            with self.assertRaises(ValueError):
                runner.server_args('qwen', model, {})
            (Path(folder) / model['artifacts'][0]['path']).write_bytes(b'x')
            (Path(folder) / model['vision_encoder']['path']).unlink()
            with self.assertRaises(ValueError):
                runner.server_args('qwen', model, {})

    def test_context_and_port_bounds(self):
        for value in ['0', '1', '65537', '-1', 'bad']:
            with self.assertRaises(ValueError):
                runner.integer_setting({'LLM_CTX':value}, 'LLM_CTX', 65536, 2, 65536)
        self.assertEqual(runner.integer_setting({}, 'LLM_CTX', 65536, 2, 65536), 65536)

    def test_existing_listener_is_not_stopped(self):
        with patch.object(socket.socket, 'connect_ex', return_value=0), \
             patch.object(runner.subprocess, 'check_output') as processes:
            with self.assertRaisesRegex(ValueError, 'already in use'):
                runner.ensure_idle([8000])
            processes.assert_not_called()

    def test_manual_ds4_process_on_another_port_blocks_loading(self):
        with patch.object(socket.socket, 'connect_ex', return_value=1), \
             patch.object(runner.subprocess, 'check_output', return_value='/some/path/ds4-server\n'):
            with self.assertRaisesRegex(ValueError, 'already running'):
                runner.ensure_idle([8000, 8001])

    def test_revision_drift_is_rejected(self):
        with patch.object(runner.subprocess, 'check_output', return_value='wrong-revision\n'):
            with self.assertRaisesRegex(ValueError, 'revision differs'):
                runner.check_revision(Path('/unused'), 'expected')


if __name__ == '__main__':
    unittest.main()
