#!/usr/bin/env bash
#SBATCH --job-name=deepseek_2node_smoke
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_gpuq
#SBATCH --partition=gpuq
#SBATCH --nodes=2
#SBATCH --ntasks=2
#SBATCH --ntasks-per-node=1
# The smoke only runs vLLM, so allocate the four GPUs needed for TP4 evenly:
# two GPUs on each node. The campaign's fifth GPU is for Ollama and is not
# started or needed by this verification.
#SBATCH --gpus-per-node=h200:2
# Make the per-task shape explicit so Slurm gives each rank two H200s.
#SBATCH --gpus-per-task=h200:2
#SBATCH --gpu-bind=closest
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=01:00:00

set -euo pipefail

# Verify the model's two-node vLLM path with one request, then terminate.
# This is intentionally independent of the campaign worker and Ollama.


PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PYTHON_ENV="${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
MODEL_ID="${1:-dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4}"
OUTPUT_ROOT="${2:-$PROJECT_DIR/logs/vllm_smoke/deepseek_multinode}"
OUTPUT_DIR="$OUTPUT_ROOT/${SLURM_JOB_ID:-local}"

module load cuda13.0/toolkit/13.0.2
[ -f "$PYTHON_ENV" ] || {
    printf 'Python environment is unavailable: %s\n' "$PYTHON_ENV" >&2
    exit 1
}
source "$PYTHON_ENV"
cd "$PROJECT_DIR"
mkdir -p "$OUTPUT_DIR"

exec > >(tee "$OUTPUT_DIR/job.log") 2>&1

mapfile -t NODES < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
[ "${#NODES[@]}" -eq 2 ] || {
    printf 'expected exactly two allocated nodes, got %s\n' "${#NODES[@]}" >&2
    exit 1
}

HEAD_NODE="${NODES[0]}"
MASTER_ADDR="${MASTER_ADDR:-$HEAD_NODE}"
MASTER_PORT="${MASTER_PORT:-$((29500 + (${SLURM_JOB_ID:-0} % 400)))}"
API_PORT="${API_PORT:-$((18000 + (${SLURM_JOB_ID:-0} % 400)))}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-2400}"
SPECULATIVE_CONFIG='{"method":"dspark","num_speculative_tokens":5}'

export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export MODEL_ID MASTER_ADDR MASTER_PORT API_PORT SPECULATIVE_CONFIG
export VLLM_LOGGING_LEVEL="${VLLM_LOGGING_LEVEL:-INFO}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"

printf 'model=%s\n' "$MODEL_ID"
printf 'nodes=%s\n' "${NODES[*]}"
printf 'master=%s:%s api=%s:%s\n' "$MASTER_ADDR" "$MASTER_PORT" "$HEAD_NODE" "$API_PORT"
printf 'output=%s\n' "$OUTPUT_DIR"

