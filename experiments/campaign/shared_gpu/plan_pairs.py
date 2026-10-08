#!/usr/bin/env python3
"""Validate and report same-attacker shared-GPU shard pairs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from experiments.campaign.sharding import load_shard_manifest, validate_shard_manifest


def pair_plan(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validate_shard_manifest(manifest)
    by_attacker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for shard in manifest["shards"]:
        by_attacker[str(shard["attacker_key"])].append(shard)

    pairs: list[dict[str, Any]] = []
    for attacker, shards in by_attacker.items():
        if len(shards) % 2:
            raise ValueError(
                f"attacker {attacker} has {len(shards)} shards; "
                "shared-GPU pairing requires an even count"
            )
        for index in range(0, len(shards), 2):
            first, second = shards[index : index + 2]
            overlap = set(first["cell_ids"]) & set(second["cell_ids"])
            if overlap:
                raise ValueError(
                    f"paired shards {first['shard_id']} and {second['shard_id']} "
                    "have overlapping cells"
                )
            resources = (first.get("serving_requirements") or {}).get(
                "resources", {}
            )
            attacker_resource = resources.get("attacker", {})
            framework = attacker_resource.get("serving_framework")
            if framework not in {"ollama", "vllm"}:
                raise ValueError(f"unsupported attacker serving framework: {framework}")
            if int(attacker_resource.get("worker_gpu_count", 0)) not in {1, 5}:
                raise ValueError(f"unsupported shared worker GPU count for {attacker}")
            pairs.append(
                {
                    "pair_index": len(pairs),
                    "attacker_key": attacker,
                    "shard_a": first["shard_id"],
                    "shard_b": second["shard_id"],
                    "cell_count": len(first["cell_ids"]) + len(second["cell_ids"]),
                    "goal_evaluation_count": sum(
                        len(cell["goal_ids"])
                        for cell in manifest["cells"]
                        if cell["cell_id"]
                        in set(first["cell_ids"]) | set(second["cell_ids"])
                    ),
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
        "gpu_type": str(
            next(iter(manifest["serving_requirements"]["resources"]["attackers"].values()))
            .get("gpu_type", "h200")
        ),
        "gpu_count": len(pairs),
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
