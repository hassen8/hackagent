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
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
SHARED_DIR="$SCRIPT_DIR/shared_gpu"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
MANIFEST="${1:-}"
[ -n "$MANIFEST" ] || { printf 'manifest is required\n' >&2; exit 2; }
[ -n "${SLURM_ARRAY_TASK_ID:-}" ] || { printf 'array task ID is required\n' >&2; exit 2; }
shift || true
RETRY_FAILED=0
RETRY_INCOMPLETE=0
SERVICE_SMOKE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --retry-failed) RETRY_FAILED=1; shift ;;
        --retry-incomplete) RETRY_INCOMPLETE=1; shift ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
[ -f "$PYTHON_ENV" ] || { printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2; exit 1; }
source "$PYTHON_ENV"
module load cuda13.0/toolkit/13.0.2
cd "$PROJECT_DIR"

PAIR="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python "$SHARED_DIR/plan_pairs.py" --manifest "$MANIFEST" | python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["pairs"][int(sys.argv[1])]))' "$SLURM_ARRAY_TASK_ID")"
SHARD_A="$(python - "$PAIR" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["shard_a"])
PY
)"
SHARD_B="$(python - "$PAIR" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["shard_b"])
PY
)"
PAIR_ARGS=("$MANIFEST" --shard-a "$SHARD_A" --shard-b "$SHARD_B")
[ "$RETRY_FAILED" -eq 1 ] && PAIR_ARGS+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && PAIR_ARGS+=(--retry-incomplete)
[ "$SERVICE_SMOKE" -eq 1 ] && PAIR_ARGS+=(--service-smoke)
exec bash "$SHARED_DIR/submit_pair.slurm" "${PAIR_ARGS[@]}"
