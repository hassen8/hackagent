#!/bin/bash
#SBATCH --job-name=hackagent_campaign
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=04:00:00

set -euo pipefail

PROJECT_DIR="/hpc/home/ais/hsali/hackagent"
HERE="$PROJECT_DIR/experiments/campaign"
MANIFEST="${1:-}"

if [ -z "$MANIFEST" ]; then
    printf 'Usage: sbatch submit.slurm MANIFEST [runner options...]\n' >&2
    exit 2
fi
if [ ! -f "$MANIFEST" ]; then
    printf 'Manifest not found: %s\n' "$MANIFEST" >&2
    exit 1
fi

cd "$PROJECT_DIR"
LOG_DIR="$PROJECT_DIR/logs/campaign_gpu"
mkdir -p "$LOG_DIR"

module load cuda13.0/toolkit/13.0.2

export LITELLM_LOCAL_MODEL_COST_MAP="true"
export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MODELS="$HOME/models"
export OLLAMA_KEEP_ALIVE="-1"
export OLLAMA_MAX_LOADED_MODELS="4"
export OLLAMA_LOAD_TIMEOUT="10m"
export OLLAMA_NUM_PARALLEL="1"

printf 'Manifest: %s\nJob: %s\nNode: %s\nCommit: %s\n' \
    "$MANIFEST" "${SLURM_JOB_ID}" "$(hostname)" "$(git rev-parse HEAD)"
nvidia-smi

if [ ! -d "$OLLAMA_MODELS/blobs" ] || [ ! -d "$OLLAMA_MODELS/manifests" ]; then
    printf 'Ollama model store is missing or incomplete: %s\n' "$OLLAMA_MODELS" >&2
    exit 1
fi

ollama serve > "$LOG_DIR/ollama_${SLURM_JOB_ID}.log" 2>&1 &
OLLAMA_PID=$!
cleanup() {
    kill "$OLLAMA_PID" 2>/dev/null || true
    wait "$OLLAMA_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

for _ in $(seq 1 60); do
    curl -fsS "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1 && break
    sleep 2
curl -fsS "http://${OLLAMA_HOST}/api/version"
ollama list

OLLAMA_ENDPOINT="http://${OLLAMA_HOST}" \
    uv run python "$HERE/runner.py" --manifest "$MANIFEST" "${@:2}" \
    2>&1 | tee "$LOG_DIR/runner_${SLURM_JOB_ID}.log"

printf 'Final Ollama process state:\n'
ollama ps
