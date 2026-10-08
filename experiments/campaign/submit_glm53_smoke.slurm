#!/usr/bin/env bash
#SBATCH --job-name=glm53_smoke
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:4
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=01:00:00

set -euo pipefail

# Standalone container smoke. It deliberately does not invoke campaign.py or
# Ollama; the later dual-service smoke owns that separate resource contract.
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL_ID="${1:-dealignai/GLM-5.3-Flash-ABLITERATED-FP8}"
SIF_IMAGE="${2:-/hpc/home/ais/hsali/llm_images/vllm-openai-glm53-flash.sif}"
OUTPUT_ROOT="${3:-$PROJECT_DIR/logs/vllm_smoke/glm53_flash_abliterated_fp8}"

module load cuda13.0/toolkit/13.0.2
module load apptainer 2>/dev/null || true
APPTAINER_BIN="${APPTAINER_BIN:-$(command -v apptainer || true)}"
[ -x "$APPTAINER_BIN" ] || {
    printf 'apptainer is unavailable after module initialization\n' >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || { printf 'curl is unavailable\n' >&2; exit 1; }
PYTHON_BIN="${PYTHON_BIN:-python}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    printf 'Python interpreter is unavailable: %s\n' "$PYTHON_BIN" >&2
    exit 1
}

[ -f "$SIF_IMAGE" ] || { printf 'Apptainer image is unavailable: %s\n' "$SIF_IMAGE" >&2; exit 1; }
cd "$PROJECT_DIR"

JOB_ID="${SLURM_JOB_ID:-local}"
OUTPUT_DIR="$OUTPUT_ROOT/$JOB_ID"
mkdir -p "$OUTPUT_DIR"
exec > >(tee "$OUTPUT_DIR/job.log") 2>&1

HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
API_PORT="${API_PORT:-$((18000 + (${SLURM_JOB_ID:-0} % 400)))}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-3600}"
# Apptainer's runscript adds one shell boundary before vLLM parses this value;
# retain escaped quotes so the container receives valid JSON.
SPECULATIVE_CONFIG='{\"method\":\"mtp\",\"num_speculative_tokens\":5}'
ENDPOINT="http://127.0.0.1:$API_PORT/v1"
VLLM_PID=""
APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-$OUTPUT_DIR/apptainer-tmp}"
APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$OUTPUT_DIR/apptainer-cache}"
mkdir -p "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"

export HF_HOME HF_HUB_OFFLINE MODEL_ID SIF_IMAGE API_PORT ENDPOINT
export APPTAINER_TMPDIR APPTAINER_CACHEDIR

IFS=',' read -r -a ALLOCATED_GPUS <<<"${CUDA_VISIBLE_DEVICES:-}"
[ "${#ALLOCATED_GPUS[@]}" -eq 4 ] || {
    printf 'expected four allocated B200 GPUs, got %s (%s)\n' \
        "${#ALLOCATED_GPUS[@]}" "${CUDA_VISIBLE_DEVICES:-unset}" >&2
    exit 1
}

printf 'model=%s\n' "$MODEL_ID"
printf 'image=%s\n' "$SIF_IMAGE"
printf 'apptainer=%s\n' "$APPTAINER_BIN"
printf 'apptainer_version=%s\n' "$("$APPTAINER_BIN" --version 2>&1 || true)"
printf 'node=%s\n' "$(hostname -s)"
printf 'cuda_visible_devices=%s\n' "${CUDA_VISIBLE_DEVICES:-unset}"
printf 'api_endpoint=%s\n' "$ENDPOINT"
printf 'output_dir=%s\n' "$OUTPUT_DIR"

terminate_tree() {
    local pid="$1"
    local child
    [ -n "$pid" ] || return 0
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        terminate_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
    if [ -n "$VLLM_PID" ] && kill -0 "$VLLM_PID" 2>/dev/null; then
        printf 'stopping Apptainer process tree pid=%s\n' "$VLLM_PID"
        terminate_tree "$VLLM_PID"
        wait "$VLLM_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$VLLM_PID" 2>/dev/null; then
            kill -KILL "$VLLM_PID" 2>/dev/null || true
            wait "$VLLM_PID" 2>/dev/null || true
        fi
    fi
}
trap cleanup EXIT TERM INT

