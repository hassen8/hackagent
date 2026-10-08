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
NODELIST=""
EXCLUDE="ainode01"
DRY_RUN=0

usage() {
    printf 'Usage: %s --manifest PATH --shard-a ID --shard-b ID [--limit N] [--service-smoke] [--retry-failed] [--retry-incomplete] [--time VALUE] [--nodelist LIST] [--exclude LIST] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --shard-a) SHARD_A="${2:-}"; shift 2 ;;
        --shard-b) SHARD_B="${2:-}"; shift 2 ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --nodelist) NODELIST="${2:-}"; shift 2 ;;
        --exclude) EXCLUDE="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] && [ -n "$SHARD_A" ] && [ -n "$SHARD_B" ] || {
    usage
    exit 2
}

cd "$PROJECT_DIR"
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python "$SCRIPT_DIR/plan_qwen38_pairs.py" \
    --manifest "$MANIFEST" >/dev/null

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/shared_gpu/slurm"
mkdir -p "$LOG_DIR"
SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    --account=ais_login
    --qos=quota_ais_aiq
    --partition=aiq
    --gres=gpu:b200:2
    --cpus-per-task=32
    --mem=400G
    "--time=$TIME_LIMIT"
    --job-name=hackagent_qwen38_bf16_pair
    "--output=$LOG_DIR/qwen38_bf16_%A.out"
    "--error=$LOG_DIR/qwen38_bf16_%A.err"
)
[ -n "$NODELIST" ] && SBATCH_CMD+=("--nodelist=$NODELIST")
[ -n "$EXCLUDE" ] && SBATCH_CMD+=("--exclude=$EXCLUDE")
SBATCH_CMD+=(
    "$SCRIPT_DIR/submit_qwen38_bf16_pair.slurm"
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
[ "$DRY_RUN" -eq 1 ] || "${SBATCH_CMD[@]}"
