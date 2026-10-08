"""Analyze normalized campaign exports without touching campaign evidence.

The analysis owner is a downstream consumer, not part of campaign execution.
It reads explicit ``runs.csv`` and ``results.csv`` paths, validates and
normalizes them, preserves every physical attempt, computes named goal/run
aggregates, and writes disposable reports. Plot generation is owned by the
separate ``plots.py`` module. It never imports HackAgent, queries SQLite, or
rewrites campaign JSONL/CSV inputs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Input schema, dimensions, and output declarations
# ---------------------------------------------------------------------------
RUN_REQUIRED_COLUMNS = {
    "experiment_run_id",
    "campaign_name",
    "target_key",
    "target_model",
    "attacker_key",
    "attacker_model",
    "attacker_applicability",
    "judge_key",
    "judge_model",
    "attack",
    "case_set_id",
    "repetition",
    "status",
    "result_count",
    "trace_count",
    "not_evaluated_count",
    "judge_parse_failures",
    "judge_parse_total",
    "run_duration_s",
    "warmup_seconds",
}

RUN_OPTIONAL_COLUMNS = {
    "target_call_count",
    "target_call_evidence_missing_count",
    "execution_scope_id",
    "cell_id",
    "goal_batch_id",
    "shard_id",
    "shard_manifest_sha256",
    "evidence_db_relpath",
    "category_id",
    "subcategory_id",
    "category_label",
    "subcategory_label",
    "goal_ids",
    "manifest_sha256",
    "hackagent_run_id",
    "hackagent_run_status",
    "expected_goal_count",
    "observed_goal_count",
    "missing_goal_count",
    "goal_evidence_complete",
    "automated_success_count",
    "automated_asr",
    "failure_phase",
    "failure_class",
    "config_hash",
    "failure_type",
    "failure_message",
    "goal_ids",
    "target_prompt_tokens_estimated",
    "target_completion_tokens_estimated",
    "target_total_tokens_estimated",
    "attacker_prompt_tokens_estimated",
    "attacker_completion_tokens_estimated",
    "attacker_total_tokens_estimated",
    "combined_prompt_tokens_estimated",
    "combined_completion_tokens_estimated",
    "combined_total_tokens_estimated",
    "token_estimation_goal_count",
    "token_estimation_missing_count",
    "token_estimation_partial_count",
    "token_evidence_status",
    "token_estimation_scope",
    "token_estimation_role_coverage",
    "token_estimation_quality",
    "token_estimation_version",
    "tokenizer_name",
    "tokenizer_revision",
    "tokenizer_class",
    "tokenizer_backend",
    "tokenization_method",
}

RESULT_REQUIRED_COLUMNS = {
    "experiment_run_id",
    "campaign_name",
    "target_key",
    "target_model",
    "attacker_key",
    "attacker_model",
    "attacker_applicability",
    "judge_key",
    "judge_model",
    "attack",
    "case_set_id",
    "case_id",
    "repetition",
    "run_status",
    "evaluation_status",
    "automated_success",
    "result_id",
}

RESULT_OPTIONAL_COLUMNS = {
    "execution_scope_id",
    "cell_id",
    "goal_batch_id",
    "shard_id",
    "shard_manifest_sha256",
    "evidence_db_relpath",
    "goal_target_call_count",
    "advprefix_ce_call_count",
    "advprefix_completion_call_count",
    "attacker_prompt",
    "target_response",
    "judge_evaluation",
    "judge_reasoning",
    "evidence_trace_ids",
    "evidence_selection",
    "target_call_evidence_status",
    "category_id",
    "subcategory_id",
    "val_harm_a",
    "val_harm_b",
    "val_harm_c",
    "val_var_a",
    "val_var_b",
    "val_var_c",
    "harm_consensus",
    "var_consensus",
    "val_judge_a",
    "val_judge_b",
    "val_judge_c",
    "valid",
    "category_label",
    "subcategory_label",
    "intent_id",
    "goal_id",
    "goal",
    "goal_index",
    "manual_label",
    "validity",
    "failure_class",
    "evaluation_notes",
    "metadata",
    "evaluation_metrics",
    "trace_ids",
    "run_duration_s",
    "goal_elapsed_s",
    "warmup_seconds",
    "config_hash",
    "hackagent_run_id",
    "manifest_sha256",
    "hackagent_run_status",
    "expected_goal_count",
    "observed_goal_count",
    "missing_goal_count",
    "goal_evidence_complete",
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
    "goal_token_evidence_status",
    "token_estimation_scope",
    "token_estimation_role_coverage",
    "token_estimation_quality",
    "token_estimation_version",
    "tokenizer_name",
    "tokenizer_revision",
    "tokenizer_class",
    "tokenizer_backend",
    "tokenization_method",
}

OPTIONAL_COLUMNS = {
    "runs": RUN_OPTIONAL_COLUMNS,
    "results": RESULT_OPTIONAL_COLUMNS,
}

RUN_ID_COLUMN = "experiment_run_id"
RESULT_ID_COLUMN = "result_id"

RUN_NUMERIC_INTEGER_COLUMNS = {
    "repetition",
    "result_count",
    "trace_count",
    "automated_success_count",
    "not_evaluated_count",
    "judge_parse_failures",
    "judge_parse_total",
    "target_call_count",
    "target_call_evidence_missing_count",
    "expected_goal_count",
    "observed_goal_count",
    "missing_goal_count",
    "target_prompt_tokens_estimated",
    "target_completion_tokens_estimated",
    "target_total_tokens_estimated",
    "attacker_prompt_tokens_estimated",
    "attacker_completion_tokens_estimated",
    "attacker_total_tokens_estimated",
    "combined_prompt_tokens_estimated",
    "combined_completion_tokens_estimated",
    "combined_total_tokens_estimated",
    "token_estimation_goal_count",
    "token_estimation_missing_count",
    "token_estimation_partial_count",
}
RUN_NUMERIC_FLOAT_COLUMNS = {
    "automated_asr",
    "run_duration_s",
    "warmup_seconds",
}
RESULT_NUMERIC_INTEGER_COLUMNS = {
    "goal_index",
    "repetition",
    "goal_target_call_count",
    "advprefix_ce_call_count",
    "advprefix_completion_call_count",
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
}
RESULT_NUMERIC_FLOAT_COLUMNS = {"run_duration_s", "goal_elapsed_s", "warmup_seconds"}

RUN_DIMENSION_COLUMNS = (
    "campaign_name",
    "execution_scope_id",
    "cell_id",
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
    "repetition",
)

R4_PROVENANCE_COLUMNS = {
    "execution_scope_id",
    "cell_id",
    "goal_batch_id",
    "shard_id",
    "shard_manifest_sha256",
    "evidence_db_relpath",
}

# Result rows add case_id to the run dimensions.  The constant documents that
# shape for readers; result/run integrity checks intentionally compare only
# RUN_DIMENSION_COLUMNS because case_id exists only at goal grain.
RESULT_DIMENSION_COLUMNS = RUN_DIMENSION_COLUMNS + ("case_id",)

TAXONOMY_COLUMNS = (
    "category_id",
    "subcategory_id",
    "category_label",
    "subcategory_label",
)
TAXONOMY_ID_COLUMNS = ("category_id", "subcategory_id")

FAILURE_METADATA_COLUMNS = (
    "failure_phase",
    "failure_type",
    "failure_message",
    "failure_class",
)

LOGICAL_CELL_COLUMNS = (
    "campaign_name",
    "execution_scope_id",
    "case_set_id",
    "cell_id",
    "target_key",
    "attacker_key",
    "judge_key",
    "attack",
    "goal_batch_id",
    "repetition",
)

GROUPINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("campaign_name", ("campaign_name",)),
    ("target", ("target_key", "target_model")),
    (
        "attacker",
        ("attacker_key", "attacker_model", "attacker_applicability"),
    ),
    ("attacker_applicability", ("attacker_applicability",)),
    ("attack", ("attack",)),
    ("goal_batch", ("goal_batch_id",)),
    ("category", ("category_id",)),
    ("subcategory", ("category_id", "subcategory_id")),
    ("case", ("case_id",)),
    ("repetition", ("repetition",)),
    (
        "target+attack",
        ("target_key", "target_model", "attack"),
    ),
    (
        "attacker+target",
        (
            "attacker_key",
            "attacker_model",
            "attacker_applicability",
            "target_key",
            "target_model",
        ),
    ),
    (
        "attacker+attack",
        ("attacker_key", "attacker_model", "attacker_applicability", "attack"),
    ),
    (
        "attacker+case",
        (
            "attacker_key",
            "attacker_model",
            "attacker_applicability",
            "case_id",
        ),
    ),
    ("target+case", ("target_key", "target_model", "case_id")),
    ("attack+case", ("attack", "case_id")),
    ("category+attacker", ("category_id", "attacker_key", "attacker_model")),
    ("category+attack", ("category_id", "attack")),
    (
        "subcategory+attacker",
        ("category_id", "subcategory_id", "attacker_key", "attacker_model"),
    ),
    ("subcategory+attack", ("category_id", "subcategory_id", "attack")),
)

AGGREGATE_COLUMNS = [
    "aggregation_level",
    "grouping_name",
    "grouping_dimensions",
    *RUN_DIMENSION_COLUMNS,
    "case_id",
    *TAXONOMY_COLUMNS,
    "goal_row_count",
    "evaluated_count",
    "success_count",
    "failure_count",
    "not_evaluated_count",
    "automated_asr",
    "ASR_goal",
    "ASR_target_call",
    "target_call_count",
    "target_call_evidence_missing_count",
    "planned_attempt_count",
    "completed_attempt_count",
    "failed_attempt_count",
    "crashed_attempt_count",
    "blocked_attempt_count",
    "uncorrelated_attempt_count",
    "incomplete_attempt_count",
    "cancelled_attempt_count",
    "goal_incomplete_attempt_count",
    "missing_harvest_count",
    "result_count",
    "trace_count",
    "judge_parse_failures",
    "judge_parse_total",
    "run_duration_mean_s",
    "run_duration_median_s",
    "run_duration_min_s",
    "run_duration_max_s",
    "warmup_seconds_mean_s",
    "warmup_seconds_median_s",
    "warmup_seconds_min_s",
    "warmup_seconds_max_s",
    "source_campaign_names",
    "source_experiment_run_ids",
    "source_result_ids",
]


class AnalysisError(Exception):
    """An input or quality error that makes an analysis unusable."""

    def __init__(self, message: str, findings: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.findings = findings or []


# ---------------------------------------------------------------------------
# Input normalization and quality findings
# ---------------------------------------------------------------------------
def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return bool(pd.isna(value)) if not isinstance(value, (list, dict, tuple)) else False


def _python_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def _json_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _identifier_values(values: Iterable[Any], limit: int = 5) -> list[str]:
    identifiers = {
        str(value) for value in values if not _is_missing(value) and str(value).strip()
    }
    return sorted(identifiers)[:limit]


def _add_finding(
    findings: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    count: int | float,
    identifiers: Iterable[Any] = (),
    source: str = "",
) -> None:
    if count <= 0:
        return
    findings.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            "count": int(count),
            "representative_ids": _json_value(_identifier_values(identifiers)),
            "source": source,
        }
    )


def _empty_to_na(frame: pd.DataFrame, preserve: set[str] | None = None) -> pd.DataFrame:
    preserve = preserve or set()
    result = frame.copy()
    for column in result.columns:
        if column in preserve:
            continue
        result[column] = result[column].map(
            lambda value: (
                pd.NA if isinstance(value, str) and not value.strip() else value
            )
        )
    return result


def _string_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    for column in columns:
        if column not in frame:
            continue
        frame[column] = (
            frame[column]
            .map(lambda value: pd.NA if _is_missing(value) else str(value).strip())
            .astype("string")
        )


def _canonical_run_status(value: Any) -> Any:
    if _is_missing(value):
        return pd.NA
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "complete": "completed",
        "completed": "completed",
        "failure": "failed",
        "error": "failed",
        "running": "incomplete",
        "pending": "incomplete",
        "queued": "incomplete",
        "started": "incomplete",
        "active": "incomplete",
        "in_progress": "incomplete",
        "crashed": "crashed",
        "failed": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "uncorrelated": "uncorrelated",
        "planned": "missing_harvest",
        "incomplete": "incomplete",
        "not_harvested": "missing_harvest",
        "missing_harvest": "missing_harvest",
    }
    return aliases.get(normalized, normalized)


def _canonical_evaluation_status(value: Any) -> Any:
    if _is_missing(value):
        return pd.NA
    normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "SUCCESS": "SUCCESSFUL_JAILBREAK",
        "SUCCESSFUL": "SUCCESSFUL_JAILBREAK",
        "SUCCESSFUL_JAILBREAK": "SUCCESSFUL_JAILBREAK",
        "FAILURE": "FAILED_JAILBREAK",
        "FAILED": "FAILED_JAILBREAK",
        "FAILED_JAILBREAK": "FAILED_JAILBREAK",
        "NOT_EVALUATED": "NOT_EVALUATED",
        "NOTEVALUATED": "NOT_EVALUATED",
    }
    return aliases.get(normalized, normalized)


def _canonical_applicability(value: Any) -> Any:
    if _is_missing(value):
        return pd.NA
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "attacker_driven": "driven",
        "attacker": "driven",
        "applicable": "driven",
        "driven": "driven",
        "not_applicable": "not_applicable",
        "notapplicable": "not_applicable",
    }
    return aliases.get(normalized, normalized)


def _boolean_value(value: Any) -> bool | Any:
    if _is_missing(value):
        return pd.NA
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    normalized = str(value).strip().lower()
    if normalized in {"", "na", "n/a", "none", "null", "nan"}:
        return pd.NA
    if normalized in {"true", "1", "yes", "y", "safe", "success", "successful"}:
        return True
    if normalized in {"false", "0", "no", "n", "unsafe", "failure", "failed"}:
        return False
    return value


def _normalize_boolean(
    series: pd.Series,
    findings: list[dict[str, Any]],
    source: str,
    identifiers: pd.Series | None = None,
) -> pd.Series:
    values = series.map(_boolean_value)
    invalid = values.map(
        lambda value: not _is_missing(value) and not isinstance(value, bool)
    )
    _add_finding(
        findings,
        "error",
        "invalid_boolean",
        "automated_success contains values that are not boolean-like",
        int(invalid.sum()),
        identifiers[invalid] if identifiers is not None else series.index[invalid],
        source,
    )
    return values.mask(invalid, pd.NA).astype("boolean")


def _normalize_numeric(
    frame: pd.DataFrame,
    columns: Iterable[str],
    dtype: str,
    findings: list[dict[str, Any]],
    source: str,
) -> None:
    for column in columns:
        if column not in frame:
            continue
        converted = pd.to_numeric(frame[column], errors="coerce")
        invalid = (
            frame[column].map(lambda value: not _is_missing(value)) & converted.isna()
        )
        non_finite = converted.notna() & ~converted.map(math.isfinite)
        invalid = invalid | non_finite
        if dtype == "Int64":
            non_integral = converted.notna() & (converted % 1 != 0)
            invalid = invalid | non_integral
        _add_finding(
            findings,
            "error",
            "invalid_numeric",
            f"{column} contains non-numeric values",
            int(invalid.sum()),
            frame.loc[invalid, RUN_ID_COLUMN]
            if RUN_ID_COLUMN in frame
            else frame.index[invalid],
            source,
        )
        frame[column] = converted.mask(invalid, pd.NA).astype(dtype)


def _normalize_frame(
    frame: pd.DataFrame,
    table: str,
    source: str,
    findings: list[dict[str, Any]],
) -> pd.DataFrame:
    """Normalize one input table without silently repairing its source file."""
    result = _empty_to_na(frame)
    string_columns = {
        "experiment_run_id",
        "campaign_name",
        "target_key",
        "target_model",
        "attacker_key",
        "attacker_model",
        "judge_key",
        "judge_model",
        "attack",
        "case_set_id",
        "execution_scope_id",
        "cell_id",
        "goal_batch_id",
        "shard_id",
        "shard_manifest_sha256",
        "evidence_db_relpath",
        "category_id",
        "subcategory_id",
        "category_label",
        "subcategory_label",
        "val_harm_a",
        "val_harm_b",
        "val_harm_c",
        "val_var_a",
        "val_var_b",
        "val_var_c",
        "harm_consensus",
        "var_consensus",
        "valid",
        "case_id",
        "intent_id",
        "goal_id",
        "goal",
        "result_id",
        "hackagent_run_id",
        "hackagent_run_status",
        "expected_goal_count",
        "observed_goal_count",
        "missing_goal_count",
        "goal_evidence_complete",
        "config_hash",
        "failure_phase",
        "failure_class",
        "failure_type",
        "failure_message",
        "evaluation_notes",
        "attacker_prompt",
        "target_response",
        "judge_evaluation",
        "evidence_trace_ids",
        "judge_reasoning",
        "evidence_selection",
        "target_call_evidence_status",
        "token_evidence_status",
        "token_estimation_scope",
        "token_estimation_role_coverage",
        "token_estimation_quality",
        "token_estimation_version",
        "tokenizer_name",
        "tokenizer_revision",
        "tokenizer_class",
        "tokenizer_backend",
        "tokenization_method",
        "goal_token_evidence_status",
    }
    _string_columns(result, string_columns)
    for column in (
        "expected_goal_count",
        "observed_goal_count",
        "missing_goal_count",
    ):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce").astype(
                "Int64"
            )
    if "goal_evidence_complete" in result:
        result["goal_evidence_complete"] = _normalize_boolean(
            result["goal_evidence_complete"], findings, source
        )

    if "attacker_applicability" in result:
        result["attacker_applicability"] = (
            result["attacker_applicability"]
            .map(_canonical_applicability)
            .astype("string")
        )

    status_column = "status" if table == "runs" else "run_status"
    if status_column in result:
        result[status_column] = (
            result[status_column].map(_canonical_run_status).astype("string")
        )
    if "evaluation_status" in result:
        result["evaluation_status"] = (
            result["evaluation_status"]
            .map(_canonical_evaluation_status)
            .astype("string")
        )
    if "automated_success" in result:
        result["automated_success"] = _normalize_boolean(
            result["automated_success"],
            findings,
            source,
            result[RUN_ID_COLUMN] if RUN_ID_COLUMN in result else None,
        )

    integer_columns = (
        RUN_NUMERIC_INTEGER_COLUMNS
        if table == "runs"
        else RESULT_NUMERIC_INTEGER_COLUMNS
    )
    float_columns = (
        RUN_NUMERIC_FLOAT_COLUMNS if table == "runs" else RESULT_NUMERIC_FLOAT_COLUMNS
    )
    _normalize_numeric(result, integer_columns, "Int64", findings, source)
    _normalize_numeric(result, float_columns, "Float64", findings, source)

    for column in ("target_call_count", "goal_target_call_count"):
        if column not in result:
            continue
        negative = result[column].notna() & result[column].lt(0)
        _add_finding(
            findings,
            "error",
            "negative_target_call_count",
            f"{column} cannot be negative",
            int(negative.sum()),
            result.loc[negative, RUN_ID_COLUMN]
            if RUN_ID_COLUMN in result
            else result.index[negative],
            source,
        )

    if table == "results" and "evaluation_status" in result:
        expected = (
            result["evaluation_status"]
            .map(
                {
                    "SUCCESSFUL_JAILBREAK": True,
                    "FAILED_JAILBREAK": False,
                    "NOT_EVALUATED": pd.NA,
                }
            )
            .astype("boolean")
        )
        inconsistent = (
            expected.notna()
            & result["automated_success"].notna()
            & (expected != result["automated_success"])
        )
        _add_finding(
            findings,
            "warning",
            "inconsistent_automated_label",
            "automated_success disagrees with the canonical evaluation status",
            int(inconsistent.sum()),
            result.loc[inconsistent, RUN_ID_COLUMN],
            source,
        )
        result.loc[expected.notna(), "automated_success"] = expected[expected.notna()]
        not_evaluated = result["evaluation_status"] == "NOT_EVALUATED"
        explicit_not_evaluated_label = (
            not_evaluated & result["automated_success"].notna()
        )
        _add_finding(
            findings,
            "warning",
            "label_on_not_evaluated",
            "NOT_EVALUATED rows carry an automated_success label; the label is discarded",
            int(explicit_not_evaluated_label.sum()),
            result.loc[explicit_not_evaluated_label, RUN_ID_COLUMN],
            source,
        )
        result.loc[not_evaluated, "automated_success"] = pd.NA

    return result


def _read_csv(path: Path, table: str, findings: list[dict[str, Any]]) -> pd.DataFrame:
    try:
        return pd.read_csv(path, dtype=object, keep_default_na=False)
    except Exception as exc:
        _add_finding(
            findings,
            "error",
            "unreadable_input",
            f"could not read {table} input: {exc}",
            1,
            [path],
            table,
        )
        raise AnalysisError(
            f"could not read {table} input {path}: {exc}", findings
        ) from exc


def _required_columns(
    frame: pd.DataFrame,
    required: set[str],
    path: Path,
    table: str,
    findings: list[dict[str, Any]],
) -> None:
    if len(set(frame.columns)) != len(frame.columns):
        _add_finding(
            findings,
            "error",
            "duplicate_columns",
            f"{table} input has duplicate column names",
            1,
            source=str(path),
        )
    missing = sorted(required - set(frame.columns))
    if missing:
        _add_finding(
            findings,
            "error",
            "missing_required_columns",
            f"{table} input is missing required columns: {', '.join(missing)}",
            len(missing),
            source=str(path),
        )
    if not missing and frame.empty:
        _add_finding(
            findings,
            "warning",
            "empty_input",
            f"{table} input has no rows; coverage is observational",
            1,
            source=str(path),
        )


def _schema_check(
    frames: list[pd.DataFrame],
    paths: list[Path],
    table: str,
    findings: list[dict[str, Any]],
) -> None:
    if not frames:
        return
    expected = set(frames[0].columns)
    optional = OPTIONAL_COLUMNS.get(table.removesuffix(".csv"), set())
    for frame, path in zip(frames[1:], paths[1:]):
        actual = set(frame.columns)
        missing = sorted((expected - actual) - optional)
        extra = sorted((actual - expected) - optional)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing={missing}")
            if extra:
                details.append(f"extra={extra}")
            _add_finding(
                findings,
                "error",
                "incompatible_schema",
                f"{table} schemas differ across campaigns ({'; '.join(details)})",
                1,
                source=str(path),
            )


def _load_inputs(
    campaign_paths: Sequence[str | Path], findings: list[dict[str, Any]]
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    """Read explicit campaign directories, validate schemas, then concatenate.

    Directory names are only input locations.  Analytical campaign dimensions
    must come from CSV columns so cross-campaign analysis cannot accidentally
    infer identity from a path convention.
    """
    if not campaign_paths:
        raise AnalysisError("at least one explicit --campaign path is required")

    roots = [Path(path).expanduser() for path in campaign_paths]
    run_frames: list[pd.DataFrame] = []
    result_frames: list[pd.DataFrame] = []
    input_records: list[dict[str, Any]] = []
    run_paths: list[Path] = []
    result_paths: list[Path] = []

    for root in roots:
        runs_path = root / "runs.csv"
        results_path = root / "results.csv"
        runs = _read_csv(runs_path, "runs.csv", findings)
        results = _read_csv(results_path, "results.csv", findings)
        _required_columns(runs, RUN_REQUIRED_COLUMNS, runs_path, "runs.csv", findings)
        _required_columns(
            results,
            RESULT_REQUIRED_COLUMNS,
            results_path,
            "results.csv",
            findings,
        )
        missing_run_optional = sorted(RUN_OPTIONAL_COLUMNS - set(runs.columns))
        missing_result_optional = sorted(RESULT_OPTIONAL_COLUMNS - set(results.columns))
        if missing_run_optional:
            missing_target_columns = sorted(
                {
                    "target_call_count",
                    "target_call_evidence_missing_count",
                }
                & set(missing_run_optional)
            )
            target_note = (
                "; target-call metrics may be unavailable"
                if missing_target_columns
                else ""
            )
            _add_finding(
                findings,
                "warning",
                "missing_optional_run_columns",
                "runs.csv is missing optional columns: "
                + ", ".join(missing_run_optional)
                + target_note,
                len(missing_run_optional),
                source=str(runs_path),
            )
        if missing_result_optional:
            missing_target_columns = sorted(
                {"goal_target_call_count", "target_call_evidence_status"}
                & set(missing_result_optional)
            )
            target_note = (
                "; target-call metrics may be unavailable"
                if missing_target_columns
                else ""
            )
            _add_finding(
                findings,
                "warning",
                "missing_optional_result_columns",
                "results.csv is missing optional columns: "
                + ", ".join(missing_result_optional)
                + "; selected evidence/validation fields may be unavailable"
                + target_note,
                len(missing_result_optional),
                source=str(results_path),
            )
        for column in OPTIONAL_COLUMNS["runs"]:
            if column not in runs:
                runs[column] = ""
        for column in OPTIONAL_COLUMNS["results"]:
            if column not in results:
                results[column] = ""
        run_frames.append(runs)
        result_frames.append(results)
        run_paths.append(runs_path)
        result_paths.append(results_path)
        input_records.append(
            {
                "campaign_path": str(root),
                "runs_csv": str(runs_path),
                "results_csv": str(results_path),
                "runs_rows": int(len(runs)),
                "results_rows": int(len(results)),
            }
        )

    _schema_check(run_frames, run_paths, "runs.csv", findings)
    _schema_check(result_frames, result_paths, "results.csv", findings)
    if any(finding["severity"] == "error" for finding in findings):
        raise AnalysisError(
            "campaign inputs failed required-column or schema validation", findings
        )

    normalized_runs = [
        _normalize_frame(frame, "runs", str(path), findings)
        for frame, path in zip(run_frames, run_paths)
    ]
    normalized_results = [
        _normalize_frame(frame, "results", str(path), findings)
        for frame, path in zip(result_frames, result_paths)
    ]
    runs = pd.concat(normalized_runs, ignore_index=True)
    results = pd.concat(normalized_results, ignore_index=True)
    return runs, results, input_records


def load_campaign_data(
    campaign_paths: Sequence[str | Path],
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Load, normalize, and validate campaign data for downstream consumers.

    The returned frames are safe for analysis or plotting.  Campaign input
    files are read-only, and validation findings are returned so a downstream
    consumer can report the same warnings as the main analysis pipeline.
    Structural errors raise :class:`AnalysisError` with the findings attached.
    """
    findings: list[dict[str, Any]] = []
    runs, results, input_records = _load_inputs(campaign_paths, findings)
    _validate_values(runs, results, findings)
    if any(finding["severity"] == "error" for finding in findings):
        raise AnalysisError(
            "campaign inputs contain structurally unusable rows", findings
        )
    return runs, results, input_records, findings


