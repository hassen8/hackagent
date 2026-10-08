"""Deterministic, frozen OmniSafeBench case selection for the next campaign."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).resolve().parents[2] / "hackagent" / "datasets" / "omnisafebench" / "dataset.json"
MANIFEST_PATH = Path(__file__).resolve().parent / "omnisafebench_af_10_per_subcategory_v1.json"
CASE_SET_ID = "omnisafebench_af_10_per_subcategory_v1"
CATEGORY_IDS = tuple("ABCDEF")
GOALS_PER_SUBCATEGORY = 10


def _dataset_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def select_cases(seed: int, dataset_path: Path = DATASET_PATH) -> dict[str, Any]:
    """Select and validate ten seeded intents from every A-F subcategory."""
    taxonomy = json.loads(dataset_path.read_text(encoding="utf-8"))
    selected: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for category_id in CATEGORY_IDS:
        category = taxonomy.get(category_id)
        if not isinstance(category, dict):
            raise ValueError(f"missing category {category_id}")
        subcategories = category.get("subcategories")
        if not isinstance(subcategories, dict):
            raise ValueError(f"category {category_id} has no subcategories")
        for subcategory_id, subcategory in subcategories.items():
            intents = subcategory.get("intents") if isinstance(subcategory, dict) else None
            if not isinstance(intents, list) or len(intents) < GOALS_PER_SUBCATEGORY:
                raise ValueError(f"{subcategory_id} has fewer than {GOALS_PER_SUBCATEGORY} intents")
            indexed = list(enumerate(str(intent).strip() for intent in intents))
            rng = random.Random(f"{seed}:{category_id}:{subcategory_id}")
            chosen = sorted(rng.sample(indexed, GOALS_PER_SUBCATEGORY))
            batch_id = f"{subcategory_id.lower()}_10"
            goals = []
            for source_index, goal in chosen:
                item = {
                    "category_id": category_id,
                    "category_label": str(category["label"]),
                    "subcategory_id": str(subcategory_id),
                    "subcategory_label": str(subcategory["label"]),
                    "goal_id": f"{subcategory_id}-{source_index + 1:03d}",
                    "goal": goal,
                    "source_index": source_index,
                    "goal_batch_id": batch_id,
                }
                selected.append(item)
                goals.append(item["goal_id"])
            batches.append({
                "goal_batch_id": batch_id,
                "category_id": category_id,
                "subcategory_id": str(subcategory_id),
                "goal_ids": goals,
            })

    if len(selected) != 340 or len({item["goal_id"] for item in selected}) != 340:
        raise ValueError("selection must contain 340 unique stable goal IDs")
    if len({item["goal"] for item in selected}) != 340:
        raise ValueError("selection contains duplicate goal text")
    manifest = {
        "case_set_id": CASE_SET_ID,
        "seed": seed,
        "source_dataset_path": str(dataset_path),
        "source_dataset_sha256": _dataset_hash(dataset_path),
        "selected_categories": list(CATEGORY_IDS),
        "goals_per_subcategory": GOALS_PER_SUBCATEGORY,
        "total_goal_count": len(selected),
        "batches": batches,
        "goals": selected,
    }
    manifest["manifest_sha256"] = manifest_hash(manifest)
    return manifest


def load_frozen_cases(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cases_by_batch(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for goal in manifest["goals"]:
        grouped.setdefault(goal["goal_batch_id"], []).append(goal)
    return grouped


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    args.output.write_text(json.dumps(select_cases(args.seed), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: 340 goals, 34 batches")
