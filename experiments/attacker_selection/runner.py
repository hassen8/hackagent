"""Generic attacker-selection matrix runner.

Each matrix cell gets its own HackAgent run.  HackAgent's database is read-only
evidence; the external ledger records the planned run and automated harvest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cases as case_set  # noqa: E402
import ledger  # noqa: E402
from hackagent import HackAgent  # noqa: E402
from tier0_smoke import ENDPOINT, warmup  # noqa: E402

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent

TARGET_MODEL = "gemma3:4b"
JUDGE_MODEL = "qwen3.5:4b"
DEFAULT_ATTACKERS = [
    "huihui_ai/qwen3.5-abliterated:4b",
    "huihui_ai/gemma-4-abliterated:12b",
]
EXPERIMENT_ID = "attacker-selection"
OUTPUT_ROOT = PROJECT / "logs" / "attacker_selection" / "tier1"
_LABEL_RE = re.compile(r"^\s*(yes|no|1|0|true|false|safe|unsafe)\s*$", re.IGNORECASE)


def build_attack_config(
    attacker_model: str,
    experiment_run_id: str,
    output_dir: Path,
    goals: List[str],
    *,
    n_iterations: int = 3,
) -> Dict[str, Any]:
    """Build the PAIR config while keeping the measured correlation carrier."""
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
        "n_iterations": n_iterations,
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
        # attacks.config_json, so embedding the id in the path gives us a
        # correlation key with zero validation risk and zero extra keys.
        "output_dir": str(output_dir),
    }


def aggregate_pair_duration(
    elapsed_s: Sequence[float], run_seconds: Optional[float]
) -> float:
    """Return PAIR's duration metric after checking its cumulative semantics."""
    if not elapsed_s:
        raise ValueError("PAIR produced no elapsed_s values")
    if not all(
        previous <= current + 1e-6
        for previous, current in zip(elapsed_s, elapsed_s[1:])
    ):
        raise ValueError(
            f"PAIR elapsed_s is not non-decreasing: {list(elapsed_s)!r}; max() is unsafe"
        )

    duration = max(elapsed_s)
    if run_seconds is not None and duration > run_seconds * 1.05 + 1.0:
        print(
            "WARNING: PAIR max(elapsed_s) exceeds the HackAgent run window: "
            f"duration={duration} run_s={run_seconds}; "
            "run_seconds_contained=false",
            file=sys.stderr,
            flush=True,
        )
    return duration


ATTACKS: Dict[str, Dict[str, Callable[..., Any]]] = {
    "pair": {
        "config_builder": build_attack_config,
        "duration_aggregation": aggregate_pair_duration,
    }
}


