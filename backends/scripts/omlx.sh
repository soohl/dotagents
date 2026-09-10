#!/usr/bin/env bash

set -euo pipefail

# shellcheck source=common.sh
. "$(dirname -- "$0")/common.sh"

if command -v omlx >/dev/null 2>&1; then
    server=${OMLX_COMMAND:-$(command -v omlx)}
else
    server=${OMLX_COMMAND:-/opt/homebrew/bin/omlx}
fi

require_omlx_os() {
    require_os "${OMLX_SUPPORTED_OS:-Darwin}"
}

require_omlx() {
    require_omlx_os
    if [ -n "${OMLX_REVISION:-}" ]; then
        require_backend_revision "$OMLX_REVISION"
    fi
    [ -x "$server" ] || missing_backend
    installed=$("$server" --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' |
        head -1)
    [ -n "$installed" ] || {
        printf 'Could not determine oMLX version from: %s --version\n' \
            "$server" >&2
        exit 1
    }
    python3 - "$installed" "${OMLX_BASELINE_VERSION:-${OMLX_MIN_VERSION:-0.6.4}}" <<'PY'
import sys

installed = tuple(map(int, sys.argv[1].split(".")))
minimum = tuple(map(int, sys.argv[2].split(".")))
if installed != minimum and __import__("os").environ.get("LLM_ALLOW_BACKEND_DRIFT") != "1":
    raise SystemExit(
        f"oMLX {sys.argv[2]} baseline is required; found {sys.argv[1]}"
    )
PY
}

write_model_settings() {
    local model_id=$1
    local speculative=$2
    local settings_dir=$3
    local context=${4:-${LLM_CTX:-${OMLX_CONTEXT:-65536}}}
    require_context_limit "$context"
    local max_tokens=${LLM_MAX_TOKENS:-${OMLX_MAX_TOKENS:-4096}}
    local mtp=false
    if [ "$speculative" = on ]; then
        [ "${OMLX_SPECULATIVE_TYPE:-}" = mtp ] || {
            printf 'oMLX speculative decoding is not configured for %s.\n' \
                "$MODEL_ID" >&2
            exit 2
        }
        mtp=true
    fi
    mkdir -p "$settings_dir"
    python3 - "$settings_dir/model_settings.json" "$model_id" \
        "${API_MODEL_ID:-$MODEL_ID}" "$context" "$max_tokens" "$mtp" \
        "${LLM_MTP_DRAFT_TOKENS:-${OMLX_SPECULATIVE_MAX_TOKENS:-3}}" \
        "${OMLX_ENABLE_THINKING:-${MODEL_REASONING:-0}}" \
        "${OMLX_QWEN4_PLE_SSD_OFFLOAD:-false}" <<'PY'
import json
import os
import sys
import tempfile

(
    path,
    model_id,
    alias,
    context,
    max_tokens,
    mtp,
    mtp_tokens,
    reasoning,
    ple_ssd,
) = sys.argv[1:]
if not 0 < int(max_tokens) < int(context):
    raise SystemExit("Output tokens must be positive and smaller than the context window")
settings = {
    "max_context_window": int(context),
    "max_tokens": int(max_tokens),
    "model_alias": alias if alias != model_id else None,
    "enable_thinking": reasoning == "1",
    "preserve_thinking": True if reasoning == "1" else None,
    "qwen4_ple_ssd_offload": ple_ssd.lower() == "true",
    "mtp_enabled": mtp == "true",
    "mtp_num_draft_tokens": int(mtp_tokens),
    "is_default": True,
}
for environment, key, convert in (
    ("OMLX_TEMPERATURE", "temperature", float),
    ("OMLX_TOP_P", "top_p", float),
    ("OMLX_TOP_K", "top_k", int),
):
    if environment in os.environ:
        settings[key] = convert(os.environ[environment])
if "OMLX_QWEN35_ANE_PREFILL_ENABLED" in os.environ:
    def env_bool(name, default):
        return os.environ.get(name, default).lower() in ("1", "true", "yes")

    settings.update(
        {
            "qwen35_ane_prefill_enabled": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_ENABLED", "false"
            ),
            "qwen35_ane_prefill_sequence_length": int(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_SEQUENCE_LENGTH", "2048")
            ),
            "qwen35_ane_prefill_tail_padding_min_tokens": int(
                os.environ.get(
                    "OMLX_QWEN35_ANE_PREFILL_TAIL_PADDING_MIN_TOKENS", "0"
                )
            ),
            "qwen35_ane_prefill_fraction": float(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_FRACTION", "0.53")
            ),
            "qwen35_ane_prefill_fused_down": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_FUSED_DOWN", "false"
            ),
            "qwen35_ane_prefill_max_layers": int(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_MAX_LAYERS", "64")
            ),
            "qwen35_ane_prefill_dual_ane": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_DUAL_ANE", "true"
            ),
            "qwen35_ane_prefill_gdn": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_GDN", "true"
            ),
            "qwen35_ane_prefill_gdn_fraction": float(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_GDN_FRACTION", "0.50")
            ),
            "qwen35_ane_prefill_gdn_max_layers": int(
                os.environ.get(
                    "OMLX_QWEN35_ANE_PREFILL_GDN_MAX_LAYERS", "48"
                )
            ),
            "qwen35_ane_prefill_cpu_enabled": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_CPU_ENABLED", "false"
            ),
            "qwen35_ane_prefill_cpu_fraction": float(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_CPU_FRACTION", "0.135")
            ),
            "qwen35_ane_prefill_cpu_down_fraction": float(
                os.environ.get(
                    "OMLX_QWEN35_ANE_PREFILL_CPU_DOWN_FRACTION", "0"
                )
            ),
            "qwen35_ane_prefill_cpu_gdn_fraction": float(
                os.environ.get(
                    "OMLX_QWEN35_ANE_PREFILL_CPU_GDN_FRACTION", "0"
                )
            ),
            "qwen35_ane_prefill_cpu_threads": int(
                os.environ.get("OMLX_QWEN35_ANE_PREFILL_CPU_THREADS", "8")
            ),
            "qwen35_ane_prefill_cpu_shared_resource": env_bool(
                "OMLX_QWEN35_ANE_PREFILL_CPU_SHARED_RESOURCE", "true"
            ),
        }
    )
