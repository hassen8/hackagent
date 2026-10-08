#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST=""
SHARD_ID=""
ARRAY=0
LIMIT=""
DRY_RUN=0
SERVICE_SMOKE=0
TIME_LIMIT="06:00:00"

usage() {
    printf 'Usage: %s --manifest PATH (--shard-id SHARD_ID | --array) [--limit N] [--service-smoke] [--time VALUE] [--dry-run]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --shard-id) SHARD_ID="${2:-}"; shift 2 ;;
        --array) ARRAY=1; shift ;;
        --limit) LIMIT="${2:-}"; shift 2 ;;
        --service-smoke) SERVICE_SMOKE=1; shift ;;
        --time) TIME_LIMIT="${2:-}"; shift 2 ;;
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
if manifest["execution_scope_id"] == "omnisafebench_af340_five_attackers_v1":
    raise SystemExit("Qwen BF16 one-GPU launcher requires an extension manifest")
indices = [
    index
    for index, entry in enumerate(manifest["shards"])
    if entry.get("attacker_key") == "qwen38_27b_abliterated_bf16"
]
if not indices:
    raise SystemExit("manifest has no Qwen BF16 shards")
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
    printf 'validated Qwen BF16 one-GPU array=%s\n' "$ARRAY_SPEC"
else
    PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_ID" <<'PY'
import sys

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest

manifest = load_shard_manifest(sys.argv[1])
validate_shard_manifest(manifest)
if manifest["execution_scope_id"] == "omnisafebench_af340_five_attackers_v1":
    raise SystemExit("Qwen BF16 one-GPU launcher requires an extension manifest")
entry = next((item for item in manifest["shards"] if item["shard_id"] == sys.argv[2]), None)
if entry is None:
    raise SystemExit("unknown shard ID")
if entry["attacker_key"] != "qwen38_27b_abliterated_bf16":
    raise SystemExit(
        "selected shard is assigned to " + str(entry["attacker_key"]) +
        ", not qwen38_27b_abliterated_bf16"
    )
print(f"validated Qwen BF16 one-GPU shard={entry['shard_id']}")
PY
fi

ROOT="$(dirname "$MANIFEST")"
LOG_DIR="$ROOT/slurm"
if [ "$ARRAY" -eq 1 ]; then
    OUTPUT="$LOG_DIR/qwen38_1gpu_%A_%a.out"
    ERROR="$LOG_DIR/qwen38_1gpu_%A_%a.err"
else
    OUTPUT="$LOG_DIR/qwen38_1gpu_%A.out"
    ERROR="$LOG_DIR/qwen38_1gpu_%A.err"
fi
SBATCH_CMD=(
    sbatch
    "--chdir=$PROJECT_DIR"
    "--time=$TIME_LIMIT"
    "--job-name=hackagent_qwen38_1gpu"
    "--output=$OUTPUT"
    "--error=$ERROR"
)
[ "$ARRAY" -eq 1 ] && SBATCH_CMD+=("--array=$ARRAY_SPEC")
SBATCH_CMD+=(
    "$SCRIPT_DIR/submit_qwen38_bf16_1gpu.slurm"
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
