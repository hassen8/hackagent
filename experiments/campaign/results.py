"""Read-only HackAgent harvesting and campaign export flattening.

This module connects two stores without merging their ownership:

* HackAgent SQLite is authoritative raw evidence and is opened read-only.
* Campaign JSONL is append-only bookkeeping; normalized CSVs are regenerated
  from it for downstream analysis.

Selected successful-goal evidence is copied into the normalized export for the
next validation stage. SQLite remains the authoritative full trace store; the
export keeps only the selected prompt/response/judge fields and trace linkage.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import traceback
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .evidence_adapters import canonical_attack_name, extract_goal_evidence
    from .token_estimation import (
        ESTIMATED_RESULT_TOKEN_COLUMNS,
        ESTIMATED_RUN_TOKEN_COLUMNS,
        TokenEstimator,
        aggregate_goal_estimates,
        empty_estimated_run_fields,
    )
except ImportError:  # Allow the documented path-based CLI invocation.
    from evidence_adapters import (  # type: ignore[no-redef]
        canonical_attack_name,
        extract_goal_evidence,
    )
    from token_estimation import (  # type: ignore[no-redef]
        ESTIMATED_RESULT_TOKEN_COLUMNS,
        ESTIMATED_RUN_TOKEN_COLUMNS,
        TokenEstimator,
        aggregate_goal_estimates,
        empty_estimated_run_fields,
    )


# ---------------------------------------------------------------------------
# Export schema and parsing constants
# ---------------------------------------------------------------------------
_LABEL_RE = re.compile(r"^\s*(yes|no|1|0|true|false|safe|unsafe)\s*$", re.IGNORECASE)

BASE_RUN_COLUMNS = [
    "experiment_run_id",
    "campaign_name",
    "execution_scope_id",
    "cell_id",
    "shard_id",
    "shard_manifest_sha256",
    "evidence_db_relpath",
    "target_key",
    "target_model",
    "attacker_key",
    "attacker_model",
    "attacker_applicability",
    "judge_key",
    "judge_model",
    "attack",
    "case_set_id",
    "goal_batch_id",
    "category_id",
    "subcategory_id",
    "goal_ids",
    "manifest_sha256",
    "repetition",
    "status",
    "hackagent_run_status",
    "expected_goal_count",
    "observed_goal_count",
    "missing_goal_count",
    "goal_evidence_complete",
    "hackagent_run_id",
    "result_count",
    "trace_count",
    "target_call_count",
    "target_call_evidence_missing_count",
    "automated_success_count",
    "automated_asr",
    "not_evaluated_count",
    "judge_parse_failures",
    "judge_parse_total",
    "run_duration_s",
    "warmup_seconds",
    "failure_phase",
    "failure_class",
    "config_hash",
    "failure_type",
    "failure_message",
]

RUN_COLUMNS = [*BASE_RUN_COLUMNS, *ESTIMATED_RUN_TOKEN_COLUMNS]

BASE_RESULT_COLUMNS = [
    "experiment_run_id",
    "campaign_name",
    "execution_scope_id",
    "cell_id",
    "shard_id",
    "shard_manifest_sha256",
    "evidence_db_relpath",
    "target_key",
    "target_model",
    "hackagent_run_id",
    "attacker_key",
    "attacker_model",
    "attacker_applicability",
    "judge_key",
    "judge_model",
    "attack",
    "case_set_id",
    "goal_batch_id",
    "category_id",
    "subcategory_id",
    "manifest_sha256",
    "case_id",
    "intent_id",
    "goal_id",
    "goal",
    "goal_index",
    "repetition",
    "run_status",
    "hackagent_run_status",
    "evaluation_status",
    "automated_success",
    "manual_label",
    "validity",
    "failure_class",
    "evaluation_notes",
    "metadata",
    "evaluation_metrics",
    "attacker_prompt",
    "target_response",
    "judge_evaluation",
    "judge_reasoning",
    "evidence_trace_ids",
    "evidence_selection",
    "target_call_evidence_status",
    "goal_target_call_count",
    "advprefix_ce_call_count",
    "advprefix_completion_call_count",
    "val_judge_a",
    "val_judge_b",
    "val_judge_c",
    "valid",
    "result_id",
    "trace_ids",
    "run_duration_s",
    "goal_elapsed_s",
    "warmup_seconds",
    "config_hash",
]

RESULT_COLUMNS = [*BASE_RESULT_COLUMNS, *ESTIMATED_RESULT_TOKEN_COLUMNS]


# ---------------------------------------------------------------------------
# Small serialization/path helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_value(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _path(value: str | Path) -> Path:
    return Path(value).expanduser()


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one JSON object to a caller-selected JSONL path.

    JSONL is the campaign's append-only attempt history.  The caller chooses
    the path so this persistence helper does not own campaign layout or create
    state merely by importing the module.
    """
    destination = _path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True, default=str))
        handle.write("\n")


