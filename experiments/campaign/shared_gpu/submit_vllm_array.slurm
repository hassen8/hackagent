#!/usr/bin/env bash
set -euo pipefail
MANIFEST="$1"; shift
RETRY_FAILED=0; RETRY_INCOMPLETE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --retry-failed) RETRY_FAILED=1; shift;;
    --retry-incomplete) RETRY_INCOMPLETE=1; shift;;
    *) break;;
  esac
done
[ -n "${SLURM_ARRAY_TASK_ID:-}" ] || exit 2
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign/shared_gpu"
cd "$PROJECT_DIR"
PAIR="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python "$SCRIPT_DIR/plan_pairs.py" --manifest "$MANIFEST" | python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["pairs"][int(sys.argv[1])]))' "$SLURM_ARRAY_TASK_ID")"
A="$(python -c 'import json,sys; print(json.load(sys.stdin)["shard_a"])' <<<"$PAIR")"; B="$(python -c 'import json,sys; print(json.load(sys.stdin)["shard_b"])' <<<"$PAIR")"
PAIR_ARGS=("$MANIFEST" --shard-a "$A" --shard-b "$B")
[ "$RETRY_FAILED" -eq 1 ] && PAIR_ARGS+=(--retry-failed)
[ "$RETRY_INCOMPLETE" -eq 1 ] && PAIR_ARGS+=(--retry-incomplete)
exec bash "$SCRIPT_DIR/submit_vllm_pair.slurm" "${PAIR_ARGS[@]}" "$@"
