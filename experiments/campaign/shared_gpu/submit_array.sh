#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MANIFEST=""
TIME_LIMIT="10:00:00"
MAX_PAIRS=""
SERVICE_SMOKE=0
RETRY_FAILED=0
RETRY_INCOMPLETE=0
DRY_RUN=0

usage() {
    printf 'Usage: %s --manifest PATH [--time VALUE] [--max-pairs N] [--service-smoke] [--retry-failed] [--retry-incomplete] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --max-pairs) MAX_PAIRS="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
[ -n "$MANIFEST" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

PAIR_REPORT="$(mktemp)"
trap 'rm -f "$PAIR_REPORT"' EXIT
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python \
    "$SCRIPT_DIR/plan_pairs.py" --manifest "$MANIFEST" --output "$PAIR_REPORT" >/dev/null

PAIR_COUNT="$(python - "$PAIR_REPORT" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["pair_count"])
PY
)"
[ -n "$MAX_PAIRS" ] && PAIR_COUNT=$((PAIR_COUNT < MAX_PAIRS ? PAIR_COUNT : MAX_PAIRS))
[ "$PAIR_COUNT" -gt 0 ] || { printf 'pair plan contains no pairs\n' >&2; exit 1; }
TIME_MINUTES="$(python - "$TIME_LIMIT" <<'PY'
import sys

value = sys.argv[1]
parts = [int(part) for part in value.split(":")]
if len(parts) == 3:
    hours, minutes, seconds = parts
elif len(parts) == 2:
    hours, minutes, seconds = 0, parts[0], parts[1]
else:
    raise SystemExit("--time must be HH:MM:SS or MM:SS")
print(hours * 60 + minutes + (seconds + 59) // 60)
PY
)"

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/shared_gpu/slurm"
SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    "--job-name=hackagent_shared_gpu"
    "--output=$LOG_DIR/array_%A_%a.out"
    "--error=$LOG_DIR/array_%A_%a.err"
    "--array=0-$((PAIR_COUNT - 1))"
    "--gres=gpu:b200:1"
    "--cpus-per-task=32"
    "--mem=400G"
    "--time=$TIME_LIMIT"
    "$SCRIPT_DIR/submit_array.slurm"
    "$MANIFEST"
)
[ "$RETRY_FAILED" -eq 1 ] && SBATCH_CMD+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && SBATCH_CMD+=(--retry-incomplete)
[ "$SERVICE_SMOKE" -eq 1 ] && SBATCH_CMD+=(--service-smoke)
printf 'pair_count=%s\nResolved sbatch command:' "$PAIR_COUNT"
printf 'requested_gpu_minutes=%s max_gpu_hours=%.2f\n' \
    "$((PAIR_COUNT * TIME_MINUTES))" \
    "$(awk "BEGIN { printf \"%.2f\", ($PAIR_COUNT * $TIME_MINUTES) / 60 }")"
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi
mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
