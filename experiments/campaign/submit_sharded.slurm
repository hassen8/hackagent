#!/usr/bin/env bash
#SBATCH --job-name=hackagent_shard
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:2
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=24:00:00

set -euo pipefail

# load cuda models needed for vllm
module load cuda13.0/toolkit/13.0.2

PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# Slurm may execute a submitted script from /var/spool/slurmd. Resolve helper
# scripts from the repository checkout, not from the spool copy.
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
[ -f "$PYTHON_ENV" ] || {
    printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2
    exit 1
}
source "$PYTHON_ENV"
MANIFEST="${1:-}"
shift || true
SHARD_ID=""
LIMIT=""
RETRY_FAILED=0
RETRY_INCOMPLETE=0
EXECUTION_SCOPE=""
SERVING_REQUIREMENTS=""
SERVICE_SMOKE=0

usage() {
    printf 'Usage: %s MANIFEST [--shard-id SHARD_ID] [--limit N] [--retry-failed] [--retry-incomplete]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-id)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            SHARD_ID="$2"
            shift 2
            ;;
        --limit)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            LIMIT="$2"
            shift 2
            ;;
        --retry-failed)
            RETRY_FAILED=1
            shift
            ;;
        --retry-incomplete)
            RETRY_INCOMPLETE=1
            shift
            ;;
        --service-smoke)
            SERVICE_SMOKE=1
            shift
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[ -n "$MANIFEST" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

# Reconstruct the selected campaign inventory from the immutable manifest. This
# keeps a target-specific manifest independent from the mutable default config.
CAMPAIGN_METADATA="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
requirements = manifest.get("serving_requirements") or {}
attackers = requirements.get("roles", {}).get("attackers", {})
separate = any(
    isinstance(role, dict)
    and role.get("serving_framework") == "ollama"
    and role.get("endpoint_env") == "OLLAMA_ATTACKER_ENDPOINT"
    for role in attackers.values()
)
print(json.dumps({
    "campaign_name": manifest.get("campaign_name"),
    "execution_scope_id": manifest.get("execution_scope_id"),
    "target_key": manifest.get("target_key"),
    "attacker_keys": manifest.get("attacker_keys", []),
    "separate_ollama_attacker": separate,
}))
PY
)"
export HACKAGENT_CAMPAIGN_NAME="$(python - "$CAMPAIGN_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["campaign_name"])
PY
)"
export HACKAGENT_EXECUTION_SCOPE_ID="$(python - "$CAMPAIGN_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["execution_scope_id"])
PY
)"
export HACKAGENT_TARGET_KEY="$(python - "$CAMPAIGN_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["target_key"])
PY
)"
export HACKAGENT_ATTACKER_KEYS="$(python - "$CAMPAIGN_METADATA" <<'PY'
import json
import sys
print(",".join(json.loads(sys.argv[1])["attacker_keys"]))
PY
)"
export HACKAGENT_SEPARATE_OLLAMA_ATTACKER="$(python - "$CAMPAIGN_METADATA" <<'PY'
import json
import sys
print("1" if json.loads(sys.argv[1])["separate_ollama_attacker"] else "0")
PY
)"

if [ -z "$SHARD_ID" ] && [ -z "${SLURM_ARRAY_TASK_ID:-}" ]; then
    printf 'array execution requires SLURM_ARRAY_TASK_ID or an explicit --shard-id\n' >&2
    exit 2
fi
if [ -z "$SHARD_ID" ]; then
    SHARD_ID="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "${SLURM_ARRAY_TASK_ID:-}" <<'PY'
import sys
from experiments.campaign.sharding import load_shard_manifest, shard_id_for_array_index, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
print(shard_id_for_array_index(manifest, int(sys.argv[2])))
PY
)"
fi

