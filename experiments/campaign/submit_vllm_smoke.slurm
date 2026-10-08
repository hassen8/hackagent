#!/usr/bin/env bash
#SBATCH --job-name=hackagent_vllm_smoke
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --gres=gpu:h200:2
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=02:00:00

set -euo pipefail

PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
[ -f "$PYTHON_ENV" ] || {
    printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2
    exit 1
}
source "$PYTHON_ENV"
MANIFEST="${1:-}"
shift || true
[ -n "$MANIFEST" ] || { printf 'manifest is required\n' >&2; exit 2; }
exec bash "$SCRIPT_DIR/submit_sharded.slurm" \
    "$MANIFEST" "$@" --service-smoke
