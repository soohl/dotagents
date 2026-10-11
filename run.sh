#!/usr/bin/env bash
set -euo pipefail
SOURCE=${BASH_SOURCE[0]}
while [ -L "$SOURCE" ]; do
    DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$SOURCE")" && pwd)
    SOURCE=$(readlink "$SOURCE")
    case "$SOURCE" in /*) ;; *) SOURCE="$DIRECTORY/$SOURCE" ;; esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$SOURCE")" && pwd)
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf 'Verification failed: missing .venv/bin/python. Prepare the project environment first.\n' >&2
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PATH="$ROOT/.venv/bin:$PATH"
exec "$PYTHON" -u "$ROOT/src/dotagents.py" "$@"
