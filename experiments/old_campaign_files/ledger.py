"""External campaign ledger and read-only HackAgent database harvest."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_LEDGER_PATH = HERE / "campaign.db"
DEFAULT_HACKAGENT_DB = (
    Path(os.environ["HACKAGENT_DB"])
    if os.environ.get("HACKAGENT_DB")
    else Path.home() / ".local/share/hackagent/hackagent.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_runs (
    experiment_run_id TEXT PRIMARY KEY,
    campaign_id       TEXT NOT NULL,
    target_model      TEXT NOT NULL,
    target_revision   TEXT,
    attacker_model    TEXT,
    attacker_revision TEXT,
    judge_model       TEXT NOT NULL,
    judge_revision    TEXT,
    attack            TEXT NOT NULL,
    case_set_id       TEXT NOT NULL,
    case_ids_json     TEXT NOT NULL,
    repetition        INTEGER NOT NULL,
    cell_key          TEXT NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    config_hash       TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'planned',
    slurm_job_id      TEXT,
    node              TEXT,
    output_dir        TEXT NOT NULL,
    error_text        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvested_runs (
    experiment_run_id   TEXT PRIMARY KEY REFERENCES planned_runs(experiment_run_id),
    hackagent_run_id    TEXT,
    hackagent_attack_id TEXT,
    run_status          TEXT,
    run_seconds         REAL,
    result_count        INTEGER,
    trace_count         INTEGER,
    automated_success   INTEGER,
    automated_asr       REAL,
    not_evaluated_count INTEGER,
    judge_parse_failures INTEGER,
    judge_parse_total   INTEGER,
    metrics_json        TEXT NOT NULL DEFAULT '{}',
    harvested_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_results (
    experiment_run_id TEXT NOT NULL REFERENCES planned_runs(experiment_run_id),
    result_id         TEXT NOT NULL,
    goal_index        INTEGER,
    case_id           TEXT,
    goal              TEXT NOT NULL,
    evaluation_status TEXT,
    automated_success INTEGER,
    evaluation_notes  TEXT,
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    trace_ids_json    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (experiment_run_id, result_id)
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    experiment_run_id TEXT NOT NULL REFERENCES planned_runs(experiment_run_id),
    case_id           TEXT NOT NULL,
    validity          TEXT NOT NULL,
    manual_label      TEXT,
    failure_class     TEXT,
    reviewer          TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (experiment_run_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_campaign_target ON planned_runs(target_model);
CREATE INDEX IF NOT EXISTS idx_campaign_attacker ON planned_runs(attacker_model);
CREATE INDEX IF NOT EXISTS idx_campaign_attack ON planned_runs(attack);
CREATE INDEX IF NOT EXISTS idx_goal_case ON goal_results(case_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_cell_attempt
    ON planned_runs(cell_key, attempt);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"campaign-{uuid.uuid4().hex[:12]}"


@contextmanager
def connect(path: Path = DEFAULT_LEDGER_PATH) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path: Path = DEFAULT_LEDGER_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def plan_run(
    *,
    experiment_run_id: Optional[str] = None,
    cell_key: str,
    campaign_id: str,
    target_model: str,
    target_revision: Optional[str],
    attacker_model: Optional[str],
    attacker_revision: Optional[str],
    judge_model: str,
    judge_revision: Optional[str],
    attack: str,
    case_set_id: str,
    case_ids: list[str],
    repetition: int,
    attempt: int = 1,
    config_hash: str,
    config: Dict[str, Any],
    output_dir: str,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> str:
    run_id = experiment_run_id or new_run_id()
    now = utc_now()
    with connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO planned_runs (
                experiment_run_id, campaign_id, target_model, target_revision,
                attacker_model, attacker_revision, judge_model, judge_revision,
                attack, case_set_id, case_ids_json, repetition, config_hash,
                cell_key, attempt, config_json, output_dir, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                campaign_id,
                target_model,
                target_revision,
                attacker_model,
                attacker_revision,
                judge_model,
                judge_revision,
                attack,
                case_set_id,
                json.dumps(case_ids),
                repetition,
                config_hash,
                cell_key,
                attempt,
                json.dumps(config, sort_keys=True, default=str),
                output_dir,
                now,
                now,
            ),
        )
    return run_id


def find_cell(
    cell_key: str, ledger_path: Path = DEFAULT_LEDGER_PATH
) -> Optional[sqlite3.Row]:
    with connect(ledger_path) as conn:
        return conn.execute(
            "SELECT * FROM planned_runs WHERE cell_key = ? ORDER BY attempt DESC LIMIT 1",
            (cell_key,),
        ).fetchone()


def next_attempt(cell_key: str, ledger_path: Path = DEFAULT_LEDGER_PATH) -> int:
    with connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt "
            "FROM planned_runs WHERE cell_key = ?",
            (cell_key,),
        ).fetchone()
    return int(row["attempt"])


def update_status(
    run_id: str,
    status: str,
    *,
    error_text: Optional[str] = None,
    slurm_job_id: Optional[str] = None,
    node: Optional[str] = None,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> None:
    with connect(ledger_path) as conn:
        conn.execute(
            """
            UPDATE planned_runs
               SET status = ?, error_text = COALESCE(?, error_text),
                   slurm_job_id = COALESCE(?, slurm_job_id),
                   node = COALESCE(?, node), updated_at = ?
             WHERE experiment_run_id = ?
            """,
            (status, error_text, slurm_job_id, node, utc_now(), run_id),
        )


def _ha_connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def find_hackagent_run(
    run_id: str, db_path: Path = DEFAULT_HACKAGENT_DB
) -> Optional[sqlite3.Row]:
    if not db_path.exists():
        return None
    conn = _ha_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT r.id AS run_id, r.attack_id, r.status,
                   r.config_json AS run_config, a.config_json AS attack_config,
                   ROUND((julianday(r.updated_at)-julianday(r.created_at))*86400, 3)
                       AS run_seconds
              FROM runs r JOIN attacks a ON r.attack_id = a.id
             WHERE a.config_json LIKE ? OR r.config_json LIKE ?
             ORDER BY r.created_at DESC LIMIT 1
            """,
            (f"%{run_id}%", f"%{run_id}%"),
        ).fetchone()
    finally:
        conn.close()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def collect_results(run_id: str, db_path: Path = DEFAULT_HACKAGENT_DB) -> List[Dict[str, Any]]:
    conn = _ha_connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, goal, goal_index, evaluation_status, evaluation_notes,
                   metrics_json, metadata_json
              FROM results WHERE run_id = ? ORDER BY goal_index
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def collect_trace_ids(result_id: str, db_path: Path = DEFAULT_HACKAGENT_DB) -> list[str]:
    conn = _ha_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM traces WHERE result_id = ? ORDER BY sequence",
            (result_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def harvest_run(
    run_id: str,
    *,
    hackagent_run_id: Optional[str],
    hackagent_attack_id: Optional[str] = None,
    run_status: Optional[str] = None,
    run_seconds: Optional[float] = None,
    result_count: Optional[int] = None,
    trace_count: Optional[int] = None,
    automated_success: Optional[int] = None,
    automated_asr: Optional[float] = None,
    not_evaluated_count: Optional[int] = None,
    judge_parse_failures: Optional[int] = None,
    judge_parse_total: Optional[int] = None,
    metrics: Optional[Dict[str, Any]] = None,
    results: Optional[list[Dict[str, Any]]] = None,
    case_by_goal: Optional[Dict[str, str]] = None,
    db_path: Path = DEFAULT_HACKAGENT_DB,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> None:
    """Upsert derived run and goal rows; HackAgent DB is only opened read-only."""
    now = utc_now()
    with connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO harvested_runs (
                experiment_run_id, hackagent_run_id, hackagent_attack_id,
                run_status, run_seconds, result_count, trace_count,
                automated_success, automated_asr, not_evaluated_count,
                judge_parse_failures, judge_parse_total, metrics_json, harvested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(experiment_run_id) DO UPDATE SET
                hackagent_run_id=excluded.hackagent_run_id,
                hackagent_attack_id=excluded.hackagent_attack_id,
                run_status=excluded.run_status, run_seconds=excluded.run_seconds,
                result_count=excluded.result_count, trace_count=excluded.trace_count,
                automated_success=excluded.automated_success,
                automated_asr=excluded.automated_asr,
                not_evaluated_count=excluded.not_evaluated_count,
                judge_parse_failures=excluded.judge_parse_failures,
                judge_parse_total=excluded.judge_parse_total,
                metrics_json=excluded.metrics_json, harvested_at=excluded.harvested_at
            """,
            (
                run_id,
                hackagent_run_id,
                hackagent_attack_id,
                run_status,
                run_seconds,
                result_count,
                trace_count,
                automated_success,
                automated_asr,
                not_evaluated_count,
                judge_parse_failures,
                judge_parse_total,
                json.dumps(metrics or {}, sort_keys=True, default=str),
                now,
            ),
        )
        conn.execute("DELETE FROM goal_results WHERE experiment_run_id = ?", (run_id,))
        for result in results or []:
            goal = str(result.get("goal") or "")
            case_id = (case_by_goal or {}).get(goal)
            result_id = str(result.get("id") or "")
            status = result.get("evaluation_status")
            conn.execute(
                """
                INSERT INTO goal_results (
                    experiment_run_id, result_id, goal_index, case_id, goal,
                    evaluation_status, automated_success, evaluation_notes,
                    metadata_json, trace_ids_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    result_id,
                    result.get("goal_index"),
                    case_id,
                    goal,
                    status,
                    int(status == "SUCCESSFUL_JAILBREAK") if status else None,
                    result.get("evaluation_notes"),
                    result.get("metadata_json") or "{}",
                    json.dumps(collect_trace_ids(result_id, db_path))
                    if hackagent_run_id
                    else "[]",
                ),
            )


def rows(query: str, params: tuple[Any, ...] = (), ledger_path: Path = DEFAULT_LEDGER_PATH) -> list[sqlite3.Row]:
    with connect(ledger_path) as conn:
        return conn.execute(query, params).fetchall()