def _nonempty(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame[column].notna() & frame[column].astype("string").str.strip().ne("")


def _same_value(left: Any, right: Any) -> bool:
    if _is_missing(left) or _is_missing(right):
        return _is_missing(left) and _is_missing(right)
    return bool(left == right)


def _validate_result_run_dimensions(
    runs: pd.DataFrame, results: pd.DataFrame, findings: list[dict[str, Any]]
) -> None:
    run_by_id = {
        str(row[RUN_ID_COLUMN]): row
        for _, row in runs.iterrows()
        if not _is_missing(row[RUN_ID_COLUMN])
    }
    for _, result in results.iterrows():
        run_id = result[RUN_ID_COLUMN]
        run = run_by_id.get(str(run_id))
        if run is None:
            continue
        for field in RUN_DIMENSION_COLUMNS:
            if field not in result or field not in run:
                continue
            result_value = _python_value(result[field])
            run_value = _python_value(run[field])
            if _same_value(result_value, run_value):
                continue
            result_id = _python_value(result[RESULT_ID_COLUMN])
            findings.append(
                {
                    "severity": "error",
                    "code": "result_run_dimension_mismatch",
                    "message": (
                        "result/run dimension mismatch: "
                        f"result_id={_json_value(result_id)}, "
                        f"experiment_run_id={_json_value(_python_value(run_id))}, "
                        f"field={_json_value(field)}, "
                        f"result_value={_json_value(result_value)}, "
                        f"run_value={_json_value(run_value)}"
                    ),
                    "count": 1,
                    "representative_ids": _json_value(
                        [result_id, _python_value(run_id)]
                    ),
                    "source": "results/runs",
                }
            )
        for field in TAXONOMY_ID_COLUMNS:
            if field not in result or field not in run:
                continue
            result_value = _python_value(result[field])
            run_value = _python_value(run[field])
            # Older pilot exports may omit taxonomy on either grain.
            if (
                result_value is None
                or run_value is None
                or _same_value(result_value, run_value)
            ):
                continue
            result_id = _python_value(result[RESULT_ID_COLUMN])
            findings.append(
                {
                    "severity": "error",
                    "code": "result_run_taxonomy_mismatch",
                    "message": (
                        "result/run taxonomy mismatch: "
                        f"result_id={_json_value(result_id)}, "
                        f"experiment_run_id={_json_value(_python_value(run_id))}, "
                        f"field={_json_value(field)}, "
                        f"result_value={_json_value(result_value)}, "
                        f"run_value={_json_value(run_value)}"
                    ),
                    "count": 1,
                    "representative_ids": _json_value(
                        [result_id, _python_value(run_id)]
                    ),
                    "source": "results/runs",
                }
            )
        result_status = _python_value(result.get("run_status"))
        run_status = _python_value(run.get("status"))
        if result_status is not None and run_status is not None:
            if result_status != run_status:
                findings.append(
                    {
                        "severity": "error",
                        "code": "result_run_status_mismatch",
                        "message": (
                            "result/run status mismatch: "
                            f"result_id={_json_value(_python_value(result[RESULT_ID_COLUMN]))}, "
                            f"experiment_run_id={_json_value(_python_value(run_id))}, "
                            f"result_status={_json_value(result_status)}, "
                            f"run_status={_json_value(run_status)}"
                        ),
                        "count": 1,
                        "representative_ids": _json_value(
                            [
                                _python_value(result[RESULT_ID_COLUMN]),
                                _python_value(run_id),
                            ]
                        ),
                        "source": "results/runs",
                    }
                )
            elif result_status != "completed":
                findings.append(
                    {
                        "severity": "warning",
                        "code": "result_rows_on_noncompleted_run",
                        "message": (
                            "non-completed run has observed result rows; they are "
                            "retained as partial evidence: "
                            f"result_id={_json_value(_python_value(result[RESULT_ID_COLUMN]))}, "
                            f"experiment_run_id={_json_value(_python_value(run_id))}, "
                            f"status={_json_value(result_status)}"
                        ),
                        "count": 1,
                        "representative_ids": _json_value(
                            [
                                _python_value(result[RESULT_ID_COLUMN]),
                                _python_value(run_id),
                            ]
                        ),
                        "source": "results/runs",
                    }
                )


def _validate_failure_coherence(
    runs: pd.DataFrame, findings: list[dict[str, Any]]
) -> None:
    populated = pd.DataFrame(
        {
            field: _nonempty(runs, field) if field in runs else False
            for field in FAILURE_METADATA_COLUMNS
        },
        index=runs.index,
    )
    populated_count = populated.sum(axis=1)
    status = runs["status"]

    completed_with_failure_fields = status.eq("completed") & populated_count.gt(0)
    _add_finding(
        findings,
        "error",
        "completed_with_failure_fields",
        "completed runs must not carry failure metadata",
        int(completed_with_failure_fields.sum()),
        runs.loc[completed_with_failure_fields, RUN_ID_COLUMN],
        "runs",
    )

    failure_statuses = {
        "crashed",
        "failed",
        "blocked",
        "uncorrelated",
        "cancelled",
        "missing_harvest",
    }
    incomplete = status.isin(failure_statuses) & populated_count.between(1, 3)
    _add_finding(
        findings,
        "warning",
        "incomplete_failure_fields",
        "runs with failure-like statuses have partial failure metadata",
        int(incomplete.sum()),
        runs.loc[incomplete, RUN_ID_COLUMN],
        "runs",
    )

    missing = status.isin(failure_statuses) & populated_count.eq(0)
    _add_finding(
        findings,
        "warning",
        "missing_failure_details",
        "runs with failure-like statuses have no failure metadata",
        int(missing.sum()),
        runs.loc[missing, RUN_ID_COLUMN],
        "runs",
    )


def _validate_values(
    runs: pd.DataFrame,
    results: pd.DataFrame,
    findings: list[dict[str, Any]],
) -> None:
    """Check identifiers, status semantics, dimensions, and coverage warnings."""
    for frame, table, required_ids in (
        (runs, "runs", (RUN_ID_COLUMN,)),
        (results, "results", (RUN_ID_COLUMN, RESULT_ID_COLUMN)),
    ):
        for column in required_ids:
            missing = frame[column].isna() | frame[column].astype(
                "string"
            ).str.strip().eq("")
            _add_finding(
                findings,
                "error",
                "missing_identifier",
                f"{table} rows require populated {column}",
                int(missing.sum()),
                frame.loc[missing, column] if column in frame else (),
                table,
            )

    for frame, table in ((runs, "runs"), (results, "results")):
        status_column = "status" if table == "runs" else "run_status"
        missing_status = frame[status_column].isna()
        _add_finding(
            findings,
            "error",
            "missing_status",
            f"{table} rows require a populated {status_column}",
            int(missing_status.sum()),
            frame.loc[missing_status, RUN_ID_COLUMN],
            table,
        )
        # ``execution_scope_id`` is the explicit marker for R4 provenance.
        # Older pilot exports may contain one of the other optional fields
        # without carrying the complete R4 contract.
        r4_rows = (
            _nonempty(frame, "execution_scope_id")
            if "execution_scope_id" in frame
            else pd.Series(False, index=frame.index)
        )
        for column in RUN_DIMENSION_COLUMNS:
            if column not in frame:
                continue
            missing = ~_nonempty(frame, column)
            if column in R4_PROVENANCE_COLUMNS:
                missing = missing & r4_rows
            if column == "attacker_key" or column == "attacker_model":
                continue
            _add_finding(
                findings,
                "error",
                "missing_dimension",
                f"{table} rows require populated {column}",
                int(missing.sum()),
                frame.loc[missing, RUN_ID_COLUMN],
                table,
            )
        for column in R4_PROVENANCE_COLUMNS:
            if column in RUN_DIMENSION_COLUMNS or column not in frame:
                continue
            missing = ~_nonempty(frame, column) & r4_rows
            _add_finding(
                findings,
                "error",
                "missing_r4_provenance",
                f"R4 {table} rows require populated {column}",
                int(missing.sum()),
                frame.loc[missing, RUN_ID_COLUMN],
                table,
            )

        if "attacker_applicability" in frame:
            missing_applicability = frame["attacker_applicability"].isna()
            _add_finding(
                findings,
                "warning",
                "missing_attacker_applicability",
                f"{table} rows have missing attacker_applicability; it is not inferred",
                int(missing_applicability.sum()),
                frame.loc[missing_applicability, RUN_ID_COLUMN],
                table,
            )
            invalid_applicability = frame["attacker_applicability"].notna() & ~frame[
                "attacker_applicability"
            ].isin(["driven", "not_applicable"])
            _add_finding(
                findings,
                "error",
                "invalid_attacker_applicability",
                f"{table} rows have an unknown attacker_applicability value",
                int(invalid_applicability.sum()),
                frame.loc[invalid_applicability, RUN_ID_COLUMN],
                table,
            )

            driven_missing_attacker = frame["attacker_applicability"].eq("driven") & (
                ~_nonempty(frame, "attacker_key") | ~_nonempty(frame, "attacker_model")
            )
            _add_finding(
                findings,
                "error",
                "driven_attacker_missing",
                f"{table} driven rows require attacker key and model",
                int(driven_missing_attacker.sum()),
                frame.loc[driven_missing_attacker, RUN_ID_COLUMN],
                table,
            )
            non_applicable_with_attacker = frame["attacker_applicability"].eq(
                "not_applicable"
            ) & (_nonempty(frame, "attacker_key") | _nonempty(frame, "attacker_model"))
            _add_finding(
                findings,
                "warning",
                "non_applicable_attacker_metadata",
                f"{table} not_applicable rows carry attacker metadata",
                int(non_applicable_with_attacker.sum()),
                frame.loc[non_applicable_with_attacker, RUN_ID_COLUMN],
                table,
            )

    if "case_id" in results:
        missing_case = results["case_id"].isna()
        _add_finding(
            findings,
            "warning",
            "missing_case_id",
            "result rows have no case_id; missing case identity is preserved",
            int(missing_case.sum()),
            results.loc[missing_case, RESULT_ID_COLUMN],
            "results",
        )

    allowed_run_statuses = {
        "completed",
        "crashed",
        "blocked",
        "uncorrelated",
        "missing_harvest",
        "failed",
        "incomplete",
        "cancelled",
    }
    invalid_run_status = runs["status"].notna() & ~runs["status"].isin(
        allowed_run_statuses
    )
    _add_finding(
        findings,
        "error",
        "invalid_run_status",
        "runs.csv contains an unknown canonical status",
        int(invalid_run_status.sum()),
        runs.loc[invalid_run_status, RUN_ID_COLUMN],
        "runs",
    )

    allowed_result_statuses = allowed_run_statuses | {"not_harvested"}
    invalid_result_run_status = results["run_status"].notna() & ~results[
        "run_status"
    ].isin(allowed_result_statuses)
    _add_finding(
        findings,
        "error",
        "invalid_result_run_status",
        "results.csv contains an unknown canonical run status",
        int(invalid_result_run_status.sum()),
        results.loc[invalid_result_run_status, RUN_ID_COLUMN],
        "results",
    )

    allowed_evaluation_statuses = {
        "SUCCESSFUL_JAILBREAK",
        "FAILED_JAILBREAK",
        "NOT_EVALUATED",
    }
    invalid_evaluation = results["evaluation_status"].isna() | ~results[
        "evaluation_status"
    ].isin(allowed_evaluation_statuses)
    _add_finding(
        findings,
        "error",
        "invalid_evaluation_status",
        "results.csv contains a missing or unknown evaluation_status",
        int(invalid_evaluation.sum()),
        results.loc[invalid_evaluation, RESULT_ID_COLUMN],
        "results",
    )

    duplicate_runs = runs[RUN_ID_COLUMN].duplicated(keep=False)
    _add_finding(
        findings,
        "error",
        "duplicate_experiment_run_id",
        "experiment_run_id must identify one physical planned attempt",
        int(duplicate_runs.sum()),
        runs.loc[duplicate_runs, RUN_ID_COLUMN],
        "runs",
    )
    duplicate_results = results[RESULT_ID_COLUMN].duplicated(keep=False)
    _add_finding(
        findings,
        "error",
        "duplicate_result_id",
        "result_id must be unique across normalized result rows",
        int(duplicate_results.sum()),
        results.loc[duplicate_results, RESULT_ID_COLUMN],
        "results",
    )

    known_runs = set(runs[RUN_ID_COLUMN].dropna().astype(str))
    unknown_result_runs = ~results[RUN_ID_COLUMN].astype(str).isin(known_runs)
    _add_finding(
        findings,
        "error",
        "unknown_experiment_run_id",
        "result rows refer to experiment_run_id values absent from runs.csv",
        int(unknown_result_runs.sum()),
        results.loc[unknown_result_runs, RUN_ID_COLUMN],
        "results",
    )

    logical_counts = runs.groupby(
        list(LOGICAL_CELL_COLUMNS), dropna=False, sort=False, observed=False
    ).size()
    duplicate_cells = logical_counts[logical_counts > 1]
    if len(duplicate_cells):
        duplicate_ids = runs.loc[
            runs.set_index(list(LOGICAL_CELL_COLUMNS)).index.isin(
                duplicate_cells.index
            ),
            RUN_ID_COLUMN,
        ]
    else:
        duplicate_ids = []
    _add_finding(
        findings,
        "warning",
        "duplicate_logical_cell",
        "multiple physical attempts share one logical cell; all attempts are retained",
        int(len(duplicate_cells)),
        duplicate_ids,
        "runs",
    )

    result_counts = results.groupby(RUN_ID_COLUMN, dropna=False, sort=False).size()
    comparable_ids = runs.index[runs["result_count"].notna()]
    count_mismatch_ids: list[Any] = []
    for index in comparable_ids:
        run_id = runs.loc[index, RUN_ID_COLUMN]
        declared = runs.loc[index, "result_count"]
        observed = result_counts.get(run_id, 0)
        if int(declared) != int(observed):
            count_mismatch_ids.append(run_id)
    _add_finding(
        findings,
        "warning",
        "result_count_mismatch",
        "run-level result_count differs from observed goal rows where comparable",
        len(count_mismatch_ids),
        count_mismatch_ids,
        "runs/results",
    )

    _validate_result_run_dimensions(runs, results, findings)
    _validate_failure_coherence(runs, findings)

    for status, code, message in (
        (
            "crashed",
            "crashed_attempts",
            "crashed attempts are covered without fabricated goal failures",
        ),
        (
            "blocked",
            "blocked_attempts",
            "blocked attempts are covered without fabricated goal failures",
        ),
        (
            "uncorrelated",
            "uncorrelated_attempts",
            "uncorrelated attempts are covered without fabricated goal failures",
        ),
        (
            "incomplete",
            "incomplete_attempts",
            "nonterminal attempts are retained as partial evidence and excluded from canonical ASR",
        ),
        (
            "cancelled",
            "cancelled_attempts",
            "cancelled attempts are covered without fabricated goal failures",
        ),
        (
            "missing_harvest",
            "missing_harvest_attempts",
            "planned attempts without a harvest are covered without fabricated goals",
        ),
    ):
        selected = runs["status"].eq(status)
        _add_finding(
            findings,
            "warning",
            code,
            message,
            int(selected.sum()),
            runs.loc[selected, RUN_ID_COLUMN],
            "runs",
        )

    not_evaluated = results["evaluation_status"].eq("NOT_EVALUATED")
    _add_finding(
        findings,
        "warning",
        "not_evaluated_goals",
        "NOT_EVALUATED goals are excluded from automated ASR denominators",
        int(not_evaluated.sum()),
        results.loc[not_evaluated, RESULT_ID_COLUMN],
        "results",
    )

    parse_failures = pd.to_numeric(
        runs["judge_parse_failures"], errors="coerce"
    ).fillna(0)
    _add_finding(
        findings,
        "warning",
        "judge_parse_failures",
        "judge parse failures are present in run-level evidence",
        int(parse_failures.gt(0).sum()),
        runs.loc[parse_failures.gt(0), RUN_ID_COLUMN],
        "runs",
    )
    parse_incoherent = (
        runs["judge_parse_failures"].notna()
        & runs["judge_parse_total"].notna()
        & (runs["judge_parse_failures"] > runs["judge_parse_total"])
    )
    _add_finding(
        findings,
        "warning",
        "judge_parse_count_incoherent",
        "judge_parse_failures exceeds judge_parse_total",
        int(parse_incoherent.sum()),
        runs.loc[parse_incoherent, RUN_ID_COLUMN],
        "runs",
    )

    if "target_call_count" in runs and "goal_target_call_count" in results:
        completed_with_results = runs["status"].eq("completed") & runs[
            "result_count"
        ].gt(0)
        missing_run_calls = completed_with_results & runs["target_call_count"].isna()
        _add_finding(
            findings,
            "warning",
            "missing_target_call_evidence",
            "completed runs with goal rows have no recoverable target-call count",
            int(missing_run_calls.sum()),
            runs.loc[missing_run_calls, RUN_ID_COLUMN],
            "runs",
        )

        completed_goal_rows = results["run_status"].eq("completed")
        missing_goal_calls = (
            results["goal_target_call_count"].isna() & completed_goal_rows
        )
        _add_finding(
            findings,
            "warning",
            "missing_goal_target_call_evidence",
            "completed goal rows have no recoverable target-call count",
            int(missing_goal_calls.sum()),
            results.loc[missing_goal_calls, RESULT_ID_COLUMN],
            "results",
        )

        successful_missing_selection = results["evaluation_status"].eq(
            "SUCCESSFUL_JAILBREAK"
        ) & results["target_call_evidence_status"].isin(
            ["missing_evidence", "observed_selection_missing"]
        )
        _add_finding(
            findings,
            "warning",
            "missing_successful_goal_evidence",
            "successful goals are missing a linked target prompt/response/judge selection",
            int(successful_missing_selection.sum()),
            results.loc[successful_missing_selection, RESULT_ID_COLUMN],
            "results",
        )

        mismatch_ids: list[Any] = []
        missing_count_mismatch_ids: list[Any] = []
        for _, run in runs.iterrows():
            run_id = run[RUN_ID_COLUMN]
            subset = results[results[RUN_ID_COLUMN].astype("string").eq(str(run_id))]
            if subset.empty:
                continue
            calls = pd.to_numeric(subset["goal_target_call_count"], errors="coerce")
            declared = _python_value(run["target_call_count"])
            if declared is not None and calls.notna().all():
                if int(declared) != int(calls.sum()):
                    mismatch_ids.append(run_id)
            declared_missing = _python_value(run["target_call_evidence_missing_count"])
            if declared_missing is not None:
                expected_missing = int(calls.isna().sum())
                if int(declared_missing) != expected_missing:
                    missing_count_mismatch_ids.append(run_id)
        _add_finding(
            findings,
            "error",
            "target_call_count_mismatch",
            "run target_call_count differs from the sum of goal_target_call_count",
            len(mismatch_ids),
            mismatch_ids,
            "runs/results",
        )
        _add_finding(
            findings,
            "error",
            "target_call_missing_count_mismatch",
            "run target-call missing count differs from goal-level missing evidence",
            len(missing_count_mismatch_ids),
            missing_count_mismatch_ids,
            "runs/results",
        )


# ---------------------------------------------------------------------------
# Metric construction and provenance-preserving aggregation
# ---------------------------------------------------------------------------
def _asr_counts(frame: pd.DataFrame) -> dict[str, Any]:
    """Count goal outcomes using the campaign's nullable ASR denominator.

    Only successful and failed jailbreak labels are evaluated goals.  Explicit
    NOT_EVALUATED rows are reported separately and excluded from the
    denominator; with no evaluated goals ASR is None rather than 0.0.
    """
    status = frame["evaluation_status"]
    success = int(status.eq("SUCCESSFUL_JAILBREAK").sum())
    failure = int(status.eq("FAILED_JAILBREAK").sum())
    not_evaluated = int(status.eq("NOT_EVALUATED").sum())
    evaluated = success + failure
    target_call_count = None
    target_call_missing_count = None
    if "goal_target_call_count" in frame:
        calls = pd.to_numeric(frame["goal_target_call_count"], errors="coerce")
        target_call_missing_count = int(calls.isna().sum())
        if target_call_missing_count == 0:
            target_call_count = int(calls.sum())
    return {
        "goal_row_count": int(len(frame)),
        "evaluated_count": evaluated,
        "success_count": success,
        "failure_count": failure,
        "not_evaluated_count": not_evaluated,
        "automated_asr": success / evaluated if evaluated else None,
        "ASR_goal": success / evaluated if evaluated else None,
        "ASR_target_call": (
            success / target_call_count
            if target_call_count is not None and target_call_count > 0
            else None
        ),
        "target_call_count": target_call_count,
        "target_call_evidence_missing_count": target_call_missing_count,
    }


def _group_iterator(
    frame: pd.DataFrame, dimensions: Sequence[str]
) -> Iterable[tuple[tuple[Any, ...], pd.DataFrame]]:
    """Yield stable pandas groups while preserving missing dimension values."""
    if frame.empty:
        return []
    grouped = frame.groupby(list(dimensions), dropna=False, sort=False, observed=False)
    return (
        ((key,) if len(dimensions) == 1 else tuple(key), group)
        for key, group in grouped
    )


def _dimension_dict(group: pd.DataFrame, dimensions: Sequence[str]) -> dict[str, Any]:
    return {
        dimension: _python_value(group.iloc[0][dimension]) for dimension in dimensions
    }


def _canonical_dimensions(
    group: pd.DataFrame, dimensions: Sequence[str]
) -> dict[str, Any]:
    values = {
        column: None
        for column in (*RUN_DIMENSION_COLUMNS, "case_id", *TAXONOMY_COLUMNS)
    }
    values.update(_dimension_dict(group, dimensions))
    for column in TAXONOMY_COLUMNS:
        if column not in group:
            continue
        group_values = [_python_value(value) for value in group[column].tolist()]
        if group_values and all(
            _same_value(group_values[0], value) for value in group_values
        ):
            values[column] = group_values[0]
    return values


def _source_lists(
    runs: pd.DataFrame,
    results: pd.DataFrame,
    run_ids: Iterable[Any],
    selected_result_ids: Iterable[Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return provenance IDs for an aggregate's contributing source rows."""
    ids = {str(value) for value in run_ids if not _is_missing(value)}
    run_subset = runs[runs[RUN_ID_COLUMN].astype(str).isin(ids)]
    result_subset = results[results[RUN_ID_COLUMN].astype(str).isin(ids)]
    if selected_result_ids is not None:
        selected_results = {
            str(value) for value in selected_result_ids if not _is_missing(value)
        }
        result_subset = result_subset[
            result_subset[RESULT_ID_COLUMN].astype(str).isin(selected_results)
        ]
    campaigns = sorted(
        {
            str(value)
            for value in run_subset["campaign_name"].tolist()
            if not _is_missing(value)
        }
        | {
            str(value)
            for value in result_subset["campaign_name"].tolist()
            if not _is_missing(value)
        }
    )
    run_ids_sorted = sorted(
        {
            str(value)
            for value in run_subset[RUN_ID_COLUMN].tolist()
            if not _is_missing(value)
        }
    )
    result_ids_sorted = sorted(
        {
            str(value)
            for value in result_subset[RESULT_ID_COLUMN].tolist()
            if not _is_missing(value)
        }
    )
    return campaigns, run_ids_sorted, result_ids_sorted


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _run_subset_metrics(
    group: pd.DataFrame, result_subset: pd.DataFrame
) -> dict[str, Any]:
    statuses = group["status"]
    completed_group = group[statuses.eq("completed")]
    if (
        "goal_evidence_complete" in completed_group
        and completed_group["goal_evidence_complete"].notna().any()
    ):
        completed_group = completed_group[
            completed_group["goal_evidence_complete"].isna()
            | completed_group["goal_evidence_complete"].eq(True)
        ]
    missing_harvest = statuses.isin(["missing_harvest"])

    def total(column: str) -> int | None:
        if column not in group or group[column].notna().sum() == 0:
            return None
        return int(group[column].sum())

    def summaries(column: str, name: str | None = None) -> dict[str, float | None]:
        output_name = name or column
        source_group = completed_group if column in {"run_duration_s", "warmup_seconds"} else group
        if column not in source_group:
            values = pd.Series(dtype="float64")
        else:
            values = pd.to_numeric(source_group[column], errors="coerce").dropna()
        if values.empty:
            return {
                f"{output_name}_mean_s": None,
                f"{output_name}_median_s": None,
                f"{output_name}_min_s": None,
                f"{output_name}_max_s": None,
            }
        return {
            f"{output_name}_mean_s": round(float(values.mean()), 3),
            f"{output_name}_median_s": round(float(values.median()), 3),
            f"{output_name}_min_s": round(float(values.min()), 3),
            f"{output_name}_max_s": round(float(values.max()), 3),
        }

    metrics: dict[str, Any] = {
        "planned_attempt_count": int(len(group)),
        "completed_attempt_count": int(statuses.eq("completed").sum()),
        "failed_attempt_count": int(statuses.eq("failed").sum()),
        "crashed_attempt_count": int(statuses.eq("crashed").sum()),
        "blocked_attempt_count": int(statuses.eq("blocked").sum()),
        "uncorrelated_attempt_count": int(statuses.eq("uncorrelated").sum()),
        "incomplete_attempt_count": int(statuses.eq("incomplete").sum()),
        "cancelled_attempt_count": int(statuses.eq("cancelled").sum()),
        "goal_incomplete_attempt_count": int(
            group["goal_evidence_complete"].eq(False).sum()
            if "goal_evidence_complete" in group
            else 0
        ),
        "missing_harvest_count": int(missing_harvest.sum()),
        "result_count": total("result_count"),
        "trace_count": total("trace_count"),
        "target_call_count": None,
        "target_call_evidence_missing_count": None,
        "not_evaluated_count": total("not_evaluated_count"),
        "judge_parse_failures": total("judge_parse_failures"),
        "judge_parse_total": total("judge_parse_total"),
    }
    # SQLite can expose useful result rows before a run reaches a terminal
    # state.  Preserve those rows in the input/quality layer, but keep them out
    # of canonical scientific outcome metrics until the physical run is done.
    completed_ids = set(completed_group["experiment_run_id"].astype("string"))
    completed_results = result_subset[
        result_subset["run_status"].eq("completed")
        & result_subset["experiment_run_id"].astype("string").isin(completed_ids)
    ]
    goal_counts = _asr_counts(completed_results) if not completed_results.empty else None
    successful_goals = goal_counts["success_count"] if goal_counts is not None else None
    if goal_counts is not None:
        metrics["target_call_count"] = goal_counts["target_call_count"]
        metrics["target_call_evidence_missing_count"] = goal_counts[
            "target_call_evidence_missing_count"
        ]
    elif (
        "target_call_count" in completed_group
        and not completed_group.empty
        and completed_group["target_call_count"].notna().all()
    ):
        metrics["target_call_count"] = int(completed_group["target_call_count"].sum())
        metrics["target_call_evidence_missing_count"] = 0
    target_calls = metrics["target_call_count"]
    missing_target_calls = metrics["target_call_evidence_missing_count"] or 0
    metrics["ASR_target_call"] = (
        successful_goals / target_calls
        if successful_goals is not None
        and target_calls is not None
        and target_calls > 0
        and missing_target_calls == 0
        else None
    )
    metrics["ASR_goal"] = (
        goal_counts["automated_asr"] if goal_counts is not None else None
    )
    metrics.update(summaries("run_duration_s", "run_duration"))
    metrics.update(summaries("warmup_seconds"))
    return metrics


def _base_aggregate_row(
    level: str,
    grouping_name: str,
    group: pd.DataFrame,
    dimensions: Sequence[str],
) -> dict[str, Any]:
    row = {column: None for column in AGGREGATE_COLUMNS}
    row["aggregation_level"] = level
    row["grouping_name"] = grouping_name
    values = _canonical_dimensions(group, dimensions)
    row.update(values)
    row["grouping_dimensions"] = _json_value(
        {dimension: _python_value(group.iloc[0][dimension]) for dimension in dimensions}
    )
    return row


def _goal_aggregates(runs: pd.DataFrame, results: pd.DataFrame) -> list[dict[str, Any]]:
    # Partial rows from live/cancelled physical attempts remain available to
    # diagnostics, but canonical outcome aggregates are terminal-run only.
    eligible_runs = runs[runs["status"].eq("completed")]
    if (
        "goal_evidence_complete" in eligible_runs
        and eligible_runs["goal_evidence_complete"].notna().any()
    ):
        eligible_runs = eligible_runs[
            eligible_runs["goal_evidence_complete"].isna()
            | eligible_runs["goal_evidence_complete"].eq(True)
        ]
    eligible_run_ids = set(eligible_runs[RUN_ID_COLUMN].astype("string"))
    completed_results = results[
        results["run_status"].eq("completed")
        & results[RUN_ID_COLUMN].astype("string").isin(eligible_run_ids)
    ]
    rows: list[dict[str, Any]] = []
    for grouping_name, dimensions in GROUPINGS:
        if not set(dimensions).issubset(completed_results.columns):
            continue
        if any(
            dimension in TAXONOMY_ID_COLUMNS
            and not _nonempty(completed_results, dimension).any()
            for dimension in dimensions
        ):
            continue
        for _, group in _group_iterator(completed_results, dimensions):
            row = _base_aggregate_row("goal", grouping_name, group, dimensions)
            row.update(_asr_counts(group))
            run_ids = group[RUN_ID_COLUMN].dropna().astype(str).unique()
            campaigns, source_runs, source_results = _source_lists(
                runs,
                results,
                run_ids,
                group[RESULT_ID_COLUMN].dropna().astype(str).unique(),
            )
            row["source_campaign_names"] = _json_value(campaigns)
            row["source_experiment_run_ids"] = _json_value(source_runs)
            row["source_result_ids"] = _json_value(source_results)
            rows.append(row)
    return rows


def _run_aggregates(runs: pd.DataFrame, results: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for grouping_name, dimensions in GROUPINGS:
        if not set(dimensions).issubset(runs.columns):
            continue
        if any(
            dimension in TAXONOMY_ID_COLUMNS and not _nonempty(runs, dimension).any()
            for dimension in dimensions
        ):
            continue
        for _, group in _group_iterator(runs, dimensions):
            row = _base_aggregate_row("run", grouping_name, group, dimensions)
            run_ids = group[RUN_ID_COLUMN].dropna().astype(str)
            result_subset = results[results[RUN_ID_COLUMN].astype(str).isin(run_ids)]
            row.update(_run_subset_metrics(group, result_subset))
            run_ids = group[RUN_ID_COLUMN].dropna().astype(str).unique()
            campaigns, source_runs, source_results = _source_lists(
                runs, results, run_ids
            )
            row["source_campaign_names"] = _json_value(campaigns)
            row["source_experiment_run_ids"] = _json_value(source_runs)
            row["source_result_ids"] = _json_value(source_results)
            rows.append(row)
    return rows


def build_aggregates(runs: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Build one long-form table while retaining every physical attempt.

    Goal-level and run-level rows share one output table, distinguished by
    aggregation_level and grouping_name.  Metrics that belong to the other
    grain remain empty rather than being fabricated or mixed.
    """
    rows = _goal_aggregates(runs, results) + _run_aggregates(runs, results)
    aggregate = pd.DataFrame(rows, columns=AGGREGATE_COLUMNS)
    if not aggregate.empty:
        aggregate = aggregate.sort_values(
            ["aggregation_level", "grouping_name", "grouping_dimensions"],
            kind="stable",
        ).reset_index(drop=True)
    return aggregate


def _write_quality_outputs(
    output_dir: Path,
    findings: list[dict[str, Any]],
    runs: pd.DataFrame | None,
    results: pd.DataFrame | None,
) -> tuple[Path, Path]:
    quality_csv = output_dir / "data_quality.csv"
    quality_md = output_dir / "data_quality_report.md"
    columns = [
        "severity",
        "code",
        "message",
        "count",
        "representative_ids",
        "source",
    ]
    quality_frame = pd.DataFrame(findings, columns=columns)
    quality_frame.to_csv(quality_csv, index=False)
    errors = sum(finding["severity"] == "error" for finding in findings)
    warnings = sum(finding["severity"] == "warning" for finding in findings)
    lines = [
        "# Campaign Analysis Data Quality",
        "",
        f"Hard errors: {errors}",
        f"Warnings: {warnings}",
        "",
        "The analysis preserves all physical attempts and does not ingest manual review.",
        "Missing attacker metadata is not inferred as not applicable.",
        "",
    ]
    if runs is not None and results is not None:
        lines.extend(
            [
                f"Input run rows: {len(runs)}",
                f"Input result rows: {len(results)}",
                "",
            ]
        )
    if findings:
        lines.extend(
            [
                "| Severity | Code | Count | Message | Representative IDs | Source |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for finding in findings:
            lines.append(
                "| {severity} | {code} | {count} | {message} | {ids} | {source} |".format(
                    severity=finding["severity"],
                    code=finding["code"],
                    count=finding["count"],
                    message=finding["message"].replace("|", "\\|"),
                    ids=finding["representative_ids"].replace("|", "\\|"),
                    source=finding["source"].replace("|", "\\|"),
                )
            )
    else:
        lines.append("No quality findings were recorded.")
    quality_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return quality_csv, quality_md


def _manifest(
    output_dir: Path,
    input_records: list[dict[str, Any]],
    runs: pd.DataFrame | None,
    results: pd.DataFrame | None,
    findings: list[dict[str, Any]],
    aggregate_path: Path,
    quality_csv: Path,
    quality_md: Path,
    status: str,
    attack_filter: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "analysis_version": "R3",
        "status": status,
        "analysis_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attack_filter": sorted({str(a) for a in (attack_filter or []) if str(a).strip()}) or None,
        "input_paths": input_records,
        "source_row_counts": {
            "runs": int(len(runs)) if runs is not None else None,
            "results": int(len(results)) if results is not None else None,
        },
        "groupings": [
            {"name": name, "dimensions": list(dimensions)}
            for name, dimensions in GROUPINGS
        ],
        "taxonomy": {
            "columns": list(TAXONOMY_COLUMNS),
            "optional_for_legacy_inputs": True,
        },
        "metric_policy": {
            "automated_metric": "ASR_goal and ASR_target_call",
            "ASR_goal": "successful_goal_count / evaluated_goal_count",
            "ASR_target_call": (
                "successful_goal_count / total_target_call_count; AdvPrefix "
                "includes cross-entropy probes and completion requests"
            ),
            "asr_formula": "ASR_goal uses success_count / (success_count + failure_count)",
            "not_evaluated": "excluded from ASR denominator",
            "target_call_numerator": "one successful goal per successful result",
            "target_call_denominator": (
                "classified target-model calls through terminal outcome; AdvPrefix "
                "includes cross-entropy probes and completion requests"
            ),
            "target_call_missing_evidence": "flagged; not treated as zero",
            "no_evaluated_goals": None,
            "runtime": "summarized from run-level rows; never summed over goal rows",
            "warmup": "diagnostic only; never included in ASR",
            "nonterminal_runs": (
                "observed result rows are retained for diagnostics, but incomplete "
                "and cancelled runs are excluded from canonical outcome aggregates"
            ),
            "manual_review": "out_of_scope",
        },
        "attempt_policy": "all physical attempts preserved; no first/latest/successful selection",
        "coverage_policy": "observed attempts only; no assumed 98-cell denominator",
        "quality": {
            "hard_error_count": sum(
                finding["severity"] == "error" for finding in findings
            ),
            "warning_count": sum(
                finding["severity"] == "warning" for finding in findings
            ),
        },
        "output_paths": {
            "output_dir": str(output_dir),
            "aggregates_csv": str(aggregate_path),
            "data_quality_csv": str(quality_csv),
            "data_quality_report": str(quality_md),
            "analysis_manifest": str(output_dir / "analysis_manifest.json"),
        },
    }


def _write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    path = output_dir / "analysis_manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Public analysis pipeline and CLI
# ---------------------------------------------------------------------------
def analyze_campaigns(
    campaign_paths: Sequence[str | Path],
    output_dir: str | Path,
    attacks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load explicit campaign CSVs and write disposable W11 analysis outputs.

    The pipeline is: load/schema-check -> normalize -> quality-check -> write
    aggregates -> write manifest. Plot generation is handled independently by
    ``plots.py``. Structural errors still produce a
    failed quality report and manifest before the exception is returned to the
    CLI; campaign inputs remain untouched throughout.
    """
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    findings: list[dict[str, Any]] = []
    input_records: list[dict[str, Any]] = []
    runs: pd.DataFrame | None = None
    results: pd.DataFrame | None = None
    aggregate_path = destination / "aggregates.csv"
    quality_csv = destination / "data_quality.csv"
    quality_md = destination / "data_quality_report.md"
    try:
        runs, results, input_records = _load_inputs(campaign_paths, findings)
        attack_filter = [str(a) for a in (attacks or []) if str(a).strip()]
        if attack_filter and runs is not None and results is not None:
            if "attack" in runs.columns:
                runs = runs[runs["attack"].astype(str).isin(attack_filter)].copy()
            if "attack" in results.columns:
                results = results[results["attack"].astype(str).isin(attack_filter)].copy()
            # Recorded in the manifest via attack_filter; intentionally not a
            # finding (findings require quality-report fields this filter lacks).
        _validate_values(runs, results, findings)
        _write_quality_outputs(destination, findings, runs, results)
        if any(finding["severity"] == "error" for finding in findings):
            raise AnalysisError(
                "campaign inputs contain structurally unusable rows", findings
            )

        aggregate = build_aggregates(runs, results)
        aggregate.to_csv(aggregate_path, index=False)
        quality_csv, quality_md = _write_quality_outputs(
            destination, findings, runs, results
        )
        status = "complete_with_warnings" if findings else "complete"
        manifest = _manifest(
            destination,
            input_records,
            runs,
            results,
            findings,
            aggregate_path,
            quality_csv,
            quality_md,
            status,
            attack_filter,
        )
        _write_manifest(destination, manifest)
        return {
            "status": status,
            "runs_rows": len(runs),
            "results_rows": len(results),
            "aggregate_rows": len(aggregate),
            "findings": findings,
            "manifest": manifest,
        }
    except AnalysisError as exc:
        if exc.findings:
            findings = exc.findings
        _write_quality_outputs(destination, findings, runs, results)
        manifest = _manifest(
            destination,
            input_records,
            runs,
            results,
            findings,
            aggregate_path,
            quality_csv,
            quality_md,
            "failed",
        )
        _write_manifest(destination, manifest)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze normalized campaign runs.csv and results.csv exports."
    )
    parser.add_argument(
        "--campaign",
        action="append",
        required=True,
        metavar="PATH",
        help="explicit campaign directory; repeat for cross-campaign analysis",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="directory for disposable aggregates, reports, and manifest",
    )
    parser.add_argument(
        "--attack",
        action="append",
        default=[],
        metavar="ATTACK",
        help="restrict analysis to this attack (e.g. tap); repeat for several; "
        "default is all attacks; recorded in the manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyze_campaigns(args.campaign, args.output, attacks=args.attack)
    except AnalysisError as exc:
        print(f"analysis failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"analysis status={report['status']} runs={report['runs_rows']} "
        f"results={report['results_rows']} aggregates={report['aggregate_rows']} "
        f"findings={len(report['findings'])} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
