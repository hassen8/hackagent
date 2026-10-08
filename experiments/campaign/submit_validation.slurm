#!/usr/bin/env bash
#SBATCH --job-name=hackagent_r5_validation
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=12:00:00
set -euo pipefail
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
INPUT="${1:-}"; OUTPUT="${2:-}"
[ -n "$INPUT" ] && [ -n "$OUTPUT" ] || { printf 'Usage: %s INPUT RESULTS_DIR\n' "$0" >&2; exit 2; }
cd "$PROJECT_DIR"
mkdir -p "$OUTPUT/slurm"
PORT=$((11000 + ((${SLURM_JOB_ID:-0} % 40000))))
export OLLAMA_HOST="127.0.0.1:$PORT"
export OLLAMA_ENDPOINT="http://$OLLAMA_HOST"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=-1
export OLLAMA_LOAD_TIMEOUT=30m
export HACKAGENT_AUTO_PULL_MODELS=0
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null || true; [ -n "$PID" ] && wait "$PID" 2>/dev/null || true; }
trap cleanup EXIT TERM INT
[ -d "$OLLAMA_MODELS/blobs" ] && [ -d "$OLLAMA_MODELS/manifests" ] || { printf 'missing Ollama model store\n' >&2; exit 1; }
command -v ollama >/dev/null || { printf 'ollama unavailable\n' >&2; exit 1; }
command -v curl >/dev/null || { printf 'curl unavailable\n' >&2; exit 1; }
ollama serve >"$OUTPUT/slurm/ollama_${SLURM_JOB_ID:-0}.log" 2>&1 & PID=$!
for _ in $(seq 1 60); do curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null
for model in gemma4:12b ministral-3:14b llama3.1:8b; do ollama list | awk 'NR>1 {print $1}' | grep -Fx "$model" >/dev/null || { printf 'missing exact model: %s\n' "$model" >&2; exit 1; }; done
uv run --no-sync python experiments/campaign/judge_validation.py --input "$INPUT" --output "$OUTPUT" --endpoint "$OLLAMA_ENDPOINT" >"$OUTPUT/slurm/validation_${SLURM_JOB_ID:-0}.log"
