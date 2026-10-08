#!/usr/bin/env bash
#SBATCH --job-name=glm53_dual_smoke
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:4
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=01:00:00

set -euo pipefail

# Four-GPU co-location smoke. vLLM remains visible on all allocated GPUs while
# Ollama is pinned to the last allocated GPU, matching the DeepSeek launcher.
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL_ID="${1:-dealignai/GLM-5.3-Flash-ABLITERATED-FP8}"
SIF_IMAGE="${2:-/hpc/home/ais/hsali/llm_images/vllm-openai-glm53-flash.sif}"
TARGET_MODEL="${3:-gemma3:4b}"
JUDGE_MODEL="${4:-qwen3.5:9b}"
OUTPUT_ROOT="${5:-$PROJECT_DIR/logs/vllm_smoke/glm53_flash_abliterated_fp8_dual}"

module load cuda13.0/toolkit/13.0.2
module load apptainer 2>/dev/null || true
APPTAINER_BIN="${APPTAINER_BIN:-$(command -v apptainer || true)}"
[ -x "$APPTAINER_BIN" ] || {
    printf 'apptainer is unavailable after module initialization\n' >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || { printf 'curl is unavailable\n' >&2; exit 1; }
command -v ollama >/dev/null 2>&1 || { printf 'ollama is unavailable\n' >&2; exit 1; }
PYTHON_BIN="${PYTHON_BIN:-python}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    printf 'Python interpreter is unavailable: %s\n' "$PYTHON_BIN" >&2
    exit 1
}

[ -f "$SIF_IMAGE" ] || { printf 'Apptainer image is unavailable: %s\n' "$SIF_IMAGE" >&2; exit 1; }
OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"
[ -d "$OLLAMA_MODELS/blobs" ] && [ -d "$OLLAMA_MODELS/manifests" ] || {
    printf 'Ollama model store is missing or incomplete: %s\n' "$OLLAMA_MODELS" >&2
    exit 1
}
cd "$PROJECT_DIR"

JOB_ID="${SLURM_JOB_ID:-local}"
OUTPUT_DIR="$OUTPUT_ROOT/$JOB_ID"
mkdir -p "$OUTPUT_DIR"
exec > >(tee "$OUTPUT_DIR/job.log") 2>&1

HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
OLLAMA_PORT="${OLLAMA_PORT:-$((17000 + (${SLURM_JOB_ID:-0} % 400)))}"
VLLM_PORT="${VLLM_PORT:-$((18000 + (${SLURM_JOB_ID:-0} % 400)))}"
OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"
OLLAMA_ENDPOINT="http://$OLLAMA_HOST"
VLLM_ENDPOINT="http://127.0.0.1:$VLLM_PORT/v1"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-3600}"
OLLAMA_READY_TIMEOUT_SECONDS="${OLLAMA_READY_TIMEOUT_SECONDS:-120}"
OLLAMA_LOAD_TIMEOUT="${OLLAMA_LOAD_TIMEOUT:-30m}"
OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.80}"
# Apptainer's runscript adds one shell boundary before vLLM parses this value.
SPECULATIVE_CONFIG='{\"method\":\"mtp\",\"num_speculative_tokens\":5}'
VLLM_PID=""
OLLAMA_PID=""
APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-$OUTPUT_DIR/apptainer-tmp}"
APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$OUTPUT_DIR/apptainer-cache}"
mkdir -p "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"

IFS=',' read -r -a ALLOCATED_GPUS <<<"${CUDA_VISIBLE_DEVICES:-}"
[ "${#ALLOCATED_GPUS[@]}" -eq 4 ] || {
    printf 'expected four allocated B200 GPUs, got %s (%s)\n' \
        "${#ALLOCATED_GPUS[@]}" "${CUDA_VISIBLE_DEVICES:-unset}" >&2
    exit 1
}
VLLM_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"
OLLAMA_GPU="${ALLOCATED_GPUS[3]}"

export HF_HOME HF_HUB_OFFLINE MODEL_ID SIF_IMAGE
export OLLAMA_MODELS OLLAMA_HOST OLLAMA_ENDPOINT OLLAMA_LOAD_TIMEOUT OLLAMA_CONTEXT_LENGTH
export VLLM_ENDPOINT VLLM_PORT APPTAINER_TMPDIR APPTAINER_CACHEDIR
export HACKAGENT_AUTO_PULL_MODELS=0

printf 'model=%s\n' "$MODEL_ID"
printf 'image=%s\n' "$SIF_IMAGE"
printf 'apptainer=%s\n' "$APPTAINER_BIN"
printf 'apptainer_version=%s\n' "$("$APPTAINER_BIN" --version 2>&1 || true)"
printf 'node=%s\n' "$(hostname -s)"
printf 'allocated_gpus=%s\n' "$VLLM_VISIBLE_DEVICES"
printf 'vllm_gpus=%s\n' "$VLLM_VISIBLE_DEVICES"
printf 'ollama_gpu=%s\n' "$OLLAMA_GPU"
printf 'vllm_endpoint=%s\n' "$VLLM_ENDPOINT"
printf 'ollama_endpoint=%s\n' "$OLLAMA_ENDPOINT"
printf 'vllm_max_model_len=%s\n' "$VLLM_MAX_MODEL_LEN"
printf 'vllm_gpu_memory_utilization=%s\n' "$VLLM_GPU_MEMORY_UTILIZATION"
printf 'target_model=%s\n' "$TARGET_MODEL"
printf 'judge_model=%s\n' "$JUDGE_MODEL"

