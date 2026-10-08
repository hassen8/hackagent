#!/usr/bin/env bash
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:2
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=15:00:00
set -euo pipefail

PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
MANIFEST="$1"
shift
SHARD_A=""
SHARD_B=""
LIMIT=""
SERVICE_SMOKE=0
RETRY_FAILED=0
RETRY_INCOMPLETE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-a) SHARD_A="$2"; shift 2 ;;
        --shard-b) SHARD_B="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        *) exit 2 ;;
    esac
done

[ -n "$SHARD_A" ] && [ -n "$SHARD_B" ] || exit 2
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
module load cuda13.0/toolkit/13.0.2
source "${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

ROOT="$(dirname "$MANIFEST")"
SHARED="$ROOT/shared_gpu"
mkdir -p "$SHARED"
exec > >(tee -a "$SHARED/launcher_${SLURM_JOB_ID}.log") 2>&1

META="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_A" "$SHARD_B" <<'PY'
import json
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
shards = {item["shard_id"]: item for item in manifest["shards"]}
first, second = shards[sys.argv[2]], shards[sys.argv[3]]
if first["attacker_key"] != "qwen38_27b_abliterated_bf16":
    raise SystemExit("invalid Qwen BF16 pair")
if second["attacker_key"] != first["attacker_key"]:
    raise SystemExit("pair attackers differ")
if set(first["cell_ids"]) & set(second["cell_ids"]):
    raise SystemExit("pair cells overlap")
roles = first["serving_requirements"]["roles"]
resources = first["serving_requirements"]["resources"]["attacker"]
if roles["attacker"]["serving_framework"] != "vllm":
    raise SystemExit("pair attacker is not vLLM")
if int(resources.get("worker_gpu_count", 0)) != 2:
    raise SystemExit("pair is not a two-GPU Qwen vLLM pair")
print(json.dumps({
    "campaign": manifest["campaign_name"],
    "scope": manifest["execution_scope_id"],
    "target": roles["target"]["identifier"],
    "judge": roles["judge"]["identifier"],
    "attacker": roles["attacker"]["identifier"],
}))
PY
)"

export HACKAGENT_CAMPAIGN_NAME="$(python -c 'import json,sys;print(json.load(sys.stdin)["campaign"])' <<<"$META")"
export HACKAGENT_EXECUTION_SCOPE_ID="$(python -c 'import json,sys;print(json.load(sys.stdin)["scope"])' <<<"$META")"
export HACKAGENT_TARGET_KEY=qwen38_27b
export HACKAGENT_ATTACKER_KEYS=qwen38_27b_abliterated_bf16
export HACKAGENT_SKIP_CAMPAIGN_WARMUP=1
export PYTHONIOENCODING=utf-8:backslashreplace

ALLOCATED="${CUDA_VISIBLE_DEVICES:-}"
IFS=',' read -r -a GPUS <<<"$ALLOCATED"
[ "${#GPUS[@]}" -eq 2 ] || {
    printf 'expected two allocated GPUs, got %s\n' "$ALLOCATED" >&2
    exit 1
}

# Keep the proven shared-pair layout: adjacent private loopback ports, one
# vLLM service GPU, and one Ollama service GPU. Stride 10 avoids overlap
# between consecutive JOB_IDs on the same node (job N uses P,P+1 and job
# N+1 uses P+10,P+11, never P+1).
BASE=$((12000 + (SLURM_JOB_ID % 2000) * 10))
OLLAMA_PORT="$BASE"
VLLM_PORT="$((BASE + 1))"
OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"
OLLAMA_ENDPOINT="http://$OLLAMA_HOST"

export MODEL_ID="$(python -c 'import json,sys;print(json.load(sys.stdin)["attacker"])' <<<"$META")"
export VLLM_VISIBLE_DEVICES="${GPUS[0]}"
export VLLM_TENSOR_PARALLEL_SIZE=1
export VLLM_DTYPE=bfloat16
export VLLM_MAX_MODEL_LEN=262144
export VLLM_EXTRA_ARGS_STRING='--gpu-memory-utilization 0.84 --reasoning-parser qwen3'
export VLLM_PORT VLLM_ENDPOINT="http://127.0.0.1:$VLLM_PORT/v1"
export VLLM_LOG="$SHARED/vllm_${SLURM_JOB_ID}.log"
export VLLM_PID_FILE="$SHARED/vllm_${SLURM_JOB_ID}.pid"
export VLLM_VERIFY_CHAT=1

