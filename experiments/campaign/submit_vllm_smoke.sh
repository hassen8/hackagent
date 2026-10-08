#!/usr/bin/env bash
set -euo pipefail

# Submit one bounded dual-service smoke for a selected extension shard.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST=""
SHARD_ID=""
DRY_RUN=0
RESOURCE_ARGS=()

usage() {
    printf 'Usage: %s --manifest PATH --shard-id SHARD_ID [--dry-run] [--gres VALUE] [--time VALUE]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --shard-id) SHARD_ID="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --gres|--time) RESOURCE_ARGS+=("${1#--}=${2:-}"); shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] && [ -n "$SHARD_ID" ] || { usage; exit 2; }
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

SCOPE="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" <<'PY'
import json
import sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest
manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
if manifest["execution_scope_id"] == "omnisafebench_af340_five_attackers_v1":
    raise SystemExit("service smoke requires an extension manifest")
print(manifest["execution_scope_id"])
PY
)"

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/slurm"
SBATCH_CMD=(sbatch "--chdir=$PROJECT_DIR" "--job-name=hackagent_vllm_smoke" "--output=$LOG_DIR/vllm_smoke_%A.out" "--error=$LOG_DIR/vllm_smoke_%A.err")
for resource in "${RESOURCE_ARGS[@]}"; do
    SBATCH_CMD+=("--$resource")
done
if [ "${#RESOURCE_ARGS[@]}" -eq 0 ]; then
    RESOURCE_SPEC="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import json
import sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text())
shard = next(item for item in manifest["shards"] if item["shard_id"] == sys.argv[2])
resource = shard["serving_requirements"]["resources"]["attacker"]
print(resource.get("gpu_type", "h200"), resource.get("worker_gpu_count", 1))
PY
    )"
    read -r GPU_TYPE WORKER_GPU_COUNT <<<"$RESOURCE_SPEC"
    SBATCH_CMD+=("--gres=gpu:$GPU_TYPE:$WORKER_GPU_COUNT" --cpus-per-task=32 --mem=400G)
fi
SBATCH_CMD+=("$SCRIPT_DIR/submit_vllm_smoke.slurm" "$MANIFEST" --shard-id "$SHARD_ID")

printf 'scope=%s\nResolved sbatch command:' "$SCOPE"
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi
mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
