#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MANIFEST=""
SHARD_A=""
SHARD_B=""
TIME_LIMIT="10:00:00"
LIMIT=""
RETRY_FAILED=0
RETRY_INCOMPLETE=0
SERVICE_SMOKE=0
EXCLUDE="ainode01"
DRY_RUN=0

usage() {
    printf 'Usage: %s --manifest PATH --shard-a SHARD_ID --shard-b SHARD_ID [--limit N] [--service-smoke] [--exclude LIST] [--retry-failed] [--retry-incomplete] [--time VALUE] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --shard-a) SHARD_A="${2:-}"; shift 2 ;;
        --shard-b) SHARD_B="${2:-}"; shift 2 ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --exclude) EXCLUDE="${2:-}"; shift 2 ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] && [ -n "$SHARD_A" ] && [ -n "$SHARD_B" ] || {
    usage
    exit 2
}
[ "$SHARD_A" != "$SHARD_B" ] || {
    printf 'shard-a and shard-b must be different\n' >&2
    exit 2
}

cd "$PROJECT_DIR"
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_A" "$SHARD_B" <<'PY'
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
    raise SystemExit("both shard IDs must exist in the manifest")
first, second = (shards[sys.argv[2]], shards[sys.argv[3]])
if first.get("attacker_key") != second.get("attacker_key"):
    raise SystemExit("shared-GPU pair shards must have the same attacker_key")
overlap = set(first["cell_ids"]) & set(second["cell_ids"])
if overlap:
    raise SystemExit("shared-GPU pair shards have overlapping cell IDs")
for shard in (first, second):
    resources = (shard.get("serving_requirements") or {}).get("resources", {})
    attacker = resources.get("attacker", {})
    if attacker.get("serving_framework") != "ollama":
        raise SystemExit("shared-GPU pilot currently supports Ollama attackers only")
    if int(attacker.get("worker_gpu_count", 0)) != 1:
        raise SystemExit("shared-GPU pilot requires one-GPU Ollama shards")
print(
    f"validated shared-GPU pair attacker={first['attacker_key']} "
    f"shards={sys.argv[2]},{sys.argv[3]}"
)
PY

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/shared_gpu/slurm"
SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    "--job-name=hackagent_shared_gpu"
    "--output=$LOG_DIR/pair_%A.out"
    "--error=$LOG_DIR/pair_%A.err"
    "--gres=gpu:b200:1"
    "--cpus-per-task=32"
    "--mem=400G"
    "--time=$TIME_LIMIT"
)
[ -n "$EXCLUDE" ] && SBATCH_CMD+=("--exclude=$EXCLUDE")
SBATCH_CMD+=(
    "$SCRIPT_DIR/submit_pair.slurm"
    "$MANIFEST"
    --shard-a "$SHARD_A"
    --shard-b "$SHARD_B"
)
[ -n "$LIMIT" ] && SBATCH_CMD+=(--limit "$LIMIT")
[ "$SERVICE_SMOKE" -eq 1 ] && SBATCH_CMD+=(--service-smoke)
[ "$RETRY_FAILED" -eq 1 ] && SBATCH_CMD+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && SBATCH_CMD+=(--retry-incomplete)

printf 'Resolved sbatch command:'
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi
mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
