from pathlib import Path


def test_shared_gpu_launcher_contract():
    root = Path(__file__).parents[3]
    wrapper = (root / "experiments/campaign/shared_gpu/submit_pair.sh").read_text()
    launcher = (root / "experiments/campaign/shared_gpu/submit_pair.slurm").read_text()
    readme = (root / "experiments/campaign/shared_gpu/README.md").read_text()

    assert "--shard-a" in wrapper
    assert "--shard-b" in wrapper
    assert "overlapping cell IDs" in wrapper
    assert "same attacker_key" in wrapper
    assert "--gres=gpu:b200:1" in wrapper
    assert 'PYTHONPATH="$PROJECT_DIR' in launcher
    assert "OLLAMA_NUM_PARALLEL=2" in launcher
    assert "HACKAGENT_SKIP_CAMPAIGN_WARMUP=1" in launcher
    assert 'CAMPAIGN_ROOT="$(dirname "$MANIFEST")"' in launcher
    assert 'HACKAGENT_DB="$CAMPAIGN_ROOT/shards/$shard_id/hackagent.db"' in launcher
    assert 'worker_${SLURM_JOB_ID}_${shard_id}.log' in launcher
    assert 'shared_gpu/job_' not in launcher
    assert 'api/generate' in launcher
    assert 'worker.lock' in launcher
    assert "CAMPAIGN_ROOT" in launcher
    assert "OLLAMA_ENDPOINT" in launcher
    assert "HACKAGENT_DB" in launcher
    assert "--shard-manifest" in launcher
    assert "--shard-id" in launcher
    assert "PID_A" in launcher and "PID_B" in launcher
    assert "worker_args" in launcher
    assert 'LIMIT=""' in launcher
    assert "RETRY_INCOMPLETE" in launcher
    assert "one H200" in readme


def test_shared_vllm_launcher_contract():
    root = Path(__file__).parents[3]
    wrapper = (root / "experiments/campaign/shared_gpu/submit_vllm_pair.sh").read_text()
    launcher = (root / "experiments/campaign/shared_gpu/submit_vllm_pair.slurm").read_text()
    array = (root / "experiments/campaign/shared_gpu/submit_vllm_array.sh").read_text()
    assert "gpu:b200:5" in wrapper
    assert "quota_ais_aiq" in wrapper
    assert "vllm" in launcher
    assert "OLLAMA_NUM_PARALLEL=2" in launcher
    assert "HACKAGENT_SKIP_CAMPAIGN_WARMUP=1" in launcher
    assert "PA=$!" in launcher and "PB=$!" in launcher
    assert "--array=0-$((PAIR_COUNT - 1))" in array


def test_qwen_bf16_shared_launcher_contract():
    root = Path(__file__).parents[3]
    pair = (root / "experiments/campaign/shared_gpu/submit_qwen38_bf16_pair.slurm").read_text()
    wrapper = (root / "experiments/campaign/shared_gpu/submit_qwen38_bf16_pair.sh").read_text()
    array = (root / "experiments/campaign/shared_gpu/submit_qwen38_bf16_array.sh").read_text()
    assert "gpu:b200:2" in wrapper
    assert "VLLM_TENSOR_PARALLEL_SIZE=1" in pair
    assert "VLLM_DTYPE=bfloat16" in pair
    assert "VLLM_VISIBLE_DEVICES=\"${GPUS[0]}\"" in pair
    assert "OLLAMA_VISIBLE_DEVICES=\"${GPUS[1]}\"" in pair
    assert "OLLAMA_NUM_PARALLEL=2" in pair
    assert 'OLLAMA_MAX_LOADED_MODELS=2' in pair
    assert 'HF_HOME="${HF_HOME:-/hpc/home/ais/hsali/.cache/huggingface}"' in pair
    assert 'HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"' in pair
    assert 'OLLAMA_LLM_LIBRARY=cuda' not in pair
    assert 'OLLAMA_VULKAN=0' not in pair
    assert 'library=CUDA' in pair
    assert "HACKAGENT_SKIP_CAMPAIGN_WARMUP=1" in pair
    assert "PYTHONIOENCODING=utf-8:backslashreplace" in pair
    assert "PA=$!" in pair and "PB=$!" in pair
    assert "--array=0-$((COUNT-1))" in array