cleanup() {
    if [ -n "${VLLM_SRUN_PID:-}" ] && kill -0 "$VLLM_SRUN_PID" 2>/dev/null; then
        printf 'stopping distributed vLLM step pid=%s\n' "$VLLM_SRUN_PID"
        kill -TERM "$VLLM_SRUN_PID" 2>/dev/null || true
        wait "$VLLM_SRUN_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

srun \
    --nodes=2 \
    --ntasks=2 \
    --ntasks-per-node=1 \
    --gpu-bind=closest \
    --kill-on-bad-exit=1 \
    --wait=30 \
    bash -c '
        set -euo pipefail
        rank="${SLURM_NODEID:-${SLURM_PROCID:-}}"
        [ "$rank" = 0 ] || [ "$rank" = 1 ] || {
            printf "unexpected distributed rank: %s\\n" "$rank" >&2
            exit 1
        }
        IFS=, read -r -a visible_gpus <<< "${CUDA_VISIBLE_DEVICES:-}"
        [ "${#visible_gpus[@]}" -eq 2 ] || {
            printf "expected two vLLM GPUs on rank %s, got %s (%s)\\n" \
                "$rank" "${#visible_gpus[@]}" "${CUDA_VISIBLE_DEVICES:-unset}" >&2
            exit 1
        }
        printf "starting rank=%s host=%s cuda=%s\\n" "$rank" "$(hostname -s)" "${CUDA_VISIBLE_DEVICES:-unset}"
        args=(
            serve "$MODEL_ID"
            --served-model-name "$MODEL_ID"
            --host 0.0.0.0
            --port "$API_PORT"
            --trust-remote-code
            --tokenizer-mode deepseek_v4
            --tensor-parallel-size 4
            --pipeline-parallel-size 1
            --enable-expert-parallel
            --distributed-executor-backend mp
            --disable-custom-all-reduce
            --nnodes 2
            --node-rank "$rank"
            --master-addr "$MASTER_ADDR"
            --master-port "$MASTER_PORT"
            --kv-cache-dtype fp8
            --block-size 256
            --enable-prefix-caching
            --max-model-len 32768
            --max-num-seqs 1
            --gpu-memory-utilization 0.80
            --speculative-config "$SPECULATIVE_CONFIG"
            --reasoning-parser deepseek_v4
            --tool-call-parser deepseek_v4
            --enable-auto-tool-choice
        )
        if [ "$rank" -ne 0 ]; then
            args+=(--headless)
        fi
        exec vllm "${args[@]}"
    ' >"$OUTPUT_DIR/vllm.log" 2>&1 &
VLLM_SRUN_PID=$!

endpoint="http://$HEAD_NODE:$API_PORT/v1"
started_at="$(date +%s)"
while true; do
    if models_response="$(curl -fsS --connect-timeout 5 --max-time 30 "$endpoint/models" 2>/dev/null)"; then
        break
    fi
    if ! kill -0 "$VLLM_SRUN_PID" 2>/dev/null; then
        printf 'distributed vLLM exited before becoming ready; inspect %s\n' "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    now="$(date +%s)"
    if [ $((now - started_at)) -ge "$READY_TIMEOUT_SECONDS" ]; then
        printf 'distributed vLLM did not become ready within %ss; inspect %s\n' \
            "$READY_TIMEOUT_SECONDS" "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    sleep 5
done

printf '%s\n' "$models_response" >"$OUTPUT_DIR/models.json"
EXPECTED_MODEL_ID="$MODEL_ID" MODELS_RESPONSE="$models_response" python - <<'PY'
import json
import os

expected = os.environ["EXPECTED_MODEL_ID"]
data = json.loads(os.environ["MODELS_RESPONSE"])
served = {
    str(item.get("id"))
    for item in data.get("data", [])
    if isinstance(item, dict) and item.get("id")
}
if expected not in served:
    raise SystemExit(f"served model IDs {sorted(served)!r}; expected {expected!r}")
print(f"verified distributed vLLM model: {expected}")
PY

payload="$(MODEL_ID="$MODEL_ID" python - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_ID"],
    "messages": [{"role": "user", "content": "Reply with the word READY."}],
    "max_tokens": 16,
    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
}))
PY
)"
chat_response="$(curl -fsS --connect-timeout 10 --max-time 900 \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$endpoint/chat/completions")"
printf '%s\n' "$chat_response" >"$OUTPUT_DIR/chat_response.json"

RESPONSE="$chat_response" python - <<'PY'
import json
import os

data = json.loads(os.environ["RESPONSE"])
choices = data.get("choices") or []
if not choices or not isinstance(choices[0], dict):
    raise SystemExit("distributed vLLM chat completion returned no choices")
message = choices[0].get("message") or {}
if not message.get("content") and not message.get("reasoning_content"):
    raise SystemExit("distributed vLLM chat completion returned no message content")
print("verified distributed vLLM chat completion")
PY

printf 'DeepSeek two-node smoke passed; one request completed.\n'
