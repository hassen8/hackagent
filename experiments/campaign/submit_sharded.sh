#!/usr/bin/env bash
set -euo pipefail

# Immutable-manifest submission wrapper.  Legacy R4 keeps its exact contract;
# extension manifests use their declared scope and serving/resource metadata.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST=""
SHARD_ID=""
ARRAY=0
DRY_RUN=0
LIMIT=""
STAGE_OUTPUT=""
RETRY_FAILED=0
RETRY_INCOMPLETE=0
SERVICE_SMOKE=0
RESOURCE_ARGS=()

usage() {
    printf 'Usage: %s --manifest PATH [--shard-id SHARD_ID | --array] [options]\n' "$0" >&2
    printf '  --manifest PATH       required immutable shard manifest\n' >&2
    printf '  --shard-id SHARD_ID   submit one shard\n' >&2
    printf '  --array               submit the complete shard array\n' >&2
    printf '  --limit N             bounded runner test limit\n' >&2
    printf '  --retry-failed       retry cells with failed harvests\n' >&2
    printf '  --retry-incomplete   retry planned cells without harvests\n' >&2
    printf '  --service-smoke      start services and verify model readiness only\n' >&2
    printf '  --stage-output PATH   write the report-only staging output\n' >&2
    printf '  --dry-run             print the sbatch command without submitting\n' >&2
    printf '  --gres VALUE          override the Slurm GPU resource\n' >&2
    printf '  --time VALUE          override the Slurm time limit\n' >&2
}

require_value() {
    if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        usage
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest)
            require_value "$@"
            MANIFEST="$2"
            shift 2
            ;;
        --shard-id)
            require_value "$@"
            SHARD_ID="$2"
            shift 2
            ;;
        --array)
            ARRAY=1
            shift
            ;;
        --limit)
            require_value "$@"
            case "$2" in ''|*[!0-9]*) printf '%s requires a non-negative integer\n' "$1" >&2; exit 2;; esac
            LIMIT="$2"
            shift 2
            ;;
        --retry-failed)
            RETRY_FAILED=1
            shift
            ;;
        --retry-incomplete)
            RETRY_INCOMPLETE=1
            shift
            ;;
        --service-smoke)
            SERVICE_SMOKE=1
            shift
            ;;
        --stage-output)
            require_value "$@"
            STAGE_OUTPUT="$2"
            shift 2
            ;;
        --dry-run|--test-only)
            DRY_RUN=1
            shift
            ;;
        --gres|--time)
            require_value "$@"
            RESOURCE_ARGS+=("${1#--}=$2")
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
done

if [ -z "$MANIFEST" ]; then
    printf '%s\n' '--manifest is required' >&2
    exit 2
fi
if [ "$ARRAY" -eq 1 ] && [ -n "$SHARD_ID" ]; then
    printf '%s\n' '--array and --shard-id are mutually exclusive' >&2
    exit 2
fi
if [ "$ARRAY" -eq 0 ] && [ -z "$SHARD_ID" ]; then
    printf '%s\n' 'choose exactly one of --shard-id or --array' >&2
    exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
export R4_MANIFEST="$MANIFEST"
export R4_SHARD_ID="$SHARD_ID"
export R4_ARRAY="$ARRAY"
export R4_STAGE_OUTPUT="$STAGE_OUTPUT"
export R4_DRY_RUN="$DRY_RUN"
cd "$PROJECT_DIR"

uv run --no-sync python - <<'PY'
from pathlib import Path
import os

from experiments.campaign.sharding import (
    LEGACY_EXECUTION_SCOPE_ID,
    load_shard_manifest,
    validate_shard_manifest,
)

manifest = load_shard_manifest(os.environ["R4_MANIFEST"])
validate_shard_manifest(manifest)
scope = manifest["execution_scope_id"]
if scope == LEGACY_EXECUTION_SCOPE_ID:
    if manifest["expected_cell_count"] != 510 or manifest["expected_goal_evaluation_count"] != 5100:
        raise SystemExit("manifest does not satisfy the 510-cell/5100-evaluation contract")
    if tuple(manifest["attacks"]) != ("tap", "crescendo", "advprefix"):
        raise SystemExit("manifest attack scope is invalid")
    if len(manifest["attacker_keys"]) != 5:
        raise SystemExit("manifest must contain exactly five attackers")
    if any("deepseek" in str(key).lower() for key in manifest["attacker_keys"]):
        raise SystemExit("DeepSeek is forbidden in the legacy R4 manifest")
else:
    if not manifest["attacker_keys"]:
        raise SystemExit("extension manifest must declare at least one attacker")
    if not manifest["attacks"] or not manifest["repetitions"]:
        raise SystemExit("extension manifest must declare attacks and repetitions")
    serving = manifest.get("serving_requirements")
    if not isinstance(serving, dict):
        raise SystemExit("extension manifest is missing serving requirements")
    roles = serving.get("roles", {})
    if roles.get("target", {}).get("serving_framework") != "ollama":
        raise SystemExit("extension target must remain an Ollama role")
    if roles.get("judge", {}).get("serving_framework") != "ollama":
        raise SystemExit("extension judge must remain an Ollama role")
    attackers = roles.get("attackers", {})
    if set(attackers) != set(manifest["attacker_keys"]):
        raise SystemExit("extension serving attacker keys do not match the manifest")
    for key, role in attackers.items():
        framework = role.get("serving_framework")
        agent_type = role.get("agent_type")
        endpoint_env = role.get("endpoint_env")
        if framework == "vllm" and agent_type == "OPENAI_SDK":
            continue
        if (
            framework == "ollama"
            and agent_type == "OLLAMA"
            and endpoint_env in {"OLLAMA_ENDPOINT", "OLLAMA_ATTACKER_ENDPOINT"}
        ):
            continue
        raise SystemExit(f"extension attacker {key} has an unsupported serving role")