SHARD_PATHS="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import json
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
entry = next((entry for entry in manifest["shards"] if entry["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
print(json.dumps(entry["paths"], sort_keys=True))
PY
)"

SHARD_METADATA="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import json
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
entry = next((entry for entry in manifest["shards"] if entry["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
print(json.dumps({
    "execution_scope_id": manifest["execution_scope_id"],
    "serving_requirements": entry.get("serving_requirements"),
}, sort_keys=True))
PY
)"
EXECUTION_SCOPE="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$SHARD_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["execution_scope_id"])
PY
)"
SERVING_REQUIREMENTS="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$SHARD_METADATA" <<'PY'
import json
import sys
value = json.loads(sys.argv[1]).get("serving_requirements")
print(json.dumps(value, sort_keys=True) if value is not None else "")
PY
)"
if [ "$EXECUTION_SCOPE" != "omnisafebench_af340_five_attackers_v1" ]; then
    [ -n "$SERVING_REQUIREMENTS" ] || {
        printf 'extension shard is missing serving requirements\n' >&2
        exit 1
    }
fi

ATTACKER_METADATA="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$SHARD_METADATA" <<'PY'
import json
import sys
serving = json.loads(sys.argv[1]).get("serving_requirements") or {}
print(json.dumps((serving.get("roles") or {}).get("attacker") or {}, sort_keys=True))
PY
)"
ATTACKER_MODEL="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get("identifier", ""))
PY
)"
ATTACKER_RESOURCES="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$SHARD_METADATA" <<'PY'
import json
import sys
serving = json.loads(sys.argv[1]).get("serving_requirements") or {}
print(json.dumps((serving.get("resources") or {}).get("attacker") or {}, sort_keys=True))
PY
)"
SERVING_FRAMEWORK="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_METADATA" <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get("serving_framework", "ollama"))
PY
)"
VLLM_TENSOR_PARALLEL_SIZE="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_RESOURCES" <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get("vllm_gpu_count", 0))
PY
)"
OLLAMA_GPU_COUNT="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_RESOURCES" <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get("ollama_gpu_count", 1))
PY
)"
VLLM_DTYPE="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_METADATA" <<'PY'
import json
import sys
options = json.loads(sys.argv[1]).get("serving_options") or {}
print(options.get("dtype", ""))
PY
)"
VLLM_MAX_MODEL_LEN="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_METADATA" <<'PY'
import json
import sys
options = json.loads(sys.argv[1]).get("serving_options") or {}
print(options.get("max_model_len", ""))
PY
)"
VLLM_EXTRA_ARGS_STRING="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$ATTACKER_METADATA" <<'PY'
import json
import shlex
import sys
options = json.loads(sys.argv[1]).get("serving_options") or {}
print(shlex.join(str(value) for value in options.get("extra_args", [])))
PY
)"

SHARD_DIR="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$SHARD_PATHS" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["relative_path"])
PY
)"
CAMPAIGN_ROOT="$(dirname "$MANIFEST")"
SHARD_DIR="$CAMPAIGN_ROOT/$SHARD_DIR"
mkdir -p "$SHARD_DIR/slurm" "$SHARD_DIR/runs"

LOCK_PATH="$SHARD_DIR/slurm/worker.lock"
exec 9>"$LOCK_PATH"
flock -n 9 || { printf 'shard %s is already owned by another worker\n' "$SHARD_ID" >&2; exit 1; }

JOB_ID="${SLURM_JOB_ID:-0}"
ARRAY_ID="${SLURM_ARRAY_TASK_ID:-0}"
PORT=$((10000 + ((JOB_ID * 100 + ARRAY_ID) % 50000)))
export OLLAMA_HOST="127.0.0.1:$PORT"
export OLLAMA_ENDPOINT="http://$OLLAMA_HOST"
VLLM_PORT=$((PORT + 1))
export VLLM_ENDPOINT="http://127.0.0.1:$VLLM_PORT/v1"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export HACKAGENT_AUTO_PULL_MODELS=0
export HACKAGENT_DB="$SHARD_DIR/hackagent.db"
export OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-3}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"
export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

LOG_DIR="$SHARD_DIR/slurm"
OLLAMA_LOG="$LOG_DIR/ollama_${JOB_ID}.log"
OLLAMA_ATTACKER_LOG="$LOG_DIR/ollama_attacker_${JOB_ID}.log"
RUN_LOG="$LOG_DIR/runner_${JOB_ID}.log"
VLLM_LOG="$LOG_DIR/vllm_${JOB_ID}.log"
VLLM_PID_FILE="$LOG_DIR/vllm_${JOB_ID}.pid"
OLLAMA_PID=""
OLLAMA_ATTACKER_PID=""
VLLM_STARTED=0

