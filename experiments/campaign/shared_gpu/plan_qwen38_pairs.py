#!/usr/bin/env python3
"""Plan same-attacker two-GPU Qwen BF16 shard pairs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest


ATTACKER_KEY = "qwen38_27b_abliterated_bf16"


def pair_plan(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validate_shard_manifest(manifest)
    if manifest.get("target_model") != "qwen3.8:27b":
        raise ValueError("Qwen BF16 shared launcher requires qwen3.8:27b target")
    if manifest.get("attacker_keys") != [ATTACKER_KEY]:
        raise ValueError("manifest must contain only the Qwen BF16 attacker")

    shards = manifest["shards"]
    if len(shards) % 2:
        raise ValueError("Qwen shared pairing requires an even shard count")
    pairs = []
    cells = {cell["cell_id"]: cell for cell in manifest["cells"]}
    for index in range(0, len(shards), 2):
        first, second = shards[index : index + 2]
        if first["attacker_key"] != ATTACKER_KEY or second["attacker_key"] != ATTACKER_KEY:
            raise ValueError("all Qwen shared shards must use the Qwen BF16 attacker")
        resources = first["serving_requirements"]["resources"]["attacker"]
        if resources.get("serving_framework") != "vllm":
            raise ValueError("Qwen attacker must use vLLM")
        if int(resources.get("worker_gpu_count", 0)) != 2:
            raise ValueError("Qwen shared worker must use two GPUs")
        if set(first["cell_ids"]) & set(second["cell_ids"]):
            raise ValueError("paired Qwen shards overlap")
        owned = set(first["cell_ids"]) | set(second["cell_ids"])
        pairs.append(
            {
                "pair_index": len(pairs),
                "attacker_key": ATTACKER_KEY,
                "shard_a": first["shard_id"],
                "shard_b": second["shard_id"],
                "cell_count": len(owned),
                "goal_evaluation_count": sum(len(cells[cell]["goal_ids"]) for cell in owned),
            }
        )
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = load_shard_manifest(args.manifest)
    pairs = pair_plan(manifest)
    report = {
        "manifest": str(args.manifest),
        "campaign_name": manifest["campaign_name"],
        "execution_scope_id": manifest["execution_scope_id"],
        "pair_count": len(pairs),
        "gpu_type": "b200",
        "gpu_count": 2,
        "pairs": pairs,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
