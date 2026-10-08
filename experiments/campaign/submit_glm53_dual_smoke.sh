#!/usr/bin/env bash
set -euo pipefail

# Submit one four-B200 GLM/vLLM plus Ollama co-location smoke. This validates
# the campaign service topology without loading a manifest or running cells.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL_ID="dealignai/GLM-5.3-Flash-ABLITERATED-FP8"
SIF_IMAGE="/hpc/home/ais/hsali/llm_images/vllm-openai-glm53-flash.sif"
TARGET_MODEL="gemma3:4b"
JUDGE_MODEL="qwen3.5:9b"
OUTPUT_DIR=""
TIME_LIMIT="01:00:00"
NODELIST=""
DRY_RUN=0

usage() {
    printf 'Usage: %s [--output-dir PATH] [--sif-image PATH] [--target-model ID] [--judge-model ID] [--time VALUE] [--nodelist NODE[,NODE...]] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
        --sif-image) SIF_IMAGE="${2:-}"; shift 2 ;;
        --target-model) TARGET_MODEL="${2:-}"; shift 2 ;;
        --judge-model) JUDGE_MODEL="${2:-}"; shift 2 ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --nodelist) NODELIST="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$SIF_IMAGE" ] || { usage; exit 2; }
[ -n "$TARGET_MODEL" ] || { usage; exit 2; }
[ -n "$JUDGE_MODEL" ] || { usage; exit 2; }
[ -n "$TIME_LIMIT" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="$PROJECT_DIR/logs/vllm_smoke/glm53_flash_abliterated_fp8_dual"
fi

SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    --job-name=glm53_dual_smoke
    "--time=$TIME_LIMIT"
    "--output=$OUTPUT_DIR/slurm_%A.out"
    "--error=$OUTPUT_DIR/slurm_%A.err"
)
[ -n "$NODELIST" ] && SBATCH_CMD+=("--nodelist=$NODELIST")
SBATCH_CMD+=(
    "$SCRIPT_DIR/submit_glm53_dual_smoke.slurm"
    "$MODEL_ID"
    "$SIF_IMAGE"
    "$TARGET_MODEL"
    "$JUDGE_MODEL"
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
