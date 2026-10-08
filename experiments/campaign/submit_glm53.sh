#!/usr/bin/env bash
set -euo pipefail

# Submit a GLM-5.3 campaign shard or homogeneous GLM array. The immutable
# manifest owns shard/cell selection; this wrapper only validates GLM affinity
# and resolves the model-specific Apptainer launcher.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST=""
SHARD_ID=""
ARRAY=0
LIMIT=""
SERVICE_SMOKE=0
TIME_LIMIT="18:00:00"
NODELIST=""
DRY_RUN=0

usage() {
    printf 'Usage: %s --manifest PATH (--shard-id SHARD_ID | --array) [--limit N] [--service-smoke] [--time VALUE] [--nodelist NODE[,NODE...]] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --shard-id) SHARD_ID="${2:-}"; shift 2 ;;
        --array) ARRAY=1; shift ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
        --nodelist) NODELIST="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$MANIFEST" ] || { usage; exit 2; }
[ "$ARRAY" -eq 1 ] && [ -n "$SHARD_ID" ] && {
    printf '%s\n' '--array and --shard-id are mutually exclusive' >&2
    exit 2
}
[ "$ARRAY" -eq 0 ] && [ -z "$SHARD_ID" ] && {
    printf '%s\n' 'choose exactly one of --shard-id or --array' >&2
    exit 2
}
cd "$PROJECT_DIR"

if [ "$ARRAY" -eq 1 ]; then
    ARRAY_SPEC="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" <<'PY'
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
if "glm53_flash_abliterated_fp8" not in manifest.get("attacker_keys", []):
    raise SystemExit("manifest has no GLM-5.3 attacker")
indices = [
    index
    for index, entry in enumerate(manifest["shards"])
    if entry.get("attacker_key") == "glm53_flash_abliterated_fp8"
]
if not indices:
    raise SystemExit("manifest has no GLM-5.3 shards")
ranges = []
start = previous = indices[0]
for index in indices[1:]:
    if index != previous + 1:
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = index
    previous = index
ranges.append(str(start) if start == previous else f"{start}-{previous}")
print(",".join(ranges))
PY
)"
    printf 'validated GLM-5.3 array=%s\n' "$ARRAY_SPEC"
else
    PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
if manifest["execution_scope_id"] == "omnisafebench_af340_five_attackers_v1":
    raise SystemExit("GLM-5.3 launcher requires an extension manifest")
entry = next((item for item in manifest["shards"] if item["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
if entry["attacker_key"] != "glm53_flash_abliterated_fp8":
    raise SystemExit(
        "selected shard is assigned to " + str(entry["attacker_key"])
        + ", not glm53_flash_abliterated_fp8"
    )
print(f"validated GLM-5.3 shard={entry['shard_id']}")
PY
fi

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/slurm"
if [ "$ARRAY" -eq 1 ]; then
    OUTPUT="$LOG_DIR/glm53_%A_%a.out"
    ERROR="$LOG_DIR/glm53_%A_%a.err"
else
    OUTPUT="$LOG_DIR/glm53_%A.out"
    ERROR="$LOG_DIR/glm53_%A.err"
fi
SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    "--time=$TIME_LIMIT"
    --job-name=hackagent_glm53
    "--output=$OUTPUT"
    "--error=$ERROR"
)
[ "$ARRAY" -eq 1 ] && SBATCH_CMD+=("--array=$ARRAY_SPEC")
[ -n "$NODELIST" ] && SBATCH_CMD+=("--nodelist=$NODELIST")
SBATCH_CMD+=(
    "$SCRIPT_DIR/submit_glm53.slurm"
    "$MANIFEST"
)
[ "$ARRAY" -eq 0 ] && SBATCH_CMD+=(--shard-id "$SHARD_ID")
[ -n "$LIMIT" ] && SBATCH_CMD+=(--limit "$LIMIT")
[ "$SERVICE_SMOKE" -eq 1 ] && SBATCH_CMD+=(--service-smoke)

printf 'Resolved sbatch command:'
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi
mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
