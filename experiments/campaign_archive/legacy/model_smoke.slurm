#!/usr/bin/env bash
#SBATCH --job-name=ollama_model_smoke
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:2
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=01:00:00

set -euo pipefail

# GPU-node-only model compatibility smoke.  This script intentionally does not
# run on the login node, pull models, invoke the campaign runner, or write
# campaign JSONL/CSV records.  It starts Ollama locally inside the allocation
# and exercises the same HTTP boundary used by the campaign runtime.
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL="${1:?usage: sbatch model_smoke.slurm '<exact-model-id>'}"
LOG_DIR="${MODEL_SMOKE_LOG_DIR:-$PROJECT_DIR/logs/model_smoke}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
PORT="${OLLAMA_PORT:-$((11435 + (${SLURM_JOB_ID:-0} % 1000)))}"
OLLAMA_HOST="127.0.0.1:${PORT}"
OLLAMA_ENDPOINT="http://${OLLAMA_HOST}"
JOB_ID="${SLURM_JOB_ID:-local}"
OLLAMA_LOG="$LOG_DIR/ollama_${JOB_ID}.log"
RESULT_LOG="$LOG_DIR/result_${JOB_ID}.json"
SHOW_LOG="$LOG_DIR/show_${JOB_ID}.txt"
OLLAMA_PID=""

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

if [ "${HACKAGENT_LOAD_CUDA_MODULE:-1}" = "1" ]; then
    type module >/dev/null 2>&1 || {
        printf 'CUDA module loading was requested but module is unavailable\n' >&2
        exit 1
    }
    module load cuda13.0/toolkit/13.0.2
fi

if [ ! -d "$OLLAMA_MODELS/blobs" ] || [ ! -d "$OLLAMA_MODELS/manifests" ]; then
    printf 'Ollama model store is missing or incomplete: %s\n' "$OLLAMA_MODELS" >&2
    exit 1
fi

command -v ollama >/dev/null 2>&1 || {
    printf 'ollama executable is unavailable on the GPU node\n' >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || {
    printf 'curl executable is unavailable on the GPU node\n' >&2
    exit 1
}

export HACKAGENT_AUTO_PULL_MODELS=0
export OLLAMA_HOST
export OLLAMA_MODELS
export OLLAMA_KEEP_ALIVE=-1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"

cleanup() {
    if [ -n "$OLLAMA_PID" ]; then
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

ollama serve >"$OLLAMA_LOG" 2>&1 &
OLLAMA_PID=$!

ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    printf 'job-local Ollama did not become ready at %s\n' "$OLLAMA_ENDPOINT" >&2
    exit 1
fi

ollama show "$MODEL" >"$SHOW_LOG"

PAYLOAD="$(MODEL="$MODEL" python - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL"],
    "prompt": "Reply with exactly: OLLAMA_MODEL_SMOKE_OK",
    "think": False,
    "stream": False,
    "options": {"num_predict": 32, "temperature": 0},
}))
PY
)"
curl -fsS "$OLLAMA_ENDPOINT/api/generate" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" >"$RESULT_LOG"

MODEL="$MODEL" RESULT_LOG="$RESULT_LOG" python - <<'PY'
import json
import os
from pathlib import Path

result = json.loads(Path(os.environ["RESULT_LOG"]).read_text(encoding="utf-8"))
if result.get("error"):
    raise SystemExit(f"Ollama returned an error: {result['error']}")
if not isinstance(result.get("response"), str) or not result["response"].strip():
    raise SystemExit("Ollama returned no text response")
print(json.dumps({
    "model": os.environ["MODEL"],
    "response": result["response"],
    "done": result.get("done"),
    "prompt_eval_count": result.get("prompt_eval_count"),
    "eval_count": result.get("eval_count"),
}, sort_keys=True))
PY

printf 'model=%s\nendpoint=%s\nshow=%s\nresult=%s\n' \
    "$MODEL" "$OLLAMA_ENDPOINT" "$SHOW_LOG" "$RESULT_LOG"
ollama ps
