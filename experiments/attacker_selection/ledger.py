"""External experiment ledger for the attacker-selection study.

Design rationale
----------------
HackAgent's SQLite database is treated as *evidence*: append-only, produced by a
third-party library at a pinned commit, and disposable/rebuildable. This ledger is
the *mutable record*: what we planned, whether a run is valid, and what it means.

The two stores are joined by a single correlation id (``experiment_run_id``) and the
flow is strictly one-way:

    plan()      -> writes a planned_runs row BEFORE the run (survives crashes)
    <run>       -> experiment_run_id is threaded into the attack config + output_dir
    harvest()   -> reads HackAgent DB + logs, writes derived metrics here
    review      -> manual labels are written here only, never back into HackAgent

We deliberately do NOT write experiment bookkeeping into ``attacks.config_json``:
that blob is serialised once at run creation, so post-hoc fields (status, timings,
manual labels) would require read-modify-write on a JSON cell, and a crashed run
would lose its record entirely.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# The correlation key injected into the attack config. Kept in one place so the
# Tier 0 probe and the harvester cannot drift apart.
CORRELATION_KEY = "experiment_run_id"

HERE = Path(__file__).resolve().parent
DEFAULT_LEDGER_PATH = HERE / "ledger.db"

# HackAgent's local store. Overridable for testing.
DEFAULT_HACKAGENT_DB = (
    Path(os.environ.get("HACKAGENT_DB", ""))
    if os.environ.get("HACKAGENT_DB")
    else Path.home() / ".local/share/hackagent/hackagent.db"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_runs (
    experiment_run_id TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL,
    tier              TEXT NOT NULL,
    attack            TEXT NOT NULL,
    attacker_model    TEXT NOT NULL,
    attacker_revision TEXT,
    target_model      TEXT NOT NULL,
    judge_model       TEXT NOT NULL,
    case_id           TEXT,
    goal              TEXT,
    repetition        INTEGER NOT NULL DEFAULT 1,
    config_json       TEXT NOT NULL DEFAULT '{}',
    -- status lifecycle: planned -> running -> completed | crashed
    status            TEXT NOT NULL DEFAULT 'planned',
    slurm_job_id      TEXT,
    node              TEXT,
    output_dir        TEXT,
    error_text        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvested_runs (
    experiment_run_id     TEXT PRIMARY KEY REFERENCES planned_runs(experiment_run_id),
    hackagent_run_id      TEXT,
    hackagent_attack_id   TEXT,
    run_status            TEXT,
    run_seconds           REAL,
    result_count          INTEGER,
    trace_count           INTEGER,
    -- automated only. manual labels live in manual_reviews and are NEVER merged in.
    automated_success     INTEGER,
    automated_asr         REAL,
    target_call_count     INTEGER,
    judge_call_count      INTEGER,
    target_latency_sum_s  REAL,
    judge_parse_failures  INTEGER,
    judge_parse_total     INTEGER,
    metrics_json          TEXT NOT NULL DEFAULT '{}',
    harvested_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    id                TEXT PRIMARY KEY,
    experiment_run_id TEXT NOT NULL REFERENCES planned_runs(experiment_run_id),
    -- validity: valid | invalid_tooling | invalid_config | invalid_infra
    validity          TEXT NOT NULL,
    manual_label      TEXT,
    failure_class     TEXT,
    reviewer          TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_planned_attacker ON planned_runs(attacker_model);
CREATE INDEX IF NOT EXISTS idx_planned_status   ON planned_runs(status);
CREATE INDEX IF NOT EXISTS idx_review_run       ON manual_reviews(experiment_run_id);
"""


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
    experiment_id: str,
    tier: str,
    attack: str,
    attacker_model: str,
    target_model: str,
    judge_model: str,
    config: Dict[str, Any],
    attacker_revision: Optional[str] = None,
    case_id: Optional[str] = None,
    goal: Optional[str] = None,
    repetition: int = 1,
    output_dir: Optional[str] = None,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> str:
    """Record a run BEFORE it executes and return its correlation id.

    Writing the row up-front is what makes crashed runs recoverable: a run that
    dies mid-execution still leaves a ``planned``/``running`` row here.
    """
    experiment_run_id = new_run_id(tier)
    now = utc_now()
    with connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO planned_runs (
                experiment_run_id, experiment_id, tier, attack, attacker_model,
                attacker_revision, target_model, judge_model, case_id, goal,
                repetition, config_json, status, output_dir, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'planned',?,?,?)
            """,
            (
                experiment_run_id, experiment_id, tier, attack, attacker_model,
                attacker_revision, target_model, judge_model, case_id, goal,
                repetition, json.dumps(config, sort_keys=True), output_dir, now, now,
            ),
        )
    return experiment_run_id


def update_status(
    experiment_run_id: str,
    status: str,
    *,
    error_text: Optional[str] = None,
    slurm_job_id: Optional[str] = None,
    node: Optional[str] = None,
    output_dir: Optional[str] = None,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> None:
    with connect(ledger_path) as conn:
        conn.execute(
            """
            UPDATE planned_runs
               SET status = ?,
                   error_text = COALESCE(?, error_text),
                   slurm_job_id = COALESCE(?, slurm_job_id),
                   node = COALESCE(?, node),
                   output_dir = COALESCE(?, output_dir),
                   updated_at = ?
             WHERE experiment_run_id = ?
            """,
            (
                status,
                error_text,
                slurm_job_id,
                node,
                output_dir,
                utc_now(),
                experiment_run_id,
            ),
        )


# ---------------------------------------------------------------------------
# Harvest: read-only against HackAgent's database
# ---------------------------------------------------------------------------

def _ha_connect(db_path: Path) -> sqlite3.Connection:
    # file:...?mode=ro guarantees we cannot mutate the evidence store.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def find_hackagent_run(
    experiment_run_id: str, db_path: Path = DEFAULT_HACKAGENT_DB
) -> Optional[sqlite3.Row]:
    """Locate the HackAgent run whose attack config carries our correlation id.

    Returns None if the correlation key did not survive into config_json - which is
    itself a meaningful Tier 0 result, not an error to swallow.
    """
    if not db_path.exists():
        return None
    conn = _ha_connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT r.id AS run_id, r.attack_id, r.status, r.config_json AS run_config,
                   a.config_json AS attack_config,
                   ROUND((julianday(r.updated_at) - julianday(r.created_at)) * 86400, 3)
                       AS run_seconds
              FROM runs r
              JOIN attacks a ON r.attack_id = a.id
             WHERE a.config_json LIKE ? OR r.config_json LIKE ?
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            (f"%{experiment_run_id}%", f"%{experiment_run_id}%"),
        )
        return cur.fetchone()
    finally:
        conn.close()


def latest_hackagent_run(
    db_path: Path = DEFAULT_HACKAGENT_DB,
) -> Optional[sqlite3.Row]:
    """Fallback when the correlation key does not survive into config_json."""
    if not db_path.exists():
        return None
    conn = _ha_connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT r.id AS run_id, r.attack_id, r.status, r.config_json AS run_config,
                   a.config_json AS attack_config,
                   ROUND((julianday(r.updated_at) - julianday(r.created_at)) * 86400, 3)
                       AS run_seconds
              FROM runs r
              JOIN attacks a ON r.attack_id = a.id
             ORDER BY r.created_at DESC
             LIMIT 1
            """
        )
        return cur.fetchone()
    finally:
        conn.close()


