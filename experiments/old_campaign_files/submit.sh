#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/hpc/home/ais/hsali/hackagent"
HERE="$PROJECT_DIR/experiments/campaign"
MANIFEST="$HERE/campaign.json"
PULL=0

usage() {
    printf 'Usage: %s [--manifest PATH] [--pull] [runner options...]\n' "$0" >&2
    printf '  --pull  stage missing local Ollama models before sbatch\n' >&2
}

RUN_ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            MANIFEST="$2"
            shift 2
            ;;
        --pull)
            PULL=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            RUN_ARGS+=("$1")
            shift
            ;;
    esac
done

[ -f "$MANIFEST" ] || { printf 'Manifest not found: %s\n' "$MANIFEST" >&2; exit 1; }

if [ "$PULL" -eq 1 ]; then
    OLLAMA_HOST="127.0.0.1:11435" \
    OLLAMA_MODELS="${HOME}/models" \
        uv run --no-sync python "$HERE/stage.py" --manifest "$MANIFEST" --pull
else
    printf 'No model downloads requested. Use --pull to stage missing local models.\n'
fi

exec sbatch \
    --job-name="hackagent_campaign" \
    "$HERE/submit.slurm" "$MANIFEST" "${RUN_ARGS[@]}"
