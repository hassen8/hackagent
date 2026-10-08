"""Deterministic cell ownership for immutable campaign manifests.

The shard manifest is an immutable description of the execution matrix.  This
module only expands and validates that description; it does not create
campaign records, open SQLite, contact Ollama, or submit Slurm jobs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

try:
    from .campaign_config import (
        CAMPAIGN_NAME,
        EXTENSION_EXECUTION_SCOPE_ID,
        SEPARATE_OLLAMA_ATTACKER,
    )
except ImportError:  # Allow the documented path-based CLI invocation.
    from campaign_config import (  # type: ignore[no-redef]
        CAMPAIGN_NAME,
        EXTENSION_EXECUTION_SCOPE_ID,
        SEPARATE_OLLAMA_ATTACKER,
    )


SCHEMA_VERSION = 1
SHARD_MANIFEST_SCHEMA_VERSION = SCHEMA_VERSION
LEGACY_EXECUTION_SCOPE_ID = "omnisafebench_af340_five_attackers_v1"
DEFAULT_EXECUTION_SCOPE_ID = EXTENSION_EXECUTION_SCOPE_ID
EXECUTION_SCOPE_ID = DEFAULT_EXECUTION_SCOPE_ID
DEFAULT_SHARD_COUNT = 20
SUPPORTED_SHARD_COUNTS = (5, 6, 10, 12, 18, 20, 24, 25, 30, 36)
ASSIGNMENT_STRATEGY = "attacker_affinity_uniform_v1"


def _json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash non-volatile manifest content."""
    payload = {
        key: value
        for key, value in manifest.items()
        if key != "shard_manifest_sha256"
    }
    return _json_hash(payload)


def _nested_value(
    run: Mapping[str, Any], role: str, field: str, default: Any = None
) -> Any:
    value = run.get(f"{role}_{field}")
    nested = run.get(role)
    if value is None and isinstance(nested, Mapping):
        value = nested.get(field)
        if field == "model" and value is None:
            value = (
                nested.get("model_id")
                or nested.get("identifier")
                or nested.get("ollama_model")
            )
    return default if value is None else value


def _attack_name(run: Mapping[str, Any]) -> Any:
    attack = run.get("attack")
    if isinstance(attack, Mapping):
        return attack.get("name") or attack.get("attack_type")
    return attack


def _goal_ids(run: Mapping[str, Any]) -> list[str]:
    values = run.get("goal_ids")
    if values is None:
        values = run.get("case_ids")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError("matrix cell must contain a list of frozen goal IDs")
    return list(values)


def _cell_identity(
    run: Mapping[str, Any], execution_scope_id: str | None = None
) -> dict[str, Any]:
    scope = execution_scope_id or str(
        run.get("execution_scope_id") or DEFAULT_EXECUTION_SCOPE_ID
    )
    frozen_hash = run.get("frozen_goal_manifest_sha256") or run.get("manifest_sha256")
    identity = {
        "campaign_name": run.get("campaign_name", CAMPAIGN_NAME),
        "execution_scope_id": scope,
        "case_set_id": run.get("case_set_id"),
        "frozen_goal_manifest_sha256": frozen_hash,
        "target_key": _nested_value(run, "target", "key"),
        "attacker_key": _nested_value(run, "attacker", "key"),
        "judge_key": _nested_value(run, "judge", "key"),
        "attack": _attack_name(run),
        "goal_batch_id": run.get("goal_batch_id"),
        "repetition": run.get("repetition"),
    }
    missing = [
        key
        for key, value in identity.items()
        if value is None and key
        not in {"attacker_key"}
    ]
    if missing:
        raise ValueError(
            "matrix cell is missing cell identity fields: " + ", ".join(missing)
        )
    return identity


def cell_id_for_run(
    run: Mapping[str, Any], execution_scope_id: str | None = None
) -> str:
    """Return the stable ID for one frozen logical cell.

    ``goal_batch_id`` is part of the hashed identity, so two ten-goal batches
    cannot collide merely because every other matrix dimension is identical.
    """
    return f"cell-{_json_hash(_cell_identity(run, execution_scope_id))}"


