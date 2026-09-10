# cook-studio rules

- Scope: maximum Qwen, GLM, and DeepSeek prefill throughput / minimum TTFT on this Mac Studio.
  The supported backends are MLX/oMLX and native Metal/DS4.
  README.md is the single project guide; keep it concise. Model settings live in model.conf.
- Backend sources are pinned submodules. Keep Git links and backends/config pins
  consistent. Record candidate revisions separately; never replace a baseline
  with an unmeasured speed claim. Read backend CONTRIBUTING guidance for engine edits.
- Prefill is the bottleneck. Use short generation for primary comparisons and
  separate cold full-prefix processing from cache reuse; decode tuning is secondary.
- Keep launcher behavior in run.sh/llm, model tuning in models/<model>/model.conf,
  and runtime dependency/kernel identity in backends/runtime-baseline.json.
- Reuse existing tools/environments; no system package changes or weight downloads
  as preflight. No sibling repository is a runtime dependency.
- Never stop another process's server. Load one large model at a time unless
  combined residency has been measured. Honor real M3 Ultra capabilities; M5/NAX,
  CUDA, multi-machine, and SSD-streaming results do not establish gains here.
- Keep weights, builds, caches, credentials, sessions, and new results ignored.
  Preserve upstream licenses and the provenance/limitations of measured results.
- Record hardware, date, model/backend revisions, runtime dependencies, actual
  tokens, quantization, cache state, speculation, and power/thermal conditions.
  Pair throughput changes with quality checks; don't optimize by silently changing
  precision, workload, reasoning budget, or output length.
- Run ./run.sh check for launcher/profile/pin changes. Engine changes need targeted
  upstream tests and matched performance checks, not an automatic full suite.
- Keep discovery, request format, reasoning, and server ownership consistent.
  Preserve sandbox boundaries in optional task-quality experiments.
- Update affected docs. Do not commit, push, deploy, remove original checkouts,
  or make system-wide changes without explicit authorization.
