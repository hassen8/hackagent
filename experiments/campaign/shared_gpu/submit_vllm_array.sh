#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MANIFEST=""; TIME_LIMIT="15:00:00"; LIMIT=""; SERVICE_SMOKE=0; RETRY_FAILED=0; RETRY_INCOMPLETE=0; DRY_RUN=0
usage() { printf 'Usage: %s --manifest PATH [--limit N] [--service-smoke] [--time VALUE] [--dry-run]\n' "$0" >&2; }
while [ "$#" -gt 0 ]; do case "$1" in
    --manifest) MANIFEST="${2:-}"; shift 2;; --limit) LIMIT="${2:-}"; shift 2;;
    --service-smoke) SERVICE_SMOKE=1; shift;; --retry-failed) RETRY_FAILED=1; shift;;
    --retry-incomplete) RETRY_INCOMPLETE=1; shift;; --time) TIME_LIMIT="${2:-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;; --help|-h) usage; exit 0;; *) usage; exit 2;; esac; done
[ -n "$MANIFEST" ] || { usage; exit 2; }; cd "$PROJECT_DIR"
PAIR_REPORT="$(mktemp)"; trap 'rm -f "$PAIR_REPORT"' EXIT
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python "$SCRIPT_DIR/plan_pairs.py" --manifest "$MANIFEST" --output "$PAIR_REPORT" >/dev/null
PAIR_COUNT="$(python - "$PAIR_REPORT" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["pair_count"])
PY
)"
KEY="$(python - "$PAIR_REPORT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))["pairs"]
print(p[0]["attacker_key"])
PY
)"
ROOT="$(dirname "$MANIFEST")"; LOG_DIR="$ROOT/shared_gpu/slurm"
SBATCH_CMD=(sbatch "--chdir=$PROJECT_DIR" --account=ais_login --qos=quota_ais_aiq --partition=aiq --gres=gpu:b200:5 --cpus-per-task=32 --mem=400G "--time=$TIME_LIMIT" --job-name="hackagent_${KEY}_pair" --output="$LOG_DIR/${KEY}_%A_%a.out" --error="$LOG_DIR/${KEY}_%A_%a.err" "--array=0-$((PAIR_COUNT - 1))" "$SCRIPT_DIR/submit_vllm_array.slurm" "$MANIFEST")
[ -n "$LIMIT" ] && SBATCH_CMD+=(--limit "$LIMIT"); [ "$SERVICE_SMOKE" -eq 1 ] && SBATCH_CMD+=(--service-smoke)
[ "$RETRY_FAILED" -eq 1 ] && SBATCH_CMD+=(--retry-failed); [ "$RETRY_INCOMPLETE" -eq 1 ] && SBATCH_CMD+=(--retry-incomplete)
printf 'pair_count=%s\nResolved sbatch command:' "$PAIR_COUNT"; printf ' %q' "${SBATCH_CMD[@]}"; printf '\n'
[ "$DRY_RUN" -eq 1 ] || { mkdir -p "$LOG_DIR"; "${SBATCH_CMD[@]}"; }