payload = {"version": 1, "models": {model_id: settings}}
directory = os.path.dirname(path)
fd, temporary = tempfile.mkstemp(prefix=".model-settings.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

case "${1:-}" in
    setup)
        require_omlx_os
        require_omlx
        "$server" --version
        ;;
    serve)
        variant=$2
        speculative=${3:-${LLM_SPECULATIVE:-off}}
        require_omlx
        model=$(model_path "$variant")
        model_id=${model##*/}
        settings_dir="$RUNTIME_ROOT/omlx"
        write_model_settings "$model_id" "$speculative" "$settings_dir"
        host=${LLM_HOST:-127.0.0.1}
        port=${LLM_PORT:-${OMLX_PORT:-8000}}
        cache_args=(serve)
        [ "${LLM_DISABLE_PROMPT_CACHE:-0}" != 1 ] || cache_args+=(--no-cache)
        exec env OMLX_BASE_PATH="$settings_dir" "$server" "${cache_args[@]}" \
            --model-dir "$MODEL_ROOT" \
            --host "$host" --port "$port" \
            --max-concurrent-requests \
            "${OMLX_MAX_CONCURRENT_REQUESTS:-1}" \
            --no-hf-cache
        ;;
    benchmark)
        variant=$2
        speculative=${3:-${LLM_SPECULATIVE:-off}}
        require_omlx
        require_command python3
        if [ "$speculative" != off ] &&
           [ "${OMLX_SPECULATIVE_TYPE:-}" != mtp ]; then
            printf 'oMLX speculative decoding is not configured for %s.\n' \
                "$MODEL_ID" >&2
            exit 2
        fi
        model=$(model_path "$variant")
        model_id=${model##*/}
        settings_dir="$RUNTIME_ROOT/omlx-benchmark"
        ctx_start=${LLM_BENCH_CTX_START:-16384}
        ctx_max=${LLM_BENCH_CTX_MAX:-57344}
        generated=${LLM_BENCH_GEN_TOKENS:-128}
        ctx_alloc=${LLM_BENCH_CTX_ALLOC:-${LLM_CTX:-${OMLX_CONTEXT:-65536}}}
        require_prompt_budget "$ctx_alloc" "$ctx_max" "$generated"
        write_model_settings "$model_id" off "$settings_dir" "$ctx_alloc"
        exec python3 "$ROOT/benchmarks/scripts/omlx.py" \
            --server "$server" \
            --model-root "$MODEL_ROOT" \
            --model "${API_MODEL_ID:-$MODEL_ID}" \
            --settings-dir "$settings_dir" \
            --prompt "$ROOT/benchmarks/speed/promessi-sposi.txt" \
            --ctx-start "$ctx_start" \
            --ctx-max "$ctx_max" \
            --step "${LLM_BENCH_STEP_MUL:-2}" \
            --generated "$generated" \
            --speculative "$speculative"
        ;;
    *)
        printf 'Unsupported oMLX action: %s\n' "${1:-}" >&2
        exit 2
        ;;
esac
