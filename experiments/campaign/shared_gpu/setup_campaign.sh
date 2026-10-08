#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CAMPAIGN_ROOT=""
CAMPAIGN_NAME=""
EXECUTION_SCOPE=""
TARGET_KEY="qwen38_27b"
SHARD_COUNT=""
ATTACKER_KEYS=()

usage() {
    printf 'Usage: %s --campaign-root PATH --campaign-name NAME --execution-scope ID --attacker-key KEY... [options]\n' "$0" >&2
    printf '  --target-key KEY       default: qwen38_27b\n' >&2
    printf '  --shard-count N        default: attacker-count x 6\n' >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --campaign-root) CAMPAIGN_ROOT="${2:-}"; shift 2 ;;
        --campaign-name) CAMPAIGN_NAME="${2:-}"; shift 2 ;;
        --execution-scope) EXECUTION_SCOPE="${2:-}"; shift 2 ;;
        --target-key) TARGET_KEY="${2:-}"; shift 2 ;;
        --shard-count) SHARD_COUNT="${2:-}"; shift 2 ;;
        --attacker-key) ATTACKER_KEYS+=("${2:-}"); shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$CAMPAIGN_ROOT" ] && [ -n "$CAMPAIGN_NAME" ] && [ -n "$EXECUTION_SCOPE" ] && [ "${#ATTACKER_KEYS[@]}" -gt 0 ] || {
    usage
    exit 2
}
[ -n "$SHARD_COUNT" ] || SHARD_COUNT=$(( ${#ATTACKER_KEYS[@]} * 6 ))
case "$SHARD_COUNT" in ''|*[!0-9]*) printf '%s\n' '--shard-count must be an integer' >&2; exit 2;; esac
[ $((SHARD_COUNT % ${#ATTACKER_KEYS[@]})) -eq 0 ] || {
    printf 'shard count must divide evenly across attackers\n' >&2
    exit 2
}
shards_per_attacker=$((SHARD_COUNT / ${#ATTACKER_KEYS[@]}))
[ $((shards_per_attacker % 2)) -eq 0 ] || {
    printf 'each attacker must receive an even shard count for pairing\n' >&2
    exit 2
}

cd "$PROJECT_DIR"
HACKAGENT_CAMPAIGN_NAME="$CAMPAIGN_NAME" \
HACKAGENT_EXECUTION_SCOPE_ID="$EXECUTION_SCOPE" \
HACKAGENT_TARGET_KEY="$TARGET_KEY" \
HACKAGENT_ATTACKER_KEYS="$(IFS=,; printf '%s' "${ATTACKER_KEYS[*]}")" \
HACKAGENT_SEPARATE_OLLAMA_ATTACKER=0 \
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - <<'PY'
from experiments.campaign import attack_adapters, campaign

campaign.validate_configuration()
selected = {model["key"]: model for model in campaign.attackers}
requested = set(campaign.ACTIVE_ATTACKER_KEYS or ())
if set(selected) != requested:
    raise SystemExit("attacker selection did not resolve to the requested active keys")
for key, model in selected.items():
    if attack_adapters.serving_framework(model) != "ollama":
        raise SystemExit(f"shared-GPU campaign requires Ollama attacker: {key}")
print(f"validated {len(selected)} Ollama attackers and target={campaign.targets[0]['key']}")
PY
mkdir -p "$CAMPAIGN_ROOT"
export HACKAGENT_CAMPAIGN_NAME="$CAMPAIGN_NAME"
export HACKAGENT_EXECUTION_SCOPE_ID="$EXECUTION_SCOPE"
export HACKAGENT_TARGET_KEY="$TARGET_KEY"
export HACKAGENT_ATTACKER_KEYS="$(IFS=,; printf '%s' "${ATTACKER_KEYS[*]}")"
export HACKAGENT_SEPARATE_OLLAMA_ATTACKER=0

ARGS=(
    --write-manifest "$CAMPAIGN_ROOT/shard-manifest.json"
    --shard-count "$SHARD_COUNT"
    --execution-scope-id "$EXECUTION_SCOPE"
)
for attacker_key in "${ATTACKER_KEYS[@]}"; do ARGS+=(--attacker-key "$attacker_key"); done
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python experiments/campaign/sharding.py "${ARGS[@]}"
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python "$SCRIPT_DIR/plan_pairs.py" \
    --manifest "$CAMPAIGN_ROOT/shard-manifest.json" \
    --output "$CAMPAIGN_ROOT/shared_gpu/pair-plan.json"