def append_planned_run(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one planned campaign record."""
    append_jsonl(path, record)


def append_harvested_run(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one harvested campaign record."""
    append_jsonl(path, record)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read JSON objects from an existing JSONL path without creating it."""
    source = _path(path)
    records: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{source}:{line_number} is not a JSON object")
            records.append(value)
    return records


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open HackAgent's evidence database in SQLite read-only URI mode.

    The campaign layer must never write bookkeeping into HackAgent's database.
    SQLite's ``mode=ro`` URI makes that boundary enforceable rather than merely
    conventional.
    """
    db_path = _path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Strict experiment-to-HackAgent correlation
# ---------------------------------------------------------------------------
def _output_dir(config: Mapping[str, Any]) -> str | None:
    value = config.get("output_dir")
    if value in (None, ""):
        return None
    return str(value)


def _correlate(
    conn: sqlite3.Connection, experiment_run_id: str
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Return the sole run whose declared output directory carries the ID.

    ``experiment_run_id`` is the campaign-side attempt ID.  The adapter puts it
    in ``output_dir``; HackAgent persists that configuration in either the
    attack or run row.  Exactly one unique HackAgent run must match.  In
    particular, this function never substitutes the newest database row when
    correlation is absent, and it does not copy raw evidence into JSONL.
    """
    rows = conn.execute(
        """
        SELECT r.id AS run_id,
               r.id AS hackagent_run_id,
               r.attack_id AS attack_id,
               r.attack_id AS hackagent_attack_id,
               a.type AS attack_type,
               r.status AS run_status,
               r.config_json AS run_config,
               r.created_at AS created_at,
               r.updated_at AS updated_at,
               a.config_json AS attack_config
          FROM runs AS r
          JOIN attacks AS a ON a.id = r.attack_id
        """
    ).fetchall()

    matches: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        row_dict = dict(row)
        attack_config = _json_object(row_dict.get("attack_config"))
        run_config = _json_object(row_dict.get("run_config"))
        declared_dirs = []
        for value in (_output_dir(attack_config), _output_dir(run_config)):
            if value is None:
                continue
            # Match the physical ID as a path component, not as a substring.
            # Otherwise run-1 would also correlate to run-10.
            if experiment_run_id in Path(value).parts:
                declared_dirs.append(value)
        if declared_dirs:
            matches.append((row_dict, declared_dirs[0]))

    unique_matches: dict[str, tuple[dict[str, Any], str]] = {}
    for row, declared_dir in matches:
        unique_matches[str(row["run_id"])] = (row, declared_dir)

    if not unique_matches:
        return (
            None,
            None,
            "no HackAgent run declared this experiment_run_id in output_dir",
        )
    if len(unique_matches) != 1:
        return (
            None,
            None,
            f"{len(unique_matches)} HackAgent runs declared this experiment_run_id in output_dir",
        )

    row, declared_dir = next(iter(unique_matches.values()))
    return row, declared_dir, "correlated"


def find_correlated_run(
    db_path: str | Path, experiment_run_id: str
) -> dict[str, Any] | None:
    """Find exactly one output-directory-correlated HackAgent run.

    A missing or ambiguous correlation returns ``None``.  In particular, this
    function never substitutes the newest run when the ID is absent.
    """
    conn = open_read_only(db_path)
    try:
        row, declared_dir, _reason = _correlate(conn, experiment_run_id)
        if row is None:
            return None
        row["declared_output_dir"] = declared_dir
        return row
    finally:
        conn.close()


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _run_duration(created_at: Any, updated_at: Any) -> float | None:
    start = _timestamp(created_at)
    end = _timestamp(updated_at)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def canonical_run_status(value: Any) -> str:
    """Normalize HackAgent lifecycle statuses for campaign exports.

    The raw database status remains in ``run_status``.  ``status`` is the
    campaign-level status used by coverage and analysis, where a live or other
    nonterminal database run is observed as incomplete rather than completed.
    """
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "complete": "completed",
        "completed": "completed",
        "running": "incomplete",
        "pending": "incomplete",
        "queued": "incomplete",
        "started": "incomplete",
        "active": "incomplete",
        "in_progress": "incomplete",
        "incomplete": "incomplete",
        "failed": "failed",
        "failure": "failed",
        "crashed": "crashed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "uncorrelated": "uncorrelated",
        "planned": "missing_harvest",
        "not_harvested": "missing_harvest",
        "missing_harvest": "missing_harvest",
    }.get(normalized, normalized or "unknown")


def _automated_success(status: Any) -> bool | None:
    """Map goal evaluation status to nullable automated success.

    ``FAILED_JAILBREAK`` is an evaluated negative goal, not an infrastructure
    crash.  Infrastructure state is represented separately by the harvested
    run status and structured ``failure`` object.
    """
    if status == "SUCCESSFUL_JAILBREAK":
        return True
    if status == "FAILED_JAILBREAK":
        return False
    return None


def _trace_parseable(content: Mapping[str, Any]) -> bool:
    score = content.get("score")
    numeric_score = isinstance(score, (int, float)) and not isinstance(score, bool)
    result = content.get("result")
    label = result is not None and _LABEL_RE.match(str(result)) is not None
    return numeric_score or label


def _case_fields(
    goal: str, goal_to_case_mapping: Mapping[str, Any] | None
) -> dict[str, Any]:
    value = (goal_to_case_mapping or {}).get(goal)
    if isinstance(value, Mapping):
        return {
            "case_id": value.get("case_id"),
            "intent_id": value.get("intent_id"),
            "goal_id": value.get("goal_id"),
            "goal_batch_id": value.get("goal_batch_id"),
            "category_id": value.get("category_id"),
            "subcategory_id": value.get("subcategory_id"),
        }
    if value is not None:
        return {"case_id": value, "intent_id": None, "goal_id": None}
    return {"case_id": None, "intent_id": None, "goal_id": None}


# ---------------------------------------------------------------------------
# Read-only evidence extraction and automated metrics
# ---------------------------------------------------------------------------
def extract_hackagent_run(
    db_path: str | Path,
    experiment_run_id: str,
    goal_to_case_mapping: Mapping[str, Any] | None = None,
    *,
    token_estimator: TokenEstimator | None = None,
) -> dict[str, Any]:
    """Extract one correlated run and its result/trace references read-only.

    ``result_count`` counts goal result rows, while ``trace_count`` counts all
    trace rows attached to those results.  Evaluator-bearing traces contribute
    to ``judge_parse_total`` and ``judge_parse_failures``.  Warmup data is
    attached later by harvest_run(); it is diagnostic and never contributes to
    goal-level ASR.
    """
    conn = open_read_only(db_path)
    try:
        run, declared_dir, reason = _correlate(conn, experiment_run_id)
        if run is None:
            return {
                "experiment_run_id": experiment_run_id,
                "status": "uncorrelated",
                "run_status": "UNCORRELATED",
                "hackagent_run_id": None,
                "hackagent_attack_id": None,
                "correlated": False,
                "correlation": {"status": "uncorrelated", "reason": reason},
                "result_count": 0,
                "trace_count": 0,
                "target_call_count": None,
                "target_call_evidence_missing_count": None,
                "automated_success_count": 0,
                "automated_asr": None,
                "not_evaluated_count": 0,
                "judge_parse_failures": 0,
                "judge_parse_total": 0,
                "run_duration_s": None,
                "goals": [],
                "failure": None,
                "harvested_at": _now(),
            }

        result_rows = conn.execute(
            """
            SELECT id, goal, goal_index, evaluation_status, evaluation_notes,
                   metrics_json, metadata_json
              FROM results
             WHERE run_id = ?
             ORDER BY goal_index, id
            """,
            (run["run_id"],),
        ).fetchall()

        result_ids = [str(row["id"]) for row in result_rows]
        traces_by_result: dict[str, list[dict[str, Any]]] = {
            result_id: [] for result_id in result_ids
        }
        if result_ids:
            placeholders = ",".join("?" for _ in result_ids)
            trace_rows = conn.execute(
                f"""
                SELECT id, result_id, sequence, step_type, content_json, created_at
                  FROM traces
                 WHERE result_id IN ({placeholders})
                 ORDER BY result_id, sequence, id
                """,
                result_ids,
            ).fetchall()
            for trace_row in trace_rows:
                content = _json_value(trace_row["content_json"], {})
                trace = {
                    "trace_id": str(trace_row["id"]),
                    "sequence": trace_row["sequence"],
                    "step_type": trace_row["step_type"],
                    "created_at": trace_row["created_at"],
                    "content": content,
                }
                traces_by_result.setdefault(str(trace_row["result_id"]), []).append(
                    trace
                )

        attack_config = _json_object(run.get("attack_config"))
        attack_name = canonical_attack_name(
            attack_config.get("attack_type")
            or attack_config.get("attack")
            or run.get("attack_type")
        )
        goals: list[dict[str, Any]] = []
        evaluator_total = 0
        evaluator_failures = 0
        success_count = 0
        failure_count = 0
        target_call_counts: list[int | None] = []

        for result_row in result_rows:
            result_id = str(result_row["id"])
            status = result_row["evaluation_status"]
            automated_success = _automated_success(status)
            if automated_success is True:
                success_count += 1
            elif automated_success is False:
                failure_count += 1

            traces = traces_by_result.get(result_id, [])
            trace_ids = [trace["trace_id"] for trace in traces]
            evaluator_traces: list[dict[str, Any]] = []
            for trace in traces:
                content = trace["content"]
                if not isinstance(content, Mapping) or content.get("evaluator") is None:
                    continue
                parseable = _trace_parseable(content)
                evaluator_total += 1
                if not parseable:
                    evaluator_failures += 1
                evaluator_traces.append(
                    {
                        "trace_id": trace["trace_id"],
                        "sequence": trace["sequence"],
                        "step_type": trace["step_type"],
                        "evaluator": content.get("evaluator"),
                        "score": content.get("score"),
                        "result": content.get("result"),
                        "parseable": parseable,
                    }
                )

            metadata = _json_object(result_row["metadata_json"])
            evaluation_metrics = _json_object(result_row["metrics_json"])
            goal = str(result_row["goal"] or "")
            elapsed = metadata.get("elapsed_s")
            if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
                elapsed = metadata.get("goal_elapsed_s")
            case_fields = _case_fields(goal, goal_to_case_mapping)
            evidence = extract_goal_evidence(
                attack_name,
                traces,
                evaluation_status=status,
                token_estimator=token_estimator,
            )
            target_call_count = evidence.get("goal_target_call_count")
            target_call_counts.append(target_call_count)
            goals.append(
                {
                    **case_fields,
                    "goal": goal,
                    "goal_index": result_row["goal_index"],
                    "result_id": result_id,
                    "evaluation_status": status,
                    "automated_success": automated_success,
                    "evaluation_notes": result_row["evaluation_notes"],
                    "metadata": metadata,
                    "evaluation_metrics": evaluation_metrics,
                    "trace_ids": trace_ids,
                    "evaluator_trace_ids": [
                        trace["trace_id"] for trace in evaluator_traces
                    ],
                    "evaluator_traces": evaluator_traces,
                    "goal_elapsed_s": elapsed,
                    **evidence,
                }
            )

        evaluated_count = success_count + failure_count
        not_evaluated_count = len(goals) - evaluated_count
        run_status = run["run_status"]
        campaign_status = canonical_run_status(run_status)
        return {
            "experiment_run_id": experiment_run_id,
            "status": campaign_status,
            "run_status": run_status,
            "hackagent_run_id": str(run["run_id"]),
            "hackagent_attack_id": str(run["attack_id"]),
            "correlated": True,
            "correlation": {
                "status": "correlated",
                "output_dir": declared_dir,
            },
            "output_dir": declared_dir,
            "result_count": len(goals),
            "trace_count": sum(len(traces) for traces in traces_by_result.values()),
            "target_call_count": (
                sum(target_call_counts)
                if target_call_counts
                and all(count is not None for count in target_call_counts)
                else None
            ),
            "target_call_evidence_missing_count": sum(
                count is None for count in target_call_counts
            ),
            "automated_success_count": success_count,
            "automated_failure_count": failure_count,
            "automated_asr": (
                success_count / evaluated_count if evaluated_count else None
            )
            if campaign_status == "completed"
            else None,
            "not_evaluated_count": not_evaluated_count,
            "judge_parse_failures": evaluator_failures,
            "judge_parse_total": evaluator_total,
            "run_duration_s": _run_duration(run["created_at"], run["updated_at"]),
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "goals": goals,
            "failure": None,
            "harvested_at": _now(),
        }
    finally:
        conn.close()


def _copy_plan_metadata(
    record: dict[str, Any], planned_record: Mapping[str, Any] | None
) -> None:
    if not planned_record:
        return
    keys = (
        "campaign_name",
        "execution_scope_id",
        "cell_id",
        "shard_id",
        "shard_manifest_sha256",
        "evidence_db_relpath",
        "target_key",
        "target_model",
        "attacker_key",
        "attacker_model",
        "attacker_applicability",
        "judge_key",
        "judge_model",
        "judge_keys",
        "judge_models",
        "attack",
        "case_set_id",
        "goal_ids",
        "goal_batch_id",
        "category_id",
        "subcategory_id",
        "manifest_sha256",
        "case_ids",
        "repetition",
        "config_hash",
        "output_dir",
    )
    for key in keys:
        if key in planned_record and key not in record:
            record[key] = planned_record[key]


def _add_warmups(record: dict[str, Any], warmups: Mapping[str, Any] | None) -> None:
    if warmups is None:
        return
    record["warmups"] = dict(warmups)
    seconds = 0.0
    found = False
    for value in warmups.values():
        candidate = value.get("seconds") if isinstance(value, Mapping) else value
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            seconds += float(candidate)
            found = True
    record["warmup_seconds"] = round(seconds, 3) if found else None


# ---------------------------------------------------------------------------
# Harvest/error lifecycle
# ---------------------------------------------------------------------------
def build_error_record(
    experiment_run_id: str,
    phase: str,
    exc: BaseException,
    *,
    traceback_text: str | None = None,
) -> dict[str, Any]:
    """Build the structured failure object used in harvested JSONL.

    The phase identifies which lifecycle boundary failed.  Keeping type,
    message, and traceback as data makes failures groupable by analysis code
    while ordinary Slurm/campaign logs remain useful for operators.
    """
    trace = traceback_text or traceback.format_exc()
    failure = {
        "phase": phase,
        "type": type(exc).__name__,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "traceback": trace,
    }
    return {
        "experiment_run_id": experiment_run_id,
        "status": "crashed",
        "run_status": None,
        "hackagent_run_id": None,
        "hackagent_attack_id": None,
        "correlated": False,
        "result_count": None,
        "trace_count": None,
        "target_call_count": None,
        "target_call_evidence_missing_count": None,
        "automated_success_count": None,
        "automated_asr": None,
        "not_evaluated_count": None,
        "judge_parse_failures": None,
        "judge_parse_total": None,
        "run_duration_s": None,
        "failure": failure,
        "goals": [],
        "harvested_at": _now(),
    }


def harvest_run(
    experiment_run_id: str,
    db_path: str | Path,
    goal_to_case_mapping: Mapping[str, Any] | None = None,
    *,
    planned_record: Mapping[str, Any] | None = None,
    warmups: Mapping[str, Any] | None = None,
    token_estimator: TokenEstimator | None = None,
    harvested_path: str | Path | None = None,
) -> dict[str, Any]:
    """Harvest one run and optionally append its record to harvested JSONL.

    Metadata from the plan and diagnostic warmups are joined here because they
    describe the physical attempt, not an individual goal result.  A harvest
    exception becomes a structured ``phase=harvest`` failure instead of being
    allowed to erase the planned attempt.
    """
    try:
        record = extract_hackagent_run(
            db_path,
            experiment_run_id,
            goal_to_case_mapping=goal_to_case_mapping,
            token_estimator=token_estimator,
        )
    except Exception as exc:
        record = build_error_record(experiment_run_id, "harvest", exc)

    _copy_plan_metadata(record, planned_record)
    _add_warmups(record, warmups)
    if harvested_path is not None:
        append_harvested_run(harvested_path, record)
    return record


def _records(
    value: str | Path | Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return read_jsonl(value)
    return [dict(record) for record in value]


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _model_fields(record: Mapping[str, Any], kind: str) -> tuple[Any, Any, Any]:
    key = record.get(f"{kind}_key")
    model = record.get(f"{kind}_model")
    nested = record.get(kind)
    if isinstance(nested, Mapping):
        key = key if key is not None else nested.get("key")
        model = model if model is not None else nested.get("ollama_model")
    return key, _first(model), nested


def _judge_fields(record: Mapping[str, Any]) -> tuple[Any, Any]:
    key = record.get("judge_key")
    model = record.get("judge_model")
    keys = record.get("judge_keys")
    models = record.get("judge_models")
    if key is None:
        key = _first(keys)
    if model is None:
        model = _first(models)
    nested = record.get("judge")
    if isinstance(nested, Mapping):
        key = key if key is not None else nested.get("key")
        model = model if model is not None else nested.get("ollama_model")
    return key, model


def _attack_name(record: Mapping[str, Any]) -> Any:
    attack = record.get("attack")
    if isinstance(attack, Mapping):
        return attack.get("name") or attack.get("attack_type")
    return attack


def _warmup_seconds(record: Mapping[str, Any]) -> Any:
    if "warmup_seconds" in record:
        return record["warmup_seconds"]
    warmups = record.get("warmups")
    if not isinstance(warmups, Mapping):
        return None
    total = 0.0
    found = False
    for value in warmups.values():
        seconds = value.get("seconds") if isinstance(value, Mapping) else value
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            total += float(seconds)
            found = True
    return round(total, 3) if found else None


def _failure_fields(record: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    failure = record.get("failure")
    if not isinstance(failure, Mapping):
        return None, None, None
    return (
        failure.get("phase"),
        failure.get("type") or failure.get("exception_type"),
        failure.get("message"),
    )


def _goal_coverage(
    plan: Mapping[str, Any], harvest: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Describe observed goal rows without inventing missing rows."""
    planned_ids = plan.get("goal_ids") or plan.get("case_ids") or []
    expected_ids = {
        str(goal_id) for goal_id in planned_ids if goal_id not in (None, "")
    }
    goals = (harvest or {}).get("goals") or (harvest or {}).get("results") or []
    observed_ids = {
        str(goal.get("goal_id"))
        for goal in goals
        if isinstance(goal, Mapping) and goal.get("goal_id") not in (None, "")
    }
    observed_expected_ids = observed_ids & expected_ids
    status = canonical_run_status(
        (harvest or {}).get("status")
        or (harvest or {}).get("run_status")
        or plan.get("status")
    )
    complete = bool(
        expected_ids
        and status == "completed"
        and observed_ids == expected_ids
        and len(goals) == len(expected_ids)
    )
    return {
        "expected_goal_count": len(expected_ids) if expected_ids else None,
        "observed_goal_count": len(observed_expected_ids),
        "missing_goal_count": (
            len(expected_ids) - len(observed_expected_ids) if expected_ids else None
        ),
        "goal_evidence_complete": complete if expected_ids else None,
    }


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _write_csv(
    path: str | Path, columns: list[str], rows: Iterable[Mapping[str, Any]]
) -> None:
    destination = _path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


# ---------------------------------------------------------------------------
# Normalized run- and goal-level exports
# ---------------------------------------------------------------------------
def _run_row(
    plan: Mapping[str, Any], harvest: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Flatten one planned cell into one coverage/reliability row.

    A run row is emitted even when no harvest exists.  That is how a hard
    process kill remains visible as a planned attempt with missing harvest,
    rather than disappearing from coverage statistics.
    """
    target_key, target_model, _ = _model_fields(plan, "target")
    attacker_key, attacker_model, _ = _model_fields(plan, "attacker")
    judge_key, judge_model = _judge_fields(plan)
    failure_phase, failure_type, failure_message = _failure_fields(harvest or plan)
    source = harvest or {}
    status = canonical_run_status(
        source.get("status") or plan.get("status") or "not_harvested"
    )
    goal_coverage = _goal_coverage(plan, harvest)
    row = {
        "experiment_run_id": plan.get("experiment_run_id"),
        "campaign_name": plan.get("campaign_name"),
        "execution_scope_id": plan.get("execution_scope_id"),
        "cell_id": plan.get("cell_id"),
        "shard_id": plan.get("shard_id"),
        "shard_manifest_sha256": plan.get("shard_manifest_sha256"),
        "evidence_db_relpath": plan.get("evidence_db_relpath"),
        "target_key": target_key,
        "target_model": target_model,
        "attacker_key": attacker_key,
        "attacker_model": attacker_model,
        "attacker_applicability": plan.get("attacker_applicability"),
        "judge_key": judge_key,
        "judge_model": judge_model,
        "attack": _attack_name(plan),
        "case_set_id": plan.get("case_set_id"),
        "goal_batch_id": plan.get("goal_batch_id"),
        "category_id": plan.get("category_id"),
        "subcategory_id": plan.get("subcategory_id"),
        "goal_ids": plan.get("goal_ids") or plan.get("case_ids"),
        "manifest_sha256": plan.get("manifest_sha256"),
        "repetition": plan.get("repetition"),
        "status": status,
        "hackagent_run_status": source.get("run_status") if harvest is not None else None,
        **goal_coverage,
        "hackagent_run_id": source.get("hackagent_run_id"),
        "result_count": source.get("result_count") if harvest is not None else None,
        "trace_count": source.get("trace_count") if harvest is not None else None,
        "target_call_count": (
            source.get("target_call_count") if harvest is not None else None
        ),
        "target_call_evidence_missing_count": (
            source.get("target_call_evidence_missing_count")
            if harvest is not None
            else None
        ),
        "automated_success_count": source.get("automated_success_count")
        if harvest is not None
        else None,
        "automated_asr": source.get("automated_asr") if harvest is not None else None,
        "not_evaluated_count": source.get("not_evaluated_count")
        if harvest is not None
        else None,
        "judge_parse_failures": source.get("judge_parse_failures")
        if harvest is not None
        else None,
        "judge_parse_total": source.get("judge_parse_total")
        if harvest is not None
        else None,
        "run_duration_s": source.get("run_duration_s") if harvest is not None else None,
        "warmup_seconds": _warmup_seconds(source) if harvest is not None else None,
        "failure_phase": failure_phase,
        "failure_class": (source.get("failure_class") or "") if harvest else "",
        "config_hash": plan.get("config_hash"),
        "failure_type": failure_type,
        "failure_message": failure_message,
    }
    row.update(
        aggregate_goal_estimates(
            [
                goal
                for goal in (source.get("goals") or source.get("results") or [])
                if isinstance(goal, Mapping)
            ]
        )
        if harvest is not None
        else empty_estimated_run_fields()
    )
    return row


def _result_rows(
    plan: Mapping[str, Any],
    harvest: Mapping[str, Any],
    token_estimator: TokenEstimator | None = None,
) -> list[dict[str, Any]]:
    """Flatten nested harvested goals into one row per executed goal.

    These are atomic analytical rows.  Manual labels and validity are carried
    through if a later producer supplies them, but this extractor does not
    interpret or invent manual review decisions.
    """
    target_key, target_model, _ = _model_fields(plan, "target")
    attacker_key, attacker_model, _ = _model_fields(plan, "attacker")
    judge_key, judge_model = _judge_fields(plan)
    goals = harvest.get("goals") or harvest.get("results") or []
    if not isinstance(goals, list):
        return []
    rows: list[dict[str, Any]] = []
    for goal in goals:
        if not isinstance(goal, Mapping):
            continue
        status = goal.get("evaluation_status")
        automated_success = goal.get("automated_success")
        if "automated_success" not in goal:
            automated_success = _automated_success(status)
        metadata = goal.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = _json_object(goal.get("metadata_json"))
        elapsed = goal.get("goal_elapsed_s")
        if elapsed is None:
            elapsed = metadata.get("elapsed_s") or metadata.get("goal_elapsed_s")
        trace_ids = goal.get("trace_ids")
        if trace_ids is None:
            trace_ids = goal.get("trace_ids_json") or []
        if isinstance(trace_ids, str):
            try:
                trace_ids = json.loads(trace_ids)
            except json.JSONDecodeError:
                pass
        row = {
            "experiment_run_id": plan.get("experiment_run_id"),
            "campaign_name": plan.get("campaign_name"),
            "execution_scope_id": plan.get("execution_scope_id"),
            "cell_id": plan.get("cell_id"),
            "shard_id": plan.get("shard_id"),
            "shard_manifest_sha256": plan.get("shard_manifest_sha256"),
            "evidence_db_relpath": plan.get("evidence_db_relpath"),
            "target_key": target_key,
            "target_model": target_model,
            "hackagent_run_id": harvest.get("hackagent_run_id"),
            "attacker_key": attacker_key,
            "attacker_model": attacker_model,
            "attacker_applicability": plan.get("attacker_applicability"),
            "judge_key": judge_key,
            "judge_model": judge_model,
            "attack": _attack_name(plan),
            "case_set_id": plan.get("case_set_id"),
            "goal_batch_id": goal.get("goal_batch_id") or plan.get("goal_batch_id"),
            "category_id": goal.get("category_id") or plan.get("category_id"),
            "subcategory_id": goal.get("subcategory_id")
            or plan.get("subcategory_id"),
            "manifest_sha256": plan.get("manifest_sha256"),
            "case_id": goal.get("case_id"),
            "intent_id": goal.get("intent_id"),
            "goal_id": goal.get("goal_id"),
            "goal": goal.get("goal"),
            "goal_index": goal.get("goal_index"),
            "repetition": plan.get("repetition"),
            # Keep the normalized lifecycle status aligned with runs.csv;
            # the raw HackAgent value remains available in the separate
            # provenance column and in harvested JSONL.
            "run_status": canonical_run_status(
                harvest.get("status") or harvest.get("run_status")
            ),
            "hackagent_run_status": harvest.get("run_status"),
            "evaluation_status": status,
            "automated_success": automated_success,
            "manual_label": goal.get("manual_label") or "",
            "validity": goal.get("validity") or "",
            "failure_class": goal.get("failure_class") or "",
            "evaluation_notes": goal.get("evaluation_notes"),
            "metadata": metadata,
            "evaluation_metrics": goal.get("evaluation_metrics") or {},
            "attacker_prompt": goal.get("attacker_prompt") or "",
            "target_response": goal.get("target_response") or "",
            "judge_evaluation": goal.get("judge_evaluation") or {},
            "judge_reasoning": goal.get("judge_reasoning") or "",
            "evidence_trace_ids": goal.get("evidence_trace_ids") or {},
            "evidence_selection": goal.get("evidence_selection") or "",
            "target_call_evidence_status": goal.get("target_call_evidence_status")
            or "",
            "val_judge_a": goal.get("val_judge_a") or "",
            "val_judge_b": goal.get("val_judge_b") or "",
            "val_judge_c": goal.get("val_judge_c") or "",
            "valid": (goal.get("valid") if goal.get("valid") is not None else ""),
            "goal_target_call_count": goal.get("goal_target_call_count"),
            "advprefix_ce_call_count": goal.get("advprefix_ce_call_count"),
            "advprefix_completion_call_count": goal.get(
                "advprefix_completion_call_count"
            ),
            "result_id": goal.get("result_id"),
            "trace_ids": trace_ids,
            "run_duration_s": harvest.get("run_duration_s"),
            "goal_elapsed_s": elapsed,
            "warmup_seconds": _warmup_seconds(harvest),
            "config_hash": plan.get("config_hash"),
        }
        estimated_fields = {
            column: goal.get(column) for column in ESTIMATED_RESULT_TOKEN_COLUMNS
        }
        if (
            token_estimator is not None
            and "goal_token_evidence_status" not in goal
        ):
            estimated_fields = token_estimator.estimate_target_events(
                fallback_prompt=goal.get("attacker_prompt"),
                fallback_response=goal.get("target_response"),
                expected_call_count=goal.get("goal_target_call_count"),
                scope="selected_target_interaction_fallback",
            )
        row.update(estimated_fields)
        rows.append(row)
    return rows


def flatten_records(
    planned_records: str | Path | Iterable[Mapping[str, Any]],
    harvested_records: str | Path | Iterable[Mapping[str, Any]],
    runs_csv_path: str | Path,
    results_csv_path: str | Path,
    *,
    token_estimator: TokenEstimator | None = None,
) -> tuple[int, int]:
    """Flatten append-only records into run-level and goal-level CSVs.

    ``runs.csv`` preserves one row per planned physical attempt.  ``results.csv``
    contains one row per harvested goal.  The two tables intentionally have
    different grains so later analysis can answer both coverage/reliability and
    goal-level outcome questions without reconstructing the JSONL history.

    Target-call counts are taken from the harvested goal records.  Existing
    harvested JSONL can therefore be stale if the authoritative SQLite traces
    were collected before a newer evidence adapter was deployed; callers that
    need backfill should re-harvest from SQLite before flattening.
    """
    plans = _records(planned_records)
    harvests = _records(harvested_records)
    harvest_by_id = {
        str(record.get("experiment_run_id")): record
        for record in harvests
        if record.get("experiment_run_id") is not None
    }

    run_rows: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for plan in plans:
        experiment_run_id = str(plan.get("experiment_run_id"))
        harvest = harvest_by_id.get(experiment_run_id)
        if harvest is not None:
            result_rows.extend(_result_rows(plan, harvest, token_estimator))
        run_row = _run_row(plan, harvest)
        if token_estimator is not None:
            goal_rows = [
                row
                for row in result_rows
                if str(row.get("experiment_run_id")) == experiment_run_id
            ]
            if goal_rows:
                run_row.update(aggregate_goal_estimates(goal_rows))
        run_rows.append(run_row)

    output_run_columns = RUN_COLUMNS if token_estimator is not None else BASE_RUN_COLUMNS
    output_result_columns = (
        RESULT_COLUMNS if token_estimator is not None else BASE_RESULT_COLUMNS
    )
    _write_csv(runs_csv_path, output_run_columns, run_rows)
    _write_csv(results_csv_path, output_result_columns, result_rows)
    return len(run_rows), len(result_rows)


def flatten_jsonl(
    planned_runs_path: str | Path,
    harvested_runs_path: str | Path,
    runs_csv_path: str | Path,
    results_csv_path: str | Path,
    *,
    token_estimator: TokenEstimator | None = None,
) -> tuple[int, int]:
    """Flatten caller-provided JSONL files into caller-provided CSV paths."""
    return flatten_records(
        planned_runs_path,
        harvested_runs_path,
        runs_csv_path,
        results_csv_path,
        token_estimator=token_estimator,
    )


__all__ = [
    "RESULT_COLUMNS",
    "RUN_COLUMNS",
    "append_harvested_run",
    "append_jsonl",
    "append_planned_run",
    "build_error_record",
    "canonical_run_status",
    "extract_hackagent_run",
    "find_correlated_run",
    "flatten_jsonl",
    "flatten_records",
    "harvest_run",
    "open_read_only",
    "read_jsonl",
]
