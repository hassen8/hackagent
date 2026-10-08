#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST=""
TIME_LIMIT="18:00:00"
DRY_RUN=0

usage() {
    printf 'Usage: %s --manifest PATH [--time VALUE] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] || { usage; exit 2; }
cd "$PROJECT_DIR"

printf 'DeepSeek submission:\n'
bash "$SCRIPT_DIR/submit_deepseek_v4_flash.sh" \
    --manifest "$MANIFEST" --array --time "$TIME_LIMIT" --dry-run
printf 'GLM submission:\n'
bash "$SCRIPT_DIR/submit_glm53.sh" \
    --manifest "$MANIFEST" --array --time "$TIME_LIMIT" --dry-run

[ "$DRY_RUN" -eq 1 ] && exit 0

bash "$SCRIPT_DIR/submit_deepseek_v4_flash.sh" \
    --manifest "$MANIFEST" --array --time "$TIME_LIMIT"
bash "$SCRIPT_DIR/submit_glm53.sh" \
    --manifest "$MANIFEST" --array --time "$TIME_LIMIT"
