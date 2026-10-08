#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MANIFEST=""
SHARD_A=""
SHARD_B=""
TIME_LIMIT="15:00:00"
LIMIT=""
SERVICE_SMOKE=0
RETRY_FAILED=0
RETRY_INCOMPLETE=0
EXCLUDE="ainode01"
DRY_RUN=0

usage() { printf 'Usage: %s --manifest PATH --shard-a ID --shard-b ID [--limit N] [--service-smoke] [--exclude LIST] [--time VALUE] [--dry-run]\n' "$0" >&2; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2;;
        --shard-a) SHARD_A="${2:-}"; shift 2;;
        --shard-b) SHARD_B="${2:-}"; shift 2;;
        --limit) LIMIT="${2:-}"; shift 2;;
        --service-smoke) SERVICE_SMOKE=1; shift;;
        --exclude) EXCLUDE="${2:-}"; shift 2;;
        --retry-failed) RETRY_FAILED=1; shift;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift;;
        --time) TIME_LIMIT="${2:-}"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --help|-h) usage; exit 0;;
        *) usage; exit 2;;
    esac
done
[ -n "$MANIFEST" ] && [ -n "$SHARD_A" ] && [ -n "$SHARD_B" ] || { usage; exit 2; }
cd "$PROJECT_DIR"
PAIR_KEY="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_A" "$SHARD_B" <<'PY'
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest
m = load_shard_manifest(sys.argv[1]); validate_shard_manifest(m)
s = {x["shard_id"]: x for x in m["shards"]}
a, b = s[sys.argv[2]], s[sys.argv[3]]
if a["attacker_key"] != b["attacker_key"]: raise SystemExit("pair attackers differ")
if set(a["cell_ids"]) & set(b["cell_ids"]): raise SystemExit("pair cells overlap")
r = (a["serving_requirements"] or {}).get("resources", {}).get("attacker", {})
if r.get("serving_framework") != "vllm" or int(r.get("worker_gpu_count", 0)) != 5:
    raise SystemExit("pair is not a five-GPU vLLM attacker pair")
print(a["attacker_key"])
PY
)"
ROOT="$(dirname "$MANIFEST")"; LOG_DIR="$ROOT/shared_gpu/slurm"
SBATCH_CMD=(sbatch "--chdir=$PROJECT_DIR" --account=ais_login --qos=quota_ais_aiq --partition=aiq --gres=gpu:b200:5 --cpus-per-task=32 --mem=400G "--time=$TIME_LIMIT" --job-name="hackagent_${PAIR_KEY}_pair" --output="$LOG_DIR/${PAIR_KEY}_%A.out" --error="$LOG_DIR/${PAIR_KEY}_%A.err")
[ -n "$EXCLUDE" ] && SBATCH_CMD+=("--exclude=$EXCLUDE")
SBATCH_CMD+=("$SCRIPT_DIR/submit_vllm_pair.slurm" "$MANIFEST" --shard-a "$SHARD_A" --shard-b "$SHARD_B")
[ -n "$LIMIT" ] && SBATCH_CMD+=(--limit "$LIMIT")
[ "$SERVICE_SMOKE" -eq 1 ] && SBATCH_CMD+=(--service-smoke)
[ "$RETRY_FAILED" -eq 1 ] && SBATCH_CMD+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && SBATCH_CMD+=(--retry-incomplete)
printf 'Resolved sbatch command:'; printf ' %q' "${SBATCH_CMD[@]}"; printf '\n'
[ "$DRY_RUN" -eq 1 ] || { mkdir -p "$LOG_DIR"; "${SBATCH_CMD[@]}"; }
