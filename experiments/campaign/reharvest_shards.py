"""Re-harvest shard SQLite evidence into a durable derived campaign export.

Workers write shard-local SQLite and JSONL records.  The ordinary merge command
flattens the harvested JSONL, which is intentionally sufficient for normal
campaign completion but cannot repair an export produced by an older harvester.
This command reads every selected shard database read-only, re-runs the
correlation/export step, and writes a separate downstream namespace containing
corrected JSONL, CSV, coverage, and provenance metadata.

The command never writes shard-local files, the merged source view, or SQLite.
Partial *coverage* is expected for an in-progress campaign; structural input
errors require ``--allow-partial`` if an operator explicitly wants a diagnostic
derived view anyway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import results
    from .merge_shards import (
        _coverage,
        _goal_evidence_complete,
        _hold_shard_locks,
        _select_canonical_attempts,
        _validate_goal_rows,
        _validate_harvest_record,
        _validate_plan_record,
    )
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
        _goal_evidence_complete,
        _hold_shard_locks,
        _select_canonical_attempts,
        _validate_goal_rows,
        _validate_harvest_record,
        _validate_plan_record,
    )
    from sharding import load_shard_manifest, validate_shard_manifest  # type: ignore[no-redef]
    from token_estimation import (  # type: ignore[no-redef]
        TokenEstimator,
        enrich_harvested_records,
        summarize_goal_estimates,
    )


DEFAULT_GOAL_MANIFEST = (
    Path(__file__).resolve().parent / "omnisafebench_af_10_per_subcategory_v1.json"
)
REHARVEST_VERSION = "R2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_jsonl_if_exists(path: Path) -> list[dict[str, Any]]:
    return results.read_jsonl(path) if path.exists() else []


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def _error(
    errors: list[dict[str, Any]], code: str, message: str, **fields: Any
) -> None:
    errors.append({"code": code, "message": message, **fields})


def _warning(
    warnings: list[dict[str, Any]], code: str, message: str, **fields: Any
) -> None:
    warnings.append({"code": code, "message": message, **fields})


def _safe_child(root: Path, relative: str | Path, description: str) -> Path:
    root = root.resolve()
    path = (root / str(relative)).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{description} escapes campaign root: {relative}")
    return path


def _goal_mapping(
    goal_manifest_path: Path, shard_manifest: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    goal_manifest = _read_json(goal_manifest_path)
    expected_hash = shard_manifest.get("frozen_goal_manifest_sha256")
    if goal_manifest.get("manifest_sha256") != expected_hash:
        raise ValueError(
            "goal manifest hash does not match the frozen shard manifest: "
            f"expected {expected_hash}, got {goal_manifest.get('manifest_sha256')}"
        )
    if goal_manifest.get("case_set_id") != shard_manifest.get("case_set_id"):
        raise ValueError("goal manifest case_set_id does not match shard manifest")
    goals = goal_manifest.get("goals")
    if not isinstance(goals, list) or not goals:
        raise ValueError("goal manifest must contain a non-empty goals list")
    mapping: dict[str, dict[str, Any]] = {}
    for goal in goals:
        if not isinstance(goal, dict) or not isinstance(goal.get("goal"), str):
            raise ValueError("goal manifest contains an invalid goal entry")
        text = goal["goal"]
        if text in mapping:
            raise ValueError("goal manifest contains duplicate goal text")
        mapping[text] = {
            "case_id": goal.get("goal_id"),
            "intent_id": goal.get("goal_id"),
            "goal_id": goal.get("goal_id"),
            "goal_batch_id": goal.get("goal_batch_id"),
            "category_id": goal.get("category_id"),
            "subcategory_id": goal.get("subcategory_id"),
        }
    return mapping, goal_manifest


def _selected_shards(
    shard_manifest: dict[str, Any], shard_ids: list[str] | None
) -> list[dict[str, Any]]:
    entries = shard_manifest.get("shards")
    if not isinstance(entries, list):
        raise ValueError("shard manifest has no shard entries")
    by_id = {str(entry.get("shard_id")): entry for entry in entries}
    selected = (
        list(by_id) if not shard_ids else [str(shard_id) for shard_id in shard_ids]
    )
    if len(set(selected)) != len(selected):
        raise ValueError("--shard-id must not contain duplicates")
    unknown = [shard_id for shard_id in selected if shard_id not in by_id]
    if unknown:
        raise ValueError(f"unknown shard IDs: {', '.join(unknown)}")
    return [by_id[shard_id] for shard_id in selected]


def _validate_output_location(
    output: Path, campaign_root: Path, shard_entries: list[dict[str, Any]]
) -> None:
    output_resolved = output.resolve()
    campaign_root_resolved = campaign_root.resolve()
    merged_root = campaign_root_resolved / "merged"
    if (
        output_resolved in {campaign_root_resolved, merged_root}
        or merged_root in output_resolved.parents
    ):
        raise ValueError("re-harvest output must be a separate derived namespace")
    for shard in shard_entries:
        paths = shard.get("paths")
        if not isinstance(paths, dict):
            continue
        shard_root = _safe_child(
            campaign_root, paths.get("relative_path", ""), "shard path"
        )
        if output_resolved == shard_root or shard_root in output_resolved.parents:
            raise ValueError("re-harvest output must not be inside a shard directory")


def _coverage_manifest(
    manifest: dict[str, Any], shard_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Limit coverage accounting to the selected shard set."""
    selected_cell_ids = {
        str(cell_id)
        for shard in shard_entries
        for cell_id in (shard.get("cell_ids") or [])
    }
    return {
        **manifest,
        "cells": [
            cell
            for cell in manifest.get("cells", [])
            if str(cell.get("cell_id")) in selected_cell_ids
        ],
    }


