"""Tier 0: harness validation for the attacker-selection study.

This is NOT an experiment. Nothing produced here may be reported as a model result.
Its only purpose is to answer four infrastructure questions before we spend GPU hours
on the real Tier 1 screening matrix:

  P1  Does an injected correlation id survive into ``attacks.config_json``?
      -> decides how the external ledger joins to HackAgent's database.
  P2  Does the judge emit parseable labels, or prose?
      -> prose inflates judge_parse_failure_rate and contaminates ASR comparisons.
  P3  Is end-to-end run duration recoverable from DB timestamps?
      -> this is the single timing metric we keep: what an end user actually waits.
  P4  Does a crashed run still leave a correlatable record?
      -> the assignment requires preserving failures, not just successes.

Dropped by decision: per-role call counts and attacker-only latency. Both required
log forensics or heuristics over ``traces.step_type`` (uniformly 'OTHER'), and neither
is a ranking criterion. End-to-end duration is what an end user experiences.

Cold-start is handled by a warmup generation per attacker BEFORE the timed run, so a
~20GB model load is not charged to the measurement. ``warmup_seconds`` is recorded as
a diagnostic only, never as a ranking metric.

Run via:  bash experiments/attacker_selection/submit.sh tier0_smoke
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cases as case_set  # noqa: E402
import ledger  # noqa: E402
from ledger import CORRELATION_KEY  # noqa: E402

from hackagent import HackAgent  # noqa: E402

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent

ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11435")
TARGET_MODEL = "gemma3:4b"
JUDGE_MODEL = "qwen3.5:4b"

# Only the two models already present in ~/models. Tier 0 must not trigger a pull.
TIER0_ATTACKERS = [
    "huihui_ai/qwen3.5-abliterated:4b",
    "huihui_ai/gemma-4-abliterated:12b",
]

# Tier 0 uses a 2-case slice of the frozen set purely to keep the smoke test short.
# Tier 1 uses the full frozen list from cases.json.
TIER0_CASE_COUNT = int(os.environ.get("TIER0_CASE_COUNT", "2"))

EXPERIMENT_ID = "tier0-harness-validation"
OUTPUT_ROOT = PROJECT / "logs" / "attacker_selection" / "tier0"


# ---------------------------------------------------------------------------
# Warmup: move model-load cost outside the measurement window
# ---------------------------------------------------------------------------

def warmup(model: str, timeout: float = 900.0) -> Tuple[bool, float, str]:
    """Issue one throwaway generation so the model is resident before timing.

    Without this, end-to-end run duration silently absorbs model load, which ranges
    from ~3GB (4b) to ~20GB (31b). That would penalise large candidates for their
    load time rather than their attack quality - corrupting the exact metric we kept.
    """
    payload = json.dumps(
        {
            "model": model,
            "prompt": "ping",
            "stream": False,
            "options": {"num_predict": 1},
        }
    ).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, time.monotonic() - start, ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, time.monotonic() - start, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------

def build_attack_config(
    attacker_model: str,
    experiment_run_id: str,
    output_dir: Path,
    goals: List[str],
) -> Dict[str, Any]:
    return {
        "attack_type": "pair",
        "goals": list(goals),
        "attacker": {
            "identifier": attacker_model,
            "endpoint": ENDPOINT,
            "agent_type": "OLLAMA",
            "temperature": 1.0,
        },
        "judge": {
            "identifier": JUDGE_MODEL,
            "endpoint": ENDPOINT,
            "agent_type": "OLLAMA",
            "type": "harmbench",
        },
        "objective": "jailbreak",
        "n_iterations": 2,
        "n_streams": 1,
        # THE CORRELATION CARRIER.
        #
        # Measured result: PairConfig inherits model_config = ConfigDict(extra="forbid")
        # from hackagent/attacks/techniques/config.py, so a bare "experiment_run_id"
        # key, a "metadata" dict, and a "custom_metadata" dict are ALL rejected with
        # extra_forbidden. They do land in attacks.config_json (the attack row is
        # persisted from the raw dict before technique validation), but the run then
        # dies - so they are unusable as a carrier.
        #
        # output_dir is a declared, validated field that is persisted in
        # attacks.config_json, so embedding the id in the path gives us a correlation
        # key with zero validation risk and zero extra keys.
        "output_dir": str(output_dir),
    }


def run_one(attacker_model: str) -> Dict[str, Any]:
    """Execute one PAIR run. Returns a record; never raises."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", attacker_model).strip("-")
    goals = case_set.goals()[:TIER0_CASE_COUNT]
    ids = case_set.case_ids()[:TIER0_CASE_COUNT]

    experiment_run_id = ledger.plan_run(
        experiment_id=EXPERIMENT_ID,
        tier="tier0",
        attack="pair",
        attacker_model=attacker_model,
        target_model=TARGET_MODEL,
        judge_model=JUDGE_MODEL,
        config={"n_iterations": 2, "n_streams": 1, "case_ids": ids},
        case_id=",".join(ids),
        repetition=1,
    )
    output_dir = OUTPUT_ROOT / f"{experiment_run_id}-{slug}"
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger.update_status(
        experiment_run_id,
        "running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        node=os.environ.get("SLURMD_NODENAME"),
    )

    # Warm every role before the timed section.
    warmups: Dict[str, Any] = {}
    for role, model in (
        ("attacker", attacker_model),
        ("target", TARGET_MODEL),
        ("judge", JUDGE_MODEL),
    ):
        ok, secs, err = warmup(model)
        warmups[role] = {"model": model, "ok": ok, "seconds": round(secs, 2), "error": err}
        print(f"  warmup {role:<8} {model:<40} {'ok' if ok else 'FAILED'} {secs:.1f}s", flush=True)

    config = build_attack_config(attacker_model, experiment_run_id, output_dir, goals)
    (output_dir / "attack_config.json").write_text(json.dumps(config, indent=2))
    (output_dir / "warmup.json").write_text(json.dumps(warmups, indent=2))

    # No wall clock is recorded by decision: HackAgent's per-process preflight makes
    # wall and db_run_s both unreliable as attack clocks. max(elapsed_s) is the metric.
    raised: Optional[str] = None
    agent = None
    try:
        agent = HackAgent(name=TARGET_MODEL, endpoint=ENDPOINT, agent_type="OLLAMA")
    except Exception as exc:  # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
        ledger.update_status(
            experiment_run_id, "crashed", error_text=traceback.format_exc()
        )
        (output_dir / "error.txt").write_text(traceback.format_exc())
    if agent is not None:
        try:
            results = agent.hack(attack_config=config)
            (output_dir / "raw_results.json").write_text(
                json.dumps([str(r) for r in results], indent=2)
            )
            ledger.update_status(experiment_run_id, "completed")
        except Exception as exc:  # noqa: BLE001 - preserve every failure
            # P4: a crash must still leave a correlatable ledger row.
            raised = f"{type(exc).__name__}: {exc}"
            ledger.update_status(
                experiment_run_id, "crashed", error_text=traceback.format_exc()
            )
            (output_dir / "error.txt").write_text(traceback.format_exc())

    return {
        "experiment_run_id": experiment_run_id,
        "attacker_model": attacker_model,
        "warmups": warmups,
        "raised": raised,
        "output_dir": str(output_dir),
    }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def probe_correlation(experiment_run_id: str) -> Dict[str, Any]:
    """P1: did the correlation key survive into HackAgent's stored config?

    Reports which of the three candidate placements actually persisted, so the
    ledger join can be built on evidence rather than assumption.
    """
    row = ledger.find_hackagent_run(experiment_run_id)
    if row is None:
        return {
            "status": "fail",
            "detail": (
                "correlation id absent from attacks.config_json and runs.config_json "
                "in all three placements (bare key / metadata / custom_metadata)"
            ),
            # Deliberately NOT falling back to latest_hackagent_run(): attributing
            # the most recent run here would silently measure a different run.
            "hackagent_run_id": None,
            "surviving_placements": [],
        }

    survived: List[str] = []
    try:
        stored = json.loads(row["attack_config"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stored = {}

    if stored.get(CORRELATION_KEY) == experiment_run_id:
        survived.append("bare_key")
    meta = stored.get("metadata")
    if isinstance(meta, dict) and meta.get(CORRELATION_KEY) == experiment_run_id:
        survived.append("metadata")
    custom = stored.get("custom_metadata")
    if isinstance(custom, dict) and custom.get(CORRELATION_KEY) == experiment_run_id:
        survived.append("custom_metadata")
    if experiment_run_id in str(stored.get("output_dir") or ""):
        survived.append("output_dir")

    return {
        "status": "pass" if survived else "fail",
        "detail": (
            f"correlated to HackAgent run {row['run_id']}; "
            f"carrier(s) present in attacks.config_json: {survived}"
        ),
        "hackagent_run_id": row["run_id"],
        "surviving_placements": survived,
    }


_LABEL_RE = re.compile(r"^\s*(yes|no|1|0|true|false|safe|unsafe)\s*$", re.IGNORECASE)


def probe_judge_parseability(hackagent_run_id: Optional[str]) -> Dict[str, Any]:
    """P2: does the judge return clean labels/scores, or unparseable prose?"""
    if not hackagent_run_id:
        return {
            "status": "skipped",
            "detail": "no correlated HackAgent run - cannot measure without "
            "attributing another run's traces to this one",
        }

    traces = ledger.collect_traces(hackagent_run_id)
    total = 0
    failures = 0
    samples: List[str] = []
    for t in traces:
        content = t["content"]
        if content.get("evaluator") is None:
            continue
        total += 1
        score = content.get("score")
        result = content.get("result")
        explanation = str(content.get("explanation") or "")

        numeric_ok = isinstance(score, (int, float))
        label_ok = result is not None and _LABEL_RE.match(str(result)) is not None
        if not (numeric_ok or label_ok):
            failures += 1
            if len(samples) < 3:
                samples.append(explanation[:200])

    rate = (failures / total) if total else None
    if total == 0:
        return {"status": "skipped", "detail": "no judge outputs in this run"}
    return {
        # A judge that cannot be parsed at all makes ASR meaningless.
        "status": "pass" if failures == 0 else "fail",
        "detail": (
            f"judge outputs={total} unparseable={failures} "
            f"rate={rate if rate is None else round(rate, 3)}"
            + (f"; samples={samples}" if samples else "")
        ),
        "judge_parse_total": total,
        "judge_parse_failures": failures,
    }


def probe_duration(
    hackagent_run_id: Optional[str], warmups: Dict[str, Any]
) -> Dict[str, Any]:
    """P3: is attack duration recoverable and trustworthy?

    attack_duration_s = max(elapsed_s) is the sole timing metric, by decision.
    run_s (DB timestamps) is retained only as a containment sanity check, because it
    absorbs HackAgent's per-process preflight and is therefore not comparable across
    the first and subsequent runs in a process.
    """
    if not hackagent_run_id:
        return {
            "status": "skipped",
            "detail": "no correlated HackAgent run - cannot attribute a duration",
        }

    import sqlite3

    conn = sqlite3.connect(f"file:{ledger.DEFAULT_HACKAGENT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id AS run_id, ROUND((julianday(updated_at)-julianday(created_at))"
        "*86400,3) AS run_seconds FROM runs WHERE id = ?",
        (hackagent_run_id,),
    ).fetchone()
    conn.close()

    db_seconds = row["run_seconds"] if row is not None else None
    if db_seconds is None:
        return {
            "status": "fail",
            "detail": "no run duration derivable from DB timestamps",
        }

    results = ledger.collect_results(hackagent_run_id)
    elapsed: List[float] = []
    missing = 0
    for res in results:
        try:
            meta = json.loads(res.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        value = meta.get("elapsed_s")
        if isinstance(value, (int, float)):
            elapsed.append(float(value))
        else:
            missing += 1

    warm_ok = all(w["ok"] for w in warmups.values())
    warm_total = round(sum(w["seconds"] for w in warmups.values()), 2)

    # Wall-clock comparison was removed by decision. Measured on GPU jobs 35086/35093:
    # the first hack() call in a process pays HackAgent model-preflight cost (~27 s
    # inside the run window on job 35094, ~1 s for the second run), so neither wall nor
    # db_run_s is a clean attack clock. elapsed_s is the only one that excludes
    # preflight consistently, so it is the sole metric. db_run_s is kept as a
    # containment sanity check only.
    elapsed_max_v = max(elapsed) if elapsed else 0.0
    contained = elapsed_max_v <= db_seconds * 1.05 + 1.0
    # PAIR's elapsed_s is cumulative from run start, so it must be non-decreasing.
    # A violation means the aggregation rule does not hold and max() is unsafe.
    monotonic = all(a <= b + 1e-6 for a, b in zip(elapsed, elapsed[1:]))

    ok = (
        bool(elapsed)
        and missing == 0
        and warm_ok
        and contained
        and monotonic
    )
    return {
        "status": "pass" if ok else "fail",
        "detail": (
            f"attack_duration_s=max(elapsed_s)={round(elapsed_max_v, 2)} "
            f"per_goal={[round(e, 2) for e in elapsed]} missing={missing}; "
            f"db_run_s={db_seconds} (containment check only) warmup={warm_total}s"
            + ("" if contained else " -- max(elapsed_s) EXCEEDS the run window")
            + ("" if monotonic else " -- elapsed_s NOT monotonic; max() is unsafe here")
            + ("" if warm_ok else " -- warmup incomplete")
        ),
        "attack_duration_s": round(elapsed_max_v, 2),
        "elapsed_s_per_goal": elapsed,
        "elapsed_s_missing": missing,
        "elapsed_s_monotonic": monotonic,
        "db_run_seconds": db_seconds,
        "warmup_seconds": warm_total,
    }


def probe_failure_record(experiment_run_id: str) -> Dict[str, Any]:
    """P4: is there a ledger row regardless of how the run ended?"""
    with ledger.connect() as conn:
        row = conn.execute(
            "SELECT status, error_text FROM planned_runs WHERE experiment_run_id = ?",
            (experiment_run_id,),
        ).fetchone()
    if row is None:
        return {"status": "fail", "detail": "no ledger row - failures would be lost"}
    return {
        "status": "pass",
        "detail": f"ledger row present with status='{row['status']}'"
        + (" and captured traceback" if row["error_text"] else ""),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ledger.init()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {"experiment_id": EXPERIMENT_ID, "runs": []}

    for attacker in TIER0_ATTACKERS:
        print(f"\n{'=' * 72}\nTier 0 run: attacker={attacker}\n{'=' * 72}", flush=True)
        record = run_one(attacker)
        experiment_run_id = record["experiment_run_id"]
        if record["raised"]:
            print(f"  run raised: {record['raised']}", flush=True)

        corr = probe_correlation(experiment_run_id)
        ha_run_id = corr.get("hackagent_run_id")
        probes = {
            "P1_correlation_key_survives": corr,
            "P2_judge_parseable": probe_judge_parseability(ha_run_id),
            "P3_attack_duration": probe_duration(ha_run_id, record["warmups"]),
            "P4_failure_recorded": probe_failure_record(experiment_run_id),
        }
        record["hackagent_run_id"] = ha_run_id
        record["probes"] = probes
        report["runs"].append(record)

    # Summary table
    print(f"\n{'=' * 72}\nTIER 0 PROBE SUMMARY\n{'=' * 72}")
    for run in report["runs"]:
        print(f"\nattacker: {run['attacker_model']}  ({run['experiment_run_id']})")
        if run["raised"]:
            print(f"  run raised: {run['raised']}")
        for name, outcome in run["probes"].items():
            status = outcome.get("status", "fail").upper()
            print(f"  {name:<32} {status:<8} {outcome.get('detail', '')}")

    any_fail = any(
        p.get("status") == "fail" for r in report["runs"] for p in r["probes"].values()
    )
    any_skip = any(
        p.get("status") == "skipped"
        for r in report["runs"]
        for p in r["probes"].values()
    )
    if any_skip:
        print(
            "\nNOTE: SKIPPED probes were not measured. They are not passes, and no "
            "conclusion may be drawn from them."
        )

    report_path = OUTPUT_ROOT / "tier0_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {report_path}")
    print(
        "\nReminder: Tier 0 validates the harness only. "
        "No value above may be reported as a model result."
    )
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
