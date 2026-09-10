#!/usr/bin/env bash

require_context_limit() {
    local value=$1
    if ! [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] ||
       [ "$value" -lt 2 ] || [ "$value" -gt 65536 ]; then
        printf 'Context must be an integer from 2 to 65536 tokens; got %s.\n' "$value" >&2
        exit 2
    fi
}

require_prompt_budget() {
    local context=$1 prompt=$2 generated=$3
    require_context_limit "$context"
    require_context_limit "$prompt"
    if ! [[ "$generated" =~ ^[1-9][0-9]{0,4}$ ]] ||
       [ "$((prompt + generated))" -gt "$context" ]; then
        printf 'Prompt target plus output tokens must fit the %s-token context.\n' "$context" >&2
        exit 2
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        printf 'Required command not found: %s\n' "$1" >&2
        exit 1
    }
}

require_os() {
    wanted=$1
    [ -z "$wanted" ] || [ "$(uname -s)" = "$wanted" ] || {
        printf 'The %s profile requires %s.\n' "$BACKEND" "$wanted" >&2
        exit 1
    }
}

require_backend_revision() {
    revision=$1
    require_command git
    [ -e "$BACKEND_ROOT/.git" ] || {
        printf 'Missing backend submodule. Run: git submodule update --init --recursive\n' >&2
        exit 1
    }
    actual=$(git -C "$BACKEND_ROOT" rev-parse HEAD)
    if [ "$actual" != "$revision" ] ||
       ! git -C "$BACKEND_ROOT" diff --quiet HEAD --; then
        if [ "${LLM_ALLOW_BACKEND_DRIFT:-0}" != 1 ]; then
            printf 'Backend differs from baseline %s (HEAD %s). Use LLM_ALLOW_BACKEND_DRIFT=1 for a recorded experiment.\n' \
                "$revision" "$actual" >&2
            exit 1
        fi
        printf 'Experimental backend: %s at %s; baseline %s.\n' \
            "$BACKEND" "$actual" "$revision" >&2
    fi
}

primary_model_file() {
    wanted=$1
    key=$(printf '%s' "$wanted" |
        tr '[:lower:]-.' '[:upper:]__' |
        tr -c '[:alnum:]_' '_')
    file_name="DOWNLOAD_${key}_FILE"
    file=${!file_name:-}
    [ -z "$file" ] || {
        printf '%s\n' "$file"
        return
    }
    printf 'No model file configured for variant: %s\n' "$wanted" >&2
    exit 1
}

model_path() {
    variant=$1
    key=$(printf '%s' "$variant" |
        tr '[:lower:]-.' '[:upper:]__' |
        tr -c '[:alnum:]_' '_')
    directory_name="DOWNLOAD_${key}_DIR"
    directory=${!directory_name:-}
    if [ -n "$directory" ]; then
        path="$MODEL_ROOT/$directory"
        [ -d "$path" ] || {
            printf 'Model is missing. Run: ./llm download %s %s\n' \
                "$MODEL_ID" "$variant" >&2
            exit 1
        }
        printf '%s\n' "$path"
        return
    fi
    path="$MODEL_ROOT/$(primary_model_file "$variant")"
    [ -f "$path" ] || {
        printf 'Model is missing. Run: ./llm download %s %s\n' \
            "$MODEL_ID" "$variant" >&2
        exit 1
    }
    printf '%s\n' "$path"
}

missing_backend() {
    printf '%s is not built. Run: ./llm setup %s\n' \
        "$BACKEND" "$MODEL_ID" >&2
    exit 1
}
