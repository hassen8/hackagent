#!/usr/bin/env bash
#SBATCH --job-name=hackagent_deepseek
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:5
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=18:00:00

set -euo pipefail

PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"

module load cuda13.0/toolkit/13.0.2
[ -f "$PYTHON_ENV" ] || { printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2; exit 1; }
source "$PYTHON_ENV"
cd "$PROJECT_DIR"

MANIFEST="${1:-}"
shift || true
[ -n "$MANIFEST" ] || { printf 'manifest is required\n' >&2; exit 2; }

SHARD_ID=""
LIMIT=""
SERVICE_SMOKE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-id) SHARD_ID="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
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
entry = next(item for item in manifest["shards"] if item["shard_id"] == sys.argv[2])
if entry["attacker_key"] != "deepseek_v4_flash_0731__crack_nvfp4":
    raise SystemExit("selected shard is not assigned to deepseek_v4_flash_0731__crack_nvfp4")
print(json.dumps(entry, sort_keys=True))
PY
)"
exec 9>"$SHARD_DIR/slurm/worker.lock"
flock -n 9 || { printf 'shard %s is already owned by another worker\n' "$SHARD_ID" >&2; exit 1; }

MODEL_ID="dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4"
export MODEL_ID
PORT_SEED="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-0}}"
ARRAY_TASK_INDEX="${SLURM_ARRAY_TASK_ID:-0}"
PORT_BASE=$((10000 + ((PORT_SEED * 100 + ARRAY_TASK_INDEX * 2) % 49998)))
VLLM_PORT=$((PORT_BASE + 1))
export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export VLLM_ENDPOINT="http://127.0.0.1:$VLLM_PORT/v1"
export VLLM_HOST=127.0.0.1
export VLLM_PORT
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
export VLLM_LOG="$SHARD_DIR/slurm/vllm_${SLURM_JOB_ID}.log"
export VLLM_PID_FILE="$SHARD_DIR/slurm/vllm_${SLURM_JOB_ID}.pid"
export VLLM_EXTRA_ARGS_STRING='--enable-expert-parallel --distributed-executor-backend mp --kv-cache-dtype fp8 --block-size 256 --enable-prefix-caching --max-model-len 393216 --gpu-memory-utilization 0.80 --reasoning-parser deepseek_v4 --tool-call-parser deepseek_v4 --enable-auto-tool-choice'
export VLLM_SPECULATIVE_CONFIG='{"method":"dspark","num_speculative_tokens":5}'
export OLLAMA_ENDPOINT="http://127.0.0.1:$((VLLM_PORT - 1))"
export OLLAMA_HOST="127.0.0.1:$((VLLM_PORT - 1))"
export OLLAMA_VISIBLE_DEVICES="${ALLOCATED_GPUS[4]}"
export HACKAGENT_TARGET_KEY="qwen38_27b"
export HACKAGENT_ATTACKER_KEYS="deepseek_v4_flash_0731__crack_nvfp4"
export HACKAGENT_SKIP_CAMPAIGN_WARMUP=1

printf 'shard=%s allocated_gpus=%s vllm_gpus=%s ollama_gpu=%s\n' \
    "$SHARD_ID" "$ALLOCATED_GPU_DEVICES" "$VLLM_VISIBLE_DEVICES" \
    "$OLLAMA_VISIBLE_DEVICES"

bash "$SCRIPT_DIR/serve_vllm.sh" start
VLLM_STARTED=1
cleanup() {
    if [ "$VLLM_STARTED" -eq 1 ]; then
        MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_LOG="$VLLM_LOG" VLLM_PID_FILE="$VLLM_PID_FILE" VLLM_VERIFY_CHAT="$VLLM_VERIFY_CHAT" \
            bash "$SCRIPT_DIR/serve_vllm.sh" stop || true
    fi
}
trap cleanup EXIT TERM INT
bash "$SCRIPT_DIR/serve_vllm.sh" wait
MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_VERIFY_CHAT="$VLLM_VERIFY_CHAT" \
    bash "$SCRIPT_DIR/serve_vllm.sh" verify

WORKER_ARGS=("$MANIFEST" --shard-id "$SHARD_ID")
[ -n "$LIMIT" ] && WORKER_ARGS+=(--limit "$LIMIT")
[ "$SERVICE_SMOKE" -eq 1 ] && WORKER_ARGS+=(--service-smoke)
PROJECT_DIR="$PROJECT_DIR" SHARD_LOCK_ALREADY_HELD=1 OLLAMA_ENDPOINT="$OLLAMA_ENDPOINT" OLLAMA_VISIBLE_DEVICES="$OLLAMA_VISIBLE_DEVICES" \
    bash "$SCRIPT_DIR/run_campaign_worker.slurm" "${WORKER_ARGS[@]}"
