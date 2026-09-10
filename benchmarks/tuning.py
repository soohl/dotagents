#!/usr/bin/env python3
"""Explicit candidate sweep; preserves launcher profiles and baseline pins."""
import argparse
import collections
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import tempfile
import time
import urllib.request

import prefill

ROOT = prefill.ROOT
_spec = importlib.util.spec_from_file_location('cook_proc_memory', ROOT/'backends/omlx/omlx/utils/proc_memory.py')
proc_memory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proc_memory)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def degenerate_output(text):
    """Reject the repeated special-token collapse observed in this workload."""
    return text.count('<｜begin▁of▁sentence｜>') >= 3


def validate_source(runtime):
    path = ROOT / runtime['source']
    head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if head != runtime['revision']:
        raise ValueError(f'Unexpected source HEAD: {path}: {head}')
    dirty = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain', '--untracked-files=no'], text=True)
    if dirty:
        raise ValueError(f'Modified candidate source: {path}: {dirty}')
    return path


def quality_prompt(source, chars, trial):
    # Independent factual-retrieval workload; three depths and no outside knowledge.
    body = source[:chars]
    nonce = hashlib.sha256(f'cook-quality:{trial}:{chars}'.encode()).hexdigest()[:20]
    facts = [('CEDAR', '482917'), ('MAPLE', '735264'), ('BIRCH', '196853')]
    cuts = [len(body) // 10, len(body) // 2, len(body) * 9 // 10]
    for at, (key, value) in reversed(list(zip(cuts, facts))):
        body = body[:at] + f'\nREGISTRY FACT: {key} has code {value}.\n' + body[at:]
    return (f'{nonce}\nExtract only the three REGISTRY FACT codes from this document.\n'
            f'{body}\nReturn one JSON object mapping CEDAR, MAPLE, BIRCH to their code strings.'), dict(facts)


def quality_request(url, backend, api_model, text, expected):
    payload = {'model': api_model, 'messages': [{'role': 'user', 'content': text}],
               'max_tokens': 512, 'temperature': 0, 'top_p': 1, 'seed': 42,
               'stream': False, 'reasoning_effort': 'low', 'think': False,
               'chat_template_kwargs': {'enable_thinking': False, 'reasoning_effort': 'low'}}
    request = urllib.request.Request(url + '/v1/chat/completions', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=1800) as response:
        data = json.load(response)
    choice = data['choices'][0]
    content = choice.get('message', {}).get('content') or ''
    # Permit a markdown JSON fence, but require exact key/value content.
    clean = content.strip()
    if clean.startswith('```'):
        clean = '\n'.join(clean.splitlines()[1:-1])
    try:
        passed = json.loads(clean) == expected
    except (ValueError, TypeError):
        passed = False
    usage = data.get('usage', {})
    cached = usage.get('prompt_tokens_details', {}).get('cached_tokens', usage.get('cached_tokens', 0))
    return {'passed': passed, 'content': content, 'finish_reason': choice.get('finish_reason'),
            'usage': usage, 'cached_tokens': cached, 'elapsed_seconds': time.monotonic()-start,
            'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'request_controls': {k: v for k,v in payload.items() if k != 'messages'}}


def report(directory, rows):
    groups = collections.defaultdict(list)
    for row in rows:
        groups[(row['candidate'], row['source_chars'])].append(row)
    quality_path = directory/'quality.jsonl'
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()] if quality_path.exists() else []
    lines = ['# Tuning measurements', '',
             'Cold input/TTFT; compare within one model. DISQUALIFIED rows are not performance wins.',
             'Retrieval applies only to its recorded document length; a pass does not establish general quality.', '',
             '| Candidate | Source chars | Trials | Median TTFT s | Median input/TTFT tok/s | Output / retrieval gate |',
             '| --- | ---: | ---: | ---: | ---: | --- |']
    for (name, chars), group in groups.items():
        med = lambda key: statistics.median(r[key] for r in group)
        checks = [q for q in quality if q['candidate']==name]
        invalid = any(degenerate_output(r.get('output','')) for r in rows if r['candidate']==name)
        if invalid or any(not q['passed'] or q.get('cached_tokens',0) for q in checks):
            gate = 'DISQUALIFIED'
        elif len(checks) < len(group):
            gate = 'PENDING'
        else:
            gate = 'PASS (bounded probe)'
        lines.append(f'| {name} | {chars} | {len(group)} | {med("ttft_seconds"):.3f} | {med("effective_input_tokens_per_ttft_second"):.2f} | {gate} |')
    (directory/'REPORT.md').write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', default=str(ROOT/'benchmarks/tuning-plan.json'))
    parser.add_argument('--candidates', nargs='+')
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--chars', nargs='+', type=int, default=[32768,131072])
    parser.add_argument('--quality-chars', type=int, default=8192)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    candidates = [c for c in plan['candidates'] if args.candidates is None or c['name'] in args.candidates]
    if not candidates or (args.candidates and set(args.candidates)-{c['name'] for c in candidates}):
        parser.error('Unknown/empty candidate selection')
    source = (ROOT/'benchmarks/speed/promessi-sposi.txt').read_text()
    if args.trials < 1 or not all(0 < n <= len(source) for n in [*args.chars,args.quality_chars]):
        parser.error('Invalid trial count or source lengths')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    directory = ROOT/'results'/('tuning-'+stamp)
    directory.mkdir(parents=True)
    print(directory,flush=True)
    confs = {}
    for c in candidates:
        confs[c['name']] = prefill.preflight([c['backend']],c['model'])[c['backend']]
        validate_source(plan['runtimes'][c['runtime']])
    meta = {'settings':vars(args),'plan':plan,'selected':[c['name'] for c in candidates],
            'baseline_inventory_sha256':digest(ROOT/'backends/runtime-baseline.json'),
            'tuning_inventory_sha256':digest(ROOT/'backends/tuning-runtime-inventory.json'),
            'runner_sha256':digest(Path(__file__)), 'wrapper_sha256':digest(ROOT/'tools/omlx_experiment.py'),
            'model_config_sha256':{c['name']:digest(ROOT/'models'/prefill.MATRIX[c['model']][c['backend']]/'model.conf') for c in candidates},
            'artifacts':{name:{k:conf[k] for k in ('TARGET_HF_REPO','TARGET_HF_REVISION','DEFAULT_VARIANT')} for name,conf in confs.items()},
            'cache_policy':'fresh prefixes; oMLX cache off; DS4 disk checkpoints off',
            'hardware':{'cpu':subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string'],text=True).strip(),
                        'memory_bytes':int(subprocess.check_output(['sysctl','-n','hw.memsize']))},
            'conditions':'One owned server; process lifetime phys_footprint high-water before/after requests; no continuous power/thermal sampling'}
    (directory/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    rows=[]
    failed=False
    for trial in range(args.trials):
        for c in candidates if trial%2==0 else reversed(candidates):
            conf=confs[c['name']];runtime=plan['runtimes'][c['runtime']]
            with tempfile.TemporaryDirectory(prefix='cook-tuning-') as temp, (directory/f'{trial}-{c["name"]}.log').open('w') as log:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                url=f'http://127.0.0.1:{port}'
                env=dict(os.environ, LLM_HOST='127.0.0.1',LLM_PORT=str(port),LLM_CTX='65536',
                         LLM_MAX_TOKENS='512',LLM_RUNTIME_ROOT=temp,LLM_DISABLE_PROMPT_CACHE='1',LLM_SPECULATIVE='off')
                env.update(c.get('env',{}))
                if c['backend']=='omlx':
                    env.update(OMLX_COMMAND=str(ROOT/'tools/omlx_experiment.py'),LLM_ALLOW_BACKEND_DRIFT='1',
                               COOK_OMLX_PYTHON=str(ROOT/runtime['python']) if not runtime['python'].startswith('/') else runtime['python'],
                               COOK_OMLX_SETTINGS=json.dumps(c.get('settings',{})))
                    if runtime.get('source_runtime',True):env['COOK_OMLX_SOURCE']=str(ROOT/runtime['source'])
                process=subprocess.Popen([str(ROOT/'run.sh'),'serve',conf['MODEL_ID']],env=env,
                                         stdout=log,stderr=log,start_new_session=True)
                try:
                    print(f'trial {trial+1}: loading {c["name"]}',flush=True)
                    api=conf.get('API_MODEL_ID',conf['MODEL_ID'])
                    prefill.wait_ready(process,url,api,conf.get('READINESS_MODEL_ID'),conf.get('READINESS_MODEL_NAME'))
                    prefill.completion(url,c['backend'],'Reply with the word ready.',16,api)
                    for chars in args.chars:
                        text=prefill.prompt(source,chars,trial,'cook-prefill-v1')
                        memory_before=proc_memory.get_lifetime_max_phys_footprint(process.pid)
                        row=prefill.completion(url,c['backend'],text,8,api)
                        row.update(process_lifetime_peak_bytes_before=memory_before,
                                   process_lifetime_peak_bytes_after=proc_memory.get_lifetime_max_phys_footprint(process.pid),
                                   process_footprint_bytes_after=proc_memory.get_phys_footprint(process.pid))
                        if row['cached_tokens'] != 0 or row['completion_tokens'] != 8:
                            raise RuntimeError('Invalid cache/output controls: '+json.dumps(row))
                        row.update(candidate=c['name'],model=c['model'],backend=c['backend'],trial=trial+1,
                                   source_chars=chars,prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
                        row['output_degenerate'] = degenerate_output(row['output'])
                        failed = failed or row['output_degenerate']
                        rows.append(row)
                        with (directory/'results.jsonl').open('a') as out:out.write(json.dumps(row)+'\n')
                        report(directory,rows)
                        print(f'{c["name"]}: {row["prompt_tokens"]} tokens, TTFT {row["ttft_seconds"]:.3f}s',flush=True)
                    text,expected=quality_prompt(source,args.quality_chars,trial)
                    result=quality_request(url,c['backend'],api,text,expected)
                    result.update(candidate=c['name'],trial=trial+1,source_chars=args.quality_chars)
                    with (directory/'quality.jsonl').open('a') as out:out.write(json.dumps(result)+'\n')
                    failed = failed or not result['passed'] or result['cached_tokens'] != 0
                    report(directory,rows)
                    print(f'{c["name"]}: retrieval {"PASS" if result["passed"] else "FAIL/INCOMPLETE"}',flush=True)
                except (OSError,RuntimeError,ValueError,TimeoutError) as error:
                    failed=True
                    with (directory/'failures.jsonl').open('a') as out:out.write(json.dumps({'candidate':c['name'],'trial':trial+1,'error':str(error)})+'\n')
                    print(f'{c["name"]}: FAILED: {error}',flush=True)
                finally:
                    prefill.stop(process)
    return int(failed)


if __name__=='__main__':
    raise SystemExit(main())
