#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf 'Create the local environment first: uv venv .venv\n' >&2
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
case "${1:-help}" in
    gateway)
        cd "$ROOT"
        exec "$PYTHON" "$ROOT/src/inference_gateway.py" "${2:-qwen}"
        ;;
    check-browser)
        cd "$ROOT"
        exec docker compose --env-file .env exec -T browser python - < tests/browser_smoke.py
        ;;
    check|test)
        bash -n "$ROOT/run.sh"
        cd "$ROOT"
        exec "$PYTHON" -m unittest discover -s tests -v
        ;;
    *) exec "$PYTHON" "$ROOT/src/cook_studio.py" "$@" ;;
esac