def _audit_target_calls(
    runs: list[dict[str, Any]], harvests: list[dict[str, Any]]
) -> dict[str, Any]:
    missing_goal_ids: list[str] = []
    missing_completed_goal_ids: list[str] = []
    missing_run_ids: list[str] = []
    incomplete_run_ids: list[str] = []
    totals: dict[str, dict[str, int]] = {}
    for harvest in harvests:
        attack = str(harvest.get("attack") or "unknown")
        attack_total = totals.setdefault(
            attack,
            {
                "runs": 0,
                "goals": 0,
                "target_call_count": 0,
                "missing_goal_target_call_count": 0,
                "advprefix_ce_call_count": 0,
                "advprefix_completion_call_count": 0,
                "missing_advprefix_ce_call_count": 0,
                "missing_advprefix_completion_call_count": 0,
            },
        )
        attack_total["runs"] += 1
        status = results.canonical_run_status(
            harvest.get("status") or harvest.get("run_status")
        )
        if status == "incomplete":
            incomplete_run_ids.append(str(harvest.get("experiment_run_id")))
        if (
            status == "completed"
            and harvest.get("target_call_count") is None
            and (harvest.get("result_count") or 0)
        ):
            missing_run_ids.append(str(harvest.get("experiment_run_id")))
        if harvest.get("target_call_count") is not None:
            attack_total["target_call_count"] += int(harvest["target_call_count"])
        for goal in harvest.get("goals") or harvest.get("results") or []:
            if not isinstance(goal, dict):
                continue
            attack_total["goals"] += 1
            if goal.get("goal_target_call_count") is None:
                attack_total["missing_goal_target_call_count"] += 1
                missing_goal_ids.append(str(goal.get("result_id")))
                if status == "completed":
                    missing_completed_goal_ids.append(str(goal.get("result_id")))
            if attack == "advprefix":
                for field, missing_field in (
                    (
                        "advprefix_ce_call_count",
                        "missing_advprefix_ce_call_count",
                    ),
                    (
                        "advprefix_completion_call_count",
                        "missing_advprefix_completion_call_count",
                    ),
                ):
                    value = goal.get(field)
                    if value is None:
                        attack_total[missing_field] += 1
                    else:
                        attack_total[field] += int(value)
    return {
        "missing_goal_target_call_count": len(missing_goal_ids),
        "missing_completed_goal_target_call_count": len(missing_completed_goal_ids),
        "missing_completed_run_target_call_count": len(missing_run_ids),
        "incomplete_run_count": len(incomplete_run_ids),
        "representative_incomplete_run_ids": incomplete_run_ids[:10],
        "representative_missing_goal_result_ids": missing_goal_ids[:10],
        "representative_missing_completed_goal_result_ids": (
            missing_completed_goal_ids[:10]
        ),
        "representative_missing_run_ids": missing_run_ids[:10],
        "by_attack": totals,
        "completed_run_rows": sum(
            results.canonical_run_status(
                run.get("status") or run.get("run_status")
            )
            == "completed"
            for run in runs
        ),
    }


