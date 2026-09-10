#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$ROOT/.venv/bin/python3" ]; then
    export PATH="$ROOT/.venv/bin:$PATH"
fi
if [ "${1:-}" = check ]; then
    cd "$ROOT"
    for script in run.sh llm backends/scripts/*.sh; do
        bash -n "$script"
    done
    exec python3 -m unittest discover -s tests -v
fi
if [ "${1:-}" = prefill ]; then
    shift
    exec python3 "$ROOT/benchmarks/prefill.py" "$@"
fi
if [ "${1:-}" = backends ]; then
    exec python3 "$ROOT/tools/backend_status.py"
fi
exec "$ROOT/llm" "$@"
