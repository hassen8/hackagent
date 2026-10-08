"""Read-only Slurm and shard-progress checkups for a campaign.

The checkup layer combines scheduler state with the immutable shard manifest and
the shard-local CSV/JSONL checkpoints.  It never opens SQLite, contacts Ollama,
changes campaign records, extends jobs, or submits jobs.  Its purpose is to
make an operator snapshot actionable: show progress, estimate cutoff risk, and
identify shards that should be considered for a time-limit extension or later
resume.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_ARRAY_JOB_ID = "36924"
DEFAULT_TIME_LIMIT_HOURS = 15.0
DEFAULT_CAMPAIGN_ROOT = Path(
    "logs/campaigns/omnisafebench_af340_attackers_vs_gemma3_4b"
)
CHECKUP_SCHEMA_VERSION = 1

_TIME_EXTENSION_BEGIN = "<!-- BEGIN OPERATOR TIME EXTENSION COMMANDS -->"
_TIME_EXTENSION_END = "<!-- END OPERATOR TIME EXTENSION COMMANDS -->"
_COMPLETION_BEGIN = "<!-- BEGIN OPERATOR CAMPAIGN COMPLETION STEPS -->"
_COMPLETION_END = "<!-- END OPERATOR CAMPAIGN COMPLETION STEPS -->"

_MISSING_SCHEDULER_VALUES = {"", "unknown", "n/a", "na", "none", "-"}
_DURATION_RE = re.compile(r"^(?:(?P<days>\d+)-)?(?P<clock>\d+(?::\d{1,2}){1,2})$")


def parse_slurm_duration(value: Any) -> float | None:
    """Parse Slurm's ``[days-]hours:minutes:seconds`` duration format."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _MISSING_SCHEDULER_VALUES or text.lower() == "unlimited":
        return None
    match = _DURATION_RE.match(text)
    if match is None:
        return None
    clock = [int(part) for part in match.group("clock").split(":")]
    if len(clock) == 2:
        hours, minutes = clock
        seconds = 0
    else:
        hours, minutes, seconds = clock
    days = int(match.group("days") or 0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normal_status(value: Any) -> str:
    return str(value or "missing").strip().lower().replace(" ", "_")


def _command_result(
    command: Sequence[str], runner: Callable[[Sequence[str]], Any] | None = None
) -> dict[str, Any]:
    """Run a read-only scheduler command and retain failures as report data."""
    if runner is not None:
        try:
            result = runner(command)
            if isinstance(result, Mapping):
                return {
                    "command": list(command),
                    "returncode": int(result.get("returncode", 0)),
                    "stdout": str(result.get("stdout", "")),
                    "stderr": str(result.get("stderr", "")),
                }
            return {
                "command": list(command),
                "returncode": int(getattr(result, "returncode", 0)),
                "stdout": str(getattr(result, "stdout", "") or ""),
                "stderr": str(getattr(result, "stderr", "") or ""),
            }
        except Exception as exc:  # pragma: no cover - defensive test seam
            return {
                "command": list(command),
                "returncode": 127,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
            }

    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return {
            "command": list(command),
            "returncode": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _array_task_index(job_id: Any, array_job_id: str) -> int | None:
    text = str(job_id or "").strip()
    prefix = f"{array_job_id}_"
    if not text.startswith(prefix):
        return None
    return _as_int(text[len(prefix) :].split(".", 1)[0])


def _parse_squeue(stdout: str, array_job_id: str) -> dict[int, dict[str, Any]]:
    tasks: dict[int, dict[str, Any]] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("|", 4)
        if len(fields) != 5:
            continue
        job_id, state, elapsed, limit, node = (field.strip() for field in fields)
        index = _array_task_index(job_id, array_job_id)
        if index is None:
            continue
        tasks[index] = {
            "job_id": job_id,
            "state": state or "UNKNOWN",
            "elapsed": elapsed,
            "elapsed_s": parse_slurm_duration(elapsed),
            "time_limit": limit,
            "time_limit_s": parse_slurm_duration(limit),
            "node": node or None,
            "source": "squeue",
        }
    return tasks


def _parse_sacct(stdout: str, array_job_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.rstrip("|").split("|")
        if len(fields) < 8:
            continue
        job_id, state, exit_code, elapsed, limit, start, end, node = (
            field.strip() for field in fields[:8]
        )
        index = _array_task_index(job_id, array_job_id)
        records.append(
            {
                "job_id": job_id,
                "array_task_index": index,
                "state": state or "UNKNOWN",
                "exit_code": exit_code or None,
                "elapsed": elapsed,
                "elapsed_s": parse_slurm_duration(elapsed),
                "time_limit": limit,
                "time_limit_s": parse_slurm_duration(limit),
                "start": start or None,
                "end": end or None,
                "node": node or None,
                "source": "sacct",
            }
        )
    return records


def get_slurm_snapshot(
    array_job_id: str = DEFAULT_ARRAY_JOB_ID,
    *,
    command_runner: Callable[[Sequence[str]], Any] | None = None,
) -> dict[str, Any]:
    """Return read-only ``squeue``/``sacct`` state for an array job."""
    squeue = _command_result(
        [
            "squeue",
            "-h",
            "-j",
            array_job_id,
            "-o",
            "%i|%T|%M|%l|%N",
        ],
        command_runner,
    )
    sacct = _command_result(
        [
            "sacct",
            "-n",
            "-P",
            "-X",
            "-j",
            array_job_id,
            "--format=JobIDRaw,State,ExitCode,Elapsed,Timelimit,Start,End,NodeList",
        ],
        command_runner,
    )
    return {
        "array_job_id": array_job_id,
        "squeue": {
            "available": squeue["returncode"] == 0,
            "returncode": squeue["returncode"],
            "stderr": squeue["stderr"].strip(),
            "tasks": _parse_squeue(squeue["stdout"], array_job_id),
        },
        "sacct": {
            "available": sacct["returncode"] == 0,
            "returncode": sacct["returncode"],
            "stderr": sacct["stderr"].strip(),
            "records": _parse_sacct(sacct["stdout"], array_job_id),
        },
    }


def _read_csv(path: Path, errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError) as exc:
        errors.append(
            {
                "path": str(path),
                "kind": "csv_read_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return []


def _read_jsonl(
    path: Path, errors: list[dict[str, Any]]
) -> tuple[int, Counter[str], int]:
    if not path.exists():
        return 0, Counter(), 0
    count = 0
    statuses: Counter[str] = Counter()
    malformed = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    malformed += 1
                    if malformed <= 5:
                        errors.append(
                            {
                                "path": str(path),
                                "kind": "jsonl_parse_error",
                                "line": line_number,
                                "message": str(exc),
                            }
                        )
                    continue
                if not isinstance(value, Mapping):
                    malformed += 1
                    continue
                count += 1
                statuses[_normal_status(value.get("status"))] += 1
    except (OSError, UnicodeError) as exc:
        errors.append(
            {
                "path": str(path),
                "kind": "jsonl_read_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    return count, statuses, malformed


def _artifact_status(paths: Mapping[str, Any], root: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name, relative in paths.items():
        if not isinstance(relative, str):
            continue
        path = (root / relative).resolve()
        artifacts[name] = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.is_file() else None,
        }
    return artifacts


def _duration_samples(run_rows: Iterable[Mapping[str, Any]]) -> list[float]:
    return [
        duration
        for row in run_rows
        if _normal_status(row.get("status")) == "completed"
        and (duration := _as_float(row.get("run_duration_s"))) is not None
    ]


def _risk_level(
    remaining_cells: int,
    remaining_time_s: float | None,
    projected_mean_s: float | None,
    projected_median_s: float | None,
) -> str:
    if remaining_cells <= 0:
        return "complete"
    if remaining_time_s is None or projected_median_s is None:
        return "unknown"
    if projected_median_s > remaining_time_s:
        return "critical"
    if projected_mean_s is not None and projected_mean_s > remaining_time_s:
        return "high"
    if projected_median_s > remaining_time_s * 0.8:
        return "watch"
    return "low"


def _recommended_action(
    risk: str, slurm_state: str, remaining_cells: int
) -> str:
    if remaining_cells <= 0:
        return "no action; shard coverage is complete"
    running = slurm_state.upper() in {"RUNNING", "CONFIGURING", "COMPLETING"}
    if risk in {"critical", "high"} and running:
        return "consider extending before the absolute cutoff; otherwise resume unfinished cells"
    if risk == "watch" and running:
        return "recheck soon and prepare an extension decision"
    if not running:
        return "resume unfinished cells with a new Slurm job after preserving artifacts"
    if risk == "unknown":
        return "continue and collect another completed-cell timing sample"
    return "continue current worker and recheck before cutoff"


def _shard_checkup(
    manifest: Mapping[str, Any],
    shard: Mapping[str, Any],
    campaign_root: Path,
    slurm_task: Mapping[str, Any] | None,
    now: datetime,
    default_time_limit_s: float,
) -> dict[str, Any]:
    shard_id = str(shard.get("shard_id"))
    paths = shard.get("paths")
    if not isinstance(paths, Mapping):
        paths = {}
    errors: list[dict[str, Any]] = []
    run_path = campaign_root / str(paths.get("runs_csv", ""))
    results_path = campaign_root / str(paths.get("results_csv", ""))
    planned_path = campaign_root / str(paths.get("planned_runs", ""))
    harvested_path = campaign_root / str(paths.get("harvested_runs", ""))
    for label, path in (
        ("runs_csv", run_path),
        ("results_csv", results_path),
        ("planned_runs", planned_path),
        ("harvested_runs", harvested_path),
    ):
        if not path.exists():
            errors.append(
                {
                    "path": str(path),
                    "kind": "missing_checkpoint",
                    "message": f"shard checkpoint {label} does not exist",
                }
            )
    run_rows = _read_csv(run_path, errors)
    result_rows = _read_csv(results_path, errors)
    planned_count, planned_statuses, planned_malformed = _read_jsonl(
        planned_path, errors
    )
    harvested_count, harvested_statuses, harvested_malformed = _read_jsonl(
        harvested_path, errors
    )

    expected_cells = len(shard.get("cell_ids") or [])
    cell_ids = {
        str(cell_id) for cell_id in (shard.get("cell_ids") or [])
    }
    cells_by_id = {
        str(cell.get("cell_id")): cell
        for cell in (manifest.get("cells") or [])
        if isinstance(cell, Mapping)
    }
    expected_goals = sum(
        len(cells_by_id.get(cell_id, {}).get("goal_ids") or [])
        for cell_id in cell_ids
    )
    status_counts: Counter[str] = Counter(
        _normal_status(row.get("status")) for row in run_rows
    )
    evaluation_counts: Counter[str] = Counter(
        str(row.get("evaluation_status") or "missing").strip().upper()
        for row in result_rows
    )
    completed_cell_ids = {
        str(row.get("cell_id"))
        for row in run_rows
        if _normal_status(row.get("status")) == "completed"
        and row.get("cell_id")
    }
    completed_cell_ids &= cell_ids
    completed_cells = len(completed_cell_ids)
    remaining_cells = max(0, expected_cells - completed_cells)
    durations = _duration_samples(run_rows)
    mean_duration_s = mean(durations) if durations else None
    median_duration_s = median(durations) if durations else None
    projected_mean_s = (
        remaining_cells * mean_duration_s if mean_duration_s is not None else None
    )
    projected_median_s = (
        remaining_cells * median_duration_s
        if median_duration_s is not None
        else None
    )

    slurm = dict(slurm_task or {})
    slurm_state = str(slurm.get("state") or "UNKNOWN")
    elapsed_s = slurm.get("elapsed_s")
    time_limit_s = slurm.get("time_limit_s") or default_time_limit_s
    remaining_time_s = (
        max(0.0, time_limit_s - elapsed_s)
        if elapsed_s is not None and time_limit_s is not None
        else None
    )
    risk = _risk_level(
        remaining_cells,
        remaining_time_s,
        projected_mean_s,
        projected_median_s,
    )
    last_activity: datetime | None = None
    for path in (run_path, results_path, planned_path, harvested_path):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if last_activity is None or modified > last_activity:
            last_activity = modified

    return {
        "shard_id": shard_id,
        "array_task_id": (
            f"{manifest.get('array_job_id', DEFAULT_ARRAY_JOB_ID)}_{task_index}"
            if (task_index := _as_int(shard_id.rsplit("-", 1)[-1])) is not None
            else None
        ),
        "attacker_key": shard.get("attacker_key"),
        "expected_cells": expected_cells,
        "expected_goal_evaluations": expected_goals,
        "planned_attempts": len(run_rows),
        "planned_jsonl_records": planned_count,
        "harvested_attempts": harvested_count,
        "completed_attempts": status_counts["completed"],
        "completed_cells": completed_cells,
        "remaining_cells": remaining_cells,
        "result_rows": len(result_rows),
        "automated_successes": evaluation_counts["SUCCESSFUL_JAILBREAK"],
        "automated_failures": evaluation_counts["FAILED_JAILBREAK"],
        "not_evaluated": evaluation_counts["NOT_EVALUATED"],
        "run_statuses": dict(status_counts),
        "planned_statuses": dict(planned_statuses),
        "harvested_statuses": dict(harvested_statuses),
        "duration_samples": len(durations),
        "mean_completed_cell_minutes": (
            mean_duration_s / 60 if mean_duration_s is not None else None
        ),
        "median_completed_cell_minutes": (
            median_duration_s / 60 if median_duration_s is not None else None
        ),
        "projected_remaining_mean_minutes": (
            projected_mean_s / 60 if projected_mean_s is not None else None
        ),
        "projected_remaining_median_minutes": (
            projected_median_s / 60 if projected_median_s is not None else None
        ),
        "slurm": {
            "job_id": slurm.get("job_id"),
            "state": slurm_state,
            "elapsed": slurm.get("elapsed"),
            "elapsed_s": elapsed_s,
            "time_limit": slurm.get("time_limit"),
            "time_limit_s": time_limit_s,
            "remaining_time_s": remaining_time_s,
            "node": slurm.get("node"),
            "source": slurm.get("source"),
        },
        "risk": risk,
        "recommended_action": _recommended_action(
            risk, slurm_state, remaining_cells
        ),
        "last_activity": last_activity.isoformat(timespec="seconds")
        if last_activity is not None
        else None,
        "malformed_jsonl_records": planned_malformed + harvested_malformed,
        "errors": errors,
        "artifacts": _artifact_status(paths, campaign_root),
    }


def check_campaign(
    campaign_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    *,
    manifest_path: str | Path | None = None,
    array_job_id: str = DEFAULT_ARRAY_JOB_ID,
    time_limit_hours: float = DEFAULT_TIME_LIMIT_HOURS,
    include_slurm: bool = True,
    now: datetime | None = None,
    command_runner: Callable[[Sequence[str]], Any] | None = None,
) -> dict[str, Any]:
    """Build one read-only campaign checkup report.

    ``command_runner`` is a test seam for ``squeue``/``sacct``.  Production
    callers should leave it unset.  The function returns data and performs no
    report-file write; use :func:`write_checkup_markdown` or the CLI when a
    durable snapshot is desired.
    """
    root = Path(campaign_root).expanduser().resolve()
    manifest_source = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else root / "shard-manifest.json"
    )
    timestamp = now or datetime.now(timezone.utc)
    errors: list[dict[str, Any]] = []
    try:
        manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": CHECKUP_SCHEMA_VERSION,
            "snapshot_time": timestamp.isoformat(timespec="seconds"),
            "campaign_root": str(root),
            "manifest_path": str(manifest_source),
            "array_job_id": array_job_id,
            "status": "failed",
            "errors": [
                {
                    "kind": "manifest_read_error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
            "shards": [],
        }
    if not isinstance(manifest, Mapping):
        return {
            "schema_version": CHECKUP_SCHEMA_VERSION,
            "snapshot_time": timestamp.isoformat(timespec="seconds"),
            "campaign_root": str(root),
            "manifest_path": str(manifest_source),
            "array_job_id": array_job_id,
            "status": "failed",
            "errors": [{"kind": "invalid_manifest", "message": "manifest is not an object"}],
            "shards": [],
        }

    default_time_limit_s = float(time_limit_hours) * 3600
    slurm = (
        get_slurm_snapshot(array_job_id, command_runner=command_runner)
        if include_slurm
        else {
            "array_job_id": array_job_id,
            "squeue": {"available": False, "tasks": {}},
            "sacct": {"available": False, "records": []},
        }
    )
    shards: list[dict[str, Any]] = []
    for index, shard in enumerate(manifest.get("shards") or []):
        if not isinstance(shard, Mapping):
            errors.append({"kind": "invalid_shard", "index": index})
            continue
        scheduler_task = slurm.get("squeue", {}).get("tasks", {}).get(index)
        if scheduler_task is None:
            scheduler_task = next(
                (
                    record
                    for record in slurm.get("sacct", {}).get("records", [])
                    if record.get("array_task_index") == index
                ),
                None,
            )
        shards.append(
            _shard_checkup(
                manifest,
                shard,
                root,
                scheduler_task,
                timestamp,
                default_time_limit_s,
            )
        )

    expected_cells = sum(shard["expected_cells"] for shard in shards)
    expected_goals = sum(shard["expected_goal_evaluations"] for shard in shards)
    completed_cells = sum(shard["completed_cells"] for shard in shards)
    remaining_cells = sum(shard["remaining_cells"] for shard in shards)
    result_rows = sum(shard["result_rows"] for shard in shards)
    successes = sum(shard["automated_successes"] for shard in shards)
    failures = sum(shard["automated_failures"] for shard in shards)
    not_evaluated = sum(shard["not_evaluated"] for shard in shards)
    run_statuses: Counter[str] = Counter()
    for shard in shards:
        run_statuses.update(shard["run_statuses"])
    active_tasks = sum(
        shard["slurm"]["state"].upper() == "RUNNING" for shard in shards
    )
    risky = [
        shard
        for shard in shards
        if shard["risk"] in {"critical", "high", "watch"}
    ]
    risky.sort(
        key=lambda shard: (
            {"critical": 0, "high": 1, "watch": 2}.get(shard["risk"], 3),
            -float(shard.get("projected_remaining_mean_minutes") or 0),
        )
    )
    report_status = "complete" if remaining_cells == 0 else "in_progress"
    if errors or any(shard["errors"] for shard in shards):
        report_status = "in_progress_with_errors" if remaining_cells else "complete_with_errors"
    return {
        "schema_version": CHECKUP_SCHEMA_VERSION,
        "snapshot_time": timestamp.isoformat(timespec="seconds"),
        "campaign_root": str(root),
        "manifest_path": str(manifest_source),
        "campaign_name": manifest.get("campaign_name"),
        "execution_scope_id": manifest.get("execution_scope_id"),
        "array_job_id": array_job_id,
        "configured_time_limit_hours": time_limit_hours,
        "status": report_status,
        "manifest": {
            "shard_count": manifest.get("shard_count"),
            "expected_cell_count": manifest.get("expected_cell_count"),
            "expected_goal_evaluation_count": manifest.get(
                "expected_goal_evaluation_count"
            ),
            "shard_manifest_sha256": manifest.get("shard_manifest_sha256"),
        },
        "slurm": {
            "array_job_id": array_job_id,
            "active_task_count": active_tasks,
            "squeue_available": slurm.get("squeue", {}).get("available", False),
            "sacct_available": slurm.get("sacct", {}).get("available", False),
            "squeue_error": slurm.get("squeue", {}).get("stderr", ""),
            "sacct_error": slurm.get("sacct", {}).get("stderr", ""),
            "accounting_records": len(slurm.get("sacct", {}).get("records", [])),
        },
        "progress": {
            "expected_cells": expected_cells,
            "completed_cells": completed_cells,
            "remaining_cells": remaining_cells,
            "cell_completion_fraction": (
                completed_cells / expected_cells if expected_cells else None
            ),
            "expected_goal_evaluations": expected_goals,
            "goal_result_rows": result_rows,
            "automated_successes": successes,
            "automated_failures": failures,
            "not_evaluated": not_evaluated,
            "run_statuses": dict(run_statuses),
        },
        "extension_candidates": [
            {
                "array_task": f"{array_job_id}_{int(shard['shard_id'].split('-')[-1])}",
                "shard_id": shard["shard_id"],
                "risk": shard["risk"],
                "remaining_cells": shard["remaining_cells"],
                "projected_remaining_mean_minutes": shard[
                    "projected_remaining_mean_minutes"
                ],
                "projected_remaining_median_minutes": shard[
                    "projected_remaining_median_minutes"
                ],
                "remaining_time_minutes": (
                    shard["slurm"]["remaining_time_s"] / 60
                    if shard["slurm"]["remaining_time_s"] is not None
                    else None
                ),
            }
            for shard in risky
        ],
        "errors": errors,
        "shards": shards,
    }


def _minutes(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}"


def _percent(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def format_checkup_markdown(report: Mapping[str, Any]) -> str:
    """Render a checkup report, including explicit future-operation placeholders."""
    progress = report.get("progress") or {}
    slurm = report.get("slurm") or {}
    shards = list(report.get("shards") or [])
    candidates = list(report.get("extension_candidates") or [])
    lines = [
        "# Campaign Checkups",
        "",
        "This file is an operator-facing snapshot generated by the read-only",
        "`experiments/campaign/checkup.py` checker. It does not submit jobs,",
        "extend time limits, open SQLite, contact Ollama, or modify campaign data.",
        "",
        f"- Snapshot: `{report.get('snapshot_time', '-')}`",
        f"- Campaign: `{report.get('campaign_name', '-')}`",
        f"- Array: `{report.get('array_job_id', '-')}`",
        f"- Status: **{report.get('status', 'unknown')}**",
        f"- Manifest: `{report.get('manifest_path', '-')}`",
        "",
        "## Executive Summary",
        "",
        f"- Completed cells: **{progress.get('completed_cells', 0)}/{progress.get('expected_cells', 0)}** ({_percent(progress.get('cell_completion_fraction'))})",
        f"- Remaining cells: **{progress.get('remaining_cells', 0)}**",
        f"- Goal result rows: **{progress.get('goal_result_rows', 0)}**",
        f"- Automated successes/failures: **{progress.get('automated_successes', 0)}/{progress.get('automated_failures', 0)}**",
        f"- Active Slurm tasks observed: **{slurm.get('active_task_count', 0)}**",
        f"- Run statuses: `{json.dumps(progress.get('run_statuses', {}), sort_keys=True)}`",
        "",
        "## Slurm State",
        "",
        f"- `squeue` available: `{slurm.get('squeue_available', False)}`",
        f"- `sacct` available: `{slurm.get('sacct_available', False)}`",
        f"- Accounting records observed: `{slurm.get('accounting_records', 0)}`",
        "",
        "## Problematic Shards",
        "",
    ]
    if candidates:
        lines.extend(
            [
                "These shards are ordered by projected cutoff risk. The commands to",
                "extend their time limits will be appended after the operator confirms",
                "the permitted Slurm/QOS limit.",
                "",
                "| Priority | Array task | Shard | Risk | Remaining cells | Projected mean min | Projected median min | Time left min |",
                "|---|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for candidate in candidates:
            lines.append(
                "| {risk} | `{array_task}` | `{shard_id}` | {risk} | {remaining_cells} | {mean} | {median} | {left} |".format(
                    risk=candidate.get("risk", "unknown").upper(),
                    array_task=candidate.get("array_task", "-"),
                    shard_id=candidate.get("shard_id", "-"),
                    remaining_cells=candidate.get("remaining_cells", 0),
                    mean=_minutes(candidate.get("projected_remaining_mean_minutes")),
                    median=_minutes(candidate.get("projected_remaining_median_minutes")),
                    left=_minutes(candidate.get("remaining_time_minutes")),
                )
            )
    else:
        lines.append("No high-risk or watch shards were identified in this snapshot.")
    lines.extend(
        [
            "",
            "## All Shards",
            "",
            "| Shard | Attacker | Slurm state | Completed | Remaining | Results | Success | Failure | Mean min | Median min | Action |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for shard in shards:
        lines.append(
            "| `{shard_id}` | `{attacker}` | {state} | {completed}/{expected} | {remaining} | {results} | {success} | {failure} | {mean} | {median} | {action} |".format(
                shard_id=shard.get("shard_id", "-"),
                attacker=shard.get("attacker_key", "-"),
                state=shard.get("slurm", {}).get("state", "UNKNOWN"),
                completed=shard.get("completed_cells", 0),
                expected=shard.get("expected_cells", 0),
                remaining=shard.get("remaining_cells", 0),
                results=shard.get("result_rows", 0),
                success=shard.get("automated_successes", 0),
                failure=shard.get("automated_failures", 0),
                mean=_minutes(shard.get("mean_completed_cell_minutes")),
                median=_minutes(shard.get("median_completed_cell_minutes")),
                action=shard.get("recommended_action", "-"),
            )
        )
    lines.extend(
        [
            "",
        "## Time Extension Commands",
        "",
        _TIME_EXTENSION_BEGIN,
        "**Pending operator action.** Commands will be appended here only after",
        "checking the permitted partition/QOS wall-time limit and deciding which",
        "running array elements to extend. This checker does not execute `scontrol`.",
        _TIME_EXTENSION_END,
        "",
        "## Campaign Completion Steps",
        "",
        _COMPLETION_BEGIN,
        "**Pending operator action.** The completion procedure will be appended",
        "after the final extension/resume decision. It will cover waiting for or",
        "stopping workers, strict merge, validation, and the later analysis/plots",
        "workpackage. Do not merge while campaign workers are active.",
        _COMPLETION_END,
            "",
            "## Checkup Interpretation",
            "",
            "- `completed_cells` counts logical cells with at least one completed run; `planned_attempts` and `completed_attempts` retain physical-attempt information.",
            "- Runtime projections multiply remaining logical cells by observed completed-cell mean/median duration; they are estimates, not guarantees.",
            "- `critical` means the observed median projection exceeds the scheduler time remaining; `high` means the mean projection exceeds it; `watch` means the median uses more than 80% of remaining time.",
            "- Missing or malformed checkpoint data is reported as an error and never fabricated as a completed or failed goal.",
            "- A shard that reaches its cutoff must later be resumed from its durable shard-local state; its existing artifacts must not be deleted or overwritten.",
            "",
        ]
    )
    if report.get("errors") or any(shard.get("errors") for shard in shards):
        lines.extend(["## Checkup Errors", ""])
        for error in report.get("errors", []):
            lines.append(f"- `{json.dumps(error, sort_keys=True)}`")
        for shard in shards:
            for error in shard.get("errors", []):
                lines.append(
                    f"- `{shard.get('shard_id')}`: `{json.dumps(error, sort_keys=True)}`"
                )
        lines.append("")
    return "\n".join(lines)


def write_checkup_markdown(report: Mapping[str, Any], path: str | Path) -> None:
    """Atomically write one rendered checkup snapshot."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = format_checkup_markdown(report)
    if destination.exists():
        try:
            previous = destination.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            previous = ""
        for begin, end in (
            (_TIME_EXTENSION_BEGIN, _TIME_EXTENSION_END),
            (_COMPLETION_BEGIN, _COMPLETION_END),
        ):
            start = previous.find(begin)
            finish = previous.find(end, start + len(begin)) if start >= 0 else -1
            if start < 0 or finish < 0:
                continue
            preserved = previous[start : finish + len(end)]
            rendered_start = rendered.find(begin)
            rendered_finish = rendered.find(end, rendered_start + len(begin))
            if rendered_start < 0 or rendered_finish < 0:
                continue
            rendered = (
                rendered[:rendered_start]
                + preserved
                + rendered[rendered_finish + len(end) :]
            )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only campaign checkup")
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN_ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--array-job-id", default=DEFAULT_ARRAY_JOB_ID)
    parser.add_argument("--time-limit-hours", type=float, default=DEFAULT_TIME_LIMIT_HOURS)
    parser.add_argument("--no-slurm", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = check_campaign(
        args.campaign_root,
        manifest_path=args.manifest,
        array_job_id=args.array_job_id,
        time_limit_hours=args.time_limit_hours,
        include_slurm=not args.no_slurm,
    )
    if args.output is not None:
        if args.as_json:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
        else:
            write_checkup_markdown(report, args.output)
    else:
        print(
            json.dumps(report, indent=2, sort_keys=True, default=str)
            if args.as_json
            else format_checkup_markdown(report)
        )
    return 0 if report.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CHECKUP_SCHEMA_VERSION",
    "check_campaign",
    "format_checkup_markdown",
    "get_slurm_snapshot",
    "parse_slurm_duration",
    "write_checkup_markdown",
]
