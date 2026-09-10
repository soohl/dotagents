"""Offline contracts for the flattened launcher and adapted provider profiles."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    import sys
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


class LauncherTests(unittest.TestCase):
    def test_python_syntax(self):
        for base in ('benchmarks', 'compatibility', 'tests', 'tools'):
            for path in (ROOT / base).rglob('*.py'):
                ast.parse(path.read_text(), filename=str(path))

    def test_flat_discovery_from_other_directory(self):
        result = subprocess.run([str(ROOT / 'run.sh'), 'list'], cwd='/tmp',
                                text=True, capture_output=True, check=True)
        for model in ('qwen3.8-flash-next', 'glm-5.3-flash', 'deepseek-v4-flash-0731', 'deepseek-v4-flash-vision-exp'):
            self.assertIn(model, result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1 + len(list((ROOT / 'models').glob('*/model.conf'))))
        self.assertNotIn('models/moe/', result.stdout)
        self.assertNotIn('nemotron', result.stdout)

    def test_profiles(self):
        resolver = module('compatibility_resolver', ROOT / 'compatibility/resolve.py')
        for path in (ROOT / 'compatibility').glob('omlx-*.json'):
            profile = resolver.load_profile(path.stem)
            resolver.validate(profile, 'omlx')
        qwen = resolver.load_profile('omlx-qwen')
        self.assertEqual(qwen['pi']['thinkingLevelMap']['xhigh'], 'xhigh')
        self.assertEqual(qwen['benchmark']['reasoning']['piThinking'], 'xhigh')
        self.assertEqual(qwen['pi']['compat']['chatTemplateKwargs']['reasoning_effort'],
                         {'$var': 'thinking.effort'})
        with self.assertRaises(ValueError):
            resolver.validate(qwen, 'ds4')

    def test_serve_routes_settings_without_real_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / 'fake-omlx'
            backend.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('0.6.4')
else:
    print(json.dumps({'args': sys.argv[1:], 'settings': json.loads(
        (pathlib.Path(os.environ['OMLX_BASE_PATH']) / 'model_settings.json').read_text())}))
''')
            backend.chmod(0o755)
            model_root = root / 'mlx'
            (model_root / 'qwen').mkdir(parents=True)
            environment = dict(os.environ, ROOT=str(ROOT), MODEL_ID='qwen-test',
                API_MODEL_ID='qwen-api', MODEL_ROOT=str(model_root),
                RUNTIME_ROOT=str(root / 'runtime'), BACKEND='omlx',
                DOWNLOAD_TEST_DIR='qwen', OMLX_COMMAND=str(backend),
                OMLX_SPECULATIVE_TYPE='mtp', OMLX_SPECULATIVE_MAX_TOKENS='2',
                MODEL_REASONING='1', LLM_CTX='8192', LLM_PORT='18765')
            result = subprocess.run([str(ROOT / 'backends/scripts/omlx.sh'),
                                     'serve', 'test', 'on'], env=environment,
                                    text=True, capture_output=True, check=True)
            data = json.loads(result.stdout)
            self.assertIn(str(model_root), data['args'])
            self.assertIn('18765', data['args'])
            settings = data['settings']['models']['qwen']
            self.assertEqual(settings['model_alias'], 'qwen-api')
            self.assertEqual(settings['max_context_window'], 8192)
            self.assertTrue(settings['mtp_enabled'])
            self.assertEqual(settings['mtp_num_draft_tokens'], 2)
            self.assertEqual(settings['max_tokens'], 4096)
            environment['LLM_CTX'] = '65537'
            rejected = subprocess.run([str(ROOT / 'backends/scripts/omlx.sh'),
                                       'serve', 'test'], env=environment,
                                      text=True, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('65536', rejected.stderr)

    def test_ds4_graph_chunks_and_cache_disabled_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / 'backend'
            commands = root / 'bin'
            backend.mkdir()
            commands.mkdir()
            (backend / '.git').touch()
            (root / 'weights.gguf').touch()
            git = commands / 'git'
            git.write_text('#!/bin/sh\nif [ "$3" = rev-parse ]; then echo fixture-pin; fi\n')
            git.chmod(0o755)
            for name in ('ds4', 'ds4-server', 'ds4-bench'):
                executable = backend / name
                executable.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
                executable.chmod(0o755)
            env = dict(os.environ, ROOT=str(ROOT), BACKEND='ds4',
                       BACKEND_ROOT=str(backend), DS4_REVISION='fixture-pin',
                       DS4_SOURCE_SUBDIR='', LLM_DS4_MAIN_CANDIDATE='0',
                       MODEL_ROOT=str(root), RUNTIME_ROOT=str(root / 'runtime'),
                       DOWNLOAD_TEST_FILE='weights.gguf', DS4_PREFILL_CHUNK='auto',
                       MODEL_ID='glm-test', LLM_DISABLE_PROMPT_CACHE='1',
                       PATH=str(commands) + os.pathsep + os.environ['PATH'])
            env.pop('LLM_PREFILL_CHUNK', None)
            env.pop('DS4_PLE_FILE', None)
            def launch():
                result = subprocess.run([str(ROOT / 'backends/scripts/ds4.sh'), 'serve', 'test'],
                                        env=env, text=True, capture_output=True, check=True)
                return json.loads(result.stdout)
            args = launch()
            self.assertNotIn('--prefill-chunk', args)
            self.assertNotIn('--kv-disk-dir', args)
            env.update(DS4_PREFILL_CHUNK='8192', LLM_DISABLE_PROMPT_CACHE='0')
            args = launch()
            self.assertEqual(args[args.index('--prefill-chunk') + 1], '8192')
            self.assertIn('--kv-disk-dir', args)
            self.assertEqual(args[args.index('--ctx') + 1], '65536')
            result = subprocess.run([str(ROOT / 'backends/scripts/ds4.sh'),
                                     'benchmark', 'test'], env=env,
                                    text=True, capture_output=True, check=True)
            args = json.loads(result.stdout)
            self.assertEqual(args[args.index('--ctx-alloc') + 1], '65536')
            self.assertEqual(args[args.index('--ctx-max') + 1], '57344')

    def test_context_cap_and_prompt_budget(self):
        def check(body, *args):
            return subprocess.run(['bash', '-c', 'source "$1"; shift; ' + body,
                                   'guard', str(ROOT / 'backends/scripts/common.sh'), *args],
                                  text=True, capture_output=True)
        for value in ('2', '32768', '65536'):
            self.assertEqual(check('require_context_limit "$1"', value).returncode, 0)
        for value in ('0', '-1', '1', '65537', '131072', '1.5', 'bad', '9' * 40):
            self.assertNotEqual(check('require_context_limit "$1"', value).returncode, 0)
        self.assertEqual(check('require_prompt_budget "$@"', '65536', '57344', '128').returncode, 0)
        self.assertNotEqual(check('require_prompt_budget "$@"', '65536', '65536', '8').returncode, 0)
        sweep = module('speed_context_test', ROOT / 'benchmarks/scripts/omlx.py')
        self.assertEqual(sweep.context_sweep(16384, 57344, 2), [16384, 32768, 57344])

    def test_every_model_declares_64k_capacity(self):
        prefill = module('profile_context_test', ROOT / 'benchmarks/prefill.py')
        for path in (ROOT / 'models').glob('*/model.conf'):
            conf = prefill.config(path)
            self.assertEqual(conf[f"{conf['BACKEND'].upper()}_CONTEXT"], '65536', str(path))

    def test_compaction_and_reply_budgets(self):
        with patch.dict(os.environ, {'PI_BENCH_SUITE': 'agentic'}):
            runner = module('context_policy_test', ROOT / 'benchmarks/task_runner.py')
        def args(*extra):
            return runner.build_parser().parse_args(['run', 'local/model', *extra])
        default = args()
        runner.validate_run_args(default)
        self.assertEqual(default.context_window - default.reserve_tokens, 58982)
        self.assertEqual(default.max_output, 4096)
        self.assertEqual(default.keep_recent_tokens, 4096)
        default.thinking = 'high'  # Normally resolved from the model compatibility profile.
        self.assertEqual(runner.shared_settings(default)['compaction']['reserveTokens'], 6554)
        smaller = args('--context-window', '32768')
        runner.validate_run_args(smaller)
        self.assertEqual(smaller.reserve_tokens, 3277)
        self.assertGreaterEqual(smaller.reserve_tokens - smaller.max_output, 1024)
        for extra in [('--context-window', '131072'), ('--max-output', '16384'),
                      ('--reserve-tokens', '65536'), ('--keep-recent-tokens', '6554')]:
            with self.assertRaises(SystemExit):
                runner.validate_run_args(args(*extra))

    def test_reports_are_separate_from_imported_history(self):
        with patch.dict(os.environ, {'PI_BENCH_SUITE': 'agentic'}):
            runner = module('task_runner_test', ROOT / 'benchmarks/task_runner.py')
        self.assertEqual(runner.RESULTS_PATH, ROOT / 'results/agentic/results.jsonl')
        self.assertEqual(runner.BASELINES, {})
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner, 'RESULTS_ROOT', Path(directory)), \
                 patch.object(runner, 'RESULTS_PATH', Path(directory) / 'results.jsonl'):
                runner.write_report()
                self.assertTrue((Path(directory) / 'REPORT.md').is_file())
        self.assertTrue((ROOT / 'docs/history/benchmarks/agentic/results.jsonl').is_file())

class PrefillTests(unittest.TestCase):
    def test_backend_pins_match_gitlinks_and_configs(self):
        candidates = json.loads((ROOT / 'backends/upstream-candidates.json').read_text())
        for backend in ('omlx', 'ds4'):
            head = subprocess.check_output(['git', '-C', str(ROOT / 'backends' / backend),
                                            'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(head, candidates[backend]['baseline'])
            config = (ROOT / 'backends/config' / f'{backend}.conf').read_text()
            self.assertIn(f'{backend.upper()}_REVISION={head}', config)
            staged = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--stage',
                                               f'backends/{backend}'], text=True)
            self.assertIn(f'160000 {head}', staged)

    def test_pin_guard_accepts_submodule_git_file_and_rejects_drift(self):
        revision = subprocess.check_output(['git', '-C', str(ROOT / 'backends/ds4'),
                                            'rev-parse', 'HEAD'], text=True).strip()
        env = dict(os.environ, BACKEND_ROOT=str(ROOT / 'backends/ds4'), BACKEND='ds4',
                   LLM_ALLOW_BACKEND_DRIFT='0')
        command = 'source "$1"; require_backend_revision "$2"'
        def check(pin):
            return subprocess.run(['bash', '-c', command, 'guard',
                str(ROOT / 'backends/scripts/common.sh'), pin], env=env,
                text=True, capture_output=True)
        self.assertEqual(check(revision).returncode, 0)
        result = check('0' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from baseline', result.stderr)

    def test_preflight_uses_snapshot_storage_and_rejects_partial_shards(self):
        prefill = module('prefill_storage_test', ROOT / 'benchmarks/prefill.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'models' / 'test-model'
            (profile / 'gguf' / 'weights').mkdir(parents=True)
            (profile / 'model.conf').write_text('DEFAULT_VARIANT=q4\nMODEL_STORAGE_DIR=gguf\nDOWNLOAD_Q4_DIR=weights\nDOWNLOAD_Q4_MANIFEST=manifest\n')
            (profile / 'manifest').write_text('abc 4 shard.gguf\n')
            weight = profile / 'gguf' / 'weights' / 'shard.gguf'
            weight.write_bytes(b'123')
            with patch.object(prefill, 'ROOT', root), patch.object(prefill, 'MATRIX', {'test': {'ds4': 'test-model'}}):
                with self.assertRaises(SystemExit):
                    prefill.preflight(['ds4'], 'test')
                weight.write_bytes(b'1234')
                self.assertIn('ds4', prefill.preflight(['ds4'], 'test'))

    def test_primary_campaign_uses_only_supported_backends(self):
        prefill = module('prefill_defaults_test', ROOT / 'benchmarks/prefill.py')
        self.assertEqual(set(prefill.default_backends('qwen')), {'omlx', 'ds4'})
        self.assertEqual(set(prefill.default_backends('deepseek-0731')), {'ds4', 'omlx'})
        campaign = module('campaign_defaults_test', ROOT / 'tools/prefill_campaign.py')
        paths = [str(path) for path, _, _ in campaign.artifacts()]
        self.assertTrue(paths)
        self.assertEqual({backend for pair in prefill.MATRIX.values() for backend in pair}, {'omlx', 'ds4'})
        self.assertTrue(any('GLM-5.3-Flash-DS4' in path for path in paths))
        self.assertTrue(any('DeepSeek-V4-Flash-0731-MLX' in path for path in paths))

    def test_qwen_experiment_pin_and_required_sidecar(self):
        prefill = module('qwen_pin_test', ROOT / 'benchmarks/prefill.py')
        candidate = json.loads((ROOT / 'backends/upstream-candidates.json').read_text())['ds4']['qwen_experiment']
        profile = ROOT / 'models' / candidate['model_profile']
        conf = prefill.config(profile / 'model.conf')
        self.assertEqual(conf['DS4_REVISION'], candidate['revision'])
        self.assertEqual(conf['DS4_SOURCE_SUBDIR'], candidate['source_subdir'])
        manifest = (profile / conf['DOWNLOAD_Q4_MANIFEST']).read_text()
        self.assertIn(conf['DS4_PLE_FILE'], manifest)
        self.assertIn(conf['DS4_MODEL_FILE'], manifest)
        source = ROOT / conf['DS4_SOURCE_SUBDIR']
        if (source / '.git').exists():
            head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(head, conf['DS4_REVISION'])

    def test_main_candidate_source_recording_preserves_model_branches(self):
        prefill = module('candidate_source_test', ROOT / 'benchmarks/prefill.py')
        with patch.dict(os.environ, {'LLM_DS4_MAIN_CANDIDATE': '1'}):
            self.assertEqual(prefill.source_directory('ds4', {}), ROOT / '.local/backends/ds4-main-6289c516')
            self.assertEqual(prefill.source_directory('omlx', {}), ROOT / 'backends/omlx')
            with self.assertRaises(ValueError):
                prefill.source_directory('ds4', {'DS4_SOURCE_SUBDIR': '.local/backends/ds4-qwen-pr991'})
        with patch.dict(os.environ, {'LLM_DS4_MAIN_CANDIDATE': '0'}):
            self.assertEqual(prefill.source_directory('ds4', {}), ROOT / 'backends/ds4')

    def test_legacy_discovery_requires_expected_model_name(self):
        import io
        from types import SimpleNamespace
        prefill = module('discovery_test', ROOT / 'benchmarks/prefill.py')
        process = SimpleNamespace(poll=lambda: None)
        for name, accepted in [('GLM 5.3 Flash', True), ('GLM 5.2', False)]:
            stream = io.BytesIO(json.dumps({'data': [{'id': 'glm-5.2', 'name': name}]}).encode())
            with patch.object(prefill.urllib.request, 'urlopen', return_value=stream), \
                 patch.object(prefill.time, 'monotonic', side_effect=[0, 1, 901]), \
                 patch.object(prefill.time, 'sleep'):
                if accepted:
                    prefill.wait_ready(process, 'http://localhost', 'glm-5.3-flash', 'glm-5.2', 'GLM 5.3 Flash')
                else:
                    with self.assertRaises(TimeoutError):
                        prefill.wait_ready(process, 'http://localhost', 'glm-5.3-flash', 'glm-5.2', 'GLM 5.3 Flash')

    def test_prompt_identity_and_fresh_trial_prefixes(self):
        prefill = module('prefill_test', ROOT / 'benchmarks/prefill.py')
        first = prefill.prompt('example source' * 100, 64, 0, 'run')
        self.assertEqual(first, prefill.prompt('example source' * 100, 64, 0, 'run'))
        self.assertNotEqual(first[:24], prefill.prompt('example source' * 100, 64, 1, 'run')[:24])

    def test_stream_measurement_keeps_native_and_client_metrics_distinct(self):
        import io
        prefill = module('prefill_stream_test', ROOT / 'benchmarks/prefill.py')
        events = [{'choices': [{'delta': {'role': 'assistant'}}]},
                  {'choices': [{'delta': {'content': 'hello'}}]},
                  {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 1,
                                           'prompt_tokens_details': {'cached_tokens': 5}}}]
        stream = io.BytesIO(('\n'.join('data: ' + json.dumps(e) for e in events) + '\ndata: [DONE]\n').encode())
        with patch.object(prefill.urllib.request, 'urlopen', return_value=stream) as opener, \
             patch.object(prefill.time, 'monotonic', side_effect=[10, 12, 13]):
            result = prefill.completion('http://localhost', 'omlx', 'prompt', 8)
        sent = json.loads(opener.call_args.args[0].data)
        self.assertEqual(sent['reasoning_effort'], 'high')
        self.assertEqual(sent['chat_template_kwargs'], {'reasoning_effort': 'high'})
        self.assertEqual(result['ttft_seconds'], 2)
        self.assertEqual(result['effective_input_tokens_per_ttft_second'], 47.5)
        self.assertEqual(result['completion_tokens'], 1)
        self.assertEqual(result['output'], 'hello')


if __name__ == '__main__':
    unittest.main()
