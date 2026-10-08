#!/usr/bin/env bash
set -euo pipefail

# Start one private vLLM OpenAI-compatible service for a shard worker.
# The caller owns the Slurm allocation and supplies the exact model ID.

MODEL_ID="${MODEL_ID:-}"
VLLM_HOST="${VLLM_HOST:-127.0.0.1}"
VLLM_PORT="${VLLM_PORT:-8316}"
VLLM_LOG="${VLLM_LOG:-vllm.log}"
VLLM_PID_FILE="${VLLM_PID_FILE:-vllm.pid}"
VLLM_VISIBLE_DEVICES="${VLLM_VISIBLE_DEVICES:-}"
VLLM_TENSOR_PARALLEL_SIZE="${VLLM_TENSOR_PARALLEL_SIZE:-1}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-}"
VLLM_DTYPE="${VLLM_DTYPE:-}"
VLLM_EXTRA_ARGS_STRING="${VLLM_EXTRA_ARGS_STRING:-}"
VLLM_SPECULATIVE_CONFIG="${VLLM_SPECULATIVE_CONFIG:-}"

usage() {
    printf 'Usage: %s start|stop|wait|verify\n' "$0" >&2
}

require_model() {
    [ -n "$MODEL_ID" ] || {
        printf 'MODEL_ID is required\n' >&2
        exit 2
    }
}

endpoint() {
    printf 'http://%s:%s/v1' "$VLLM_HOST" "$VLLM_PORT"
}

read_pid() {
    if [ -f "$VLLM_PID_FILE" ]; then
        tr -d '[:space:]' <"$VLLM_PID_FILE"
    fi
}

start() {
    require_model
    if [ -f "$VLLM_PID_FILE" ]; then
        pid="$(read_pid)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            printf 'vLLM is already running: pid=%s endpoint=%s\n' "$pid" "$(endpoint)"
            return 0
        fi
        rm -f "$VLLM_PID_FILE"
    fi

    args=(serve "$MODEL_ID" --host "$VLLM_HOST" --port "$VLLM_PORT")
    if [ "$VLLM_TENSOR_PARALLEL_SIZE" != "1" ]; then
        args+=(--tensor-parallel-size "$VLLM_TENSOR_PARALLEL_SIZE")
    fi
    if [ -n "$VLLM_MAX_MODEL_LEN" ]; then
        args+=(--max-model-len "$VLLM_MAX_MODEL_LEN")
    fi
    if [ -n "$VLLM_DTYPE" ]; then
        args+=(--dtype "$VLLM_DTYPE")
    fi
    if [ -n "$VLLM_SPECULATIVE_CONFIG" ]; then
        args+=(--speculative-config "$VLLM_SPECULATIVE_CONFIG")
    fi
    if [ -n "$VLLM_EXTRA_ARGS_STRING" ]; then
        # This is an operator-controlled Slurm environment value, not model
        # output. Keep the launcher generic for model-specific vLLM options.
        mapfile -t extra_args < <(
            VLLM_EXTRA_ARGS_STRING="$VLLM_EXTRA_ARGS_STRING" python - <<'PY'
import os
import shlex

for value in shlex.split(os.environ["VLLM_EXTRA_ARGS_STRING"]):
    print(value)
PY
        )
        args+=("${extra_args[@]}")
    fi

    mkdir -p "$(dirname "$VLLM_LOG")" "$(dirname "$VLLM_PID_FILE")"
    if [ -n "$VLLM_VISIBLE_DEVICES" ]; then
        CUDA_VISIBLE_DEVICES="$VLLM_VISIBLE_DEVICES" \
            vllm "${args[@]}" >"$VLLM_LOG" 2>&1 &
    else
        vllm "${args[@]}" >"$VLLM_LOG" 2>&1 &
    fi
    echo "$!" >"$VLLM_PID_FILE"
    printf 'started vLLM pid=%s model=%s endpoint=%s\n' "$!" "$MODEL_ID" "$(endpoint)"
}

stop() {
    pid="$(read_pid)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    rm -f "$VLLM_PID_FILE"
}

wait_ready() {
    require_model
    timeout_seconds="${VLLM_READY_TIMEOUT_SECONDS:-1800}"
    started_at="$(date +%s)"
    while true; do
        if ! curl -fsS "$(endpoint)/models" >"${VLLM_MODELS_RESPONSE:-/dev/null}" 2>/dev/null; then
            pid="$(read_pid)"
            if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
                printf 'vLLM exited before becoming ready; inspect %s\n' "$VLLM_LOG" >&2
                return 1
            fi
        else
            printf 'vLLM endpoint is ready: %s\n' "$(endpoint)"
            return 0
        fi
        now="$(date +%s)"
        if [ $((now - started_at)) -ge "$timeout_seconds" ]; then
            printf 'vLLM did not become ready within %ss; inspect %s\n' "$timeout_seconds" "$VLLM_LOG" >&2
            return 1
        fi
        sleep 2
    done
}

verify() {
    require_model
    response="$(curl -fsS "$(endpoint)/models")"
    EXPECTED_MODEL_ID="$MODEL_ID" MODELS_RESPONSE="$response" python - <<'PY'
import json
import os

expected = os.environ["EXPECTED_MODEL_ID"]
data = json.loads(os.environ["MODELS_RESPONSE"])
served = {
    str(item.get("id"))
    for item in data.get("data", [])
    if isinstance(item, dict) and item.get("id")
}
if expected not in served:
    raise SystemExit(
        f"vLLM served model IDs {sorted(served)!r}; expected {expected!r}"
    )
print(f"verified vLLM model: {expected}")
PY
    if [ "${VLLM_VERIFY_CHAT:-1}" != "1" ]; then
        printf 'skipped vLLM chat verification by operator request\n'
        return 0
    fi
    payload="$(MODEL_ID="$MODEL_ID" python - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_ID"],
    "messages": [{"role": "user", "content": "Reply with the word READY."}],
    "max_tokens": 16,
    "chat_template_kwargs": {"enable_thinking": False},
}))
PY
    )"
    chat_response="$(curl -fsS "$(endpoint)/chat/completions" \
        -H 'Content-Type: application/json' \
        -d "$payload")"
    RESPONSE="$chat_response" python - <<'PY'
import json
import os

data = json.loads(os.environ["RESPONSE"])
choices = data.get("choices") or []
if not choices or not isinstance(choices[0], dict):
    raise SystemExit("vLLM chat completion returned no choices")
message = choices[0].get("message") or {}
if message.get("reasoning_content") or message.get("reasoning"):
    raise SystemExit("vLLM returned reasoning despite enable_thinking=false")
if not message.get("content"):
    raise SystemExit("vLLM chat completion returned no message content")
print("verified vLLM chat completion")
PY
}

command="${1:-}"
case "$command" in
    start) start ;;
    stop) stop ;;
    wait) wait_ready ;;
    verify) verify ;;
    *) usage; exit 2 ;;
esac
