#!/usr/bin/env bash
set -euo pipefail

# Submit the bounded two-node DeepSeek vLLM smoke. The batch script exits after
# one model-list check and one chat completion; it does not run the campaign.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL_ID="dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4"
OUTPUT_DIR=""
DRY_RUN=0

usage() {
    printf 'Usage: %s [--model-id ID] [--output-dir PATH] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --model-id) MODEL_ID="${2:-}"; shift 2 ;;
        --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MODEL_ID" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="$PROJECT_DIR/logs/vllm_smoke/deepseek_multinode"
fi

SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    --job-name=deepseek_2node_smoke
    --time=01:00:00
    "--output=$OUTPUT_DIR/slurm_%A.out"
    "--error=$OUTPUT_DIR/slurm_%A.err"
    "$SCRIPT_DIR/submit_deepseek_multinode_smoke.slurm"
    "$MODEL_ID"
    "$OUTPUT_DIR"
)

printf 'Resolved sbatch command:'
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi

mkdir -p "$OUTPUT_DIR"
"${SBATCH_CMD[@]}"