VLLM_VISIBLE_DEVICES=""
OLLAMA_VISIBLE_DEVICES=""
OLLAMA_ATTACKER_VISIBLE_DEVICES=""
if [ "$EXECUTION_SCOPE" != "omnisafebench_af340_five_attackers_v1" ]; then
    [ "$SERVING_FRAMEWORK" = "vllm" ] || [ "$SERVING_FRAMEWORK" = "ollama" ] || {
        printf 'unsupported extension attacker serving framework: %s\n' "$SERVING_FRAMEWORK" >&2
        exit 1
    }
    ALLOCATED_GPU_COUNT="${SLURM_GPUS_ON_NODE:-1}"
    if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
        IFS=',' read -r -a ALLOCATED_GPUS <<<"$CUDA_VISIBLE_DEVICES"
        ALLOCATED_GPU_COUNT="${#ALLOCATED_GPUS[@]}"
    fi
    if [ "$SERVING_FRAMEWORK" = "vllm" ]; then
        REQUIRED_GPU_COUNT=$((VLLM_TENSOR_PARALLEL_SIZE + OLLAMA_GPU_COUNT))
    else
        REQUIRED_GPU_COUNT="$OLLAMA_GPU_COUNT"
    fi
    if [ "$ALLOCATED_GPU_COUNT" -lt "$REQUIRED_GPU_COUNT" ]; then
        printf 'extension shard requires %s GPUs: vLLM=%s Ollama=%s; allocation has %s\n' \
            "$REQUIRED_GPU_COUNT" "$VLLM_TENSOR_PARALLEL_SIZE" "$OLLAMA_GPU_COUNT" "$ALLOCATED_GPU_COUNT" >&2
        exit 1
    fi
    if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
        if [ "$SERVING_FRAMEWORK" = "vllm" ]; then
            VLLM_VISIBLE_DEVICES="$(IFS=,; printf '%s' "${ALLOCATED_GPUS[*]:0:VLLM_TENSOR_PARALLEL_SIZE}")"
            OLLAMA_VISIBLE_DEVICES="$(IFS=,; printf '%s' "${ALLOCATED_GPUS[*]:VLLM_TENSOR_PARALLEL_SIZE:OLLAMA_GPU_COUNT}")"
        else
            OLLAMA_VISIBLE_DEVICES="${ALLOCATED_GPUS[0]}"
            if [ "$OLLAMA_GPU_COUNT" -gt 1 ]; then
                OLLAMA_ATTACKER_VISIBLE_DEVICES="${ALLOCATED_GPUS[1]}"
            fi
        fi
    else
        if [ "$SERVING_FRAMEWORK" = "vllm" ]; then
            VLLM_VISIBLE_DEVICES="$(seq -s, 0 $((VLLM_TENSOR_PARALLEL_SIZE - 1)))"
            OLLAMA_VISIBLE_DEVICES="$(seq -s, "$VLLM_TENSOR_PARALLEL_SIZE" $((REQUIRED_GPU_COUNT - 1)))"
        else
            OLLAMA_VISIBLE_DEVICES=0
            if [ "$OLLAMA_GPU_COUNT" -gt 1 ]; then
                OLLAMA_ATTACKER_VISIBLE_DEVICES=1
            fi
        fi
    fi
fi

