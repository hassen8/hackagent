#!/usr/bin/env bash
#SBATCH --account=ais_login
#SBATCH --qos=quota_ais_aiq
#SBATCH --partition=aiq
#SBATCH --gres=gpu:b200:5
#SBATCH --nodes=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=400G
#SBATCH --time=15:00:00
set -euo pipefail
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SCRIPT_DIR="$PROJECT_DIR/experiments/campaign"
MANIFEST="$1"; shift; SHARD_A=""; SHARD_B=""; LIMIT=""; SERVICE_SMOKE=0; RETRY_FAILED=0; RETRY_INCOMPLETE=0
while [ "$#" -gt 0 ]; do case "$1" in
  --shard-a) SHARD_A="$2"; shift 2;; --shard-b) SHARD_B="$2"; shift 2;;
  --limit) LIMIT="$2"; shift 2;; --service-smoke) SERVICE_SMOKE=1; shift;;
  --retry-failed) RETRY_FAILED=1; shift;; --retry-incomplete) RETRY_INCOMPLETE=1; shift;; *) exit 2;; esac; done
cd "$PROJECT_DIR"; module load cuda13.0/toolkit/13.0.2; module load apptainer 2>/dev/null || true; source "${PYTHON_ENV:-/hpc/home/ais/hsali/.venv/bin/activate}"
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
module load apptainer 2>/dev/null || true
export HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}" HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
META="$(PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python - "$MANIFEST" "$SHARD_A" "$SHARD_B" <<'PY'
import json, sys
from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest
m=load_shard_manifest(sys.argv[1]); validate_shard_manifest(m); s={x["shard_id"]:x for x in m["shards"]}; a,b=s[sys.argv[2]],s[sys.argv[3]]
if a["attacker_key"] != b["attacker_key"] or set(a["cell_ids"]) & set(b["cell_ids"]): raise SystemExit("invalid pair")
roles=(a["serving_requirements"] or {}).get("roles",{}); res=(a["serving_requirements"] or {}).get("resources",{}).get("attacker",{})
if roles.get("attacker",{}).get("serving_framework") != "vllm" or int(res.get("worker_gpu_count",0)) != 5: raise SystemExit("pair is not vLLM TP4 plus Ollama")
print(json.dumps({"campaign_name":m["campaign_name"],"scope":m["execution_scope_id"],"target":m["target_model"],"judge":m["judge_model"],"attacker":roles["attacker"]["identifier"],"key":a["attacker_key"]}))
PY
)"
export HACKAGENT_CAMPAIGN_NAME="$(python -c 'import json,sys; print(json.load(sys.stdin)["campaign_name"])' <<<"$META")" HACKAGENT_EXECUTION_SCOPE_ID="$(python -c 'import json,sys; print(json.load(sys.stdin)["scope"])' <<<"$META")" HACKAGENT_TARGET_KEY=qwen38_27b HACKAGENT_ATTACKER_KEYS="$(python -c 'import json,sys; print(json.load(sys.stdin)["key"])' <<<"$META")" HACKAGENT_SKIP_CAMPAIGN_WARMUP=1
ROOT="$(dirname "$MANIFEST")"; SHARED="$ROOT/shared_gpu"; mkdir -p "$SHARED"; exec > >(tee -a "$SHARED/launcher_${SLURM_JOB_ID}.log") 2>&1
BASE=$((12000 + (SLURM_JOB_ID % 2000) * 10)); OLLAMA_HOST="127.0.0.1:$BASE"; OLLAMA_ENDPOINT="http://$OLLAMA_HOST"; export OLLAMA_HOST OLLAMA_ENDPOINT OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}" OLLAMA_NUM_PARALLEL=2 OLLAMA_MAX_LOADED_MODELS=2 HACKAGENT_AUTO_PULL_MODELS=0
export MODEL_ID="$(python -c 'import json,sys; print(json.load(sys.stdin)["attacker"])' <<<"$META")" VLLM_VISIBLE_DEVICES=0,1,2,3 VLLM_TENSOR_PARALLEL_SIZE=4 VLLM_DTYPE=bfloat16 VLLM_MAX_MODEL_LEN=393216 VLLM_EXTRA_ARGS_STRING='--enable-expert-parallel --distributed-executor-backend mp --kv-cache-dtype fp8 --block-size 256 --enable-prefix-caching --gpu-memory-utilization 0.80 --reasoning-parser deepseek_v4 --tool-call-parser deepseek_v4 --enable-auto-tool-choice' VLLM_SPECULATIVE_CONFIG='{"method":"dspark","num_speculative_tokens":5}' VLLM_PORT=$((BASE+1)) VLLM_ENDPOINT="http://127.0.0.1:$((BASE+1))/v1" VLLM_LOG="$SHARED/vllm_${SLURM_JOB_ID}.log" VLLM_PID_FILE="$SHARED/vllm_${SLURM_JOB_ID}.pid" VLLM_VERIFY_CHAT=1
export OLLAMA_VISIBLE_DEVICES=4
ATTACKER_KEY="$(python -c 'import json,sys; print(json.load(sys.stdin)["key"])' <<<"$META")"
APPTAINER_BIN="$(command -v apptainer || true)"; VLLM_PID=""
cleanup(){
  if [ "$ATTACKER_KEY" = "glm53_flash_abliterated_fp8" ]; then
    [ -n "$VLLM_PID" ] && kill "$VLLM_PID" 2>/dev/null || true
  else
    MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_LOG="$VLLM_LOG" VLLM_PID_FILE="$VLLM_PID_FILE" bash "$SCRIPT_DIR/serve_vllm.sh" stop || true
  fi
}; trap cleanup EXIT TERM INT
if [ "$ATTACKER_KEY" = "glm53_flash_abliterated_fp8" ]; then
  [ -x "$APPTAINER_BIN" ] || { printf 'apptainer is unavailable\n' >&2; exit 1; }
  SPECULATIVE_CONFIG='{\"method\":\"mtp\",\"num_speculative_tokens\":5}'
  APPTAINER_MESSAGELEVEL=5 CUDA_VISIBLE_DEVICES="$VLLM_VISIBLE_DEVICES" "$APPTAINER_BIN" run --nv \
    --bind "$HF_HOME:/root/.cache/huggingface" --env "HF_HOME=/root/.cache/huggingface" \
    --env "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" --env "CUDA_VISIBLE_DEVICES=$VLLM_VISIBLE_DEVICES" \
    /hpc/home/ais/hsali/llm_images/vllm-openai-glm53-flash.sif --model "$MODEL_ID" \
    --enable-expert-parallel --tensor-parallel-size 4 --kv-cache-dtype fp8 \
    --speculative-config "$SPECULATIVE_CONFIG" \
    --tool-call-parser glm47 --reasoning-parser glm45 --enable-auto-tool-choice \
    --distributed-executor-backend mp --max-model-len 32768 --gpu-memory-utilization 0.80 \
    --host 127.0.0.1 --port "$VLLM_PORT" >"$VLLM_LOG" 2>&1 &
  VLLM_PID=$!
  for _ in $(seq 1 720); do curl -fsS "$VLLM_ENDPOINT/models" >/dev/null 2>&1 && break; kill -0 "$VLLM_PID" 2>/dev/null || exit 1; sleep 5; done
  curl -fsS "$VLLM_ENDPOINT/models" >/dev/null
  PROBE="$(python - <<'PY'