def _reharvest_shards(
    manifest_path: str | Path,
    campaign_root: str | Path,
    output: str | Path,
    *,
    goal_manifest_path: str | Path | None = None,
    shard_ids: list[str] | None = None,
    allow_partial: bool = False,
    token_estimator: TokenEstimator | None = None,
) -> dict[str, Any]:
    """Re-harvest selected shard databases and write a derived export.

    The source shard plans and databases are never changed.  A database-backed
    harvest supersedes any stale shard-local harvested JSONL for the same
    physical attempt.  If a database is unavailable, an existing harvested
    record is preserved with a report warning; if neither source exists, the
    plan remains visible without fabricated goal rows. Correlated nonterminal
    database runs are audit-only evidence and are excluded from the canonical
    derived export when the manifest cell has a completed attempt.
    """
    manifest_source = Path(manifest_path).expanduser().resolve()
    root = Path(campaign_root).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    goal_source = (
        Path(goal_manifest_path or DEFAULT_GOAL_MANIFEST).expanduser().resolve()
    )

    shard_manifest = load_shard_manifest(manifest_source)
    validate_shard_manifest(shard_manifest)
    selected_entries = _selected_shards(shard_manifest, shard_ids)
    _validate_output_location(output_path, root, shard_manifest["shards"])
    goal_mapping, goal_manifest = _goal_mapping(goal_source, shard_manifest)

    cells = {
        str(cell["cell_id"]): cell
        for cell in shard_manifest.get("cells", [])
        if isinstance(cell, dict) and cell.get("cell_id")
    }
    errors: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    harvests: list[dict[str, Any]] = []
    plan_by_id: dict[str, dict[str, Any]] = {}
    source_details: list[dict[str, Any]] = []
    harvest_sources: dict[str, str] = {}
    warnings: list[dict[str, Any]] = []

    for shard in selected_entries:
        shard_id = str(shard.get("shard_id"))
        paths = shard.get("paths")
        if not isinstance(paths, dict):
            _error(
                errors, "missing_shard_paths", "shard has no paths", shard_id=shard_id
            )
            continue
        shard_root = _safe_child(
            root, paths.get("relative_path", ""), f"{shard_id} path"
        )
        plan_path = _safe_child(
            root, paths.get("planned_runs", ""), f"{shard_id} planned path"
        )
        harvest_path = _safe_child(
            root, paths.get("harvested_runs", ""), f"{shard_id} harvested path"
        )
        database_path = _safe_child(
            root, paths.get("hackagent_db", ""), f"{shard_id} database path"
        )
        shard_plans = _read_jsonl_if_exists(plan_path)
        existing_harvests = _read_jsonl_if_exists(harvest_path)
        if not plan_path.exists():
            _error(
                errors,
                "missing_plans",
                "shard has no planned_runs.jsonl",
                shard_id=shard_id,
            )
        existing_by_id: dict[str, dict[str, Any]] = {}
        for existing in existing_harvests:
            attempt_id = str(existing.get("experiment_run_id"))
            if attempt_id in existing_by_id:
                _error(
                    errors,
                    "duplicate_existing_harvest",
                    "shard harvested JSONL contains duplicate physical IDs",
                    shard_id=shard_id,
                    experiment_run_id=attempt_id,
                )
            existing_by_id[attempt_id] = existing

        shard_plan_ids: set[str] = set()
        for plan in shard_plans:
            attempt_id = str(plan.get("experiment_run_id"))
            if attempt_id in plan_by_id:
                _error(
                    errors,
                    "duplicate_plan_id",
                    "physical plan IDs must be unique across selected shards",
                    experiment_run_id=attempt_id,
                )
            if attempt_id in shard_plan_ids:
                _error(
                    errors,
                    "duplicate_shard_plan_id",
                    "shard contains duplicate physical plan IDs",
                    shard_id=shard_id,
                    experiment_run_id=attempt_id,
                )
            shard_plan_ids.add(attempt_id)
            plan_by_id[attempt_id] = plan
            _validate_plan_record(plan, shard_id, shard_manifest, cells, errors)
            plans.append(plan)

        for existing in existing_harvests:
            _validate_harvest_record(
                existing,
                shard_id,
                shard_manifest,
                {str(plan.get("experiment_run_id")): plan for plan in shard_plans},
                cells,
                errors,
            )

        reharvested_count = 0
        preserved_count = 0
        plan_only_count = 0
        completed_attempt_ids = {
            str(plan.get("experiment_run_id"))
            for plan in shard_plans
            if _goal_evidence_complete(
                plan, existing_by_id.get(str(plan.get("experiment_run_id")))
            )
        }
        completed_cell_ids = {
            str(plan.get("cell_id"))
            for plan in shard_plans
            if str(plan.get("experiment_run_id")) in completed_attempt_ids
        }
        if database_path.exists():
            for plan in shard_plans:
                attempt_id = str(plan.get("experiment_run_id"))
                if (
                    str(plan.get("cell_id")) in completed_cell_ids
                    and attempt_id not in completed_attempt_ids
                ):
                    _warning(
                        warnings,
                        "superseded_retry_plan",
                        "stale retry plan shares a manifest cell with a completed attempt and was not harvested",
                        shard_id=shard_id,
                        experiment_run_id=attempt_id,
                        cell_id=plan.get("cell_id"),
                    )
                    plan_only_count += 1
                    continue
                record = results.harvest_run(
                    attempt_id,
                    database_path,
                    goal_to_case_mapping=goal_mapping,
                    planned_record=plan,
                    token_estimator=token_estimator,
                    harvested_path=None,
                )
                if record.get("correlated") is True:
                    record["status"] = results.canonical_run_status(
                        record.get("status") or record.get("run_status")
                    )
                    _validate_harvest_record(
                        record,
                        shard_id,
                        shard_manifest,
                        {
                            str(plan.get("experiment_run_id")): plan
                            for plan in shard_plans
                        },
                        cells,
                        errors,
                    )
                    harvests.append(record)
                    harvest_sources[attempt_id] = "sqlite_reharvest"
                    reharvested_count += 1
                    if record["status"] == "incomplete":
                        _warning(
                            warnings,
                            "nonterminal_sqlite_run",
                            "SQLite run is nonterminal; observed evidence is retained as incomplete",
                            shard_id=shard_id,
                            experiment_run_id=attempt_id,
                            run_status=record.get("run_status"),
                            result_count=record.get("result_count"),
                        )
                    continue

                existing = existing_by_id.get(attempt_id)
                if existing is not None:
                    harvests.append(existing)
                    harvest_sources[attempt_id] = (
                        "existing_harvest_after_reharvest_failure"
                    )
                    preserved_count += 1
                    _warning(
                        warnings,
                        "reharvest_correlation_failed",
                        "SQLite did not correlate a planned attempt; preserved the existing harvested record",
                        shard_id=shard_id,
                        experiment_run_id=attempt_id,
                        reason=record.get("correlation", {}).get("reason")
                        if isinstance(record.get("correlation"), dict)
                        else None,
                    )
                else:
                    plan_only_count += 1
                    _warning(
                        warnings,
                        "plan_without_correlated_harvest",
                        "planned attempt has no correlated SQLite run; kept plan-only without fabricated goals",
                        shard_id=shard_id,
                        experiment_run_id=attempt_id,
                        reason=record.get("correlation", {}).get("reason")
                        if isinstance(record.get("correlation"), dict)
                        else None,
                    )
        else:
            _warning(
                warnings,
                "missing_shard_database",
                "referenced shard database does not exist; existing harvests are preserved",
                shard_id=shard_id,
                path=str(database_path),
            )
            for plan in shard_plans:
                attempt_id = str(plan.get("experiment_run_id"))
                existing = existing_by_id.get(attempt_id)
                if existing is None:
                    plan_only_count += 1
                    _warning(
                        warnings,
                        "plan_without_database_or_harvest",
                        "planned attempt has neither SQLite evidence nor an existing harvest; kept plan-only",
                        shard_id=shard_id,
                        experiment_run_id=attempt_id,
                    )
                    continue
                harvests.append(existing)
                harvest_sources[attempt_id] = "existing_harvest_no_database"
                preserved_count += 1

        source_details.append(
            {
                "shard_id": shard_id,
                "shard_root": str(shard_root),
                "planned_runs": str(plan_path),
                "existing_harvested_runs": str(harvest_path),
                "hackagent_db": str(database_path),
                "database_exists": database_path.exists(),
                "planned_count": len(shard_plans),
                "existing_harvest_count": len(existing_harvests),
                "sqlite_reharvest_count": reharvested_count,
                "preserved_existing_harvest_count": preserved_count,
                "plan_only_count": plan_only_count,
            }
        )

    if errors and not allow_partial:
        raise ValueError(json.dumps({"errors": errors}, sort_keys=True))

    # Keep only one terminal attempt for each immutable manifest cell. Retry
    # plans and live database rows remain auditable in the report but must not
    # enter the canonical derived export.
    canonical_plans, canonical_harvests, canonical_selection = (
        _select_canonical_attempts(plans, harvests)
    )
    harvest_by_id = {
        str(record.get("experiment_run_id")): record
        for record in canonical_harvests
    }
    ordered_harvests = [
        harvest_by_id[str(plan.get("experiment_run_id"))]
        for plan in canonical_plans
        if str(plan.get("experiment_run_id")) in harvest_by_id
    ]
    if token_estimator is not None:
        ordered_harvests = enrich_harvested_records(ordered_harvests, token_estimator)
    _validate_goal_rows(ordered_harvests, errors)
    if errors and not allow_partial:
        raise ValueError(json.dumps({"errors": errors}, sort_keys=True))

    output_path.mkdir(parents=True, exist_ok=True)
    planned_path = output_path / "planned_runs.jsonl"
    harvested_path = output_path / "harvested_runs.jsonl"
    runs_path = output_path / "runs.csv"
    results_path = output_path / "results.csv"
    coverage_path = output_path / "coverage.json"
    report_path = output_path / "reharvest-report.json"
    manifest_report_path = output_path / "reharvest-manifest.json"

    _write_jsonl(planned_path, canonical_plans)
    _write_jsonl(harvested_path, ordered_harvests)
    run_count, result_count = results.flatten_records(
        canonical_plans,
        ordered_harvests,
        runs_path,
        results_path,
        token_estimator=token_estimator,
    )
    coverage = _coverage(
        _coverage_manifest(shard_manifest, selected_entries),
        canonical_plans,
        ordered_harvests,
    )
    coverage["validation_status"] = "invalid" if errors else "valid"
    if errors:
        coverage["status"] = "partial"
    coverage_path.write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    target_call_audit = _audit_target_calls(
        [
            {
                "experiment_run_id": plan.get("experiment_run_id"),
                "status": harvest_by_id.get(str(plan.get("experiment_run_id")), {}).get(
                    "status", "planned"
                ),
            }
            for plan in canonical_plans
        ],
        ordered_harvests,
    )
    token_goal_rows = [
        goal
        for harvest in ordered_harvests
        for goal in (harvest.get("goals") or harvest.get("results") or [])
        if isinstance(goal, dict)
    ]
    status = (
        "complete"
        if not errors
        and coverage.get("status") == "complete"
        else "partial"
    )
    report = {
        "reharvest_version": REHARVEST_VERSION,
        "status": status,
        "created_at": _now(),
        "manifest_path": str(manifest_source),
        "campaign_root": str(root),
        "goal_manifest_path": str(goal_source),
        "goal_manifest_sha256": goal_manifest.get("manifest_sha256"),
        "shard_manifest_sha256": shard_manifest.get("shard_manifest_sha256"),
        "execution_scope_id": shard_manifest.get("execution_scope_id"),
        "selected_shards": [str(shard.get("shard_id")) for shard in selected_entries],
        "source_shards": source_details,
        "source_counts": {
            "planned_attempts": len(canonical_plans),
            "reharvested_attempts": sum(
                source == "sqlite_reharvest" for source in harvest_sources.values()
            ),
            "preserved_existing_harvests": sum(
                source.startswith("existing_harvest")
                for source in harvest_sources.values()
            ),
            "harvested_attempts_written": len(ordered_harvests),
            "plan_only_attempts": len(canonical_plans) - len(ordered_harvests),
            "nonterminal_attempts": sum(
                item.get("status") == "incomplete"
                for item in canonical_selection["excluded_attempt_details"]
            ),
            "raw_planned_attempts": canonical_selection["raw_planned_attempts"],
            "raw_harvested_attempts": canonical_selection["raw_harvested_attempts"],
            "excluded_attempts": canonical_selection["excluded_attempts"],
            "observed_goal_rows": sum(
                len(record.get("goals") or record.get("results") or [])
                for record in ordered_harvests
            ),
            "result_rows_written": result_count,
            "run_rows_written": run_count,
        },
        "coverage": coverage,
        "canonical_selection": canonical_selection,
        "target_call_audit": target_call_audit,
        "token_estimation": (
            {
                **token_estimator.metadata,
                "enabled": True,
                "coverage": summarize_goal_estimates(token_goal_rows),
                "policy": (
                    "target-side tokenizer estimate only; attacker and combined "
                    "fields remain blank without role-specific text evidence"
                ),
            }
            if token_estimator is not None
            else {
                "enabled": False,
                "coverage": summarize_goal_estimates([]),
                "policy": "no tokenizer estimate requested",
            }
        ),
        "policy": {
            "sqlite": "authoritative read-only source when available",
            "stale_harvest_jsonl": "superseded for attempts with available SQLite",
            "nonterminal_sqlite_run": "excluded from canonical planned, harvested, run, and result exports; retained only in canonical_selection audit",
            "retry_selection": "one completed attempt with complete goal evidence per manifest cell; stale retries are excluded",
            "missing_database": "existing harvest preserved; otherwise plan-only",
            "missing_goals": "never fabricated",
            "output": "derived namespace; source shards and merged view unchanged",
        },
        "errors": errors,
        "warnings": warnings,
        "output_paths": {
            "planned_runs": str(planned_path),
            "harvested_runs": str(harvested_path),
            "runs_csv": str(runs_path),
            "results_csv": str(results_path),
            "coverage": str(coverage_path),
            "reharvest_report": str(report_path),
            "reharvest_manifest": str(manifest_report_path),
        },
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_report = {
        "reharvest_version": REHARVEST_VERSION,
        "created_at": report["created_at"],
        "source": {
            "manifest_path": str(manifest_source),
            "declared_manifest_sha256": shard_manifest.get(
                "shard_manifest_sha256"
            ),
            "source_file_sha256": _sha256(manifest_source),
            "goal_manifest_path": str(goal_source),
            "declared_goal_manifest_sha256": goal_manifest.get("manifest_sha256"),
            "goal_source_file_sha256": _sha256(goal_source),
            "selected_shards": report["selected_shards"],
        },
        "output": {
            "runs_csv_sha256": _sha256(runs_path),
            "results_csv_sha256": _sha256(results_path),
            "coverage_sha256": _sha256(coverage_path),
            "reharvest_report_sha256": _sha256(report_path),
        },
        "target_call_policy": (
            "AdvPrefix includes target cross-entropy probes and surviving-prefix "
            "completion requests; other target-role calls are classified by the "
            "evidence adapter."
        ),
        "token_estimation": (
            {**token_estimator.metadata, "enabled": True}
            if token_estimator is not None
            else {"enabled": False}
        ),
        "lifecycle_policy": (
            "COMPLETED is the only terminal status eligible for scientific "
            "aggregates; PENDING/RUNNING are retained as incomplete observed "
            "evidence and missing planned goals are never synthesized."
        ),
    }
    manifest_report_path.write_text(
        json.dumps(manifest_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def reharvest_shards(
    manifest_path: str | Path,
    campaign_root: str | Path,
    output: str | Path,
    *,
    goal_manifest_path: str | Path | None = None,
    shard_ids: list[str] | None = None,
    allow_partial: bool = False,
    token_estimator: TokenEstimator | None = None,
    tokenizer_name: str | None = None,
    tokenizer_revision: str | None = None,
    tokenizer_prompt_mode: str = "chat_template",
    tokenizer_trust_remote_code: bool = False,
) -> dict[str, Any]:
    """Re-harvest a stable shard snapshot while holding worker locks."""
    manifest_source = Path(manifest_path).expanduser().resolve()
    root = Path(campaign_root).expanduser().resolve()
    manifest = load_shard_manifest(manifest_source)
    validate_shard_manifest(manifest)
    selected_entries = _selected_shards(manifest, shard_ids)
    if token_estimator is None and tokenizer_name:
        token_estimator = TokenEstimator(
            tokenizer_name,
            revision=tokenizer_revision,
            prompt_mode=tokenizer_prompt_mode,
            trust_remote_code=tokenizer_trust_remote_code,
        )
    with _hold_shard_locks(root, selected_entries):
        return _reharvest_shards(
            manifest_source,
            root,
            output,
            goal_manifest_path=goal_manifest_path,
            shard_ids=shard_ids,
            allow_partial=allow_partial,
            token_estimator=token_estimator,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-harvest shard SQLite evidence into a derived campaign export."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--goal-manifest",
        type=Path,
        default=DEFAULT_GOAL_MANIFEST,
        help="frozen goal manifest used to map exact goal text to taxonomy metadata",
    )
    parser.add_argument(
        "--shard-id",
        action="append",
        help="re-harvest only this shard; repeat to select multiple shards",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="write a diagnostic export despite structural source errors",
    )
    parser.add_argument(
        "--tokenizer",
        dest="tokenizer_name",
        help="Hugging Face tokenizer/model ID; enables explicit target-token estimates",
    )
    parser.add_argument(
        "--tokenizer-revision",
        help="optional tokenizer revision, commit, or tag",
    )
    parser.add_argument(
        "--tokenizer-prompt-mode",
        choices=("chat_template", "text"),
        default="chat_template",
        help="prompt serialization used for estimates (default: chat_template)",
    )
    parser.add_argument(
        "--tokenizer-trust-remote-code",
        action="store_true",
        help="allow custom tokenizer code while loading the requested tokenizer",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = reharvest_shards(
            args.manifest,
            args.campaign_root,
            args.output,
            goal_manifest_path=args.goal_manifest,
            shard_ids=args.shard_id,
            allow_partial=args.allow_partial,
            tokenizer_name=args.tokenizer_name,
            tokenizer_revision=args.tokenizer_revision,
            tokenizer_prompt_mode=args.tokenizer_prompt_mode,
            tokenizer_trust_remote_code=args.tokenizer_trust_remote_code,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"shard reharvest failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"reharvest status={report['status']} "
        f"plans={report['source_counts']['planned_attempts']} "
        f"harvests={report['source_counts']['harvested_attempts_written']} "
        f"results={report['source_counts']['result_rows_written']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