if os.environ["R4_ARRAY"] == "0":
    if not any(entry["shard_id"] == os.environ["R4_SHARD_ID"] for entry in manifest["shards"]):
        raise SystemExit("unknown shard ID")
print(f"manifest validated scope={scope} cells={manifest['expected_cell_count']} evaluations={manifest['expected_goal_evaluation_count']}")
PY

STAGE_ARGS=()
if [ -n "$STAGE_OUTPUT" ]; then
    STAGE_ARGS+=(--output "$STAGE_OUTPUT")
fi
# The existing staging script is Ollama-specific.  Preserve it for legacy R4;
# the extension manifest itself is the offline role/resource staging contract
# until the W3 dual-service worker is installed.
EXTENSION_SCOPE="$(uv run --no-sync python -c 'import json, os; print(json.load(open(os.environ["R4_MANIFEST"]))["execution_scope_id"])')"
if [ "$EXTENSION_SCOPE" = "omnisafebench_af340_five_attackers_v1" ]; then
    bash "$SCRIPT_DIR/stage.sh" "${STAGE_ARGS[@]}"
else
    printf 'Skipping Ollama-only staging for extension scope %s; manifest role/resource checks passed.\n' "$EXTENSION_SCOPE"
fi

CAMPAIGN_ROOT="$(uv run --no-sync python -c 'from pathlib import Path; import os; print(Path(os.environ["R4_MANIFEST"]).expanduser().resolve().parent)')"
LOG_DIR="$CAMPAIGN_ROOT/slurm"
JOB_NAME="hackagent_r4"
DEFAULT_GRES=""
if [ "$EXTENSION_SCOPE" != "omnisafebench_af340_five_attackers_v1" ]; then
    JOB_NAME="hackagent_extension"
    DEFAULT_GRES="$(uv run --no-sync python - "$MANIFEST" "$SHARD_ID" "$ARRAY" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
if sys.argv[3] == "0":
    shard = next(item for item in manifest["shards"] if item["shard_id"] == sys.argv[2])
    count = shard["serving_requirements"]["resources"]["attacker"].get("worker_gpu_count", 1)
    gpu_type = shard["serving_requirements"]["resources"]["attacker"].get("gpu_type", "h200")
else:
    count = max(
        item["serving_requirements"]["resources"]["attacker"].get("worker_gpu_count", 1)
        for item in manifest["shards"]
    )
    gpu_type = next(iter({
        item["serving_requirements"]["resources"]["attacker"].get("gpu_type", "h200")
        for item in manifest["shards"]
    }))
print(f"gres=gpu:{gpu_type}:{int(count)}")
PY
)"
    if [ "${#RESOURCE_ARGS[@]}" -eq 0 ]; then
        RESOURCE_ARGS+=("$DEFAULT_GRES" "cpus-per-task=32" "mem=400G")
    elif ! printf '%s\n' "${RESOURCE_ARGS[@]}" | grep -q '^gres='; then
        RESOURCE_ARGS=("$DEFAULT_GRES" "${RESOURCE_ARGS[@]}")
    fi
fi
SBATCH_CMD=(sbatch "--chdir=$PROJECT_DIR" "--job-name=$JOB_NAME" "--output=$LOG_DIR/sharded_%A_%a.out" "--error=$LOG_DIR/sharded_%A_%a.err")
for resource in "${RESOURCE_ARGS[@]}"; do
    SBATCH_CMD+=("--$resource")
done
if [ "$ARRAY" -eq 1 ]; then
    SHARD_COUNT="$(uv run --no-sync python -c 'import json, os; print(len(json.load(open(os.environ["R4_MANIFEST"]))["shards"]))')"
    SBATCH_CMD+=("--array=0-$((SHARD_COUNT - 1))")
fi
SBATCH_CMD+=("$SCRIPT_DIR/submit_sharded.slurm" "$MANIFEST")
if [ -n "$SHARD_ID" ]; then
    SBATCH_CMD+=(--shard-id "$SHARD_ID")
fi
if [ -n "$LIMIT" ]; then
    SBATCH_CMD+=(--limit "$LIMIT")
fi
if [ "$RETRY_FAILED" -eq 1 ]; then
    SBATCH_CMD+=(--retry-failed)
fi
if [ "$RETRY_INCOMPLETE" -eq 1 ]; then
    SBATCH_CMD+=(--retry-incomplete)
fi
if [ "$SERVICE_SMOKE" -eq 1 ]; then
    SBATCH_CMD+=(--service-smoke)
fi

printf 'Resolved sbatch command:'
printf ' %q' "${SBATCH_CMD[@]}"
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi
mkdir -p "$LOG_DIR"
"${SBATCH_CMD[@]}"
