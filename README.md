# cook-studio

Fast local prefill for Qwen, GLM and DeepSeek on an **M3 Ultra Mac Studio,
60 GPU cores, 256 GiB unified memory**, using oMLX and DS4. Context stays below 64K.

## Serve

Primary weights are downloaded and previously SHA-256 verified on this Mac.
Pinned backends and profiles have completed local inference and prefill benchmarks.
Start **one model at a time**:

```sh
./run.sh serve qwen3.8-flash-next
./run.sh serve glm-5.3-flash
./run.sh serve deepseek-v4-flash-0731-mlx
```

These oMLX profiles serve at `http://127.0.0.1:8000/v1`, with API model IDs
`qwen3.8-flash-next`, `glm-5.3-flash`, and `deepseek-v4-flash`, respectively.
Prefix with `caffeinate -i` to prevent idle sleep during overnight serving.

DS4 alternatives: `qwen3.8-flash-next-ds4`, `glm-5.3-flash-ds4`, and
`deepseek-v4-flash-0731`. Qwen DS4 uses unmerged PR #991; DeepSeek DS4 uses port 8002.
For vision: `./run.sh serve deepseek-v4-flash-vision-exp --vision`.
Qwen's optional `--speculative` enables MTP; the prefill baseline has it off.

## Settings

- **Context:** 65,536 per profile; larger overrides are rejected. `LLM_CTX` lowers it.
  oMLX defaults to 4,096 output tokens, adjustable with `LLM_MAX_TOKENS`. Clients
  must reserve output space: oMLX's context check limits the prompt, not necessarily
  the combined input and generation.
- **Compaction:** the included Pi runner triggers around 58,982 tokens (90%),
  reserves 6,554 tokens and retains 4,096 recent tokens. Larger replies require
  earlier compaction. Other clients need equivalent settings.
- **Cache:** serving keeps prefix reuse enabled. Keep instructions/tool definitions
  stable, append turns, bound tool output and save task checkpoints. Compaction's
  summary call can be expensive; the next request processes the shorter summary
  plus recent history. Changed prefixes generally invalidate the old suffix cache.
- **Tuning:** resident weights, optimized native kernels, one request stream;
  Qwen oMLX uses its measured ANE/GPU split. Native KV precision stays selected:
  generic lower-bit KV is not a verified prefill win for these architectures.

Profiles live in `models/*/model.conf`. `LLM_HOST`/`LLM_PORT` change the listener;
`LLM_PREFILL_CHUNK` changes DS4 chunks. No system memory-limit changes are required.

## Benchmarks and readiness

September 9, 2026: median time to first content/reasoning token over three trials,
roughly 39–42K input tokens, eight output tokens, 64K capacity, no cache reuse,
speculation off. Model load is excluded; backend conversions differ.

| Model | DS4 | oMLX |
| --- | ---: | ---: |
| Qwen3.8 Flash Next | 40.62 s | 42.26 s |
| GLM-5.3 Flash | 142.22 s | 123.75 s |
| DeepSeek V4 Flash 0731 | 105.60 s | 93.98 s |
| DeepSeek V4 Flash Vision Exp, text only | 105.72 s | — |

**Ready for local serving with measured baselines.** Maximum possible performance,
near-64K inference, full reasoning/vision quality and overnight reliability are not
established. The revised 4K reply/90% compaction policy passes offline checks but
has not had an overnight run. DeepSeek V4.1 has no verified configured weights.

The tuning campaign screened 24 configurations. Small single-pass gains were not
promoted. DeepSeek GPU on a separate experimental revision reached 91.67 s with
3/3 long retrieval passes; this is not the normal serving default. Its faster ANE
path failed all long retrieval checks and is rejected.

[Baseline measurements](docs/results/2026-09-09-prefill.json),
[tuning measurements](docs/results/2026-09-09-tuning-screen.json), and
[confirmation](docs/results/2026-09-09-tuning-confirmation.json) preserve the evidence.
Raw runs remain ignored under `results/`; reproducible benchmark tools are retained.

```sh
./run.sh list
./run.sh downloaded       # presence/sizes; not a fresh checksum audit
./run.sh backends
./run.sh prefill --models qwen glm deepseek-0731 --backends ds4 omlx
./run.sh check
```

`prefill` measures cold requests. Older `benchmark speed` sweeps target 16K/32K/56K;
DS4 uses incremental prefixes and oMLX full prefixes, so those rates differ in meaning.
Optional Pi task benchmarks require Docker Sandboxes; serving does not.

## Setup and pins

A fresh checkout needs Bash, Python 3.11+, pinned submodules, compatible backends
and model downloads. `run.sh` reuses `.venv` when present:

```sh
git submodule update --init --recursive
./run.sh setup qwen3.8-flash-next
./run.sh download qwen3.8-flash-next oq4e-mtp
```

`setup` validates installed oMLX 0.6.4 or builds DS4 with the available Apple toolchain.
It does not install system packages. Baselines: oMLX `1d782618`, DS4 `b0a147a7`.
[Runtime identity](backends/runtime-baseline.json) records dependencies/native kernels;
[candidate pins](backends/upstream-candidates.json) retain separate experiments.
Qwen DS4 needs its separate checkout at `6c1e8367`; submodule initialization does
not create it. oMLX and DS4 are the only supported backends.

Model profiles pin weight revisions and hashes. Weights, builds, caches and private
sessions stay ignored. Earlier experiments installed Apple's Metal toolchain 17F109;
Homebrew oMLX was not upgraded.
