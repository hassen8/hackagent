#!/bin/bash
#SBATCH --job-name=attsel
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=04:00:00
#
# Runs an attacker-selection experiment script.
#
# There is deliberately NO hardcoded model preflight list here: model staging is
# the wrapper's job (submit.sh), driven by attackers.json. Adding a candidate
# therefore requires no edit to this file.
#
# Usage: sbatch experiments/attacker_selection/submit.slurm <script_name>

set -euo pipefail

SCRIPT_NAME="${1:-}"
PROJECT_DIR="/hpc/home/ais/hsali/hackagent"
EXP_DIR="$PROJECT_DIR/experiments/attacker_selection"

if [ -z "$SCRIPT_NAME" ]; then
    echo "Usage: sbatch submit.slurm <script_name>" >&2
    exit 2
fi

SCRIPT="$EXP_DIR/${SCRIPT_NAME}.py"
if [ ! -f "$SCRIPT" ]; then
    echo "Experiment script not found: $SCRIPT" >&2
    exit 1
fi

cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs/attacker_selection_gpu"
mkdir -p "$LOG_DIR"

module load cuda13.0/toolkit/13.0.2

export LITELLM_LOCAL_MODEL_COST_MAP="true"
export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MODELS="$HOME/models"
# Keep every role resident: evicting the attacker or judge mid-run would show up as
# a fake latency spike in exactly the metric we are trying to compare.
export OLLAMA_KEEP_ALIVE="-1"
export OLLAMA_MAX_LOADED_MODELS="4"
export OLLAMA_LOAD_TIMEOUT="10m"
export OLLAMA_NUM_PARALLEL="1"

echo "Script:   $SCRIPT_NAME"
echo "Job:      ${SLURM_JOB_ID}"
echo "Node:     $(hostname)"
echo "Commit:   $(git rev-parse HEAD)"
echo "CUDA_VISIBLE_DEVICES: ${CUDA_VISIBLE_DEVICES:-unset}"
nvidia-smi

if [ ! -d "$OLLAMA_MODELS/blobs" ] || [ ! -d "$OLLAMA_MODELS/manifests" ]; then
    echo "Ollama model store is missing or incomplete: $OLLAMA_MODELS" >&2
    exit 1
fi

# Job-local daemon. Never reuse the login node's shared daemon on :11434.
ollama serve > "$LOG_DIR/ollama_${SLURM_JOB_ID}.log" 2>&1 &
OLLAMA_PID=$!

cleanup() {
    kill "$OLLAMA_PID" 2>/dev/null || true
    wait "$OLLAMA_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

echo "Waiting for Ollama..."
for _ in $(seq 1 60); do
    curl -fsS "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1 && break
    sleep 2
done
curl -fsS "http://${OLLAMA_HOST}/api/version"
echo

echo "Installed models:"
ollama list

echo "Running $SCRIPT"
OLLAMA_ENDPOINT="http://${OLLAMA_HOST}" \
    uv run python "$SCRIPT" "${@:2}" 2>&1 | tee "$LOG_DIR/${SCRIPT_NAME}_${SLURM_JOB_ID}.log"

echo "Final Ollama process state:"
ollama ps
