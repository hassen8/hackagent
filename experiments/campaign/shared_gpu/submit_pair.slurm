#!/usr/bin/env bash
#SBATCH --job-name=hackagent_shared_gpu
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=10:00:00

set -euo pipefail

PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
CAMPAIGN_DIR="$PROJECT_DIR/experiments/campaign"
SHARED_DIR="$CAMPAIGN_DIR/shared_gpu"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
MANIFEST="${1:-}"
shift || true
SHARD_A=""
SHARD_B=""
LIMIT=""
RETRY_FAILED=0
RETRY_INCOMPLETE=0
SERVICE_SMOKE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-a) SHARD_A="$2"; shift 2 ;;
        --shard-b) SHARD_B="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] && [ -n "$SHARD_A" ] && [ -n "$SHARD_B" ] || {
    printf 'manifest, --shard-a, and --shard-b are required\n' >&2
    exit 2
}
[ -f "$PYTHON_ENV" ] || { printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2; exit 1; }
source "$PYTHON_ENV"
module load cuda13.0/toolkit/13.0.2
command -v ollama >/dev/null 2>&1 || { printf 'ollama is unavailable\n' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf 'curl is unavailable\n' >&2; exit 1; }
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

PAIR_METADATA="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_A" "$SHARD_B" <<'PY'
import json
import sys
from pathlib import Path

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
shards = {
    shard["shard_id"]: shard
    for shard in manifest["shards"]
    if shard["shard_id"] in {sys.argv[2], sys.argv[3]}
}
if len(shards) != 2:
    raise SystemExit("both pair shards must be present")
first, second = (shards[sys.argv[2]], shards[sys.argv[3]])
if first.get("attacker_key") != second.get("attacker_key"):
    raise SystemExit("pair shards must have the same attacker")
if set(first["cell_ids"]) & set(second["cell_ids"]):
    raise SystemExit("pair shards overlap")
attacker = (first.get("serving_requirements") or {}).get("roles", {}).get("attacker", {})
resources = (first.get("serving_requirements") or {}).get("resources", {}).get("attacker", {})
if attacker.get("serving_framework") != "ollama" or int(resources.get("worker_gpu_count", 0)) != 1:
    raise SystemExit("pair requires a one-GPU Ollama attacker")
print(json.dumps({
    "campaign_name": manifest["campaign_name"],
    "execution_scope_id": manifest["execution_scope_id"],
    "target_key": manifest["target_key"],
    "attacker_keys": manifest["attacker_keys"],
    "attacker_key": first["attacker_key"],
    "attacker_model": attacker["identifier"],
    "target_model": manifest["target_model"],
    "judge_model": manifest["judge_model"],
}))
PY
)"

export HACKAGENT_CAMPAIGN_NAME="$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["campaign_name"])
PY
)"
export HACKAGENT_EXECUTION_SCOPE_ID="$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["execution_scope_id"])
PY
)"
export HACKAGENT_TARGET_KEY="$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["target_key"])
PY
)"
export HACKAGENT_ATTACKER_KEYS="$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["attacker_key"])
PY
)"
export HACKAGENT_SEPARATE_OLLAMA_ATTACKER=0

CAMPAIGN_ROOT="$(dirname "$MANIFEST")"
SHARED_ROOT="$CAMPAIGN_ROOT/shared_gpu"
mkdir -p "$SHARED_ROOT"
LAUNCHER_LOG="$SHARED_ROOT/launcher_${SLURM_JOB_ID}.log"
exec > >(tee -a "$LAUNCHER_LOG") 2>&1

PORT_BASE=$((12000 + (SLURM_JOB_ID % 20000)))
export OLLAMA_HOST="127.0.0.1:$PORT_BASE"
export OLLAMA_ENDPOINT="http://$OLLAMA_HOST"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
export OLLAMA_NUM_PARALLEL=2
export OLLAMA_MAX_LOADED_MODELS=3
export OLLAMA_KEEP_ALIVE=-1
export HACKAGENT_AUTO_PULL_MODELS=0

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}" ollama serve >"$SHARED_ROOT/ollama_${SLURM_JOB_ID}.log" 2>&1 &
OLLAMA_PID=$!
cleanup() {
    kill "$OLLAMA_PID" 2>/dev/null || true
    wait "$OLLAMA_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

ready=0
for _ in $(seq 1 90); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || { printf 'shared Ollama did not become ready\n' >&2; exit 1; }
if ! grep -q "library=CUDA" "$SHARED_ROOT/ollama_${SLURM_JOB_ID}.log"; then
    printf 'Ollama CUDA backend not discovered; aborting to avoid Vulkan CLIP segfault\n' >&2
    exit 1
fi

for model in \
    "$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["target_model"])
PY
)" \
    "$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["judge_model"])
PY
)" \
    "$(python - "$PAIR_METADATA" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["attacker_model"])
PY
)"; do
    curl -fsS -X POST "$OLLAMA_ENDPOINT/api/show" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"$model\"}" >/dev/null
    curl -fsS -X POST "$OLLAMA_ENDPOINT/api/generate" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$model\",\"prompt\":\"Reply with READY.\",\"stream\":false,\"options\":{\"num_predict\":1}}" >/dev/null
    printf 'verified shared model=%s\n' "$model"
done

if [ "$SERVICE_SMOKE" -eq 1 ]; then
    printf 'shared service smoke passed\n'
    exit 0
fi

LOCK_A="$CAMPAIGN_ROOT/shards/$SHARD_A/slurm/worker.lock"
LOCK_B="$CAMPAIGN_ROOT/shards/$SHARD_B/slurm/worker.lock"
mkdir -p "$(dirname "$LOCK_A")" "$(dirname "$LOCK_B")"
exec 9>"$LOCK_A"
exec 10>"$LOCK_B"
flock -n 9 || { printf 'shard %s is already owned by another worker\n' "$SHARD_A" >&2; exit 1; }
flock -n 10 || { printf 'shard %s is already owned by another worker\n' "$SHARD_B" >&2; exit 1; }

run_worker() {
    local shard_id="$1"
    local worker_log="$SHARED_ROOT/worker_${SLURM_JOB_ID}_${shard_id}.log"
    local worker_args=(
        --execute
        --matrix
        --shard-manifest "$MANIFEST"
        --shard-id "$shard_id"
    )
    [ -n "$LIMIT" ] && worker_args+=(--limit "$LIMIT")
    [ "$RETRY_FAILED" -eq 1 ] && worker_args+=(--retry-failed)
    [ "$RETRY_INCOMPLETE" -eq 1 ] && worker_args+=(--retry-incomplete)
    HACKAGENT_DB="$CAMPAIGN_ROOT/shards/$shard_id/hackagent.db" \
    OLLAMA_ENDPOINT="$OLLAMA_ENDPOINT" \
    HACKAGENT_SKIP_CAMPAIGN_WARMUP=1 \
    uv run --no-sync python "$CAMPAIGN_DIR/campaign.py" \
        "${worker_args[@]}" \
        >"$worker_log" 2>&1
}

run_worker "$SHARD_A" &
PID_A=$!
run_worker "$SHARD_B" &
PID_B=$!

status=0
wait "$PID_A" || status=1
wait "$PID_B" || status=1
printf 'shared-GPU pair completed shard_a=%s shard_b=%s status=%s\n' \
    "$SHARD_A" "$SHARD_B" "$status"
exit "$status"
