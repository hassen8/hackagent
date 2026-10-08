#!/usr/bin/env bash
set -euo pipefail

# Common campaign worker. Model-specific launchers own Slurm resources and
# vLLM startup; this worker owns Ollama, shard locking, campaign.py, and cleanup.

PROJECT_DIR="${PROJECT_DIR:-${SLURM_SUBMIT_DIR:-$(pwd)}}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
MANIFEST="${1:-}"
shift || true
SHARD_ID=""
LIMIT=""
RETRY_FAILED=0
RETRY_INCOMPLETE=0
SERVICE_SMOKE=0

usage() {
    printf 'Usage: %s MANIFEST --shard-id SHARD_ID [--limit N] [--retry-failed] [--retry-incomplete] [--service-smoke]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --shard-id) SHARD_ID="${2:-}"; shift 2 ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] && [ -n "$SHARD_ID" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

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

SHARD_PATHS="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import json
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest
manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
entry = next((item for item in manifest["shards"] if item["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
print(json.dumps(entry["paths"], sort_keys=True))
PY
)"
CAMPAIGN_ROOT="$(dirname "$MANIFEST")"
SHARD_DIR="$CAMPAIGN_ROOT/$(python - "$SHARD_PATHS" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["relative_path"])
PY
)"
mkdir -p "$SHARD_DIR/slurm" "$SHARD_DIR/runs"

if [ "${SHARD_LOCK_ALREADY_HELD:-0}" != "1" ]; then
    exec 9>"$SHARD_DIR/slurm/worker.lock"
    flock -n 9 || { printf 'shard %s is already owned by another worker\n' "$SHARD_ID" >&2; exit 1; }
fi

JOB_ID="${SLURM_JOB_ID:-0}"
LOG_DIR="$SHARD_DIR/slurm"
OLLAMA_LOG="$LOG_DIR/ollama_${JOB_ID}.log"
RUN_LOG="$LOG_DIR/runner_${JOB_ID}.log"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export HACKAGENT_AUTO_PULL_MODELS=0
export HACKAGENT_DB="$SHARD_DIR/hackagent.db"
export OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"

[ -n "${OLLAMA_ENDPOINT:-}" ] || { printf 'OLLAMA_ENDPOINT is required\n' >&2; exit 1; }
[ -n "${OLLAMA_VISIBLE_DEVICES:-}" ] || { printf 'GPU-backed OLLAMA_VISIBLE_DEVICES is required\n' >&2; exit 1; }
export OLLAMA_HOST="${OLLAMA_HOST:-${OLLAMA_ENDPOINT#http://}}"

cleanup() {
    if [ -n "${OLLAMA_PID:-}" ]; then
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

CUDA_VISIBLE_DEVICES="$OLLAMA_VISIBLE_DEVICES" ollama serve >"$OLLAMA_LOG" 2>&1 &
OLLAMA_PID=$!
ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || { printf 'job-local Ollama did not become ready\n' >&2; exit 1; }

if [ "$SERVICE_SMOKE" -eq 1 ]; then
    OLLAMA_MODELS_TO_CHECK="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
shard = next(item for item in manifest["shards"] if item["shard_id"] == sys.argv[2])
roles = (shard.get("serving_requirements") or {}).get("roles") or {}
models = [
    roles.get("target", {}).get("identifier") or manifest.get("target_model"),
    roles.get("judge", {}).get("identifier") or manifest.get("judge_model"),
]
attacker = roles.get("attacker") or {}
if attacker.get("serving_framework") == "ollama":
    models.append(attacker.get("identifier"))
for model in dict.fromkeys(model for model in models if model):
    print(model)
PY
)"
    while IFS= read -r model; do
        [ -n "$model" ] || continue
        curl -fsS -X POST "$OLLAMA_ENDPOINT/api/show" \
            -H 'Content-Type: application/json' \
            -d "{\"name\":\"$model\"}" >/dev/null
        printf 'verified Ollama model=%s\n' "$model"
    done <<<"$OLLAMA_MODELS_TO_CHECK"
    printf 'common worker service smoke passed for shard=%s\n' "$SHARD_ID"
    exit 0
fi

RUN_ARGS=(--execute --matrix --shard-manifest "$MANIFEST" --shard-id "$SHARD_ID")
[ -n "$LIMIT" ] && RUN_ARGS+=(--limit "$LIMIT")
[ "$RETRY_FAILED" -eq 1 ] && RUN_ARGS+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && RUN_ARGS+=(--retry-incomplete)
uv run --no-sync python experiments/campaign/campaign.py "${RUN_ARGS[@]}" 2>&1 | tee "$RUN_LOG"
