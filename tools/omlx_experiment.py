#!/usr/bin/env python3
"""Launch an isolated oMLX source/runtime with recorded per-model overrides."""
import json
import os
from pathlib import Path
import sys


def main():
    python = os.environ['COOK_OMLX_PYTHON']
    source = os.environ.get('COOK_OMLX_SOURCE')
    env = dict(os.environ)
    if source:
        env['PYTHONPATH'] = source
    else:
        env.pop('PYTHONPATH', None)
    if 'serve' in sys.argv[1:]:
        path = Path(env['OMLX_BASE_PATH']) / 'model_settings.json'
        data = json.loads(path.read_text())
        overrides = json.loads(env.get('COOK_OMLX_SETTINGS', '{}'))
        for settings in data['models'].values():
            settings.update(overrides)
            if not 2 <= int(settings['max_context_window']) <= 65536:
                raise SystemExit('Experimental context must be 2..65536 tokens')
        temporary = path.with_suffix('.experiment.tmp')
        temporary.write_text(json.dumps(data, indent=2) + '\n')
        temporary.replace(path)
        print('COOK_EXPERIMENT ' + json.dumps({'source': source, 'python': python,
              'model_settings': data}), flush=True)
    bootstrap = '''
import importlib.metadata, json, os, runpy, sys
if os.environ.get("COOK_OMLX_PREFILL_STEP") and "serve" in sys.argv:
    from omlx.scheduler import SchedulerConfig
    original_init = SchedulerConfig.__init__
    def configured_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.prefill_step_size = int(os.environ["COOK_OMLX_PREFILL_STEP"])
    SchedulerConfig.__init__ = configured_init
    print("COOK_EXPERIMENT: process-local scheduler prefill step=" + os.environ["COOK_OMLX_PREFILL_STEP"],flush=True)
if os.environ.get("COOK_OMLX_PROFILE_ANE") == "1" and "serve" in sys.argv:
    from omlx.custom_kernels.qwen35_prefill import fast
    from omlx.server import app
    class AneProfileMiddleware:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            measure = scope.get("path") == "/v1/chat/completions"
            if measure:
                if not fast.qwen35_ane_profile_set_enabled(True):
                    raise RuntimeError("Requested ANE profiler unavailable")
                fast.qwen35_ane_profile_reset()
            try:
                await self.app(scope, receive, send)
            finally:
                if measure:
                    print("COOK_ANE_PROFILE " + json.dumps(fast.qwen35_ane_profile_snapshot()),flush=True)
    app.add_middleware(AneProfileMiddleware)
if os.environ.get("COOK_OMLX_GDN_ONLY") == "1" and "serve" in sys.argv:
    from omlx.patches import qwen35_ane_prefill as ane
    ane._eligible_pair = lambda module: False
    print("COOK_EXPERIMENT: shared-expert ANE eligibility disabled; GDN retained", flush=True)
if "serve" in sys.argv:
    import mlx.core, omlx
    def version(name):
        try: return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: return None
    print("COOK_RUNTIME " + json.dumps({"python":sys.executable,"mlx_module":mlx.core.__file__,
        "omlx_module":omlx.__file__,"versions":{name:version(name)
        for name in ["mlx","mlx-lm","mlx-vlm","transformers","nanobind"]}}),flush=True)
runpy.run_module("omlx.cli",run_name="__main__")
'''
    os.execve(python, [python, '-c', bootstrap, *sys.argv[1:]], env)


if __name__ == '__main__':
    main()
