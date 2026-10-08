#!/usr/bin/env bash
set -euo pipefail
INPUT=""; OUTPUT=""; DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --input) INPUT="${2:?missing input}"; shift 2;;
    --output) OUTPUT="${2:?missing output}"; shift 2;;
    --dry-run|--test-only) DRY_RUN=1; shift;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2;;
  esac
done
[ -n "$INPUT" ] && [ -n "$OUTPUT" ] || { printf 'Usage: %s --input PATH --output DIR [--dry-run]\n' "$0" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
INPUT="$(readlink -f "$INPUT")"
OUTPUT="$(readlink -m "$OUTPUT")"
SOURCE_DIR="$(dirname "$INPUT")"
if [ "$OUTPUT" = "$INPUT" ] || [ "$OUTPUT" = "$SOURCE_DIR" ] || [ "$OUTPUT/results.csv" = "$INPUT" ]; then
  printf 'validation output must be a separate directory from the input results.csv\n' >&2
  exit 2
fi
case "$INPUT" in
  "$OUTPUT"/*)
    printf 'validation output must not contain the input results.csv\n' >&2
    exit 2
    ;;
esac
cd "$PROJECT_DIR"
mkdir -p "$OUTPUT/slurm"
CMD=(
  sbatch
  "--output=$OUTPUT/slurm/slurm_%j.out"
  "--error=$OUTPUT/slurm/slurm_%j.err"
  "$PROJECT_DIR/experiments/campaign/submit_validation.slurm"
  "$INPUT"
  "$OUTPUT"
)
printf 'R5 validation: one gpu:b200:1, private Ollama endpoint, no pulls, models=gemma4:12b ministral-3:14b llama3.1:8b\n'
printf 'input=%s\noutput=%s\ncommand=' "$INPUT" "$OUTPUT"
printf '%q ' "${CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 0 ]; then
  exec "${CMD[@]}"
fi