def collect_traces(run_id: str, db_path: Path = DEFAULT_HACKAGENT_DB) -> List[Dict[str, Any]]:
    """Return parsed trace payloads for a HackAgent run, in order."""
    conn = _ha_connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT t.id, t.sequence, t.step_type, t.content_json, t.created_at
              FROM traces t
              JOIN results r ON t.result_id = r.id
             WHERE r.run_id = ?
             ORDER BY r.goal_index, t.sequence
            """,
            (run_id,),
        )
        out: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            try:
                content = json.loads(row["content_json"])
            except (json.JSONDecodeError, TypeError):
                content = {}
            out.append(
                {
                    "id": row["id"],
                    "sequence": row["sequence"],
                    "step_type": row["step_type"],
                    "created_at": row["created_at"],
                    "content": content,
                }
            )
        return out
    finally:
        conn.close()


def collect_results(run_id: str, db_path: Path = DEFAULT_HACKAGENT_DB) -> List[Dict[str, Any]]:
    conn = _ha_connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, goal, goal_index, evaluation_status, evaluation_notes,
                   metrics_json, metadata_json
              FROM results
             WHERE run_id = ?
             ORDER BY goal_index
            """,
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def harvest_run(
    experiment_run_id: str,
    *,
    hackagent_run_id: Optional[str],
    hackagent_attack_id: Optional[str] = None,
    run_status: Optional[str] = None,
    run_seconds: Optional[float] = None,
    result_count: Optional[int] = None,
    trace_count: Optional[int] = None,
    automated_success: Optional[int] = None,
    automated_asr: Optional[float] = None,
    judge_parse_failures: Optional[int] = None,
    judge_parse_total: Optional[int] = None,
    metrics: Optional[Dict[str, Any]] = None,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> None:
    """Write automated harvest data without merging human review fields."""
    values = (
        experiment_run_id,
        hackagent_run_id,
        hackagent_attack_id,
        run_status,
        run_seconds,
        result_count,
        trace_count,
        automated_success,
        automated_asr,
        judge_parse_failures,
        judge_parse_total,
        json.dumps(metrics or {}, sort_keys=True),
        utc_now(),
    )
    with connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO harvested_runs (
                experiment_run_id, hackagent_run_id, hackagent_attack_id,
                run_status, run_seconds, result_count, trace_count,
                automated_success, automated_asr,
                target_call_count, judge_call_count, target_latency_sum_s,
                judge_parse_failures, judge_parse_total, metrics_json, harvested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?)
            ON CONFLICT(experiment_run_id) DO UPDATE SET
                hackagent_run_id = excluded.hackagent_run_id,
                hackagent_attack_id = excluded.hackagent_attack_id,
                run_status = excluded.run_status,
                run_seconds = excluded.run_seconds,
                result_count = excluded.result_count,
                trace_count = excluded.trace_count,
                automated_success = excluded.automated_success,
                automated_asr = excluded.automated_asr,
                target_call_count = NULL,
                judge_call_count = NULL,
                target_latency_sum_s = NULL,
                judge_parse_failures = excluded.judge_parse_failures,
                judge_parse_total = excluded.judge_parse_total,
                metrics_json = excluded.metrics_json,
                harvested_at = excluded.harvested_at
            """,
            values,
        )