export OLLAMA_HOST OLLAMA_ENDPOINT
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export OLLAMA_VISIBLE_DEVICES="${GPUS[1]}"
export OLLAMA_NUM_PARALLEL=2
export OLLAMA_MAX_LOADED_MODELS=2
export HACKAGENT_AUTO_PULL_MODELS=0

bash "$SCRIPT_DIR/serve_vllm.sh" start
VLLM_STARTED=1
OLLAMA_PID=""
cleanup() {
    [ -n "$OLLAMA_PID" ] && kill "$OLLAMA_PID" 2>/dev/null || true
    [ -n "$OLLAMA_PID" ] && wait "$OLLAMA_PID" 2>/dev/null || true
    if [ "$VLLM_STARTED" -eq 1 ]; then
        MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_LOG="$VLLM_LOG" \
            VLLM_PID_FILE="$VLLM_PID_FILE" bash "$SCRIPT_DIR/serve_vllm.sh" stop || true
    fi
}
trap cleanup EXIT TERM INT

bash "$SCRIPT_DIR/serve_vllm.sh" wait
bash "$SCRIPT_DIR/serve_vllm.sh" verify

CUDA_VISIBLE_DEVICES="${GPUS[1]}" ollama serve >"$SHARED/ollama_${SLURM_JOB_ID}.log" 2>&1 &
OLLAMA_PID=$!
ready=0
for _ in $(seq 1 90); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || { printf 'shared Ollama did not become ready\n' >&2; exit 1; }
if ! grep -q "library=CUDA" "$SHARED/ollama_${SLURM_JOB_ID}.log"; then
    printf 'Ollama CUDA backend not discovered; aborting to avoid Vulkan CLIP segfault\n' >&2
    exit 1
fi

for model in \
    "$(python -c 'import json,sys;print(json.load(sys.stdin)["target"])' <<<"$META")" \
    "$(python -c 'import json,sys;print(json.load(sys.stdin)["judge"])' <<<"$META")"; do
    curl -fsS -X POST "$OLLAMA_ENDPOINT/api/show" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"$model\"}" >/dev/null
    curl -fsS -X POST "$OLLAMA_ENDPOINT/api/generate" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$model\",\"prompt\":\"Reply with READY.\",\"stream\":false,\"options\":{\"num_predict\":1}}" >/dev/null
    printf 'verified shared model=%s\n' "$model"
done

if [ "$SERVICE_SMOKE" -eq 1 ]; then
    printf 'Qwen shared service smoke passed\n'
    exit 0
fi

run_worker() {
    local shard_id="$1"
    local worker_log="$SHARED/worker_${SLURM_JOB_ID}_${shard_id}.log"
    local args=(--execute --matrix --shard-manifest "$MANIFEST" --shard-id "$shard_id")
    [ -n "$LIMIT" ] && args+=(--limit "$LIMIT")
    [ "$RETRY_FAILED" -eq 1 ] && args+=(--retry-failed)
    [ "$RETRY_INCOMPLETE" -eq 1 ] && args+=(--retry-incomplete)
    HACKAGENT_DB="$ROOT/shards/$shard_id/hackagent.db" \
        OLLAMA_ENDPOINT="$OLLAMA_ENDPOINT" \
        VLLM_ENDPOINT="$VLLM_ENDPOINT" \
        uv run --no-sync python experiments/campaign/campaign.py "${args[@]}" \
        >"$worker_log" 2>&1
}

run_worker "$SHARD_A" &
PA=$!
run_worker "$SHARD_B" &
PB=$!
status=0
wait "$PA" || status=1
wait "$PB" || status=1
printf 'shared-GPU Qwen pair completed shard_a=%s shard_b=%s status=%s\n' \
    "$SHARD_A" "$SHARD_B" "$status"
exit "$status"
