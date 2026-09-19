#!/usr/bin/env bash

set -euo pipefail

# shellcheck source=common.sh
. "$(dirname -- "$0")/common.sh"

if [ "${LLM_DS4_MAIN_CANDIDATE:-0}" = 1 ]; then
    [ -z "${DS4_SOURCE_SUBDIR:-}" ] || {
        printf 'Main candidate cannot replace a model-specific DS4 branch.\n' >&2
        exit 2
    }
    . "$ROOT/backends/config/ds4-main.conf"
fi

if [ -n "${DS4_SOURCE_SUBDIR:-}" ]; then
    BACKEND_ROOT="$ROOT/$DS4_SOURCE_SUBDIR"
fi

require_ds4() {
    require_os "${DS4_OS:-}"
    require_backend_revision "$DS4_REVISION"
    [ -x "$BACKEND_ROOT/ds4" ] &&
    [ -x "$BACKEND_ROOT/ds4-server" ] &&
        [ -x "$BACKEND_ROOT/ds4-bench" ] || missing_backend
}

case "${1:-}" in
    setup)
        require_os "${DS4_OS:-}"
        require_command make
        require_command cc
        require_backend_revision "$DS4_REVISION"
        exec make -C "$BACKEND_ROOT" -j "${DS4_BUILD_JOBS:-8}" ds4 ds4-server ds4-bench
        ;;
    serve)
        variant=$2
        speculative=${3:-${LLM_SPECULATIVE:-${DS4_SPECULATIVE_DEFAULT:-off}}}
        vision=${4:-${LLM_VISION:-off}}
        require_ds4
        model=$(model_path "$variant")
        artifact_root="$model"
        if [ -d "$model" ]; then
            model="$model/$DS4_MODEL_FILE"
            [ -f "$model" ] || { printf 'Missing DS4 main model: %s\n' "$model" >&2; exit 1; }
        fi
        vision_model=
        if [ "$vision" = on ]; then
            [ -n "${VISION_FILE:-}" ] || {
                printf 'Vision is not configured for model: %s\n' \
                    "$MODEL_ID" >&2
                exit 2
            }
            vision_model="$GGUF_ROOT/$VISION_FILE"
            [ -f "$vision_model" ] || {
                printf 'Vision encoder is missing. Run: ./llm download %s %s --vision\n' \
                    "$MODEL_ID" "$variant" >&2
                exit 1
            }
        fi
        ctx=${LLM_CTX:-${DS4_CONTEXT:-65536}}
        require_context_limit "$ctx"
        prefill=${LLM_PREFILL_CHUNK:-${DS4_PREFILL_CHUNK:-8192}}
        host=${LLM_HOST:-127.0.0.1}
        port=${LLM_PORT:-${DS4_PORT:-8000}}
        kv_mb=${LLM_KV_DISK_SPACE_MB:-${DS4_KV_DISK_SPACE_MB:-32768}}
        cache="$RUNTIME_ROOT/$BACKEND/$variant-kv-cache"

        unset DS4_METAL_Q8_MV_NSG DS4_METAL_Q8_MV_ROWS
        export DS4_METAL_MODEL_UNTRACKED=${DS4_METAL_MODEL_UNTRACKED:-1}
        args=(
            --model "$model" --metal \
            --host "$host" --port "$port" \
            --ctx "$ctx"
        )
        [ "$prefill" = auto ] || args+=(--prefill-chunk "$prefill")
        if [ "${LLM_DISABLE_PROMPT_CACHE:-0}" != 1 ]; then
            mkdir -p "$cache"
            args+=(--kv-disk-dir "$cache" --kv-disk-space-mb "$kv_mb")
        fi
        if [ -n "${DS4_PLE_FILE:-}" ]; then
            ple="$artifact_root/$DS4_PLE_FILE"
            [ -f "$ple" ] || { printf 'Missing Qwen PLE sidecar: %s\n' "$ple" >&2; exit 1; }
            args+=(--ple "$ple")
        fi
        [ -z "$vision_model" ] || args+=(--vision "$vision_model")
        if [ "$speculative" = on ]; then
            [ "${DS4_SPECULATIVE_TYPE:-}" = dspark ] || {
                printf 'Unsupported DS4 speculative type: %s\n' \
                    "${DS4_SPECULATIVE_TYPE:-none}" >&2
                exit 2
            }
            support="$GGUF_ROOT/${DS4_SPECULATIVE_FILE:-}"
            [ -f "$support" ] || {
                printf 'Speculative model is missing. Run: ./llm download %s %s --speculative\n' \
                    "$MODEL_ID" "$variant" >&2
                exit 1
            }
            args+=(--mtp-model "$support" --dspark)
            [ -z "${DS4_SPECULATIVE_CONFIDENCE:-}" ] ||
                args+=(--dspark-confidence "$DS4_SPECULATIVE_CONFIDENCE")
        fi
        cd "$BACKEND_ROOT"
        exec ./ds4-server "${args[@]}"
        ;;
    benchmark)
        [ -z "${DS4_PLE_FILE:-}" ] || {
            printf 'Use ./run.sh prefill --models qwen --backends ds4 for Qwen.\n' >&2
            exit 2
        }
        variant=$2
        speculative=${3:-${LLM_SPECULATIVE:-off}}
        vision=${4:-${LLM_VISION:-off}}
        [ "$vision" != on ] || {
            printf 'DS4 speed benchmarks do not support image input.\n' >&2
            exit 2
        }
        [ "$speculative" != on ] || {
            printf 'DS4 speed benchmarks support target-only or --compare.\n' >&2
            exit 2
        }
        require_ds4
        model=$(model_path "$variant")
        artifact_root="$model"
        if [ -d "$model" ]; then
            model="$model/$DS4_MODEL_FILE"
            [ -f "$model" ] || { printf 'Missing DS4 main model: %s\n' "$model" >&2; exit 1; }
        fi
        if [ "$speculative" = compare ]; then
            require_command python3
            [ "${DS4_SPECULATIVE_TYPE:-}" = dspark ] || {
                printf 'Unsupported DS4 speculative type: %s\n' \
                    "${DS4_SPECULATIVE_TYPE:-none}" >&2
                exit 2
            }
            support="$GGUF_ROOT/${DS4_SPECULATIVE_FILE:-}"
            [ -f "$support" ] || {
                printf 'Speculative model is missing. Run: ./llm download %s %s --speculative\n' \
                    "$MODEL_ID" "$variant" >&2
                exit 1
            }
            speculative_ctx=${LLM_BENCH_SPECULATIVE_CTX:-${DS4_BENCH_SPECULATIVE_CONTEXT:-65536}}
            require_context_limit "$speculative_ctx"
            exec python3 "$ROOT/benchmarks/scripts/ds4.py" \
                --binary "$BACKEND_ROOT/ds4" \
                --model "$model" \
                --support "$support" \
                --prompts "$ROOT/benchmarks/speed/speculative.json" \
                --generated "${LLM_BENCH_GEN_TOKENS:-512}" \
                --context \
                "$speculative_ctx" \
                --prefill-chunk \
                "${LLM_PREFILL_CHUNK:-${DS4_PREFILL_CHUNK:-8192}}" \
                --confidence \
                "${LLM_SPECULATIVE_CONFIDENCE:-${DS4_SPECULATIVE_CONFIDENCE:-0.6}}"
        fi
        ctx_start=${LLM_BENCH_CTX_START:-${DS4_BENCH_CTX_START:-16384}}
        ctx_max=${LLM_BENCH_CTX_MAX:-${DS4_BENCH_CTX_MAX:-57344}}
        generated=${LLM_BENCH_GEN_TOKENS:-${DS4_BENCH_GEN_TOKENS:-128}}
        ctx_alloc=${LLM_BENCH_CTX_ALLOC:-${LLM_CTX:-${DS4_CONTEXT:-65536}}}
        require_prompt_budget "$ctx_alloc" "$ctx_max" "$generated"
        step_mul=${LLM_BENCH_STEP_MUL:-${DS4_BENCH_STEP_MUL:-2}}
        step_incr=${LLM_BENCH_STEP_INCR:-}
        prefill=${LLM_PREFILL_CHUNK:-${DS4_PREFILL_CHUNK:-8192}}

        unset DS4_METAL_Q8_MV_NSG DS4_METAL_Q8_MV_ROWS
        export DS4_METAL_MODEL_UNTRACKED=${DS4_METAL_MODEL_UNTRACKED:-1}
        args=(
            --model "$model" --metal
            --prompt-file "$ROOT/benchmarks/speed/promessi-sposi.txt"
            --ctx-start "$ctx_start" --ctx-max "$ctx_max"
            --ctx-alloc "$ctx_alloc"
            --gen-tokens "$generated"
        )
        [ "$prefill" = auto ] || args+=(--prefill-chunk "$prefill")
        if [ -n "$step_incr" ]; then
            args+=(--step-mul 1 --step-incr "$step_incr")
        else
            args+=(--step-mul "$step_mul")
        fi
        cd "$BACKEND_ROOT"
        exec ./ds4-bench "${args[@]}"
        ;;
    *)
        printf 'Unsupported DS4 action: %s\n' "${1:-}" >&2
        exit 2
        ;;
esac
