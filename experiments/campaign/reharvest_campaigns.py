"""Reharvest and combine immutable campaign sources into one derived export.

The shard reharvester owns recovery for one manifest/root pair.  This module is
only orchestration: it gives every source its own derived namespace, rejects
physical ID collisions, and combines the resulting records without touching
source shards, SQLite, JSONL, CSV, or ``merged/`` directories.

Manifest-backed sources use :mod:`reharvest_shards` and therefore prefer
read-only SQLite evidence.  A legacy flat campaign can be supplied with
``--campaign-root``; it has no shard manifest or database contract, so its
existing JSONL is treated as immutable input and flattened into the same
derived schema without pretending that SQLite evidence was recovered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import results
    from .merge_shards import (
        _coverage,
        _select_canonical_attempts,
        _validate_goal_rows,
    )
    from .reharvest_shards import reharvest_shards
    from .sharding import load_shard_manifest, validate_shard_manifest
    from .token_estimation import (
        TokenEstimator,
        enrich_harvested_records,
        summarize_goal_estimates,
    )
except ImportError:  # pragma: no cover - supports direct script execution
    import results  # type: ignore[no-redef]
    from merge_shards import (  # type: ignore[no-redef]
        _coverage,
        _select_canonical_attempts,
        _validate_goal_rows,
    )
    from reharvest_shards import reharvest_shards  # type: ignore[no-redef]
    from sharding import load_shard_manifest, validate_shard_manifest  # type: ignore[no-redef]
    from token_estimation import (  # type: ignore[no-redef]
        TokenEstimator,
        enrich_harvested_records,
        summarize_goal_estimates,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return results.read_jsonl(path)


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), sort_keys=True, default=str) + "\n")


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unique(values: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


_TOKEN_GOAL_FIELDS = (
    "goal_target_prompt_tokens_estimated",
    "goal_target_completion_tokens_estimated",
    "goal_target_total_tokens_estimated",
    "goal_attacker_prompt_tokens_estimated",
    "goal_attacker_completion_tokens_estimated",
    "goal_attacker_total_tokens_estimated",
    "goal_combined_prompt_tokens_estimated",
    "goal_combined_completion_tokens_estimated",
    "goal_combined_total_tokens_estimated",
    "goal_token_call_count_estimated",
    "goal_token_calls_missing_count",
)
_TOKEN_COUNT_FIELDS = (
    "goal_target_call_count",
    "advprefix_ce_call_count",
    "advprefix_completion_call_count",
)


def _valid_token_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _token_goal_rows(
    harvests: Iterable[Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for harvest in harvests:
        attack = str(harvest.get("attack") or "unknown")
        goals = harvest.get("goals") or harvest.get("results") or []
        if not isinstance(goals, list):
            continue
        rows.extend(
            (attack, goal) for goal in goals if isinstance(goal, Mapping)
        )
    return rows


def _audit_token_evidence(
    harvests: Iterable[Mapping[str, Any]],
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    """Audit derived token fields without filling or changing any values."""
    rows = _token_goal_rows(harvests)
    if not enabled:
        return {
            "enabled": False,
            "goal_rows": len(rows),
            "token_goal_rows": 0,
            "malformed_value_count": 0,
            "total_mismatch_count": 0,
            "call_count_mismatch_count": 0,
            "estimated_incomplete_count": 0,
            "missing_status_count": 0,
            "by_attack": {},
            "representative_issues": [],
        }
    token_rows = [
        (attack, goal)
        for attack, goal in rows
        if goal.get("goal_token_evidence_status") not in (None, "")
        or any(goal.get(field) not in (None, "") for field in _TOKEN_GOAL_FIELDS)
        or any(goal.get(field) not in (None, "") for field in _TOKEN_COUNT_FIELDS)
    ]
    if not token_rows:
        return {
            "enabled": False,
            "goal_rows": len(rows),
            "token_goal_rows": 0,
            "malformed_value_count": 0,
            "total_mismatch_count": 0,
            "call_count_mismatch_count": 0,
            "estimated_incomplete_count": 0,
            "missing_status_count": 0,
            "by_attack": {},
            "representative_issues": [],
        }

    by_attack: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    malformed_value_count = 0
    total_mismatch_count = 0
    call_count_mismatch_count = 0
    estimated_incomplete_count = 0
    missing_status_count = 0

    def issue(code: str, attack: str, goal: Mapping[str, Any], **fields: Any) -> None:
        if len(issues) >= 25:
            return
        issues.append(
            {
                "code": code,
                "attack": attack,
                "result_id": goal.get("result_id"),
                "goal_id": goal.get("goal_id"),
                **fields,
            }
        )

    for attack, goal in token_rows:
        stats = by_attack.setdefault(
            attack,
            {
                "goal_rows": 0,
                "status_counts": {},
                "numeric_counts": {
                    field: 0 for field in (*_TOKEN_GOAL_FIELDS, *_TOKEN_COUNT_FIELDS)
                },
                "malformed_value_count": 0,
                "total_mismatch_count": 0,
                "call_count_mismatch_count": 0,
                "estimated_incomplete_count": 0,
                "missing_status_count": 0,
            },
        )
        stats["goal_rows"] += 1
        status = str(goal.get("goal_token_evidence_status"))
        status_counts = Counter(stats["status_counts"])
        status_counts[status] += 1
        stats["status_counts"] = dict(status_counts)
        if goal.get("goal_token_evidence_status") in (None, ""):
            missing_status_count += 1
            stats["missing_status_count"] += 1
            issue("missing_token_evidence_status", attack, goal)

        valid_values: dict[str, int] = {}
        for field in (*_TOKEN_GOAL_FIELDS, *_TOKEN_COUNT_FIELDS):
            value = goal.get(field)
            if value in (None, ""):
                continue
            if not _valid_token_value(value):
                malformed_value_count += 1
                stats["malformed_value_count"] += 1
                issue(
                    "malformed_token_value",
                    attack,
                    goal,
                    field=field,
                    value=value,
                )
                continue
            valid_values[field] = value
            stats["numeric_counts"][field] += 1

        prompt = valid_values.get("goal_target_prompt_tokens_estimated")
        completion = valid_values.get("goal_target_completion_tokens_estimated")
        total = valid_values.get("goal_target_total_tokens_estimated")
        if prompt is not None and completion is not None and total is not None:
            if total != prompt + completion:
                total_mismatch_count += 1
                stats["total_mismatch_count"] += 1
                issue(
                    "target_total_mismatch",
                    attack,
                    goal,
                    prompt=prompt,
                    completion=completion,
                    total=total,
                )

        if status == "estimated" and not all(
            field in valid_values
            for field in (
                "goal_target_prompt_tokens_estimated",
                "goal_target_completion_tokens_estimated",
                "goal_target_total_tokens_estimated",
            )
        ):
            estimated_incomplete_count += 1
            stats["estimated_incomplete_count"] += 1
            issue("estimated_missing_target_field", attack, goal)

        expected_calls = valid_values.get("goal_target_call_count")
        observed_calls = valid_values.get("goal_token_call_count_estimated")
        missing_calls = valid_values.get("goal_token_calls_missing_count")
        if _valid_token_value(expected_calls) and observed_calls is not None:
            if observed_calls > expected_calls:
                call_count_mismatch_count += 1
                stats["call_count_mismatch_count"] += 1
                issue(
                    "observed_token_calls_exceed_target_calls",
                    attack,
                    goal,
                    observed_calls=observed_calls,
                    expected_calls=expected_calls,
                )
        if attack == "advprefix":
            ce_calls = goal.get("advprefix_ce_call_count")
            completion_calls = goal.get("advprefix_completion_call_count")
            if (
                _valid_token_value(expected_calls)
                and _valid_token_value(ce_calls)
                and _valid_token_value(completion_calls)
                and expected_calls != ce_calls + completion_calls
            ):
                call_count_mismatch_count += 1
                stats["call_count_mismatch_count"] += 1
                issue(
                    "advprefix_phase_call_mismatch",
                    attack,
                    goal,
                    target_calls=expected_calls,
                    ce_calls=ce_calls,
                    completion_calls=completion_calls,
                )

    return {
        "enabled": True,
        "goal_rows": len(rows),
        "token_goal_rows": len(token_rows),
        "malformed_value_count": malformed_value_count,
        "total_mismatch_count": total_mismatch_count,
        "call_count_mismatch_count": call_count_mismatch_count,
        "estimated_incomplete_count": estimated_incomplete_count,
        "missing_status_count": missing_status_count,
        "by_attack": by_attack,
        "representative_issues": issues,
    }


def _require_unique_ids(
    records: Iterable[Mapping[str, Any]],
    field: str,
    seen: dict[str, str],
    source_label: str,
) -> None:
    local: set[str] = set()
    for record in records:
        value = record.get(field)
        if value in (None, ""):
            raise ValueError(f"{source_label} record has no {field}")
        identifier = str(value)
        if identifier in local:
            raise ValueError(
                f"{source_label} contains duplicate physical {field}: {identifier}"
            )
        previous = seen.get(identifier)
        if previous is not None:
            raise ValueError(
                f"duplicate physical {field} across campaigns: {identifier} "
                f"({previous}, {source_label})"
            )
        local.add(identifier)
        seen[identifier] = source_label


def _legacy_scope(root: Path, plans: list[Mapping[str, Any]]) -> str:
    campaign_names = _unique(
        str(plan.get("campaign_name"))
        for plan in plans
        if plan.get("campaign_name") not in (None, "")
    )
    name = campaign_names[0] if len(campaign_names) == 1 else root.name
    return f"legacy_flat::{name}"


def _legacy_cell(plan: Mapping[str, Any], scope: str) -> dict[str, Any]:
    goal_ids = list(plan.get("goal_ids") or plan.get("case_ids") or [])
    identity = {
        "campaign_name": plan.get("campaign_name"),
        "execution_scope_id": scope,
        "case_set_id": plan.get("case_set_id"),
        "target_key": plan.get("target_key"),
        "target_model": plan.get("target_model"),
        "attacker_key": plan.get("attacker_key"),
        "attacker_model": plan.get("attacker_model"),
        "judge_key": plan.get("judge_key"),
        "judge_model": plan.get("judge_model"),
        "attack": plan.get("attack"),
        "goal_batch_id": plan.get("goal_batch_id"),
        "repetition": plan.get("repetition"),
        "goal_ids": goal_ids,
    }
    cell_id = f"legacy-cell-{_json_hash(identity)}"
    return {
        "cell_id": cell_id,
        "campaign_name": plan.get("campaign_name"),
        "execution_scope_id": scope,
        "case_set_id": plan.get("case_set_id"),
        "frozen_goal_manifest_sha256": plan.get("manifest_sha256"),
        "target_key": plan.get("target_key"),
        "target_model": plan.get("target_model"),
        "attacker_key": plan.get("attacker_key"),
        "attacker_model": plan.get("attacker_model"),
        "judge_key": plan.get("judge_key"),
        "judge_model": plan.get("judge_model"),
        "attack": plan.get("attack"),
        "goal_batch_id": plan.get("goal_batch_id"),
        "repetition": plan.get("repetition"),
        "goal_ids": goal_ids,
    }


def _legacy_source(
    root: Path,
    output: Path,
    token_estimator: TokenEstimator | None = None,
) -> dict[str, Any]:
    planned_path = root / "planned_runs.jsonl"
    harvested_path = root / "harvested_runs.jsonl"
    if not planned_path.exists() or not harvested_path.exists():
        raise ValueError(
            f"legacy campaign {root} must contain planned_runs.jsonl and "
            "harvested_runs.jsonl"
        )
    source_label = str(root)
    plans = _read_jsonl(planned_path)
    harvests = _read_jsonl(harvested_path)
    _require_unique_ids(plans, "experiment_run_id", {}, source_label)
    _require_unique_ids(harvests, "experiment_run_id", {}, source_label)
    plan_ids = {str(plan.get("experiment_run_id")) for plan in plans}
    if any(str(record.get("experiment_run_id")) not in plan_ids for record in harvests):
        raise ValueError(f"{source_label} has a harvest without a planned attempt")

    scope = _legacy_scope(root, plans)
    cell_by_id: dict[str, dict[str, Any]] = {}
    derived_plans: list[dict[str, Any]] = []
    for plan in plans:
        cell = _legacy_cell(plan, scope)
        cell_by_id[cell["cell_id"]] = cell
        derived = dict(plan)
        derived["execution_scope_id"] = scope
        derived["cell_id"] = cell["cell_id"]
        derived["goal_ids"] = list(plan.get("goal_ids") or plan.get("case_ids") or [])
        derived_plans.append(derived)

    derived_harvests = [dict(harvest) for harvest in harvests]
    if token_estimator is not None:
        derived_harvests = enrich_harvested_records(derived_harvests, token_estimator)
    derived_plans, derived_harvests, canonical_selection = _select_canonical_attempts(
        derived_plans, derived_harvests
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "planned_runs.jsonl", derived_plans)
    _write_jsonl(output / "harvested_runs.jsonl", derived_harvests)
    run_count, result_count = results.flatten_records(
        derived_plans,
        derived_harvests,
        output / "runs.csv",
        output / "results.csv",
        token_estimator=token_estimator,
    )
    manifest = {
        "schema_version": 1,
        "manifest_type": "legacy_flat_derived",
        "campaign_name": next(
            (plan.get("campaign_name") for plan in plans if plan.get("campaign_name")),
            root.name,
        ),
        "execution_scope_id": scope,
        "attacker_keys": _unique(cell.get("attacker_key") for cell in cell_by_id.values()),
        "attacker_models": {
            str(cell.get("attacker_key")): cell.get("attacker_model")
            for cell in cell_by_id.values()
            if cell.get("attacker_key") is not None
        },
        "attacks": _unique(cell.get("attack") for cell in cell_by_id.values()),
        "repetitions": _unique(cell.get("repetition") for cell in cell_by_id.values()),
        "expected_cell_count": len(cell_by_id),
        "expected_goal_evaluation_count": sum(
            len(cell.get("goal_ids") or []) for cell in cell_by_id.values()
        ),
        "cells": list(cell_by_id.values()),
        "source": {"campaign_root": source_label, "manifest_path": None},
    }
    coverage = _coverage(manifest, derived_plans, derived_harvests)
    (output / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = {
        "source_type": "legacy_flat",
        "status": "complete" if coverage["status"] == "complete" else "partial",
        "created_at": _now(),
        "campaign_root": source_label,
        "manifest_path": None,
        "source_counts": {
            "planned_attempts": len(derived_plans),
            "harvested_attempts": len(derived_harvests),
            "run_rows_written": run_count,
            "result_rows_written": result_count,
        },
        "coverage": coverage,
        "canonical_selection": canonical_selection,
        "policy": {
            "sqlite": "not available for legacy flat source",
            "source_jsonl": "read-only input",
            "output": "derived namespace",
        },
        "errors": [],
        "warnings": [
            {
                "code": "legacy_flat_source",
                "message": "source has no immutable shard manifest or SQLite evidence",
            }
        ],
    }
    (output / "reharvest-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "reharvest-manifest.json").write_text(
        json.dumps(
            {
                "reharvest_version": "R1",
                "created_at": report["created_at"],
                "source": report["policy"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"manifest": manifest, "plans": derived_plans, "harvests": derived_harvests, "report": report}


def _manifest_source(
    manifest_path: Path,
    root: Path,
    output: Path,
    allow_partial: bool,
    token_estimator: TokenEstimator | None = None,
) -> dict[str, Any]:
    manifest = load_shard_manifest(manifest_path)
    validate_shard_manifest(manifest)
    report = reharvest_shards(
        manifest_path,
        root,
        output,
        allow_partial=allow_partial,
        token_estimator=token_estimator,
    )
    return {
        "manifest": manifest,
        "plans": _read_jsonl(output / "planned_runs.jsonl"),
        "harvests": _read_jsonl(output / "harvested_runs.jsonl"),
        "report": report,
    }


def _combine_manifest(sources: list[dict[str, Any]]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    seen_cells: set[str] = set()
    attacker_models: dict[str, Any] = {}
    for source in sources:
        manifest = source["manifest"]
        for cell in manifest.get("cells", []):
            cell_id = str(cell.get("cell_id"))
            if cell_id in seen_cells:
                raise ValueError(f"duplicate logical cell_id across campaigns: {cell_id}")
            seen_cells.add(cell_id)
            cells.append(dict(cell))
        declared = manifest.get("attacker_models", {})
        if isinstance(declared, Mapping):
            for key, model in declared.items():
                if key in attacker_models and attacker_models[key] != model:
                    raise ValueError(f"attacker key has conflicting models: {key}")
                attacker_models[str(key)] = model

    attacks = _unique(cell.get("attack") for cell in cells)
    repetitions = _unique(cell.get("repetition") for cell in cells)
    attacker_keys = _unique(cell.get("attacker_key") for cell in cells)
    return {
        "schema_version": 1,
        "manifest_type": "combined_expected_cells",
        "campaign_name": "combined",
        "execution_scope_id": "combined",
        "attacker_keys": attacker_keys,
        "attacker_models": attacker_models,
        "attacks": attacks,
        "repetitions": repetitions,
        "expected_cell_count": len(cells),
        "expected_goal_evaluation_count": sum(
            len(cell.get("goal_ids") or []) for cell in cells
        ),
        "cells": cells,
        "source_manifests": [
            {
                "campaign_name": source["manifest"].get("campaign_name"),
                "execution_scope_id": source["manifest"].get("execution_scope_id"),
                "expected_cell_count": source["manifest"].get("expected_cell_count"),
                "expected_goal_evaluation_count": source["manifest"].get(
                    "expected_goal_evaluation_count"
                ),
                "shard_manifest_sha256": source["manifest"].get(
                    "shard_manifest_sha256"
                ),
            }
            for source in sources
        ],
    }


def combine_campaigns(
    campaigns: list[tuple[str | Path, str | Path]],
    output: str | Path,
    *,
    legacy_roots: Iterable[str | Path] = (),
    allow_partial: bool = False,
    tokenizer_name: str | None = None,
    tokenizer_revision: str | None = None,
    tokenizer_prompt_mode: str = "chat_template",
    tokenizer_trust_remote_code: bool = False,
) -> dict[str, Any]:
    """Reharvest source campaigns and write one combined derived export."""
    destination = Path(output).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    token_estimator = (
        TokenEstimator(
            tokenizer_name,
            revision=tokenizer_revision,
            prompt_mode=tokenizer_prompt_mode,
            trust_remote_code=tokenizer_trust_remote_code,
        )
        if tokenizer_name
        else None
    )
    sources: list[dict[str, Any]] = []
    for index, (manifest_path, root_path) in enumerate(campaigns):
        source_output = destination / f"source-{index:03d}"
        sources.append(
            _manifest_source(
                Path(manifest_path).expanduser().resolve(),
                Path(root_path).expanduser().resolve(),
                source_output,
                allow_partial,
                token_estimator,
            )
        )
    for offset, root_path in enumerate(legacy_roots, start=len(sources)):
        source_output = destination / f"source-{offset:03d}"
        sources.append(
            _legacy_source(
                Path(root_path).expanduser().resolve(),
                source_output,
                token_estimator,
            )
        )
    if not sources:
        raise ValueError("at least one manifest campaign or legacy campaign is required")

    plan_ids: dict[str, str] = {}
    harvest_ids: dict[str, str] = {}
    all_plans: list[dict[str, Any]] = []
    all_harvests: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        label = f"source-{index:03d}"
        _require_unique_ids(source["plans"], "experiment_run_id", plan_ids, label)
        _require_unique_ids(source["harvests"], "experiment_run_id", harvest_ids, label)
        all_plans.extend(source["plans"])
        all_harvests.extend(source["harvests"])

    all_plans, all_harvests, canonical_selection = _select_canonical_attempts(
        all_plans, all_harvests
    )

    errors: list[dict[str, Any]] = []
    _validate_goal_rows(all_harvests, errors)
    result_id_sources: dict[str, str] = {}
    for index, source in enumerate(sources):
        label = f"source-{index:03d}"
        for harvest in source["harvests"]:
            for goal in harvest.get("goals") or harvest.get("results") or []:
                if not isinstance(goal, Mapping) or goal.get("result_id") in (None, ""):
                    continue
                result_id = str(goal["result_id"])
                previous = result_id_sources.get(result_id)
                if previous is not None:
                    raise ValueError(
                        f"duplicate physical result_id across campaigns: {result_id} "
                        f"({previous}, {label})"
                    )
                result_id_sources[result_id] = label
    if errors and not allow_partial:
        raise ValueError(json.dumps({"errors": errors}, sort_keys=True))

    token_audit = _audit_token_evidence(
        all_harvests,
        enabled=token_estimator is not None,
    )
    source_reports = [source["report"] for source in sources]
    source_errors = [
        {
            "source": f"source-{index:03d}",
            **error,
        }
        for index, source in enumerate(sources)
        for error in source["report"].get("errors", [])
        if isinstance(error, Mapping)
    ]
    source_warnings = [
        {
            "source": f"source-{index:03d}",
            **warning,
        }
        for index, source in enumerate(sources)
        for warning in source["report"].get("warnings", [])
        if isinstance(warning, Mapping)
    ]
    if source_errors:
        errors.extend(source_errors)

    combined_manifest = _combine_manifest(sources)
    combined_coverage = _coverage(combined_manifest, all_plans, all_harvests)
    if errors:
        combined_coverage["validation_status"] = "invalid"
        combined_coverage["status"] = "partial"
    else:
        combined_coverage["validation_status"] = "valid"

    _write_jsonl(destination / "planned_runs.jsonl", all_plans)
    _write_jsonl(destination / "harvested_runs.jsonl", all_harvests)
    run_count, result_count = results.flatten_records(
        all_plans,
        all_harvests,
        destination / "runs.csv",
        destination / "results.csv",
        token_estimator=token_estimator,
    )
    (destination / "coverage.json").write_text(
        json.dumps(combined_coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "combined-manifest.json").write_text(
        json.dumps(combined_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    token_goal_rows = [
        goal
        for harvest in all_harvests
        for goal in (harvest.get("goals") or harvest.get("results") or [])
        if isinstance(goal, Mapping)
    ]
    status = (
        "complete"
        if not errors
        and combined_coverage.get("status") == "complete"
        and all(report.get("status") == "complete" for report in source_reports)
        else "partial"
    )
    token_audit_warnings = []
    if any(
        token_audit[field] > 0
        for field in (
            "malformed_value_count",
            "total_mismatch_count",
            "call_count_mismatch_count",
            "estimated_incomplete_count",
            "missing_status_count",
        )
    ):
        token_audit_warnings.append(
            {
                "code": "token_evidence_audit",
                "message": (
                    "derived token fields contain missing, partial, or malformed "
                    "evidence; see token_estimation.audit"
                ),
                "malformed_value_count": token_audit["malformed_value_count"],
                "total_mismatch_count": token_audit["total_mismatch_count"],
                "call_count_mismatch_count": token_audit[
                    "call_count_mismatch_count"
                ],
                "estimated_incomplete_count": token_audit[
                    "estimated_incomplete_count"
                ],
                "missing_status_count": token_audit["missing_status_count"],
            }
        )
    report = {
        "reharvest_version": "R2",
        "status": status,
        "created_at": _now(),
        "source_counts": {
            "campaigns": len(sources),
            "planned_attempts": len(all_plans),
            "harvested_attempts": len(all_harvests),
            "run_rows_written": run_count,
            "result_rows_written": result_count,
        },
        "coverage": combined_coverage,
        "canonical_selection": canonical_selection,
        "sources": source_reports,
        "errors": errors,
        "warnings": [*source_warnings, *token_audit_warnings],
        "policy": {
            "source_campaigns": "each source reharvested independently",
            "physical_ids": "experiment_run_id and result_id collisions are rejected",
            "output": "combined derived namespace; source files are read-only",
        },
        "token_estimation": (
            {
                **token_estimator.metadata,
                "enabled": True,
                "coverage": summarize_goal_estimates(token_goal_rows),
                "audit": token_audit,
                "policy": "target-side estimate only; attacker and combined values require role text",
            }
            if token_estimator is not None
            else {
                "enabled": False,
                "coverage": summarize_goal_estimates([]),
                "audit": token_audit,
                "policy": "no tokenizer estimate requested",
            }
        ),
        "output_paths": {
            "planned_runs": str(destination / "planned_runs.jsonl"),
            "harvested_runs": str(destination / "harvested_runs.jsonl"),
            "runs_csv": str(destination / "runs.csv"),
            "results_csv": str(destination / "results.csv"),
            "coverage": str(destination / "coverage.json"),
            "combined_manifest": str(destination / "combined-manifest.json"),
        },
    }
    (destination / "reharvest-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reharvest multiple campaigns into one derived export."
    )
    parser.add_argument(
        "--campaign",
        action="append",
        nargs=2,
        metavar=("MANIFEST", "CAMPAIGN_ROOT"),
        help="manifest/root pair; repeat for each shard-backed campaign",
    )
    parser.add_argument(
        "--campaign-root",
        action="append",
        type=Path,
        help="legacy flat campaign root; repeat as needed",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument(
        "--tokenizer",
        dest="tokenizer_name",
        help="Hugging Face tokenizer/model ID; enables explicit target-token estimates",
    )
    parser.add_argument("--tokenizer-revision")
    parser.add_argument(
        "--tokenizer-prompt-mode",
        choices=("chat_template", "text"),
        default="chat_template",
    )
    parser.add_argument("--tokenizer-trust-remote-code", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = combine_campaigns(
            [(manifest, root) for manifest, root in (args.campaign or [])],
            args.output,
            legacy_roots=args.campaign_root or [],
            allow_partial=args.allow_partial,
            tokenizer_name=args.tokenizer_name,
            tokenizer_revision=args.tokenizer_revision,
            tokenizer_prompt_mode=args.tokenizer_prompt_mode,
            tokenizer_trust_remote_code=args.tokenizer_trust_remote_code,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"campaign reharvest failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"reharvest status={report['status']} "
        f"campaigns={report['source_counts']['campaigns']} "
        f"plans={report['source_counts']['planned_attempts']} "
        f"harvests={report['source_counts']['harvested_attempts']} "
        f"results={report['source_counts']['result_rows_written']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "combine_campaigns", "main"]