START_TIME="$(date +%s)"
APPTAINER_MESSAGELEVEL=5 "$APPTAINER_BIN" -d run --nv \
    --bind "$HF_HOME:/root/.cache/huggingface" \
    --env "HF_HOME=/root/.cache/huggingface" \
    --env "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
    --env VLLM_ENGINE_READY_TIMEOUT_S=3600 \
    "$SIF_IMAGE" \
    --model "$MODEL_ID" \
    --enable-expert-parallel \
    --tensor-parallel-size 4 \
    --kv-cache-dtype fp8 \
    --speculative-config "$SPECULATIVE_CONFIG" \
    --tool-call-parser glm47 \
    --reasoning-parser glm45 \
    --enable-auto-tool-choice \
    --distributed-executor-backend mp \
    --host 127.0.0.1 \
    --port "$API_PORT" \
    >"$OUTPUT_DIR/vllm.log" 2>&1 &
VLLM_PID=$!
printf 'started Apptainer process pid=%s\n' "$VLLM_PID"

ready=0
started_at="$(date +%s)"
while true; do
    if curl -fsS --connect-timeout 5 --max-time 30 "$ENDPOINT/models" >"$OUTPUT_DIR/models.json" 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        printf 'Apptainer vLLM exited before becoming ready; inspect %s\n' \
            "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    now="$(date +%s)"
    if [ $((now - started_at)) -ge "$READY_TIMEOUT_SECONDS" ]; then
        printf 'GLM vLLM did not become ready within %ss; inspect %s\n' \
            "$READY_TIMEOUT_SECONDS" "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    sleep 5
done

[ "$ready" -eq 1 ] || { printf 'GLM vLLM readiness failed\n' >&2; exit 1; }
END_TIME="$(date +%s)"
printf 'vLLM is ready; model_load_seconds=%s\n' "$((END_TIME - START_TIME))"

MODEL_ID="$MODEL_ID" MODELS_FILE="$OUTPUT_DIR/models.json" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

expected = os.environ["MODEL_ID"]
data = json.loads(Path(os.environ["MODELS_FILE"]).read_text(encoding="utf-8"))
served = {
    str(item.get("id"))
    for item in data.get("data", [])
    if isinstance(item, dict) and item.get("id")
}
if expected not in served:
    raise SystemExit(f"served model IDs {sorted(served)!r}; expected {expected!r}")
print(f"verified GLM model ID: {expected}")
PY

MODEL_ID="$MODEL_ID" "$PYTHON_BIN" - <<'PY' >"$OUTPUT_DIR/request.json"
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_ID"],
    "messages": [{"role": "user", "content": "Reply with the word READY."}],
    "max_tokens": 16,
    "chat_template_kwargs": {"enable_thinking": False},
}))
PY

REQUEST_START="$(date +%s)"
curl -fsS --connect-timeout 10 --max-time 900 \
    -H 'Content-Type: application/json' \
    -d @"$OUTPUT_DIR/request.json" \
    "$ENDPOINT/chat/completions" >"$OUTPUT_DIR/chat_response.json"
REQUEST_END="$(date +%s)"

MODEL_ID="$MODEL_ID" RESPONSE_FILE="$OUTPUT_DIR/chat_response.json" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

expected = os.environ["MODEL_ID"]
data = json.loads(Path(os.environ["RESPONSE_FILE"]).read_text(encoding="utf-8"))
if data.get("model") != expected:
    raise SystemExit(f"response model {data.get('model')!r} does not match {expected!r}")
choices = data.get("choices") or []
if not choices or not isinstance(choices[0], dict):
    raise SystemExit("GLM chat completion returned no choices")
message = choices[0].get("message") or {}
if message.get("reasoning") or message.get("reasoning_content"):
    raise SystemExit("GLM returned reasoning despite enable_thinking=false")
content = message.get("content")
if not isinstance(content, str) or not content.strip():
    raise SystemExit("GLM chat completion returned no message content")
print(f"verified no-thinking GLM response: {content.strip()!r}")
PY

printf 'request_seconds=%s\n' "$((REQUEST_END - REQUEST_START))"
printf 'GLM-5.3 standalone Apptainer smoke passed.\n'
