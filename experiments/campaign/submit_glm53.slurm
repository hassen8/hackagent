#!/usr/bin/env bash
#SBATCH --job-name=hackagent_glm53
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:5
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=18:00:00

set -euo pipefail

# Model-specific campaign launcher. GLM vLLM owns four GPUs;
# the common worker starts GPU-backed Ollama on a separate fifth GPU.
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"

module load cuda13.0/toolkit/13.0.2
module load apptainer 2>/dev/null || true
APPTAINER_BIN="${APPTAINER_BIN:-$(command -v apptainer || true)}"
[ -x "$APPTAINER_BIN" ] || { printf 'apptainer is unavailable\n' >&2; exit 1; }
[ -f "$PYTHON_ENV" ] || { printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2; exit 1; }
source "$PYTHON_ENV"
command -v curl >/dev/null 2>&1 || { printf 'curl is unavailable\n' >&2; exit 1; }
command -v ollama >/dev/null 2>&1 || { printf 'ollama is unavailable\n' >&2; exit 1; }
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "$PROJECT_DIR"
MANIFEST="${1:-}"
shift || true
[ -n "$MANIFEST" ] || { printf 'manifest is required\n' >&2; exit 2; }

SHARD_ID=""
LIMIT=""
SERVICE_SMOKE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-id) SHARD_ID="${2:-}"; shift 2 ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [ -z "$SHARD_ID" ]; then
    [ -n "${SLURM_ARRAY_TASK_ID:-}" ] || {
        printf '%s\n' '--shard-id is required outside an array' >&2
        exit 2
    }
    SHARD_ID="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SLURM_ARRAY_TASK_ID" <<'PY'
import sys

from experiments.campaign.sharding import (
    load_shard_manifest,
    shard_id_for_array_index,
    validate_shard_manifest,
)

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
print(shard_id_for_array_index(manifest, int(sys.argv[2])))
PY
)"
fi

CAMPAIGN_ROOT="$(dirname "$MANIFEST")"
SHARD_DIR="$CAMPAIGN_ROOT/shards/$SHARD_ID"
mkdir -p "$SHARD_DIR/slurm" "$SHARD_DIR/runs"
LAUNCHER_LOG="$SHARD_DIR/slurm/launcher_${SLURM_JOB_ID}.log"
exec > >(tee -a "$LAUNCHER_LOG") 2>&1