terminate_tree() {
    local pid="$1"
    local child
    [ -n "$pid" ] || return 0
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        terminate_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
    if [ -n "$OLLAMA_PID" ] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
        printf 'stopping Ollama process tree pid=%s\n' "$OLLAMA_PID"
        terminate_tree "$OLLAMA_PID"
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
    if [ -n "$VLLM_PID" ] && kill -0 "$VLLM_PID" 2>/dev/null; then
        printf 'stopping Apptainer process tree pid=%s\n' "$VLLM_PID"
        terminate_tree "$VLLM_PID"
        wait "$VLLM_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$VLLM_PID" 2>/dev/null; then
            kill -KILL "$VLLM_PID" 2>/dev/null || true
            wait "$VLLM_PID" 2>/dev/null || true
        fi
    fi
}
trap cleanup EXIT TERM INT

verify_glm_response() {
    local response_file="$1"
    MODEL_ID="$MODEL_ID" RESPONSE_FILE="$response_file" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

expected = os.environ["MODEL_ID"]
data = json.loads(Path(os.environ["RESPONSE_FILE"]).read_text(encoding="utf-8"))
if data.get("model") != expected:
    raise SystemExit(f"response model {data.get('model')!r} does not match {expected!r}")
choices = data.get("choices") or []
if not choices or not isinstance(choices[0], dict):
    raise SystemExit("GLM chat completion returned no choices")
message = choices[0].get("message") or {}
if message.get("reasoning") or message.get("reasoning_content"):
    raise SystemExit("GLM returned reasoning despite enable_thinking=false")
content = message.get("content")
if not isinstance(content, str) or not content.strip():
    raise SystemExit("GLM chat completion returned no message content")
print(f"verified no-thinking GLM response: {content.strip()!r}")
PY
}

verify_ollama_models() {
    TAGS_FILE="$OUTPUT_DIR/ollama_tags.json" TARGET_MODEL="$TARGET_MODEL" JUDGE_MODEL="$JUDGE_MODEL" \
        "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

data = json.loads(Path(os.environ["TAGS_FILE"]).read_text(encoding="utf-8"))
available = {
    str(item.get("name"))
    for item in data.get("models", [])
    if isinstance(item, dict) and item.get("name")
}
required = {os.environ["TARGET_MODEL"], os.environ["JUDGE_MODEL"]}
missing = sorted(required - available)
if missing:
    raise SystemExit(f"Ollama model store is missing exact models: {missing!r}")
print(f"verified Ollama models: {sorted(required)!r}")
PY
}

warm_ollama_model() {
    local model="$1"
    local output="$2"
    MODEL="$model" "$PYTHON_BIN" - <<'PY' >"$OUTPUT_DIR/ollama_request.json"
import json
import os

print(json.dumps({
    "model": os.environ["MODEL"],
    "prompt": "Reply with the word READY.",
    "stream": False,
    "think": False,
    "keep_alive": -1,
    "options": {"num_predict": 1},
}))
PY
    curl -fsS --connect-timeout 10 --max-time 1800 \
        -H 'Content-Type: application/json' \
        -d @"$OUTPUT_DIR/ollama_request.json" \
        "$OLLAMA_ENDPOINT/api/generate" >"$output"
    MODEL="$model" RESPONSE_FILE="$output" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

data = json.loads(Path(os.environ["RESPONSE_FILE"]).read_text(encoding="utf-8"))
if data.get("model") != os.environ["MODEL"]:
    raise SystemExit(f"Ollama response model {data.get('model')!r} does not match {os.environ['MODEL']!r}")
if not isinstance(data.get("response"), str) or not data["response"].strip():
    raise SystemExit(f"Ollama returned no response for {os.environ['MODEL']!r}")
print(f"verified Ollama response: {os.environ['MODEL']}")
PY
}

START_TIME="$(date +%s)"
APPTAINER_MESSAGELEVEL=5 "$APPTAINER_BIN" -d run --nv \
    --bind "$HF_HOME:/root/.cache/huggingface" \
    --env "HF_HOME=/root/.cache/huggingface" \
    --env "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
    --env VLLM_ENGINE_READY_TIMEOUT_S=3600 \
    "$SIF_IMAGE" \
    --model "$MODEL_ID" \
    --enable-expert-parallel \
    --tensor-parallel-size 4 \
    --kv-cache-dtype fp8 \
    --speculative-config "$SPECULATIVE_CONFIG" \
    --tool-call-parser glm47 \
    --reasoning-parser glm45 \
    --enable-auto-tool-choice \
    --distributed-executor-backend mp \
    --max-model-len "$VLLM_MAX_MODEL_LEN" \
    --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
    --host 127.0.0.1 \
    --port "$VLLM_PORT" \
    >"$OUTPUT_DIR/vllm.log" 2>&1 &
