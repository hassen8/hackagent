"""Validate and merge shard-local campaign outputs.

Workers own only their shard directories.  This command is the sole writer of
the campaign-level ``merged/`` view and opens shard databases read-only while
checking that exported records retain their shard provenance.
"""

from __future__ import annotations

import argparse
import fcntl
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from . import results
    from .sharding import load_shard_manifest, validate_shard_manifest
except ImportError:  # Allow the documented path-based CLI invocation.
    import results  # type: ignore[no-redef]
    from sharding import load_shard_manifest, validate_shard_manifest  # type: ignore[no-redef]


def _record_value(record: Mapping[str, Any], key: str) -> Any:
    value = record.get(key)
    if value is not None:
        return value
    nested = record.get(key.removesuffix("_key"))
    if isinstance(nested, Mapping):
        return nested.get("key")
    return None


def _read_optional(path: Path) -> list[dict[str, Any]]:
    return results.read_jsonl(path) if path.exists() else []


@contextmanager
def _hold_shard_locks(
    campaign_root: Path, shard_entries: Iterable[Mapping[str, Any]]
):
    """Hold every existing worker lock while reading a shard set.

    R4 workers acquire the same lock before opening their database or writing
    shard records.  A non-blocking acquisition makes merge/reharvest fail
    instead of producing a mixed snapshot while a worker is active.
    """
    handles: list[tuple[str, Any]] = []
    try:
        for shard in sorted(
            shard_entries, key=lambda entry: str(entry.get("shard_id"))
        ):
            paths = shard.get("paths")
            if not isinstance(paths, Mapping):
                continue
            relative_path = paths.get("relative_path")
            if not relative_path:
                continue
            shard_root = (campaign_root / str(relative_path)).resolve()
            if not shard_root.exists():
                continue
            lock_path = shard_root / "slurm" / "worker.lock"
            if not lock_path.exists():
                raise ValueError(
                    f"cannot verify shard quiescence; missing worker lock: {lock_path}"
                )
            handle = lock_path.open("r+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                handle.close()
                raise ValueError(
                    f"shard {shard.get('shard_id')} is active; refusing to read a live source"
                ) from exc
            handles.append((str(shard.get("shard_id")), handle))
        yield
    finally:
        for _shard_id, handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _error(
    errors: list[dict[str, Any]], code: str, message: str, **fields: Any
) -> None:
    errors.append({"code": code, "message": message, **fields})


def _field_matches_cell(
    record: Mapping[str, Any], cell: Mapping[str, Any], field: str
) -> bool:
    if field == "attacker_key":
        value = _record_value(record, "attacker_key")
    elif field == "target_key":
        value = _record_value(record, "target_key")
    elif field == "judge_key":
        value = _record_value(record, "judge_key")
    else:
        value = record.get(field)
    return value == cell.get(field)


def _validate_plan_record(
    plan: Mapping[str, Any],
    shard_id: str,
    manifest: Mapping[str, Any],
    cells: Mapping[str, Mapping[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    experiment_run_id = plan.get("experiment_run_id")
    cell_id = plan.get("cell_id")
    if not isinstance(experiment_run_id, str) or not experiment_run_id:
        _error(errors, "missing_experiment_run_id", "planned record has no physical ID")
    if not isinstance(cell_id, str) or not cell_id:
        _error(
            errors,
            "missing_cell_id",
            "planned record has no cell_id",
            shard_id=shard_id,
        )
        return
    cell = cells.get(cell_id)
    if cell is None:
        _error(
            errors,
            "unknown_cell_id",
            "planned record references an unknown cell",
            cell_id=cell_id,
        )
        return
    if plan.get("shard_id") != shard_id or cell.get("shard_id") != shard_id:
        _error(
            errors,
            "plan_shard_mismatch",
            "planned record is outside its shard",
            cell_id=cell_id,
        )
    if plan.get("shard_manifest_sha256") != manifest.get("shard_manifest_sha256"):
        _error(
            errors,
            "plan_manifest_hash_mismatch",
            "planned record has the wrong shard manifest hash",
            cell_id=cell_id,
        )
    if plan.get("execution_scope_id") != manifest.get("execution_scope_id"):
        _error(
            errors,
            "plan_scope_mismatch",
            "planned record has the wrong execution scope",
            cell_id=cell_id,
        )
    if plan.get("manifest_sha256") != cell.get("frozen_goal_manifest_sha256"):
        _error(
            errors,
            "plan_frozen_manifest_hash_mismatch",
            "planned record has the wrong frozen goal manifest hash",
            cell_id=cell_id,
        )
    for field in (
        "campaign_name",
        "case_set_id",
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
        if not _field_matches_cell(plan, cell, field):
            _error(
                errors,
                "plan_assignment_mismatch",
                f"planned record does not match manifest cell field {field}",
                cell_id=cell_id,
                field=field,
            )

    # Immutable manifests describe the attacker-driven matrix and do not carry the
    # derived applicability label.  Validate that label against the normalized
    # export later, but do not compare a plan field to an absent manifest field.


def _validate_harvest_record(
    harvest: Mapping[str, Any],
    shard_id: str,
    manifest: Mapping[str, Any],
    plans: Mapping[str, Mapping[str, Any]],
    cells: Mapping[str, Mapping[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    experiment_run_id = harvest.get("experiment_run_id")
    if not isinstance(experiment_run_id, str) or not experiment_run_id:
        _error(errors, "missing_harvest_id", "harvested record has no physical ID")
        return
    plan = plans.get(experiment_run_id)
    if plan is None:
        _error(
            errors,
            "harvest_without_plan",
            "every harvest must have exactly one planned record",
            experiment_run_id=experiment_run_id,
        )
        return
    for field in (
        "cell_id",
        "shard_id",
        "shard_manifest_sha256",
        "execution_scope_id",
    ):
        if harvest.get(field) != plan.get(field):
            _error(
                errors,
                "harvest_provenance_mismatch",
                f"harvested record does not match plan field {field}",
                experiment_run_id=experiment_run_id,
                field=field,
            )
    if harvest.get("shard_id") != shard_id:
        _error(
            errors,
            "harvest_shard_mismatch",
            "harvested record is outside its shard",
            experiment_run_id=experiment_run_id,
        )
    cell = cells.get(str(plan.get("cell_id")))
    if cell is None:
        return
    goals = harvest.get("goals") or harvest.get("results") or []
    expected_goal_ids = set(plan.get("goal_ids") or [])
    if not isinstance(goals, list):
        return
    for goal in goals:
        if not isinstance(goal, Mapping):
            continue
        if goal.get("goal_id") not in expected_goal_ids:
            _error(
                errors,
                "goal_assignment_mismatch",
                "harvested goal is not part of the planned manifest batch",
                experiment_run_id=experiment_run_id,
                goal_id=goal.get("goal_id"),
            )


def _evidence_trace_ids(goal: Mapping[str, Any]) -> list[str]:
    value = goal.get("evidence_trace_ids")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, Mapping):
        return []
    trace_ids: list[str] = []
    for field in ("target_trace_id", "judge_trace_ids", "target_call_trace_ids"):
        candidate = value.get(field)
        candidates = candidate if isinstance(candidate, list) else [candidate]
        trace_ids.extend(
            str(trace_id) for trace_id in candidates if trace_id not in (None, "")
        )
    return trace_ids


def _validate_goal_rows(
    harvests: Iterable[Mapping[str, Any]], errors: list[dict[str, Any]]
) -> set[str]:
    result_ids: dict[str, str] = {}
    evidence_ids: dict[str, str] = {}
    known_run_ids: set[str] = set()
    for harvest in harvests:
        experiment_run_id = str(harvest.get("experiment_run_id"))
        known_run_ids.add(experiment_run_id)
        goals = harvest.get("goals") or harvest.get("results") or []
        if not isinstance(goals, list):
            _error(
                errors,
                "invalid_goals",
                "harvest goals must be a list",
                experiment_run_id=experiment_run_id,
            )
            continue
        for goal in goals:
            if not isinstance(goal, Mapping):
                _error(
                    errors,
                    "invalid_goal",
                    "harvest goal must be an object",
                    experiment_run_id=experiment_run_id,
                )
                continue
            result_id = goal.get("result_id")
            if result_id not in (None, ""):
                result_key = str(result_id)
                previous = result_ids.get(result_key)
                if previous is not None:
                    _error(
                        errors,
                        "duplicate_result_id",
                        "result IDs must be unique across shards",
                        result_id=result_key,
                        first_run=previous,
                        second_run=experiment_run_id,
                    )
                result_ids[result_key] = experiment_run_id
            # A selected target trace is intentionally present both as the
            # scalar ``target_trace_id`` and inside the complete
            # ``target_call_trace_ids`` list.  Deduplicate references within
            # one goal, while still rejecting reuse of an evidence trace by a
            # different goal.
            for trace_id in dict.fromkeys(_evidence_trace_ids(goal)):
                previous = evidence_ids.get(trace_id)
                if previous is not None:
                    _error(
                        errors,
                        "duplicate_evidence_trace_id",
                        "selected evidence trace IDs must be unique across goals",
                        trace_id=trace_id,
                        first_run=previous,
                        second_run=experiment_run_id,
                    )
                evidence_ids[trace_id] = experiment_run_id
    return known_run_ids


def _goal_evidence_complete(
    plan: Mapping[str, Any], harvest: Mapping[str, Any] | None
) -> bool:
    if harvest is None:
        return False
    if results.canonical_run_status(
        harvest.get("status") or harvest.get("run_status")
    ) != "completed" or harvest.get("failure") is not None:
        return False
    expected_ids = {str(goal_id) for goal_id in (plan.get("goal_ids") or [])}
    goals = harvest.get("goals") or harvest.get("results") or []
    if not expected_ids or not isinstance(goals, list):
        return False
    observed_ids = {
        str(goal.get("goal_id"))
        for goal in goals
        if isinstance(goal, Mapping) and goal.get("goal_id") is not None
    }
    return observed_ids == expected_ids and len(goals) == len(expected_ids)


def _select_canonical_attempts(
    plans: Iterable[Mapping[str, Any]],
    harvests: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Select one terminal attempt per immutable logical cell.

    Retry plans are physical attempts, but the manifest cell is the scientific
    unit.  A stale RUNNING/PENDING plan must not create a row in a final export
    when the same cell already has a completed attempt.  Nonterminal and failed
    attempts are retained only in the returned audit metadata.
    """
    plan_list = [dict(plan) for plan in plans]
    harvest_by_id = {
        str(harvest.get("experiment_run_id")): dict(harvest)
        for harvest in harvests
        if harvest.get("experiment_run_id") is not None
    }
    by_cell: dict[str, list[tuple[int, dict[str, Any], dict[str, Any] | None]]] = {}
    for index, plan in enumerate(plan_list):
        cell_id = str(plan.get("cell_id"))
        attempt_id = str(plan.get("experiment_run_id"))
        by_cell.setdefault(cell_id, []).append(
            (index, plan, harvest_by_id.get(attempt_id))
        )

    selected_ids: set[str] = set()
    excluded: list[dict[str, Any]] = []
    for cell_id, candidates in by_cell.items():
        eligible = [
            candidate
            for candidate in candidates
            if _goal_evidence_complete(candidate[1], candidate[2])
        ]
        selected: tuple[int, dict[str, Any], dict[str, Any] | None] | None = None
        if eligible:
            # Prefer complete goal evidence, then the last completed attempt in
            # source order so a deliberate later retry wins deterministically.
            selected = max(eligible, key=lambda candidate: candidate[0])
            selected_ids.add(str(selected[1].get("experiment_run_id")))

        for _index, plan, harvest in candidates:
            attempt_id = str(plan.get("experiment_run_id"))
            if selected is not None and attempt_id == str(
                selected[1].get("experiment_run_id")
            ):
                continue
            raw_status = (
                harvest.get("run_status")
                if harvest is not None
                else plan.get("status") or "not_harvested"
            )
            canonical_status = results.canonical_run_status(
                harvest.get("status") if harvest is not None else raw_status
            )
            if canonical_status == "incomplete":
                reason = "nonterminal_attempt_excluded"
            elif selected is not None:
                reason = "superseded_by_canonical_completed_attempt"
            elif canonical_status == "missing_harvest":
                reason = "unharvested_attempt_excluded"
            else:
                reason = "nonterminal_or_failed_attempt_excluded"
            excluded.append(
                {
                    "experiment_run_id": attempt_id,
                    "cell_id": cell_id,
                    "status": canonical_status,
                    "run_status": raw_status,
                    "reason": reason,
                    "selected_experiment_run_id": (
                        str(selected[1].get("experiment_run_id"))
                        if selected is not None
                        else None
                    ),
                }
            )

    selected_plans = [
        plan
        for plan in plan_list
        if str(plan.get("experiment_run_id")) in selected_ids
    ]
    selected_harvests = [
        harvest_by_id[str(plan.get("experiment_run_id"))]
        for plan in selected_plans
        if str(plan.get("experiment_run_id")) in harvest_by_id
    ]
    by_reason = Counter(str(item["reason"]) for item in excluded)
    return selected_plans, selected_harvests, {
        "raw_planned_attempts": len(plan_list),
        "raw_harvested_attempts": len(harvest_by_id),
        "canonical_planned_attempts": len(selected_plans),
        "canonical_harvested_attempts": len(selected_harvests),
        "excluded_attempts": len(excluded),
        "excluded_by_reason": dict(by_reason),
        "excluded_attempt_details": excluded,
    }


def _coverage(
    manifest: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    harvests: list[Mapping[str, Any]],
) -> dict[str, Any]:
    plans_by_cell: dict[str, list[Mapping[str, Any]]] = {}
    harvest_by_run = {
        str(record.get("experiment_run_id")): record for record in harvests
    }
    for plan in plans:
        cell_id = str(plan.get("cell_id"))
        plans_by_cell.setdefault(cell_id, []).append(plan)
    cells = []
    for cell in manifest["cells"]:
        cell_id = str(cell["cell_id"])
        cell_plans = plans_by_cell.get(cell_id, [])
        cell_harvests = [
            harvest_by_run[str(plan.get("experiment_run_id"))]
            for plan in cell_plans
            if str(plan.get("experiment_run_id")) in harvest_by_run
        ]
        statuses = [
            results.canonical_run_status(
                harvest.get("status") or harvest.get("run_status")
            )
            for harvest in cell_harvests
        ]
        statuses.extend(
            "not_harvested"
            for _ in range(max(0, len(cell_plans) - len(cell_harvests)))
        )
        expected_goal_ids = {
            str(goal_id) for goal_id in (cell.get("goal_ids") or [])
        }
        observed_goal_counts: list[int] = []
        missing_goal_counts: list[int] = []
        complete_goal_harvests = 0
        for harvest in cell_harvests:
            goals = harvest.get("goals") or harvest.get("results") or []
            observed_goal_ids = {
                str(goal.get("goal_id"))
                for goal in goals
                if isinstance(goal, Mapping) and goal.get("goal_id") is not None
            }
            observed_count = len(observed_goal_ids & expected_goal_ids)
            observed_goal_counts.append(observed_count)
            missing_goal_counts.append(max(0, len(expected_goal_ids) - observed_count))
            if (
                results.canonical_run_status(
                    harvest.get("status") or harvest.get("run_status")
                )
                == "completed"
                and observed_count == len(expected_goal_ids)
                and len(goals) == len(expected_goal_ids)
            ):
                complete_goal_harvests += 1
        goal_evidence_complete = complete_goal_harvests > 0
        cells.append(
            {
                "cell_id": cell_id,
                "shard_id": cell.get("shard_id"),
                "attacker_key": cell.get("attacker_key"),
                "planned_attempt_count": len(cell_plans),
                "harvested_attempt_count": len(cell_harvests),
                "expected_goal_count": len(expected_goal_ids),
                "observed_goal_count": max(observed_goal_counts, default=0),
                "missing_goal_count": min(
                    missing_goal_counts, default=len(expected_goal_ids)
                ),
                "statuses": statuses,
                "execution_complete": any(status == "completed" for status in statuses),
                "goal_evidence_complete": goal_evidence_complete,
                "complete": goal_evidence_complete,
            }
        )
    counts = {
        "expected_cells": len(cells),
        "expected_goal_rows": sum(item["expected_goal_count"] for item in cells),
        "observed_goal_rows": sum(
            len(harvest.get("goals") or harvest.get("results") or [])
            for harvest in harvests
        ),
        "covered_goal_rows": sum(
            item["expected_goal_count"]
            for item in cells
            if item["goal_evidence_complete"]
        ),
        "missing_goal_rows": sum(
            item["missing_goal_count"]
            for item in cells
            if not item["goal_evidence_complete"]
        ),
        "planned_cells": sum(bool(item["planned_attempt_count"]) for item in cells),
        "harvested_cells": sum(bool(item["harvested_attempt_count"]) for item in cells),
        "completed_cells": sum(item["complete"] for item in cells),
        "execution_completed_cells": sum(
            item["execution_complete"] for item in cells
        ),
        "goal_incomplete_cells": sum(
            item["execution_complete"] and not item["goal_evidence_complete"]
            for item in cells
        ),
        "missing_cells": sum(not item["planned_attempt_count"] for item in cells),
        "incomplete_cells": sum(
            bool(item["planned_attempt_count"]) and not item["complete"]
            for item in cells
        ),
        "failed_attempts": sum(
            status == "failed" for item in cells for status in item["statuses"]
        ),
        "incomplete_attempts": sum(
            status in {"incomplete", "not_harvested"}
            for item in cells
            for status in item["statuses"]
        ),
        "unharvested_attempts": sum(
            max(0, item["planned_attempt_count"] - item["harvested_attempt_count"])
            for item in cells
        ),
        "nonterminal_attempts": sum(
            status == "incomplete" for item in cells for status in item["statuses"]
        ),
        "cancelled_attempts": sum(
            status == "cancelled" for item in cells for status in item["statuses"]
        ),
        "blocked_attempts": sum(
            status == "blocked" for item in cells for status in item["statuses"]
        ),
        "crashed_attempts": sum(
            status == "crashed" for item in cells for status in item["statuses"]
        ),
        "uncorrelated_attempts": sum(
            status == "uncorrelated" for item in cells for status in item["statuses"]
        ),
    }
    return {
        "status": "complete"
        if counts["completed_cells"] == counts["expected_cells"]
        else "partial",
        "counts": counts,
        "cells": cells,
    }


def _merge_shards(
    manifest_path: str | Path,
    campaign_root: str | Path,
    output: str | Path,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Validate shard evidence and optionally write the merged view."""
    manifest_source = Path(manifest_path).expanduser().resolve()
    root = Path(campaign_root).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    manifest = load_shard_manifest(manifest_source)
    errors: list[dict[str, Any]] = []
    try:
        validate_shard_manifest(manifest)
    except ValueError as exc:
        _error(errors, "invalid_manifest", str(exc))

    cells = {
        str(cell["cell_id"]): cell
        for cell in manifest.get("cells", [])
        if isinstance(cell, Mapping)
    }
    plans: list[dict[str, Any]] = []
    harvests: list[dict[str, Any]] = []
    physical_ids: dict[str, str] = {}
    shard_db_status: list[dict[str, Any]] = []

    shard_entries = manifest.get("shards", [])
    for shard in shard_entries:
        shard_id = str(shard.get("shard_id"))
        paths = shard.get("paths") if isinstance(shard, Mapping) else None
        if not isinstance(paths, Mapping):
            _error(
                errors,
                "missing_shard_paths",
                "manifest shard has no paths",
                shard_id=shard_id,
            )
            continue
        db_path = root / str(paths["hackagent_db"])
        db_report: dict[str, Any] = {
            "shard_id": shard_id,
            "path": str(db_path),
            "exists": db_path.exists(),
            "read_only_opened": False,
        }
        if db_path.exists():
            try:
                connection = results.open_read_only(db_path)
                connection.close()
                db_report["read_only_opened"] = True
            except Exception as exc:
                _error(errors, "database_open_failed", str(exc), shard_id=shard_id)
        else:
            _error(
                errors,
                "missing_shard_database",
                "referenced shard database does not exist",
                shard_id=shard_id,
            )
        shard_db_status.append(db_report)

        plan_path = root / str(paths["planned_runs"])
        harvest_path = root / str(paths["harvested_runs"])
        shard_plans = _read_optional(plan_path)
        shard_harvests = _read_optional(harvest_path)
        if not plan_path.exists():
            _error(
                errors,
                "missing_plans",
                "shard has no planned_runs.jsonl",
                shard_id=shard_id,
            )
        if not harvest_path.exists():
            _error(
                errors,
                "missing_harvests",
                "shard has no harvested_runs.jsonl",
                shard_id=shard_id,
            )
        plan_map: dict[str, dict[str, Any]] = {}
        for plan in shard_plans:
            experiment_run_id = str(plan.get("experiment_run_id"))
            if experiment_run_id in physical_ids:
                _error(
                    errors,
                    "duplicate_experiment_run_id",
                    "physical experiment IDs must be unique across shards",
                    experiment_run_id=experiment_run_id,
                )
            physical_ids[experiment_run_id] = shard_id
            if experiment_run_id in plan_map:
                _error(
                    errors,
                    "duplicate_plan",
                    "a shard contains duplicate physical plans",
                    experiment_run_id=experiment_run_id,
                )
            plan_map[experiment_run_id] = plan
            _validate_plan_record(plan, shard_id, manifest, cells, errors)
        for harvest in shard_harvests:
            _validate_harvest_record(
                harvest, shard_id, manifest, plan_map, cells, errors
            )
        plans.extend(shard_plans)
        harvests.extend(shard_harvests)

    plan_ids = [str(plan.get("experiment_run_id")) for plan in plans]
    harvest_ids = [str(harvest.get("experiment_run_id")) for harvest in harvests]
    if len(set(harvest_ids)) != len(harvest_ids):
        _error(errors, "duplicate_harvest", "physical harvest IDs must be unique")
    for harvest_id in harvest_ids:
        if harvest_id not in set(plan_ids):
            _error(
                errors,
                "harvest_without_plan",
                "every harvest must have exactly one plan",
                experiment_run_id=harvest_id,
            )

    raw_plan_count = len(plans)
    raw_harvest_count = len(harvests)
    plans, harvests, canonical_selection = _select_canonical_attempts(
        plans, harvests
    )
    _validate_goal_rows(harvests, errors)
    coverage = _coverage(manifest, plans, harvests)
    if coverage["status"] != "complete":
        _error(
            errors,
            "incomplete_coverage",
            "one or more manifest cells lack a completed attempt",
        )

    output_inside_shard = any(
        output_path == (root / str(shard["paths"]["relative_path"])).resolve()
        or (root / str(shard["paths"]["relative_path"])).resolve()
        in output_path.parents
        for shard in shard_entries
        if isinstance(shard, Mapping) and isinstance(shard.get("paths"), Mapping)
    )
    if output_inside_shard:
        _error(
            errors,
            "output_overwrites_shard",
            "merged output must not be inside a shard directory",
        )

    if errors:
        coverage["validation_status"] = "invalid"
        coverage["status"] = "partial"
    else:
        coverage["validation_status"] = "valid"

    report = {
        "schema_version": 1,
        "status": (
            "complete" if not errors else "partial" if allow_partial else "failed"
        ),
        "manifest_path": str(manifest_source),
        "campaign_root": str(root),
        "output": str(output_path),
        "shard_manifest_sha256": manifest.get("shard_manifest_sha256"),
        "execution_scope_id": manifest.get("execution_scope_id"),
        "errors": errors,
        "coverage": coverage,
        "shard_databases": shard_db_status,
        "source_counts": {
            "plans": len(plans),
            "harvests": len(harvests),
            "raw_plans": raw_plan_count,
            "raw_harvests": raw_harvest_count,
            "excluded_attempts": canonical_selection["excluded_attempts"],
        },
        "canonical_selection": canonical_selection,
    }
    if output_inside_shard or (errors and not allow_partial):
        raise ValueError(json.dumps(report, sort_keys=True))

    output_path.mkdir(parents=True, exist_ok=True)
    for filename, source_records in (
        ("planned_runs.jsonl", plans),
        ("harvested_runs.jsonl", harvests),
    ):
        destination = output_path / filename
        with destination.open("w", encoding="utf-8") as handle:
            for record in source_records:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    results.flatten_records(
        plans,
        harvests,
        output_path / "runs.csv",
        output_path / "results.csv",
    )
    (output_path / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_path / "merge-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def merge_shards(
    manifest_path: str | Path,
    campaign_root: str | Path,
    output: str | Path,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Merge a stable shard snapshot while holding every worker lock."""
    manifest_source = Path(manifest_path).expanduser().resolve()
    root = Path(campaign_root).expanduser().resolve()
    manifest = load_shard_manifest(manifest_source)
    validate_shard_manifest(manifest)
    with _hold_shard_locks(root, manifest.get("shards", [])):
        return _merge_shards(
            manifest_source,
            root,
            output,
            allow_partial=allow_partial,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and merge shard outputs.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = merge_shards(
            args.manifest,
            args.campaign_root,
            args.output,
            allow_partial=args.allow_partial,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"shard merge failed: {exc}")
        return 1
    print(
        f"merge status={report['status']} "
        f"plans={report['source_counts']['plans']} "
        f"harvests={report['source_counts']['harvests']} "
        f"errors={len(report['errors'])} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["merge_shards"]
