#!/usr/bin/env bash
set -euo pipefail

# W10 submission boundary.
#
# The wrapper has two jobs: stage/verify the exact selected model set on the
# login node, then submit the existing matrix runner to Slurm.  It never owns
# attack logic or result state.  --dry-run/--test-only stops before sbatch.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGE_SCRIPT="$SCRIPT_DIR/stage.sh"
BATCH_SCRIPT="$SCRIPT_DIR/submit.slurm"
PULL=0
ALLOW_FULL_MATRIX=0
DRY_RUN=0
STAGE_OUTPUT=""
RUN_ARGS=()
STAGE_ARGS=()
HAS_SELECTOR=0
HAS_LIMIT=0

# ---------------------------------------------------------------------------
# Submission argument parsing and safety gates
# ---------------------------------------------------------------------------
usage() {
    printf 'Usage: %s [submission-options] [matrix-selectors]\n' "$0" >&2
    printf 'This entry point always submits the existing campaign in matrix mode.\n' >&2
    printf 'Submission options:\n' >&2
    printf '  --pull                 pull missing resolved models before submission\n' >&2
    printf '  --allow-full-matrix    explicitly permit an unfiltered matrix request\n' >&2
    printf '  --stage-output PATH    write the staging report to PATH\n' >&2
    printf '  --dry-run|--test-only  print the resolved sbatch command only\n' >&2
    printf 'Runner selectors:\n' >&2
    printf '  --target KEY --attacker KEY --attack NAME --repetition N\n' >&2
    printf '  --limit N --retry-failed --retry-incomplete\n' >&2
}

require_value() {
    if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        usage
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --pull)
            PULL=1
            shift
            ;;
        --allow-full-matrix)
            ALLOW_FULL_MATRIX=1
            shift
            ;;
        --dry-run|--test-only)
            DRY_RUN=1
            shift
            ;;
        --stage-output|--output)
            require_value "$@"
            STAGE_OUTPUT="$2"
            shift 2
            ;;
        --target|--attacker|--attack|--repetition)
            require_value "$@"
            RUN_ARGS+=("$1" "$2")
            STAGE_ARGS+=("$1" "$2")
            HAS_SELECTOR=1
            shift 2
            ;;
        --limit)
            require_value "$@"
            case "$2" in
                ''|*[!0-9]*)
                    printf '%s requires a non-negative integer\n' "$1" >&2
                    exit 2
                    ;;
            esac
            RUN_ARGS+=("$1" "$2")
            HAS_LIMIT=1
            shift 2
            ;;
        --retry-failed|--retry-incomplete)
            RUN_ARGS+=("$1")
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --matrix)
            printf '%s is implicit for this matrix-only submission wrapper\n' "$1" >&2
            exit 2
            ;;
        --execute|--dry-run-runner)
            printf 'runner mode option %s is not accepted; this wrapper supplies --execute --matrix\n' "$1" >&2
            exit 2
            ;;
        *)
            printf 'Unknown submission option: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
done

if [ "$HAS_SELECTOR" -eq 0 ] && [ "$HAS_LIMIT" -eq 0 ] && [ "$ALLOW_FULL_MATRIX" -ne 1 ]; then
    printf 'unfiltered full-matrix submission requires --allow-full-matrix\n' >&2
    exit 2
fi

# Keep staging arguments separate from runner arguments.  Staging validates the
# complete selector-matched slice; the runner alone applies --limit after its
# JSONL resume skips.
export PYTHONDONTWRITEBYTECODE=1
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11435}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
export HACKAGENT_ALLOW_FULL_MATRIX="$ALLOW_FULL_MATRIX"
cd "$PROJECT_DIR"
CAMPAIGN_NAME="$(uv run --no-sync python -c 'from experiments.campaign.campaign_config import CAMPAIGN_NAME; print(CAMPAIGN_NAME)')"
export HACKAGENT_CAMPAIGN_NAME="$CAMPAIGN_NAME"

if [ "$PULL" -eq 1 ]; then
    STAGE_ARGS+=(--pull)
fi
if [ -n "$STAGE_OUTPUT" ]; then
    STAGE_ARGS+=(--output "$STAGE_OUTPUT")
fi

# Readiness is established before sbatch.  submit.slurm assumes the model store
# is already staged and contains no pull operation.
printf 'Staging selected matrix slice before submission...\n'
bash "$STAGE_SCRIPT" "${STAGE_ARGS[@]}"

# Slurm logs are campaign-scoped operational evidence.  They do not replace
# the campaign JSONL/CSV records or the HackAgent SQLite evidence store.
LOG_DIR="${HACKAGENT_SLURM_LOG_DIR:-$PROJECT_DIR/logs/campaigns/$CAMPAIGN_NAME/slurm}"
SBATCH_CMD=(
    sbatch
    "--job-name=hackagent_${CAMPAIGN_NAME}"
    "--output=$LOG_DIR/slurm_%j.out"
    "--error=$LOG_DIR/slurm_%j.err"
    "$BATCH_SCRIPT"
    "${RUN_ARGS[@]}"
)

printf 'Resolved sbatch command:'
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
printf 'Batch runner: uv run --no-sync python experiments/campaign/campaign.py --execute --matrix'
printf ' %q' "${RUN_ARGS[@]}"
printf '\n'
printf 'Batch environment: HACKAGENT_AUTO_PULL_MODELS=0 OLLAMA_HOST=%q OLLAMA_MODELS=%q\n' \
    "$OLLAMA_HOST" "$OLLAMA_MODELS"

if [ "$DRY_RUN" -eq 1 ]; then
    # The staging check has still happened; this branch only suppresses job
    # submission after printing the exact command that would be used.
    exit 0
fi

mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
