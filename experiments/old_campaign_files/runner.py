"""Execute a manifest-defined HackAgent campaign one independent cell at a time."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import attack_registry  # noqa: E402
import ledger  # noqa: E402
import manifest as campaign_manifest  # noqa: E402
from hackagent import HackAgent  # noqa: E402


DEFAULT_MANIFEST = HERE / "campaign.json"
DEFAULT_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11435")
DEFAULT_LEDGER = HERE / "campaign.db"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def _cell_key(spec: campaign_manifest.RunSpec) -> str:
    attacker = spec.attacker.model_id if spec.attacker else "no-attacker"
    return "|".join(
        (
            spec.campaign_id,
            f"target={spec.target.model_id}",
            f"attacker={attacker}",
            f"attack={spec.attack}",
            f"rep={spec.repetition}",
        )
    )


def _canonical_config(config: Dict[str, Any]) -> Dict[str, Any]:
    stable = copy.deepcopy(config)
    stable.pop("output_dir", None)
    stable.pop("run_id", None)
    return stable


def warmup(model: str, endpoint: str, timeout: float = 900.0) -> tuple[bool, float, str]:
    """Load one model with a throwaway generation before the timed attack."""
    payload = json.dumps(
        {
            "model": model,
            "prompt": "ping",
            "stream": False,
            "options": {"num_predict": 1},
        }
    ).encode()
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        return True, time.monotonic() - started, ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def _role_models(
    attack: str, config: Dict[str, Any], target_model: str
) -> list[tuple[str, str]]:
    """Return unique model roles actually used by this attack configuration."""
    roles: list[tuple[str, str]] = [("target", target_model)]
    for key in attack_registry.ATTACKS[attack]["roles"]:
        value = config.get(key)
        if isinstance(value, dict) and value.get("identifier"):
            roles.append((key, str(value["identifier"])))
        elif key == "judge":
            judges = config.get("judges")
            if isinstance(judges, list):
                for index, judge in enumerate(judges):
                    if isinstance(judge, dict) and judge.get("identifier"):
                        roles.append((f"judge_{index + 1}", str(judge["identifier"])))

    if attack == "tap":
        on_topic = config.get("on_topic_judge")
        if isinstance(on_topic, dict) and on_topic.get("identifier"):
            roles.append(("on_topic_judge", str(on_topic["identifier"])))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, model in roles:
        if model not in seen:
            unique.append((role, model))
            seen.add(model)
    return unique


def _judge_parse_counts(traces: Iterable[Dict[str, Any]]) -> tuple[int, int]:
    total = failures = 0
    for trace in traces:
        content = trace.get("content") or {}
        if content.get("evaluator") is None:
            continue
        total += 1
        score = content.get("score")
        result = content.get("result")
        if not isinstance(score, (int, float)) and result is None:
            failures += 1
    return failures, total


def _harvest(
    spec: campaign_manifest.RunSpec,
    run_id: str,
    *,
    db_path: Path,
    ledger_path: Path,
    attack_duration: Optional[float] = None,
) -> Dict[str, Any]:
    """Harvest one correlated HackAgent run without ever writing to its database."""
    row = ledger.find_hackagent_run(run_id, db_path)
    if row is None:
        ledger.harvest_run(
            run_id,
            hackagent_run_id=None,
            run_status="UNCORRELATED",
            metrics={"correlated": False},
            ledger_path=ledger_path,
        )
        return {"correlated": False, "run_status": "UNCORRELATED"}

    hackagent_run_id = str(row["run_id"])
    results = ledger.collect_results(hackagent_run_id, db_path)
    traces = []
    # The ledger module intentionally keeps trace collection focused on IDs. The
    # parseability metric only needs the evaluator-bearing rows.
    connection = ledger._ha_connect(db_path)
    try:
        connection.row_factory = __import__("sqlite3").Row
        for trace_row in connection.execute(
            """
            SELECT t.content_json FROM traces t
            JOIN results r ON t.result_id = r.id
            WHERE r.run_id = ? ORDER BY r.goal_index, t.sequence
            """,
            (hackagent_run_id,),
        ):
            try:
                content = json.loads(trace_row["content_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                content = {}
            traces.append({"content": content})
    finally:
        connection.close()

    successful = sum(
        result.get("evaluation_status") == "SUCCESSFUL_JAILBREAK" for result in results
    )
    failures = sum(
        result.get("evaluation_status") == "FAILED_JAILBREAK" for result in results
    )
    not_evaluated = len(results) - successful - failures
    evaluated = successful + failures
    automated_asr = successful / evaluated if evaluated else None
    judge_parse_failures, judge_parse_total = _judge_parse_counts(traces)

    elapsed: list[float] = []
    for result in results:
        try:
            metadata = json.loads(result.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        value = metadata.get("elapsed_s")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            elapsed.append(float(value))

    metrics: Dict[str, Any] = {
        "correlated": True,
        "elapsed_s": elapsed,
        "not_evaluated_count": not_evaluated,
        "per_case": {},
        "per_case_unmapped": {},
        "duration_source": "max(elapsed_s) provisional generic metric",
    }
    case_by_goal = dict(zip(spec.goals, spec.case_ids))
    for result in results:
        goal = str(result.get("goal") or "")
        case_id = case_by_goal.get(goal)
        status = result.get("evaluation_status")
        if case_id:
            metrics["per_case"][case_id] = status
        else:
            metrics["per_case_unmapped"][goal] = status

    duration = attack_duration
    if duration is None and elapsed:
        duration = max(elapsed)
    metrics["attack_duration_s"] = duration
    if spec.attack != "pair":
        metrics["duration_warning"] = (
            "Elapsed-time aggregation is unverified for this technique; inspect metadata."
        )

    ledger.harvest_run(
        run_id,
        hackagent_run_id=hackagent_run_id,
        hackagent_attack_id=str(row["attack_id"]),
        run_status=row["status"],
        run_seconds=row["run_seconds"],
        result_count=len(results),
        trace_count=len(traces),
        automated_success=successful,
        automated_asr=automated_asr,
        not_evaluated_count=not_evaluated,
        judge_parse_failures=judge_parse_failures,
        judge_parse_total=judge_parse_total,
        metrics=metrics,
        results=results,
        case_by_goal=case_by_goal,
        db_path=db_path,
        ledger_path=ledger_path,
    )
    return {
        "correlated": True,
        "hackagent_run_id": hackagent_run_id,
        "run_status": row["status"],
        "result_count": len(results),
        "trace_count": len(traces),
        "automated_asr": automated_asr,
        "attack_duration_s": duration,
    }


def _plan_spec(
    spec: campaign_manifest.RunSpec,
    *,
    endpoint: str,
    output_root: Path,
    ledger_path: Path,
    embedder_model: str,
    target_config: Dict[str, Any],
    run_config: Dict[str, Any],
) -> tuple[str, Dict[str, Any], Path, int]:
    cell_key = _cell_key(spec)
    attempt = ledger.next_attempt(cell_key, ledger_path)
    run_id = ledger.new_run_id()
    output_dir = (
        output_root
        / _slug(spec.target.model_id)
        / _slug(spec.attack)
        / _slug(spec.attacker.model_id if spec.attacker else "no-attacker")
        / f"rep-{spec.repetition}-attempt-{attempt}-{run_id}"
    )
    config = attack_registry.build_attack_config(
        spec.attack,
        spec.attack_config,
        target_model=spec.target.model_id,
        attacker_model=spec.attacker.model_id if spec.attacker else None,
        judge_model=spec.judge.model_id,
        embedder_model=embedder_model,
        endpoint=endpoint,
        output_dir=str(output_dir),
        goals=spec.goals,
    )
    config_hash = campaign_manifest.config_hash(
        {
            "attack_config": _canonical_config(config),
            "target_config": target_config,
            "run_config": run_config,
            "endpoint": endpoint,
        }
    )
    ledger.plan_run(
        experiment_run_id=run_id,
        cell_key=cell_key,
        campaign_id=spec.campaign_id,
        target_model=spec.target.model_id,
        target_revision=spec.target.revision,
        attacker_model=spec.attacker.model_id if spec.attacker else None,
        attacker_revision=spec.attacker.revision if spec.attacker else None,
        judge_model=spec.judge.model_id,
        judge_revision=spec.judge.revision,
        attack=spec.attack,
        case_set_id=spec.case_set_id,
        case_ids=spec.case_ids,
        repetition=spec.repetition,
        attempt=attempt,
        config_hash=config_hash,
        config=config,
        output_dir=str(output_dir),
        ledger_path=ledger_path,
    )
    return run_id, config, output_dir, attempt


def run_one(
    spec: campaign_manifest.RunSpec,
    *,
    endpoint: str,
    output_root: Path,
    target_config: Dict[str, Any],
    run_config: Dict[str, Any],
    embedder_model: str,
    warm_models: bool,
    retry_failed: bool,
    db_path: Path,
    ledger_path: Path,
) -> Dict[str, Any]:
    cell_key = _cell_key(spec)
    previous = ledger.find_cell(cell_key, ledger_path)
    if previous is not None:
        status = str(previous["status"])
        if status in {"planned", "running", "completed"} or not retry_failed:
            return {
                "cell_key": cell_key,
                "status": f"skipped:{status}",
                "experiment_run_id": previous["experiment_run_id"],
            }

    if spec.target.execution_mode != "ollama-local":
        run_id, config, output_dir, _ = _plan_spec(
            spec,
            endpoint=endpoint,
            output_root=output_root,
            ledger_path=ledger_path,
            embedder_model=embedder_model,
            target_config=target_config,
            run_config=run_config,
        )
        message = f"target {spec.target.model_id} is {spec.target.execution_mode}, not local"
        ledger.update_status(run_id, "skipped", error_text=message, ledger_path=ledger_path)
        return {"cell_key": cell_key, "status": "skipped", "reason": message}

    run_id, config, output_dir, attempt = _plan_spec(
        spec,
        endpoint=endpoint,
        output_root=output_root,
        ledger_path=ledger_path,
        embedder_model=embedder_model,
        target_config=target_config,
        run_config=run_config,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "attack_config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True, default=str)
    )
    ledger.update_status(
        run_id,
        "running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID"),
        node=os.environ.get("SLURMD_NODENAME") or os.uname().nodename,
        ledger_path=ledger_path,
    )

    warmups: Dict[str, Any] = {}
    failure: Optional[str] = None
    harvested: Optional[Dict[str, Any]] = None
    try:
        if warm_models:
            for role, model in _role_models(spec.attack, config, spec.target.model_id):
                ok, seconds, error = warmup(model, endpoint)
                warmups[role] = {
                    "model": model,
                    "ok": ok,
                    "seconds": round(seconds, 2),
                    "error": error,
                }
                print(
                    f"  warmup {role:<14} {model:<45} "
                    f"{'ok' if ok else 'FAILED'} {seconds:.1f}s",
                    flush=True,
                )
                if not ok:
                    raise RuntimeError(f"{role} warmup failed: {error}")
        (output_dir / "warmup.json").write_text(json.dumps(warmups, indent=2))
        agent = HackAgent(
            name=spec.target.model_id,
            endpoint=endpoint,
            agent_type="OLLAMA",
            target_config=target_config,
            thinking=target_config.get("thinking"),
        )
        results = agent.hack(
            attack_config=config,
            run_config_override=run_config,
        )
        (output_dir / "raw_results.json").write_text(
            json.dumps([str(result) for result in results], indent=2)
        )
    except Exception:
        failure = traceback.format_exc()

    try:
        harvested = _harvest(
            spec,
            run_id,
            db_path=db_path,
            ledger_path=ledger_path,
        )
    except Exception:
        harvest_failure = traceback.format_exc()
        failure = f"{failure}\nHarvest failure:\n{harvest_failure}" if failure else harvest_failure

    if failure is None and (harvested is None or not harvested.get("correlated")):
        failure = "HackAgent run was uncorrelated; no output_dir match was found"
    elif failure is None and harvested.get("run_status") != "COMPLETED":
        failure = f"HackAgent run ended with status {harvested.get('run_status')!r}"

    if failure is None:
        ledger.update_status(run_id, "completed", ledger_path=ledger_path)
        status = "completed"
    else:
        (output_dir / "error.txt").write_text(failure)
        ledger.update_status(run_id, "crashed", error_text=failure, ledger_path=ledger_path)
        status = "crashed"
    return {
        "cell_key": cell_key,
        "experiment_run_id": run_id,
        "attempt": attempt,
        "status": status,
        "harvested": harvested,
        "error": failure,
    }


def run_matrix(
    manifest_path: Path,
    *,
    ledger_path: Path = DEFAULT_LEDGER,
    endpoint: str = DEFAULT_ENDPOINT,
    target_filter: Optional[set[str]] = None,
    attacker_filter: Optional[set[str]] = None,
    attack_filter: Optional[set[str]] = None,
    repetition_filter: Optional[set[int]] = None,
    dry_run: bool = False,
    warm_models: Optional[bool] = None,
    retry_failed: bool = False,
    limit: Optional[int] = None,
) -> list[Dict[str, Any]]:
    manifest = campaign_manifest.load_manifest(manifest_path)
    models = campaign_manifest.load_models(manifest, manifest_path)
    campaign_manifest.validate_manifest(manifest, models)
    case_set_id, case_ids, goals = campaign_manifest.load_cases(manifest, manifest_path)
    embedder_model = str(manifest.get("embedder_id") or "embeddinggemma:300m")
    specs = campaign_manifest.expand_runs(
        manifest,
        models,
        case_set_id=case_set_id,
        case_ids=case_ids,
        goals=goals,
        targets=target_filter,
        attackers=attacker_filter,
        attacks=attack_filter,
        repetitions=repetition_filter,
    )
    if limit is not None:
        specs = specs[:limit]
    print(
        f"Campaign {manifest['campaign_id']}: {len(specs)} cell(s), "
        f"{len(case_ids)} case(s), attacks={sorted({s.attack for s in specs})}",
        flush=True,
    )
    if dry_run:
        for spec in specs:
            attacker = spec.attacker.model_id if spec.attacker else "-"
            print(
                f"target={spec.target.model_id} attacker={attacker} "
                f"attack={spec.attack} repetition={spec.repetition}"
            )
        return []

    ledger.init(ledger_path)
    output_root = PROJECT / manifest.get("execution", {}).get("output_root", "logs/campaign")
    warm = (
        bool(manifest.get("execution", {}).get("warmup", True))
        if warm_models is None
        else warm_models
    )
    results = []
    for spec in specs:
        print(
            f"\nRun: target={spec.target.model_id} "
            f"attacker={spec.attacker.model_id if spec.attacker else '-'} "
            f"attack={spec.attack} repetition={spec.repetition}",
            flush=True,
        )
        results.append(
            run_one(
                spec,
                endpoint=endpoint,
                output_root=output_root,
                target_config=dict(manifest.get("target_config") or {}),
                run_config=dict(manifest.get("run_config") or {}),
                embedder_model=embedder_model,
                warm_models=warm,
                retry_failed=retry_failed,
                db_path=ledger.DEFAULT_HACKAGENT_DB,
                ledger_path=ledger_path,
            )
        )
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--target", action="append", dest="targets")
    parser.add_argument("--attacker", action="append", dest="attackers")
    parser.add_argument("--attack", action="append", dest="attacks")
    parser.add_argument("--repetition", action="append", type=int, dest="repetitions")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    records = run_matrix(
        args.manifest.resolve(),
        ledger_path=args.ledger.resolve(),
        endpoint=args.endpoint,
        target_filter=set(args.targets) if args.targets else None,
        attacker_filter=set(args.attackers) if args.attackers else None,
        attack_filter=set(args.attacks) if args.attacks else None,
        repetition_filter=set(args.repetitions) if args.repetitions else None,
        dry_run=args.dry_run,
        warm_models=False if args.no_warmup else None,
        retry_failed=args.retry_failed,
        limit=args.limit,
    )
    return 1 if any(record.get("status") == "crashed" for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