cleanup() {
    if [ "$VLLM_STARTED" -eq 1 ]; then
        MODEL_ID="$ATTACKER_MODEL" \
        VLLM_LOG="$VLLM_LOG" \
        VLLM_PID_FILE="$VLLM_PID_FILE" \
            bash "$SCRIPT_DIR/serve_vllm.sh" stop || true
    fi
    if [ -n "$OLLAMA_PID" ]; then
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
    if [ -n "$OLLAMA_ATTACKER_PID" ]; then
        kill "$OLLAMA_ATTACKER_PID" 2>/dev/null || true
        wait "$OLLAMA_ATTACKER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

[ -d "$OLLAMA_MODELS/blobs" ] && [ -d "$OLLAMA_MODELS/manifests" ] || {
    printf 'Ollama model store is missing or incomplete: %s\n' "$OLLAMA_MODELS" >&2
    exit 1
}
command -v ollama >/dev/null 2>&1 || { printf 'ollama is unavailable\n' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf 'curl is unavailable\n' >&2; exit 1; }
if [ "$EXECUTION_SCOPE" != "omnisafebench_af340_five_attackers_v1" ] && [ "$SERVING_FRAMEWORK" = "vllm" ]; then
    command -v vllm >/dev/null 2>&1 || {
        printf 'vllm is unavailable\n' >&2
        exit 1
    }
fi

if [ -n "$OLLAMA_VISIBLE_DEVICES" ]; then
    CUDA_VISIBLE_DEVICES="$OLLAMA_VISIBLE_DEVICES" ollama serve >"$OLLAMA_LOG" 2>&1 &
else
    ollama serve >"$OLLAMA_LOG" 2>&1 &
fi
OLLAMA_PID=$!

if [ -n "$OLLAMA_ATTACKER_VISIBLE_DEVICES" ]; then
    OLLAMA_ATTACKER_PORT=$((PORT + 1))
    export OLLAMA_ATTACKER_HOST="127.0.0.1:$OLLAMA_ATTACKER_PORT"
    export OLLAMA_ATTACKER_ENDPOINT="http://$OLLAMA_ATTACKER_HOST"
    CUDA_VISIBLE_DEVICES="$OLLAMA_ATTACKER_VISIBLE_DEVICES" ollama serve >"$OLLAMA_ATTACKER_LOG" 2>&1 &
    OLLAMA_ATTACKER_PID=$!
fi
ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || { printf 'job-local Ollama did not become ready\n' >&2; exit 1; }

if [ -n "$OLLAMA_ATTACKER_PID" ]; then
    ready=0
    for _ in $(seq 1 60); do
        if curl -fsS "$OLLAMA_ATTACKER_ENDPOINT/api/version" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 2
    done
    [ "$ready" -eq 1 ] || { printf 'job-local attacker Ollama did not become ready\n' >&2; exit 1; }
fi

if [ "$EXECUTION_SCOPE" != "omnisafebench_af340_five_attackers_v1" ] && [ "$SERVING_FRAMEWORK" = "vllm" ]; then
    MODEL_ID="$ATTACKER_MODEL" \
    VLLM_PORT="$VLLM_PORT" \
    VLLM_LOG="$VLLM_LOG" \
    VLLM_PID_FILE="$VLLM_PID_FILE" \
    VLLM_VISIBLE_DEVICES="$VLLM_VISIBLE_DEVICES" \
    VLLM_TENSOR_PARALLEL_SIZE="$VLLM_TENSOR_PARALLEL_SIZE" \
    VLLM_DTYPE="$VLLM_DTYPE" \
    VLLM_MAX_MODEL_LEN="$VLLM_MAX_MODEL_LEN" \
    VLLM_EXTRA_ARGS_STRING="$VLLM_EXTRA_ARGS_STRING" \
        bash "$SCRIPT_DIR/serve_vllm.sh" start
    VLLM_STARTED=1
    MODEL_ID="$ATTACKER_MODEL" VLLM_PORT="$VLLM_PORT" VLLM_LOG="$VLLM_LOG" VLLM_PID_FILE="$VLLM_PID_FILE" \
        bash "$SCRIPT_DIR/serve_vllm.sh" wait
    MODEL_ID="$ATTACKER_MODEL" VLLM_PORT="$VLLM_PORT" VLLM_LOG="$VLLM_LOG" VLLM_PID_FILE="$VLLM_PID_FILE" \
        bash "$SCRIPT_DIR/serve_vllm.sh" verify
    if [ "$SERVICE_SMOKE" -eq 1 ]; then
        printf 'dual-service smoke passed for shard=%s attacker=%s\n' "$SHARD_ID" "$ATTACKER_MODEL"
        exit 0
    fi
fi

if [ "$SERVICE_SMOKE" -eq 1 ] && [ "$SERVING_FRAMEWORK" = "ollama" ]; then
    OLLAMA_MODELS_TO_CHECK="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest
manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
shard = next(item for item in manifest["shards"] if item["shard_id"] == sys.argv[2])
roles = (shard.get("serving_requirements") or {}).get("roles") or {}
for role in (roles.get("target"), roles.get("judge"), roles.get("attacker")):
    if isinstance(role, dict) and role.get("identifier"):
        print(role["identifier"])
PY
)"
    while IFS= read -r model; do
        [ -n "$model" ] || continue
        endpoint="$OLLAMA_ENDPOINT"
        if [ -n "${OLLAMA_ATTACKER_ENDPOINT:-}" ] && [ "$model" = "$ATTACKER_MODEL" ]; then
            endpoint="$OLLAMA_ATTACKER_ENDPOINT"
        fi
        curl -fsS -X POST "$endpoint/api/show" \
            -H 'Content-Type: application/json' \
            -d "{\"name\":\"$model\"}" >/dev/null
        curl -fsS -X POST "$endpoint/api/generate" \
            -H 'Content-Type: application/json' \
            -d "{\"model\":\"$model\",\"prompt\":\"Reply with READY.\",\"stream\":false,\"options\":{\"num_predict\":1}}" >/dev/null
        printf 'verified Ollama model=%s endpoint=%s\n' "$model" "$endpoint"
    done <<<"$OLLAMA_MODELS_TO_CHECK"
    OLLAMA_HOST="$OLLAMA_HOST" ollama ps | tee "$LOG_DIR/ollama_ps_${JOB_ID}.txt"
    if [ -n "${OLLAMA_ATTACKER_ENDPOINT:-}" ]; then
        OLLAMA_HOST="$OLLAMA_ATTACKER_HOST" ollama ps | tee "$LOG_DIR/ollama_attacker_ps_${JOB_ID}.txt"
    fi
    printf 'dual-Ollama smoke passed for shard=%s attacker=%s\n' "$SHARD_ID" "$ATTACKER_MODEL"
    exit 0
fi

RUN_ARGS=(
    --execute --matrix
    --shard-manifest "$MANIFEST"
    --shard-id "$SHARD_ID"
)
if [ -n "$LIMIT" ]; then
    RUN_ARGS+=(--limit "$LIMIT")
fi
if [ "$RETRY_FAILED" -eq 1 ]; then
    RUN_ARGS+=(--retry-failed)
fi
if [ "$RETRY_INCOMPLETE" -eq 1 ]; then
    RUN_ARGS+=(--retry-incomplete)
fi
uv run --no-sync python experiments/campaign/campaign.py "${RUN_ARGS[@]}" 2>&1 | tee "$RUN_LOG"
