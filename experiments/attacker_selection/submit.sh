#!/usr/bin/env bash
#
# Wrapper for attacker-selection experiments.
#
# Responsibilities (all on the LOGIN node, before the job is queued):
#   1. Resolve the required model list from attackers.json - never hardcoded here,
#      so adding a candidate is a JSON edit and needs no change to this script.
#   2. Pull any missing model into the user-owned store.
#      This happens BEFORE sbatch on purpose: an on-demand pull inside a timed job
#      would silently corrupt the latency metrics that are a primary ranking criterion.
#   3. Submit the Slurm job.
#
# Usage:
#   bash experiments/attacker_selection/submit.sh <script_name> [max_tier]
#
# Examples:
#   bash experiments/attacker_selection/submit.sh tier0_smoke 0
#   bash experiments/attacker_selection/submit.sh tier1_pair  1

set -euo pipefail

SCRIPT_NAME="${1:-}"
MAX_TIER="${2:-0}"

PROJECT_DIR="/hpc/home/ais/hsali/hackagent"
EXP_DIR="$PROJECT_DIR/experiments/attacker_selection"
INVENTORY="$EXP_DIR/attackers.json"
SLURM_SCRIPT="$EXP_DIR/submit.slurm"

if [ -z "$SCRIPT_NAME" ]; then
    echo "Usage: $0 <script_name> [max_tier]" >&2
    echo "  e.g. $0 tier0_smoke 0" >&2
    exit 2
fi

if [ ! -f "$EXP_DIR/${SCRIPT_NAME}.py" ]; then
    echo "Experiment script not found: $EXP_DIR/${SCRIPT_NAME}.py" >&2
    exit 1
fi

export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MODELS="$HOME/models"

LOG_DIR="$PROJECT_DIR/logs/attacker_selection"
mkdir -p "$LOG_DIR" "$OLLAMA_MODELS"

# ---------------------------------------------------------------------------
# 1. Resolve required models from the inventory
# ---------------------------------------------------------------------------
mapfile -t REQUIRED_MODELS < <(
    python3 - "$INVENTORY" "$MAX_TIER" <<'PY'
import json, sys
inv = json.load(open(sys.argv[1]))
max_tier = int(sys.argv[2])
roles = inv.get("fixed_roles", {})
for key in ("target", "judge"):
    if roles.get(key):
        print(roles[key])
for a in inv.get("attackers", []):
    if a.get("tier") is not None and int(a["tier"]) <= max_tier:
        print(a["attacker_id"])
PY
)

if [ "${#REQUIRED_MODELS[@]}" -eq 0 ]; then
    echo "No models resolved from $INVENTORY for tier <= $MAX_TIER" >&2
    exit 1
fi

echo "Required models (tier <= $MAX_TIER):"
printf '  %s\n' "${REQUIRED_MODELS[@]}"

# ---------------------------------------------------------------------------
# 2. Start a private Ollama daemon and pull anything missing
# ---------------------------------------------------------------------------
# The login node hosts another user's daemon on :11434. We never touch it; we run
# our own on :11435 against our own model store.
OWN_DAEMON=""
if curl -fsS "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1; then
    echo "Reusing existing private Ollama daemon on ${OLLAMA_HOST}"
else
    echo "Starting private Ollama daemon on ${OLLAMA_HOST} for model staging..."
    ollama serve > "$LOG_DIR/ollama_staging.log" 2>&1 &
    OWN_DAEMON=$!
    for _ in $(seq 1 60); do
        curl -fsS "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1 && break
        sleep 2
    done
    if ! curl -fsS "http://${OLLAMA_HOST}/api/version" >/dev/null 2>&1; then
        echo "Ollama staging daemon failed to start; see $LOG_DIR/ollama_staging.log" >&2
        exit 1
    fi
fi

cleanup_staging() {
    if [ -n "$OWN_DAEMON" ]; then
        kill "$OWN_DAEMON" 2>/dev/null || true
        wait "$OWN_DAEMON" 2>/dev/null || true
    fi
}
trap cleanup_staging EXIT

INSTALLED="$(ollama list 2>/dev/null || true)"

for model in "${REQUIRED_MODELS[@]}"; do
    if printf '%s' "$INSTALLED" | grep -Fq -- "$model"; then
        echo "  present: $model"
    else
        echo "  pulling: $model"
        if ! ollama pull "$model"; then
            echo "Failed to pull $model" >&2
            exit 1
        fi
    fi
done

echo "All required models staged."

# Record resolved context lengths, which are GGUF-internal and absent from the
# registry manifest. This is the only reliable point to capture them.
CTX_FILE="$LOG_DIR/model_context_lengths.txt"
: > "$CTX_FILE"
for model in "${REQUIRED_MODELS[@]}"; do
    echo "### $model" >> "$CTX_FILE"
    ollama show "$model" >> "$CTX_FILE" 2>&1 || true
done
echo "Model metadata written to $CTX_FILE"

cleanup_staging
trap - EXIT

# ---------------------------------------------------------------------------
# 3. Submit
# ---------------------------------------------------------------------------
exec sbatch \
    --job-name="attsel_${SCRIPT_NAME}" \
    --output="$LOG_DIR/slurm_%j.out" \
    --error="$LOG_DIR/slurm_%j.err" \
    "$SLURM_SCRIPT" "$SCRIPT_NAME"