def _cell_from_run(
    run: Mapping[str, Any], execution_scope_id: str
) -> dict[str, Any]:
    identity = _cell_identity(run, execution_scope_id)
    goal_ids = _goal_ids(run)
    return {
        "cell_id": cell_id_for_run(run, execution_scope_id),
        **identity,
        "target_model": _nested_value(run, "target", "model"),
        "attacker_model": _nested_value(run, "attacker", "model"),
        "judge_model": _nested_value(run, "judge", "model"),
        "goal_ids": goal_ids,
    }


def _expected_matrix(
    attacker_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    # Lazy import avoids a campaign -> sharding -> campaign import cycle.
    try:
        from .campaign import build_run_descriptions
    except ImportError:  # pragma: no cover - path-based CLI support
        from campaign import build_run_descriptions  # type: ignore[no-redef]

    matrix = build_run_descriptions()
    if attacker_keys is None:
        return matrix
    selected = {str(key) for key in attacker_keys}
    if not selected:
        raise ValueError("attacker subset must contain at least one attacker key")
    filtered = [
        run
        for run in matrix
        if isinstance(run.get("attacker"), Mapping)
        and str(run["attacker"].get("key")) in selected
    ]
    found = {
        str(run["attacker"]["key"])
        for run in filtered
        if isinstance(run.get("attacker"), Mapping)
    }
    missing = sorted(selected - found)
    if missing:
        raise ValueError(
            "attacker subset contains inactive or unknown keys: " + ", ".join(missing)
        )
    return filtered


def _role_requirement(run: Mapping[str, Any], role: str) -> dict[str, Any]:
    model = run.get(role)
    if not isinstance(model, Mapping):
        raise ValueError(f"matrix cell has no {role} role definition")
    identifier = _nested_value(run, role, "model")
    if not identifier:
        raise ValueError(f"matrix cell has no resolved {role} model identifier")
    framework = str(model.get("serving_framework") or "ollama").lower()
    agent_type = str(
        model.get("agent_type")
        or ("OPENAI_SDK" if framework == "vllm" else "OLLAMA")
    )
    return {
        "identifier": str(identifier),
        "agent_type": agent_type,
        "serving_framework": framework,
        "endpoint_env": str(
            model.get("endpoint_env")
            or ("VLLM_ENDPOINT" if framework == "vllm" else "OLLAMA_ENDPOINT")
        ),
        "endpoint_path": str(model.get("endpoint_path") or ""),
        "launcher_profile": str(model.get("launcher_profile") or ""),
        "serving_options": dict(model.get("serving_options") or {}),
    }


def _resource_requirement(model: Mapping[str, Any]) -> dict[str, Any]:
    framework = str(model.get("serving_framework") or "ollama").lower()
    options = dict(model.get("serving_options") or {})
    if framework == "vllm":
        vllm_gpu_count = int(options.get("tensor_parallel_size", 1))
        ollama_gpu_count = int(options.get("ollama_gpu_count", 1))
    else:
        vllm_gpu_count = 0
        ollama_gpu_count = int(
            options.get(
                "ollama_gpu_count",
                2 if SEPARATE_OLLAMA_ATTACKER else 1,
            )
        )
    return {
        "serving_framework": framework,
        "launcher_profile": str(model.get("launcher_profile") or ""),
        "gpu_type": str(options.get("gpu_type", "h200")),
        "vllm_gpu_count": vllm_gpu_count,
        "ollama_gpu_count": ollama_gpu_count,
        "worker_gpu_count": vllm_gpu_count + ollama_gpu_count,
        "requires_separate_service_gpu": ollama_gpu_count > 0,
    }


def _serving_requirements(cells: list[Mapping[str, Any]]) -> dict[str, Any]:
    first = cells[0]
    requirements: dict[str, Any] = {
        "target": _role_requirement(first, "target"),
        "judge": _role_requirement(first, "judge"),
        "attackers": {},
    }
    resources: dict[str, Any] = {"attackers": {}, "worker": {}}
    for cell in cells:
        attacker_key = str(_nested_value(cell, "attacker", "key"))
        if attacker_key in requirements["attackers"]:
            continue
        attacker = cell.get("attacker")
        if not isinstance(attacker, Mapping):
            raise ValueError(f"cell has no attacker profile for {attacker_key}")
        requirements["attackers"][attacker_key] = _role_requirement(
            cell, "attacker"
        )
        resources["attackers"][attacker_key] = _resource_requirement(attacker)
    resources["worker"] = {
        "services": ["ollama", "vllm"],
        "endpoint_env": ["OLLAMA_ENDPOINT", "VLLM_ENDPOINT"],
        "gpu_allocation": "separate_visibility_required",
    }
    return {"roles": requirements, "resources": resources}


def _relative_paths(shard_id: str) -> dict[str, str]:
    root = f"shards/{shard_id}"
    return {
        "relative_path": root,
        "hackagent_db": f"{root}/hackagent.db",
        "planned_runs": f"{root}/planned_runs.jsonl",
        "harvested_runs": f"{root}/harvested_runs.jsonl",
        "runs_csv": f"{root}/runs.csv",
        "results_csv": f"{root}/results.csv",
        "runs_dir": f"{root}/runs",
        "slurm_dir": f"{root}/slurm",
    }


def _unique_in_order(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_shard_manifest(
    shard_count: int = DEFAULT_SHARD_COUNT,
    execution_scope_id: str = DEFAULT_EXECUTION_SCOPE_ID,
    expected_cells: Iterable[Mapping[str, Any]] | None = None,
    attacker_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic attacker-affine shard manifest."""
    if shard_count not in SUPPORTED_SHARD_COUNTS:
        raise ValueError(
            f"shard_count must be one of {SUPPORTED_SHARD_COUNTS}, got {shard_count}"
        )
    raw_cells = (
        list(expected_cells)
        if expected_cells is not None
        else _expected_matrix(attacker_keys)
    )
    cells = [_cell_from_run(run, execution_scope_id) for run in raw_cells]
    if not cells:
        raise ValueError("cannot build a shard manifest for an empty matrix")
    if len({cell["cell_id"] for cell in cells}) != len(cells):
        raise ValueError("expected matrix contains duplicate cell IDs")

    attacker_keys = _unique_in_order(cell.get("attacker_key") for cell in cells)
    if not attacker_keys or any(key is None for key in attacker_keys):
        raise ValueError("shard assignment requires attacker-driven cells")
    if any(not cell["goal_ids"] or not cell["goal_batch_id"] for cell in cells):
        raise ValueError("every cell must contain a goal batch and at least one goal")
    if shard_count < len(attacker_keys):
        raise ValueError("shard_count must be at least the attacker count")
    shard_ids = [f"shard-{index:03d}" for index in range(shard_count)]
    cells_by_shard: dict[str, list[dict[str, Any]]] = {
        shard_id: [] for shard_id in shard_ids
    }

    shard_cursor = 0
    base_shards, extra_shards = divmod(shard_count, len(attacker_keys))
    for attacker_index, attacker_key in enumerate(attacker_keys):
        attacker_cells = [
            cell for cell in cells if cell["attacker_key"] == attacker_key
        ]
        attacker_shard_count = base_shards + int(attacker_index < extra_shards)
        owners = shard_ids[shard_cursor : shard_cursor + attacker_shard_count]
        shard_cursor += attacker_shard_count
        for cell_index, cell in enumerate(attacker_cells):
            # Contiguous balanced buckets keep ownership stable and make each
            # shard's model warm rather than interleaving attacker models.
            owner_index = min(
                len(owners) - 1,
                cell_index * len(owners) // len(attacker_cells),
            )
            cell["shard_id"] = owners[owner_index]
            cells_by_shard[owners[owner_index]].append(cell)

    shard_entries = []
    has_role_profiles = all(
        isinstance(run.get("target"), Mapping)
        and isinstance(run.get("attacker"), Mapping)
        and isinstance(run.get("judge"), Mapping)
        for run in raw_cells
    )
    serving = _serving_requirements(raw_cells) if has_role_profiles else None
    for shard_id in shard_ids:
        owned = cells_by_shard[shard_id]
        shard_attacker_key = owned[0]["attacker_key"] if owned else None
        shard_entries.append(
            {
                "shard_id": shard_id,
                "attacker_key": shard_attacker_key,
                "cell_ids": [cell["cell_id"] for cell in owned],
                "paths": _relative_paths(shard_id),
                "serving_requirements": (
                    {
                        "roles": {
                            "target": serving["roles"]["target"],
                            "judge": serving["roles"]["judge"],
                            "attacker": serving["roles"]["attackers"][shard_attacker_key],
                        },
                        "resources": {
                            "attacker": serving["resources"]["attackers"][
                                shard_attacker_key
                            ],
                            "worker": serving["resources"]["worker"],
                        },
                    }
                    if serving is not None and shard_attacker_key is not None
                    else None
                ),
            }
        )

    target_keys = _unique_in_order(cell["target_key"] for cell in cells)
    target_models = _unique_in_order(cell["target_model"] for cell in cells)
    judge_keys = _unique_in_order(cell["judge_key"] for cell in cells)
    judge_models = _unique_in_order(cell["judge_model"] for cell in cells)
    attack_names = _unique_in_order(cell["attack"] for cell in cells)
    repetitions = _unique_in_order(cell["repetition"] for cell in cells)
    attacker_models = {
        str(cell["attacker_key"]): cell["attacker_model"]
        for cell in cells
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_name": cells[0]["campaign_name"],
        "execution_scope_id": execution_scope_id,
        "case_set_id": cells[0]["case_set_id"],
        "frozen_goal_manifest_sha256": cells[0][
            "frozen_goal_manifest_sha256"
        ],
        "shard_count": shard_count,
        "assignment_strategy": ASSIGNMENT_STRATEGY,
        "target_key": target_keys[0],
        "target_model": target_models[0],
        "attacker_keys": attacker_keys,
        "attacker_models": attacker_models,
        "judge_key": judge_keys[0],
        "judge_model": judge_models[0],
        "attacks": attack_names,
        "repetitions": repetitions,
        "expected_cell_count": len(cells),
        "expected_goal_evaluation_count": sum(len(cell["goal_ids"]) for cell in cells),
        "shards": shard_entries,
        "cells": cells,
    }
    if serving is not None:
        manifest["serving_requirements"] = serving
    manifest["shard_manifest_sha256"] = manifest_hash(manifest)
    return manifest


def _basic_manifest_checks(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported shard manifest schema_version")
    if manifest.get("assignment_strategy") != ASSIGNMENT_STRATEGY:
        raise ValueError("unsupported shard assignment strategy")
    shard_count = manifest.get("shard_count")
    if shard_count not in SUPPORTED_SHARD_COUNTS:
        raise ValueError("shard manifest has an unsupported shard_count")
    declared_hash = manifest.get("shard_manifest_sha256")
    if not isinstance(declared_hash, str) or manifest_hash(manifest) != declared_hash:
        raise ValueError("shard manifest hash validation failed")
    if not isinstance(manifest.get("cells"), list):
        raise ValueError("shard manifest cells must be a list")
    if not isinstance(manifest.get("shards"), list):
        raise ValueError("shard manifest shards must be a list")


def _validate_serving_requirements(manifest: Mapping[str, Any]) -> None:
    requirements = manifest.get("serving_requirements")
    if requirements is None:
        return
    if not isinstance(requirements, Mapping):
        raise ValueError("shard manifest serving_requirements must be an object")
    roles = requirements.get("roles")
    resources = requirements.get("resources")
    if not isinstance(roles, Mapping) or not isinstance(resources, Mapping):
        raise ValueError("shard manifest serving_requirements has invalid sections")
    for role in ("target", "judge", "attackers"):
        if role not in roles:
            raise ValueError(f"shard manifest serving requirements omit {role}")
    if not isinstance(roles["attackers"], Mapping) or not roles["attackers"]:
        raise ValueError("shard manifest serving requirements have no attackers")
    if not isinstance(resources.get("attackers"), Mapping):
        raise ValueError("shard manifest resource requirements omit attackers")
    if roles["target"].get("identifier") != manifest.get("target_model"):
        raise ValueError("serving target identifier does not match the manifest")
    if roles["judge"].get("identifier") != manifest.get("judge_model"):
        raise ValueError("serving judge identifier does not match the manifest")
    expected_attackers = {
        str(key): value for key, value in (manifest.get("attacker_models") or {}).items()
    }
    if set(roles["attackers"]) != set(expected_attackers):
        raise ValueError("serving attacker keys do not match the manifest")
    for key, role in roles["attackers"].items():
        if role.get("identifier") != expected_attackers.get(str(key)):
            raise ValueError(f"serving attacker {key} does not match the manifest")
    for shard in manifest.get("shards", []):
        shard_requirements = shard.get("serving_requirements")
        if shard_requirements is None:
            raise ValueError(
                f"shard {shard.get('shard_id')} has no serving requirements"
            )
        shard_roles = shard_requirements.get("roles")
        shard_resources = shard_requirements.get("resources")
        if not isinstance(shard_roles, Mapping) or not isinstance(
            shard_resources, Mapping
        ):
            raise ValueError(
                f"shard {shard.get('shard_id')} has invalid serving requirements"
            )
        shard_key = shard.get("attacker_key")
        shard_attacker = shard_roles.get("attacker")
        if shard_key not in expected_attackers or not isinstance(shard_attacker, Mapping):
            raise ValueError(f"shard {shard.get('shard_id')} has an invalid attacker role")
        if shard_attacker.get("identifier") != expected_attackers[str(shard_key)]:
            raise ValueError(
                f"shard {shard.get('shard_id')} attacker does not match the manifest"
            )


def validate_shard_manifest(
    manifest: Mapping[str, Any],
    expected_cells: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate ownership, identity, scope, and immutable manifest content."""
    _basic_manifest_checks(manifest)
    _validate_serving_requirements(manifest)
    scope = str(manifest.get("execution_scope_id") or "")
    if not scope:
        raise ValueError("shard manifest has no execution_scope_id")
    # An immutable manifest is the source of truth for reharvest.  Comparing it
    # to the current active configuration would make a later role swap or
    # attacker extension invalidate historical evidence.  Callers that are
    # creating/checking a manifest against a selected matrix can still pass
    # ``expected_cells`` explicitly.
    expected = (
        [_cell_from_run(run, scope) for run in list(expected_cells)]
        if expected_cells is not None
        else None
    )
    expected_by_id = {cell["cell_id"]: cell for cell in expected or []}
    actual_cells = manifest["cells"]
    actual_by_id: dict[str, Mapping[str, Any]] = {}
    for cell in actual_cells:
        if not isinstance(cell, Mapping):
            raise ValueError("shard manifest cell entries must be objects")
        cell_id = cell.get("cell_id")
        if not isinstance(cell_id, str) or not cell_id:
            raise ValueError("shard manifest cell has no cell_id")
        if cell_id in actual_by_id:
            raise ValueError(f"duplicate cell_id in shard manifest: {cell_id}")
        required_fields = (
            "campaign_name",
            "execution_scope_id",
            "case_set_id",
            "frozen_goal_manifest_sha256",
            "target_key",
            "target_model",
            "attacker_key",
            "attacker_model",
            "judge_key",
            "judge_model",
            "attack",
            "goal_batch_id",
            "repetition",
            "goal_ids",
        )
        missing = [field for field in required_fields if cell.get(field) is None]
        if missing:
            raise ValueError(
                f"cell {cell_id} is missing immutable fields: {', '.join(missing)}"
            )
        if not isinstance(cell.get("goal_ids"), list) or not cell["goal_ids"]:
            raise ValueError(f"cell {cell_id} goal_ids must be a non-empty list")
        if cell.get("execution_scope_id") != scope:
            raise ValueError(f"cell {cell_id} has a mismatched execution scope")
        if cell_id_for_run(cell, scope) != cell_id:
            raise ValueError(f"cell {cell_id} does not match its immutable identity")
        actual_by_id[cell_id] = cell

    if expected is not None:
        if set(actual_by_id) != set(expected_by_id):
            raise ValueError("shard manifest cells do not match the expected matrix")
        for cell_id, expected_cell in expected_by_id.items():
            actual = actual_by_id[cell_id]
            for field in (
                "campaign_name",
                "execution_scope_id",
                "case_set_id",
                "frozen_goal_manifest_sha256",
                "target_key",
                "target_model",
                "attacker_key",
                "attacker_model",
                "judge_key",
                "judge_model",
                "attack",
                "goal_batch_id",
                "repetition",
                "goal_ids",
            ):
                if actual.get(field) != expected_cell.get(field):
                    raise ValueError(f"cell {cell_id} does not match expected {field}")

    shard_ids = [f"shard-{index:03d}" for index in range(manifest["shard_count"])]
    actual_shards = manifest["shards"]
    if [shard.get("shard_id") for shard in actual_shards] != shard_ids:
        raise ValueError("shard entries are not ordered or complete")
    owners: dict[str, str] = {}
    for shard in actual_shards:
        if not isinstance(shard, Mapping):
            raise ValueError("shard entries must be objects")
        shard_id = shard.get("shard_id")
        paths = shard.get("paths")
        if not isinstance(paths, Mapping) or paths.get("relative_path") != (
            f"shards/{shard_id}"
        ):
            raise ValueError(f"shard {shard_id} has invalid relative paths")
        cell_ids = shard.get("cell_ids")
        if not isinstance(cell_ids, list):
            raise ValueError(f"shard {shard_id} cell_ids must be a list")
        attacker_key = shard.get("attacker_key")
        for cell_id in cell_ids:
            if cell_id not in actual_by_id:
                raise ValueError(f"shard {shard_id} owns an unknown cell")
            if cell_id in owners:
                raise ValueError(f"cell {cell_id} has duplicate shard ownership")
            owners[cell_id] = str(shard_id)
            cell = actual_by_id[cell_id]
            if cell.get("shard_id") != shard_id:
                raise ValueError(f"cell {cell_id} has a mismatched shard owner")
            if cell.get("attacker_key") != attacker_key:
                raise ValueError(f"shard {shard_id} contains multiple attackers")
        if not cell_ids:
            raise ValueError(f"shard {shard_id} has no assigned cells")
    if set(owners) != set(actual_by_id):
        raise ValueError("every manifest cell must have exactly one shard owner")

    manifest_cells = list(actual_by_id.values())
    expected_goal_count = sum(len(cell["goal_ids"]) for cell in manifest_cells)
    if manifest.get("expected_cell_count") != len(manifest_cells):
        raise ValueError("shard manifest expected_cell_count is incorrect")
    if manifest.get("expected_goal_evaluation_count") != expected_goal_count:
        raise ValueError("shard manifest expected_goal_evaluation_count is incorrect")
    first_cell = manifest_cells[0]
    if manifest.get("campaign_name") != first_cell["campaign_name"]:
        raise ValueError("shard manifest campaign_name does not match the matrix")
    if manifest.get("case_set_id") != first_cell["case_set_id"]:
        raise ValueError("shard manifest case_set_id does not match the frozen case set")
    if manifest.get("frozen_goal_manifest_sha256") != first_cell[
        "frozen_goal_manifest_sha256"
    ]:
        raise ValueError("shard manifest frozen goal hash does not match the matrix")

    attacker_keys = _unique_in_order(cell["attacker_key"] for cell in manifest_cells)
    if manifest.get("attacker_keys") != attacker_keys:
        raise ValueError("shard manifest attacker inventory does not match the matrix")
    expected_attacker_models = {
        str(cell["attacker_key"]): cell["attacker_model"] for cell in manifest_cells
    }
    if manifest.get("attacker_models") != expected_attacker_models:
        raise ValueError("shard manifest attacker model IDs do not match the matrix")
    if manifest.get("target_key") != first_cell["target_key"] or manifest.get(
        "target_model"
    ) != first_cell["target_model"]:
        raise ValueError("shard manifest target does not match the matrix")
    if manifest.get("judge_key") != first_cell["judge_key"] or manifest.get(
        "judge_model"
    ) != first_cell["judge_model"]:
        raise ValueError("shard manifest judge does not match the matrix")
    if manifest.get("attacks") != _unique_in_order(
        cell["attack"] for cell in manifest_cells
    ):
        raise ValueError("shard manifest attacks do not match the matrix")
    if manifest.get("repetitions") != _unique_in_order(
        cell["repetition"] for cell in manifest_cells
    ):
        raise ValueError("shard manifest repetitions do not match the matrix")
    return dict(manifest)


def load_shard_manifest(path: str | Path) -> dict[str, Any]:
    """Load and hash-check a manifest without changing it."""
    source = Path(path).expanduser()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("shard manifest must be a JSON object")
    _basic_manifest_checks(value)
    return value


def cells_for_shard(
    manifest: Mapping[str, Any], shard_id: str
) -> list[dict[str, Any]]:
    """Return the ordered cell entries owned by one shard."""
    shard = next(
        (entry for entry in manifest.get("shards", []) if entry.get("shard_id") == shard_id),
        None,
    )
    if shard is None:
        raise ValueError(f"unknown shard_id: {shard_id}")
    by_id = {cell["cell_id"]: cell for cell in manifest.get("cells", [])}
    return [dict(by_id[cell_id]) for cell_id in shard.get("cell_ids", [])]


def shard_id_for_array_index(manifest: Mapping[str, Any], array_index: int) -> str:
    """Map a zero-based Slurm array index to its immutable shard ID."""
    try:
        index = int(array_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("array index must be an integer") from exc
    entries = manifest.get("shards", [])
    if index < 0 or index >= len(entries):
        raise ValueError(f"array index is outside the shard manifest: {array_index}")
    return str(entries[index]["shard_id"])


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or validate a shard manifest.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-manifest", type=Path, metavar="PATH")
    mode.add_argument("--validate", action="store_true")
    parser.add_argument("--manifest", type=Path, metavar="PATH")
    parser.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    parser.add_argument("--execution-scope-id", default=DEFAULT_EXECUTION_SCOPE_ID)
    parser.add_argument(
        "--attacker-key",
        action="append",
        dest="attacker_keys",
        help="limit a newly generated manifest to one or more attacker keys",
    )
    args = parser.parse_args(argv)

    try:
        if args.write_manifest is not None:
            manifest = build_shard_manifest(
                shard_count=args.shard_count,
                execution_scope_id=args.execution_scope_id,
                attacker_keys=args.attacker_keys,
            )
            validate_shard_manifest(manifest)
            _write_manifest(args.write_manifest, manifest)
            print(
                f"wrote {args.write_manifest}: "
                f"{manifest['expected_cell_count']} cells, "
                f"{manifest['expected_goal_evaluation_count']} goal evaluations, "
                f"{manifest['shard_count']} shards"
            )
            return 0
        if args.manifest is None:
            parser.error("--validate requires --manifest PATH")
        manifest = load_shard_manifest(args.manifest)
        validate_shard_manifest(manifest)
        print(
            f"validated {args.manifest}: "
            f"{manifest['expected_cell_count']} cells, "
            f"{manifest['expected_goal_evaluation_count']} goal evaluations, "
            f"{manifest['shard_count']} shards, "
            f"hash={manifest['shard_manifest_sha256']}"
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"shard manifest error: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ASSIGNMENT_STRATEGY",
    "DEFAULT_EXECUTION_SCOPE_ID",
    "DEFAULT_SHARD_COUNT",
    "EXECUTION_SCOPE_ID",
    "SCHEMA_VERSION",
    "SUPPORTED_SHARD_COUNTS",
    "build_shard_manifest",
    "cell_id_for_run",
    "cells_for_shard",
    "load_shard_manifest",
    "manifest_hash",
    "shard_id_for_array_index",
    "validate_shard_manifest",
]
