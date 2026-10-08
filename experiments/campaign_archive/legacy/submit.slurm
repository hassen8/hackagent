#!/usr/bin/env bash
#SBATCH --job-name=hackagent_campaign
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=04:00:00

set -euo pipefail

# W10 GPU-node boundary.
#
# Models must already be present in OLLAMA_MODELS.  This job starts a private
# Ollama service, enforces no-pull execution, and invokes the unchanged
# campaign.py --execute --matrix interface.  It does not stage models, add
# attack-specific logic, or maintain a second result ledger.
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CAMPAIGN_NAME="${HACKAGENT_CAMPAIGN_NAME:-omnisafebench_af340_attackers_vs_gemma3_4b}"
LOG_DIR="${HACKAGENT_SLURM_LOG_DIR:-$PROJECT_DIR/logs/campaigns/$CAMPAIGN_NAME/slurm}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11435}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"

if [ "$#" -eq 0 ] && [ "${HACKAGENT_ALLOW_FULL_MATRIX:-0}" != "1" ]; then
    printf 'an unfiltered matrix requires --allow-full-matrix through submit.sh\n' >&2
    exit 2
fi

# Validate the staged store before starting a timed attack job.  A missing
# store is an operational readiness failure, not an opportunity for Ollama to
# download a model during measured execution.
cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR"

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

case "$OLLAMA_HOST" in
    http://*|https://*) OLLAMA_ENDPOINT="${OLLAMA_HOST%/}" ;;
    *) OLLAMA_ENDPOINT="http://$OLLAMA_HOST" ;;
esac

export OLLAMA_HOST
export OLLAMA_MODELS
export OLLAMA_ENDPOINT
export OLLAMA_LOAD_TIMEOUT
export OLLAMA_CONTEXT_LENGTH
export HACKAGENT_AUTO_PULL_MODELS=0

# Keep the Ollama and campaign runner logs under the named campaign.  The
# cleanup trap below stops the job-local server on every exit path.
OLLAMA_LOG="$LOG_DIR/ollama_${SLURM_JOB_ID:-unknown}.log"
RUN_LOG="$LOG_DIR/runner_${SLURM_JOB_ID:-unknown}.log"
OLLAMA_PID=""

cleanup() {
    if [ -n "$OLLAMA_PID" ]; then
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

command -v ollama >/dev/null 2>&1 || {
    printf 'ollama executable is unavailable on the GPU node\n' >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || {
    printf 'curl executable is unavailable on the GPU node\n' >&2
    exit 1
}

# Start Ollama only after all readiness checks have passed, then poll the
# private endpoint before handing control to campaign.py.
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

# The selectors and retry/limit flags supplied by submit.sh are forwarded
# unchanged.  campaign.py owns matrix filtering, resume state, and harvesting.
uv run --no-sync python experiments/campaign/campaign.py \
    --execute --matrix "${@}" 2>&1 | tee "$RUN_LOG"