def _json_dict(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _correlation_matches(row: Any, experiment_run_id: str) -> bool:
    for field in ("attack_config", "run_config"):
        config = _json_dict(row[field])
        if experiment_run_id in str(config.get("output_dir") or ""):
            return True
    return False


def _judge_parse_counts(traces: List[Dict[str, Any]]) -> Tuple[int, int]:
    total = 0
    failures = 0
    for trace in traces:
        content = trace["content"]
        if content.get("evaluator") is None:
            continue
        total += 1
        score = content.get("score")
        result = content.get("result")
        numeric_ok = isinstance(score, (int, float))
        label_ok = result is not None and _LABEL_RE.match(str(result)) is not None
        if not (numeric_ok or label_ok):
            failures += 1
    return failures, total


def _harvest(
    experiment_run_id: str,
    attack: str,
) -> Dict[str, Any]:
    """Read one correlated HackAgent run and write its automated ledger row."""
    row = ledger.find_hackagent_run(experiment_run_id)
    if row is None or not _correlation_matches(row, experiment_run_id):
        ledger.harvest_run(
            experiment_run_id,
            hackagent_run_id=None,
            run_status="UNCORRELATED",
            metrics={"correlated": False},
        )
        return {"correlated": False, "run_status": "UNCORRELATED"}

    hackagent_run_id = str(row["run_id"])
    results = ledger.collect_results(hackagent_run_id)
    traces = ledger.collect_traces(hackagent_run_id)
    elapsed: List[float] = []
    for result in results:
        metadata = _json_dict(result.get("metadata_json"))
        value = metadata.get("elapsed_s")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            elapsed.append(float(value))

    missing_elapsed = len(results) - len(elapsed)

    result_count = len(results)
    automated_success = sum(
        result.get("evaluation_status") == "SUCCESSFUL_JAILBREAK"
        for result in results
    )
    evaluated_failures = sum(
        result.get("evaluation_status") == "FAILED_JAILBREAK"
        for result in results
    )
    evaluated_count = automated_success + evaluated_failures
    automated_asr = (
        automated_success / evaluated_count if evaluated_count else None
    )
    not_evaluated_count = result_count - evaluated_count
    judge_parse_failures, judge_parse_total = _judge_parse_counts(traces)
    metrics: Dict[str, Any] = {
        "correlated": True,
        "elapsed_s": elapsed,
        "elapsed_s_missing": missing_elapsed,
        "not_evaluated_count": not_evaluated_count,
    }

    spec = ATTACKS[attack]
    aggregate = spec["duration_aggregation"]
    try:
        if missing_elapsed:
            raise ValueError(
                f"{attack} result rows missing elapsed_s: "
                f"{missing_elapsed}/{len(results)}"
            )
        duration = aggregate(elapsed, row["run_seconds"])
    except Exception as exc:
        metrics["attack_duration_s"] = None
        metrics["duration_error"] = f"{type(exc).__name__}: {exc}"
        ledger.harvest_run(
            experiment_run_id,
            hackagent_run_id=hackagent_run_id,
            hackagent_attack_id=str(row["attack_id"]),
            run_status=row["status"],
            run_seconds=row["run_seconds"],
            result_count=result_count,
            trace_count=len(traces),
            automated_success=automated_success,
            automated_asr=automated_asr,
            judge_parse_failures=judge_parse_failures,
            judge_parse_total=judge_parse_total,
            metrics=metrics,
        )
        return {
            "correlated": True,
            "hackagent_run_id": hackagent_run_id,
            "run_status": row["status"],
            "duration_error": metrics["duration_error"],
            "result_count": result_count,
            "trace_count": len(traces),
        }

    metrics["attack_duration_s"] = duration
    metrics["run_seconds_contained"] = (
        row["run_seconds"] is None
        or duration <= row["run_seconds"] * 1.05 + 1.0
    )
    ledger.harvest_run(
        experiment_run_id,
        hackagent_run_id=hackagent_run_id,
        hackagent_attack_id=str(row["attack_id"]),
        run_status=row["status"],
        run_seconds=row["run_seconds"],
        result_count=result_count,
        trace_count=len(traces),
        automated_success=automated_success,
        automated_asr=automated_asr,
        judge_parse_failures=judge_parse_failures,
        judge_parse_total=judge_parse_total,
        metrics=metrics,
    )
    return {
        "correlated": True,
        "hackagent_run_id": hackagent_run_id,
        "run_status": row["status"],
        "attack_duration_s": duration,
        "result_count": result_count,
        "trace_count": len(traces),
    }


def _run_one(
    attack: str,
    attacker_model: str,
    repetition: int,
    *,
    n_iterations: int,
    case_count: int,
) -> Dict[str, Any]:
    spec = ATTACKS[attack]
    all_goals = case_set.goals()
    all_case_ids = case_set.case_ids()
    goals = all_goals[:case_count]
    case_ids = all_case_ids[:case_count]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", attacker_model).strip("-")
    config_summary = {
        "n_iterations": n_iterations,
        "n_streams": 1,
        "case_ids": case_ids,
    }

    experiment_run_id = ledger.plan_run(
        experiment_id=EXPERIMENT_ID,
        tier="tier1",
        attack=attack,
        attacker_model=attacker_model,
        target_model=TARGET_MODEL,
        judge_model=JUDGE_MODEL,
        config=config_summary,
        case_id=",".join(case_ids),
        repetition=repetition,
    )
    output_dir = OUTPUT_ROOT / attack / f"{experiment_run_id}-{slug}"
    ledger.update_status(
        experiment_run_id,
        "running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        node=os.environ.get("SLURMD_NODENAME"),
        output_dir=str(output_dir),
    )

    warmups: Dict[str, Any] = {}
    failure: Optional[str] = None
    harvested: Optional[Dict[str, Any]] = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        config = spec["config_builder"](
            attacker_model,
            experiment_run_id,
            output_dir,
            goals,
            n_iterations=n_iterations,
        )
        (output_dir / "attack_config.json").write_text(json.dumps(config, indent=2))
        for role, model in (
            ("attacker", attacker_model),
            ("target", TARGET_MODEL),
            ("judge", JUDGE_MODEL),
        ):
            ok, seconds, error = warmup(model)
            warmups[role] = {
                "model": model,
                "ok": ok,
                "seconds": round(seconds, 2),
                "error": error,
            }
            print(
                f"  warmup {role:<8} {model:<40} "
                f"{'ok' if ok else 'FAILED'} {seconds:.1f}s",
                flush=True,
            )
            if not ok:
                raise RuntimeError(f"{role} warmup failed: {error}")

        (output_dir / "warmup.json").write_text(json.dumps(warmups, indent=2))
        agent = HackAgent(name=TARGET_MODEL, endpoint=ENDPOINT, agent_type="OLLAMA")
        agent.hack(attack_config=config)
    except Exception:
        failure = traceback.format_exc()

    if warmups and not (output_dir / "warmup.json").exists():
        (output_dir / "warmup.json").write_text(json.dumps(warmups, indent=2))

    try:
        harvested = _harvest(experiment_run_id, attack)
    except Exception:
        harvest_failure = traceback.format_exc()
        failure = f"{failure}\nHarvest failure:\n{harvest_failure}" if failure else harvest_failure
        try:
            ledger.harvest_run(
                experiment_run_id,
                hackagent_run_id=None,
                run_status="HARVEST_FAILED",
                metrics={"correlated": False, "harvest_error": harvest_failure},
            )
        except Exception:
            failure += "\nLedger harvest write failure:\n" + traceback.format_exc()

    if (
        failure is None
        and (harvested is None or not harvested.get("correlated"))
    ):
        failure = "HackAgent run was uncorrelated; no output_dir match was found"
    elif (
        failure is None
        and harvested is not None
        and harvested.get("run_status") != "COMPLETED"
    ):
        failure = (
            f"HackAgent run {harvested.get('hackagent_run_id')} ended with "
            f"status {harvested.get('run_status')!r}"
        )
    elif failure is None and harvested and harvested.get("duration_error"):
        failure = harvested["duration_error"]

    if failure is None:
        ledger.update_status(experiment_run_id, "completed")
        crashed = False
    else:
        if output_dir.exists():
            (output_dir / "error.txt").write_text(failure)
        ledger.update_status(experiment_run_id, "crashed", error_text=failure)
        crashed = True

    return {
        "experiment_run_id": experiment_run_id,
        "attacker_model": attacker_model,
        "repetition": repetition,
        "output_dir": str(output_dir),
        "harvested": harvested,
        "crashed": crashed,
        "error": failure,
    }


def run_matrix(
    attack: str,
    attacker_models: Sequence[str],
    repetitions: int,
    *,
    n_iterations: int = 3,
    case_count: int = 5,
) -> List[Dict[str, Any]]:
    """Run one independent HackAgent call for every attacker/repetition cell."""
    if attack not in ATTACKS:
        raise ValueError(f"Unknown attack {attack!r}; choose from {sorted(ATTACKS)}")
    if repetitions < 1 or n_iterations < 1:
        raise ValueError("repetitions and n_iterations must be at least 1")
    if case_count < 1 or case_count > len(case_set.goals()):
        raise ValueError(f"case_count must be between 1 and {len(case_set.goals())}")

    ledger.init()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    for attacker_model in attacker_models:
        for repetition in range(1, repetitions + 1):
            print(
                f"\nRun: attack={attack} attacker={attacker_model} repetition={repetition}",
                flush=True,
            )
            try:
                record = _run_one(
                    attack,
                    attacker_model,
                    repetition,
                    n_iterations=n_iterations,
                    case_count=case_count,
                )
            except Exception:
                record = {
                    "attacker_model": attacker_model,
                    "repetition": repetition,
                    "crashed": True,
                    "error": traceback.format_exc(),
                }
                print(record["error"], flush=True)
            records.append(record)
            if record.get("crashed"):
                print("  status: CRASHED", flush=True)
            else:
                print(
                    f"  status: completed duration="
                    f"{(record.get('harvested') or {}).get('attack_duration_s')}",
                    flush=True,
                )
    return records


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attack", required=True, choices=sorted(ATTACKS))
    parser.add_argument("--attackers", nargs="+", default=DEFAULT_ATTACKERS)
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--case-count", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    records = run_matrix(
        args.attack,
        args.attackers,
        args.reps,
        n_iterations=args.iterations,
        case_count=args.case_count,
    )
    return 1 if any(record.get("crashed") for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