VLLM_PID=$!
printf 'started Apptainer process pid=%s\n' "$VLLM_PID"

ready=0
started_at="$(date +%s)"
while true; do
    if curl -fsS --connect-timeout 5 --max-time 30 "$VLLM_ENDPOINT/models" >"$OUTPUT_DIR/vllm_models.json" 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        printf 'Apptainer vLLM exited before becoming ready; inspect %s\n' \
            "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    now="$(date +%s)"
    if [ $((now - started_at)) -ge "$READY_TIMEOUT_SECONDS" ]; then
        printf 'GLM vLLM did not become ready within %ss; inspect %s\n' \
            "$READY_TIMEOUT_SECONDS" "$OUTPUT_DIR/vllm.log" >&2
        exit 1
    fi
    sleep 5
done

[ "$ready" -eq 1 ] || { printf 'GLM vLLM readiness failed\n' >&2; exit 1; }
END_TIME="$(date +%s)"
printf 'vLLM is ready; model_load_seconds=%s\n' "$((END_TIME - START_TIME))"

MODEL_ID="$MODEL_ID" MODELS_FILE="$OUTPUT_DIR/vllm_models.json" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

expected = os.environ["MODEL_ID"]
data = json.loads(Path(os.environ["MODELS_FILE"]).read_text(encoding="utf-8"))
served = {
    str(item.get("id"))
    for item in data.get("data", [])
    if isinstance(item, dict) and item.get("id")
}
if expected not in served:
    raise SystemExit(f"served model IDs {sorted(served)!r}; expected {expected!r}")
print(f"verified GLM model ID: {expected}")
PY

MODEL_ID="$MODEL_ID" "$PYTHON_BIN" - <<'PY' >"$OUTPUT_DIR/glm_request.json"
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_ID"],
    "messages": [{"role": "user", "content": "Reply with the word READY."}],
    "max_tokens": 16,
    "chat_template_kwargs": {"enable_thinking": False},
}))
PY
curl -fsS --connect-timeout 10 --max-time 900 \
    -H 'Content-Type: application/json' \
    -d @"$OUTPUT_DIR/glm_request.json" \
    "$VLLM_ENDPOINT/chat/completions" >"$OUTPUT_DIR/glm_initial_response.json"
verify_glm_response "$OUTPUT_DIR/glm_initial_response.json"

CUDA_VISIBLE_DEVICES="$OLLAMA_GPU" \
    OLLAMA_HOST="$OLLAMA_HOST" \
    OLLAMA_MODELS="$OLLAMA_MODELS" \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_KEEP_ALIVE=-1 \
    ollama serve >"$OUTPUT_DIR/ollama.log" 2>&1 &
OLLAMA_PID=$!
printf 'started Ollama process pid=%s\n' "$OLLAMA_PID"

ollama_ready=0
for _ in $(seq 1 "$((OLLAMA_READY_TIMEOUT_SECONDS / 2))"); do
    if curl -fsS "$OLLAMA_ENDPOINT/api/version" >"$OUTPUT_DIR/ollama_version.json" 2>/dev/null; then
        ollama_ready=1
        break
    fi
    if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
        printf 'Ollama exited before becoming ready; inspect %s\n' "$OUTPUT_DIR/ollama.log" >&2
        exit 1
    fi
    sleep 2
done
[ "$ollama_ready" -eq 1 ] || {
    printf 'Ollama did not become ready within %ss\n' "$OLLAMA_READY_TIMEOUT_SECONDS" >&2
    exit 1
}

curl -fsS "$OLLAMA_ENDPOINT/api/tags" >"$OUTPUT_DIR/ollama_tags.json"
verify_ollama_models
warm_ollama_model "$TARGET_MODEL" "$OUTPUT_DIR/target_response.json"
warm_ollama_model "$JUDGE_MODEL" "$OUTPUT_DIR/judge_response.json"
ollama ps >"$OUTPUT_DIR/ollama_ps.txt"
OLLAMA_PS_FILE="$OUTPUT_DIR/ollama_ps.txt" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

text = Path(os.environ["OLLAMA_PS_FILE"]).read_text(encoding="utf-8")
if "GPU" not in text.upper():
    raise SystemExit("Ollama ps output does not confirm GPU placement")
print("verified Ollama GPU placement")
PY

curl -fsS --connect-timeout 10 --max-time 900 \
    -H 'Content-Type: application/json' \
    -d @"$OUTPUT_DIR/glm_request.json" \
    "$VLLM_ENDPOINT/chat/completions" >"$OUTPUT_DIR/glm_concurrent_response.json"
verify_glm_response "$OUTPUT_DIR/glm_concurrent_response.json"

printf 'GLM/Ollama four-B200 dual-service smoke passed.\n'
