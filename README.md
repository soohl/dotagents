# cook-studio

Fast local prefill for Qwen, GLM and DeepSeek on an **M3 Ultra Mac Studio,
60 GPU cores, 256 GiB unified memory**, using DS4. Context stays below 64K.
oMLX is temporarily disabled in `backends/config/omlx.conf` (`BACKEND_ENABLED=0`).
Its profiles, weights and latest rollback environment are retained; set that value to `1` to
re-enable project commands. `list` and `downloaded` still show the retained artifacts.

## Serve

Primary weights are downloaded and previously SHA-256 verified on this Mac.
Pinned backends and profiles have completed local inference and prefill benchmarks.
Start **one model at a time**:

```sh
./run.sh serve qwen3.8-flash-next-ds4
./run.sh serve glm-5.3-flash-ds4
./run.sh serve deepseek-v4-flash-0731
```

Qwen serves at `http://127.0.0.1:8000/v1`; GLM and DeepSeek use port `8002`.
Their API model IDs are
`qwen3.8-flash-next`, `glm-5.3-flash`, and `deepseek-v4-flash`, respectively.
Prefix with `caffeinate -i` to prevent idle sleep during overnight serving.

<details>
<summary>Retained oMLX LAN configuration (requires re-enabling oMLX)</summary>

For oMLX LAN chat, bind with `LLM_HOST=0.0.0.0` and open
`http://<Mac-LAN-IP>:8000/admin/chat`. Current oMLX requires `OMLX_API_KEY`
for a LAN listener; use the same key to log into the web UI.
The September 18 local candidate is oMLX `0.7.0.dev4` (`14194fe7`), with
rebuilt native kernels and its own environment. To reuse that installation:

```sh
OMLX_COMMAND="$PWD/.local/venvs/omlx-14194fe7-py312/bin/omlx" \
LLM_ALLOW_BACKEND_DRIFT=1 LLM_ANE_PREFILL_ENABLED=false LLM_HOST=0.0.0.0 \
LLM_RUNTIME_ROOT="$PWD/.local/qwen-lan-14194fe7" \
OMLX_API_KEY="$(cat .local/qwen-lan-14194fe7/api-key.txt)" \
caffeinate -i ./run.sh serve qwen3.8-flash-next
```

This candidate requires GPU prefill: its ANE validation rejects these weights'
`qwen4_exp` metadata. `LLM_ANE_PREFILL_ENABLED` overrides the profile for this run.
It is separate from the measured baseline; no speed improvement has been
established. Its credentials, logs and runtime identity stay under
the ignored `.local/qwen-lan-14194fe7/` directory.

</details>

Qwen DS4 uses the pinned PR #991 checkout; that PR has since merged upstream,
but newer main requires a different weight layout and is not the local baseline.
For vision: `./run.sh serve deepseek-v4-flash-vision-exp --vision`.
The current DS4 launcher does not expose Qwen MTP; speculation remains off.

## Settings

- **Context:** 65,536 per profile; larger overrides are rejected. `LLM_CTX` lowers it.
  Clients must reserve output space within the context window.
- **Compaction:** the included Pi runner triggers around 58,982 tokens (90%),
  reserves 6,554 tokens and retains 4,096 recent tokens. Larger replies require
  earlier compaction. Other clients need equivalent settings.
- **Cache:** serving keeps prefix reuse enabled. Keep instructions/tool definitions
  stable, append turns, bound tool output and save task checkpoints. Compaction's
  summary call can be expensive; the next request processes the shorter summary
  plus recent history. Changed prefixes generally invalidate the old suffix cache.
- **Tuning:** resident weights, optimized native Metal kernels, one request stream.
  Native KV precision stays selected:
  generic lower-bit KV is not a verified prefill win for these architectures.

Profiles live in `models/*/model.conf`. `LLM_HOST`/`LLM_PORT` change the listener;
`LLM_PREFILL_CHUNK` changes DS4 chunks. No system memory-limit changes are required.
DeepSeek V4 Flash now uses a 4,096-token prefill chunk, matching the pinned DS4
default. The DS4 DeepSeek measurements below used 8,192; the new setting has not
yet been benchmarked locally.

## Benchmarks and readiness

September 19, 2026 DS4 measurements on macOS 27.0: one completed trial per model
before the user stopped the mixed-backend campaign. Fresh prefixes, zero cached
tokens, speculation off, 64K capacity; 8 output tokens for TTFT and a separate
128-token generation request at roughly 10K input. Loading is excluded.

| Model | Short input tokens | TTFT | Long input tokens | TTFT | Approx. decode tok/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3.8 Flash Next | 9,579 | 10.31 s | 38,790 | 40.73 s | 47.32 |
| GLM-5.3 Flash | 10,302 | 35.52 s | 41,618 | 145.08 s | 21.16 |
| DeepSeek V4 Flash 0731 | 10,371 | 25.20 s | 42,300 | 106.60 s | 36.55 |

These are single-trial observations, not new tuning qualifications or repeated
medians. Raw outputs and runtime metadata are under ignored
`results/prefill-20260919T070039.194470Z/`. Decode is measured after the first
visible stream chunk and includes API overhead. DeepSeek V4.1 was not tested.

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
Older oMLX tuning checkouts/environments were moved to
`~/.Trash/cook-studio-cleanup-20260919/`; restore them to their original
`.local/backends/` and `.local/venvs/` paths before using the historical tuning
plans. The latest `14194fe7` checkout and its Python 3.12 environment remain
available for rollback. No model weights or benchmark evidence were removed.

```sh
./run.sh list
./run.sh downloaded       # presence/sizes; not a fresh checksum audit
./run.sh backends
./run.sh prefill --models qwen glm deepseek-0731 --backends ds4
./run.sh prefill --models qwen glm deepseek-0731 --backends ds4 \
  --decode-generated 128 --decode-chars 32768
./run.sh check
```

`prefill` measures cold requests. Older `benchmark speed` sweeps target 16K/32K/56K;
DS4 uses incremental prefixes and oMLX full prefixes, so those rates differ in meaning.
The optional decode request uses a separate fresh prefix, reports an approximate rate
after the first visible stream chunk, and keeps prompt caching and speculation disabled. Stop any
user-owned large-model server before running a matrix; the benchmark only stops the
servers it starts itself. Model families match across backends, but serialized weights,
quantization and templates differ and remain part of the result metadata.
Optional Pi task benchmarks require Docker Sandboxes; serving does not.

## Setup and pins

A fresh checkout needs Bash, Python 3.11+, pinned submodules, compatible backends
and model downloads. `run.sh` reuses `.venv` when present:

```sh
git submodule update --init --recursive
./run.sh setup qwen3.8-flash-next-ds4
./run.sh download qwen3.8-flash-next-ds4 q4
```

`setup` builds DS4 with the available Apple toolchain. When re-enabled, oMLX setup
validates installed oMLX 0.6.4.
It does not install system packages. Baselines: oMLX `1d782618`, DS4 `b0a147a7`.
[Runtime identity](backends/runtime-baseline.json) records dependencies/native kernels;
[candidate pins](backends/upstream-candidates.json) retain separate experiments.
Qwen DS4 needs its separate checkout at `6c1e8367`; submodule initialization does
not create it. oMLX and DS4 are the only supported backends.

Model profiles pin weight revisions and hashes. Weights, builds, caches and private
sessions stay ignored. Earlier experiments installed Apple's Metal toolchain 17F109;
Homebrew oMLX was not upgraded.