import json
print(json.dumps({"model": "" , "messages": [{"role": "user", "content": "Reply with READY."}], "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False}}))
PY
)"
  PROBE="${PROBE/\"model\": \"\"/\"model\": \"$MODEL_ID\"}"
  curl -fsS -H 'Content-Type: application/json' -d "$PROBE" "$VLLM_ENDPOINT/chat/completions" >/dev/null
else
  bash "$SCRIPT_DIR/serve_vllm.sh" start
  MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_VERIFY_CHAT=1 bash "$SCRIPT_DIR/serve_vllm.sh" wait
  MODEL_ID="$MODEL_ID" VLLM_PORT="$VLLM_PORT" VLLM_VERIFY_CHAT=1 bash "$SCRIPT_DIR/serve_vllm.sh" verify
fi
CUDA_VISIBLE_DEVICES=4 ollama serve >"$SHARED/ollama_${SLURM_JOB_ID}.log" 2>&1 & OP=$!; trap 'kill "$OP" 2>/dev/null || true; wait "$OP" 2>/dev/null || true; cleanup' EXIT TERM INT
for _ in $(seq 1 90); do curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$OLLAMA_ENDPOINT/api/version" >/dev/null 2>&1 || { printf 'shared Ollama did not become ready\n' >&2; exit 1; }
if ! grep -q "library=CUDA" "$SHARED/ollama_${SLURM_JOB_ID}.log"; then
    printf 'Ollama CUDA backend not discovered; aborting to avoid Vulkan CLIP segfault\n' >&2
    exit 1
fi
for model in "$(python -c 'import json,sys; print(json.load(sys.stdin)["target"])' <<<"$META")" "$(python -c 'import json,sys; print(json.load(sys.stdin)["judge"])' <<<"$META")"; do curl -fsS -X POST "$OLLAMA_ENDPOINT/api/show" -H 'Content-Type: application/json' -d "{\"name\":\"$model\"}" >/dev/null; done
if [ "$SERVICE_SMOKE" -eq 1 ]; then
    printf 'shared service smoke passed\n'
    exit 0
fi
run(){ local s="$1"; local log="$SHARED/worker_${SLURM_JOB_ID}_${s}.log"; local args=(--execute --matrix --shard-manifest "$MANIFEST" --shard-id "$s"); [ -n "$LIMIT" ] && args+=(--limit "$LIMIT"); [ "$RETRY_FAILED" -eq 1 ] && args+=(--retry-failed); [ "$RETRY_INCOMPLETE" -eq 1 ] && args+=(--retry-incomplete); HACKAGENT_DB="$ROOT/shards/$s/hackagent.db" OLLAMA_ENDPOINT="$OLLAMA_ENDPOINT" VLLM_ENDPOINT="$VLLM_ENDPOINT" uv run --no-sync python experiments/campaign/campaign.py "${args[@]}" >"$log" 2>&1; }
run "$SHARD_A" & PA=$!; run "$SHARD_B" & PB=$!; status=0; wait "$PA" || status=1; wait "$PB" || status=1; exit "$status"