SHARD_METADATA="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import json
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
entry = next((item for item in manifest["shards"] if item["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
if entry["attacker_key"] != "glm53_flash_abliterated_fp8":
    raise SystemExit("selected shard is not assigned to glm53_flash_abliterated_fp8")
print(json.dumps(entry, sort_keys=True))
PY
)"

exec 9>"$SHARD_DIR/slurm/worker.lock"
flock -n 9 || { printf 'shard %s is already owned by another worker\n' "$SHARD_ID" >&2; exit 1; }

MODEL_ID="dealignai/GLM-5.3-Flash-ABLITERATED-FP8"
SIF_IMAGE="${GLM53_SIF_IMAGE:-/hpc/home/ais/hsali/llm_images/vllm-openai-glm53-flash.sif}"
export MODEL_ID SIF_IMAGE
PORT_SEED="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-0}}"
ARRAY_TASK_INDEX="${SLURM_ARRAY_TASK_ID:-0}"
PORT_BASE=$((10000 + ((PORT_SEED * 100 + ARRAY_TASK_INDEX * 2) % 49998)))
OLLAMA_PORT="$PORT_BASE"
VLLM_PORT=$((PORT_BASE + 1))
export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export VLLM_ENDPOINT="http://127.0.0.1:$VLLM_PORT/v1"
export VLLM_HOST=127.0.0.1
export VLLM_PORT
export OLLAMA_ENDPOINT="http://127.0.0.1:$OLLAMA_PORT"
export OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
export HACKAGENT_AUTO_PULL_MODELS=0

ALLOCATED_GPU_DEVICES="${CUDA_VISIBLE_DEVICES:-}"
[ -n "$ALLOCATED_GPU_DEVICES" ] || {
    printf 'Slurm did not provide CUDA_VISIBLE_DEVICES\n' >&2
    exit 1
}
IFS=',' read -r -a ALLOCATED_GPUS <<<"$ALLOCATED_GPU_DEVICES"
[ "${#ALLOCATED_GPUS[@]}" -eq 5 ] || {
    printf 'expected five allocated B200 GPUs, got %s (%s)\n' \
        "${#ALLOCATED_GPUS[@]}" "$ALLOCATED_GPU_DEVICES" >&2
    exit 1
}
export VLLM_VISIBLE_DEVICES="${ALLOCATED_GPUS[0]},${ALLOCATED_GPUS[1]},${ALLOCATED_GPUS[2]},${ALLOCATED_GPUS[3]}"
export VLLM_VERIFY_CHAT=1
export VLLM_TENSOR_PARALLEL_SIZE=4
export VLLM_MAX_MODEL_LEN="${GLM53_MAX_MODEL_LEN:-32768}"
export VLLM_GPU_MEMORY_UTILIZATION="${GLM53_GPU_MEMORY_UTILIZATION:-0.80}"
export OLLAMA_VISIBLE_DEVICES="${ALLOCATED_GPUS[4]}"
export HACKAGENT_TARGET_KEY="qwen38_27b"
export HACKAGENT_ATTACKER_KEYS="glm53_flash_abliterated_fp8"
export HACKAGENT_SKIP_CAMPAIGN_WARMUP=1
VLLM_LOG="$SHARD_DIR/slurm/vllm_${SLURM_JOB_ID}.log"
VLLM_PID_FILE="$SHARD_DIR/slurm/vllm_${SLURM_JOB_ID}.pid"
APPTAINER_TMPDIR="$SHARD_DIR/slurm/apptainer-tmp_${SLURM_JOB_ID}"
APPTAINER_CACHEDIR="$SHARD_DIR/slurm/apptainer-cache_${SLURM_JOB_ID}"
mkdir -p "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"

printf 'shard=%s allocated_gpus=%s vllm_gpus=%s ollama_gpu=%s\n' \
    "$SHARD_ID" "$ALLOCATED_GPU_DEVICES" "$VLLM_VISIBLE_DEVICES" \
    "$OLLAMA_VISIBLE_DEVICES"
printf 'model=%s image=%s vllm_endpoint=%s ollama_endpoint=%s\n' \
    "$MODEL_ID" "$SIF_IMAGE" "$VLLM_ENDPOINT" "$OLLAMA_ENDPOINT"
printf 'vllm_max_model_len=%s vllm_gpu_memory_utilization=%s\n' \
    "$VLLM_MAX_MODEL_LEN" "$VLLM_GPU_MEMORY_UTILIZATION"

VLLM_PID=""
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
        terminate_tree "$VLLM_PID"
        wait "$VLLM_PID" 2>/dev/null || true
        sleep 1
        kill -KILL "$VLLM_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

SPECULATIVE_CONFIG='{\"method\":\"mtp\",\"num_speculative_tokens\":5}'
APPTAINER_MESSAGELEVEL=5 CUDA_VISIBLE_DEVICES="$VLLM_VISIBLE_DEVICES" "$APPTAINER_BIN" -d run --nv \
    --bind "$HF_HOME:/root/.cache/huggingface" \
    --env "HF_HOME=/root/.cache/huggingface" \
    --env "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
    --env "CUDA_VISIBLE_DEVICES=$VLLM_VISIBLE_DEVICES" \
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
    --max-model-len "$VLLM_MAX_MODEL_LEN" \
    --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
    --host 127.0.0.1 \
    --port "$VLLM_PORT" \
    >"$VLLM_LOG" 2>&1 &
VLLM_PID=$!

ready=0
for _ in $(seq 1 720); do
    if curl -fsS --connect-timeout 5 --max-time 30 "$VLLM_ENDPOINT/models" >/dev/null 2>/dev/null; then
        ready=1
        break
    fi
    kill -0 "$VLLM_PID" 2>/dev/null || break
    sleep 5
done
[ "$ready" -eq 1 ] || { printf 'GLM vLLM did not become ready; inspect %s\n' "$VLLM_LOG" >&2; exit 1; }

VLLM_MODELS_JSON="$(curl -fsS --connect-timeout 5 --max-time 30 "$VLLM_ENDPOINT/models")"
MODEL_ID="$MODEL_ID" MODELS_JSON="$VLLM_MODELS_JSON" "$PYTHON_BIN" - <<'PY'
import json
import os

expected = os.environ["MODEL_ID"]
data = json.loads(os.environ["MODELS_JSON"])
served = {str(item.get("id")) for item in data.get("data", []) if isinstance(item, dict) and item.get("id")}
if expected not in served:
    raise SystemExit(f"served model IDs {sorted(served)!r}; expected {expected!r}")
print(f"verified GLM model ID: {expected}")
PY

PROBE_RESPONSE_JSON="$(
    MODEL_ID="$MODEL_ID" "$PYTHON_BIN" - <<'PY' |
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_ID"],
    "messages": [{"role": "user", "content": "Reply with the word READY."}],
    "max_tokens": 16,
    "chat_template_kwargs": {"enable_thinking": False},
}))
PY
    curl -fsS --connect-timeout 10 --max-time 900 \
        -H 'Content-Type: application/json' \
        -d @- \
        "$VLLM_ENDPOINT/chat/completions"
)"
MODEL_ID="$MODEL_ID" RESPONSE_JSON="$PROBE_RESPONSE_JSON" "$PYTHON_BIN" - <<'PY'
import json
import os

expected = os.environ["MODEL_ID"]
data = json.loads(os.environ["RESPONSE_JSON"])
if data.get("model") != expected:
    raise SystemExit(f"response model {data.get('model')!r} does not match {expected!r}")
choices = data.get("choices") or []
if not choices or not isinstance(choices[0], dict):
    raise SystemExit("GLM campaign readiness probe returned no choices")
message = choices[0].get("message") or {}
if message.get("reasoning") or message.get("reasoning_content"):
    raise SystemExit("GLM returned reasoning despite enable_thinking=false")
content = message.get("content")
if not isinstance(content, str) or not content.strip():
    raise SystemExit("GLM campaign readiness probe returned no message content")
print(f"verified no-thinking GLM campaign probe: {content.strip()!r}")
PY

WORKER_ARGS=("$MANIFEST" --shard-id "$SHARD_ID")
[ -n "$LIMIT" ] && WORKER_ARGS+=(--limit "$LIMIT")
[ "$SERVICE_SMOKE" -eq 1 ] && WORKER_ARGS+=(--service-smoke)
PROJECT_DIR="$PROJECT_DIR" SHARD_LOCK_ALREADY_HELD=1 \
    OLLAMA_ENDPOINT="$OLLAMA_ENDPOINT" OLLAMA_HOST="$OLLAMA_HOST" \
    OLLAMA_VISIBLE_DEVICES="$OLLAMA_VISIBLE_DEVICES" \
    bash "$SCRIPT_DIR/run_campaign_worker.slurm" "${WORKER_ARGS[@]}"
