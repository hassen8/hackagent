"""Preview and execute selected campaign matrix cells.

The runner is the campaign coordinator, not an attack implementation.  It
filters the declarative configuration, expands logical matrix cells, creates a
physical attempt ID, persists the plan, warms the roles requested by the
adapter, invokes HackAgent, and asks results.py to harvest the evidence.

The important boundary is that this file should not need to know whether an
attack calls its model role ``attacker``, ``generator``, ``summarizer``, or
something else.  Those differences belong in attack_adapters.py.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import attack_adapters
    from .campaign_config import (
        ATTACKERS,
        ATTACKS,
        ACTIVE_ATTACKER_KEYS,
        ACTIVE_TARGET_KEY,
        CAMPAIGN_NAME,
        JUDGES,
        REPETITIONS,
        RUN_CONFIG,
        SEPARATE_OLLAMA_ATTACKER,
        TARGET_CONFIG,
        TARGETS,
    )
    from .case_selection import CASE_SET_ID, cases_by_batch, load_frozen_cases
    from .sharding import (
        DEFAULT_EXECUTION_SCOPE_ID,
        cell_id_for_run,
        load_shard_manifest,
        validate_shard_manifest,
    )
except ImportError:  # Allow the documented path-based CLI invocation.
    import attack_adapters  # type: ignore[no-redef]

    from campaign_config import (  # type: ignore[no-redef]
        ATTACKERS,
        ATTACKS,
        ACTIVE_ATTACKER_KEYS,
        ACTIVE_TARGET_KEY,
        CAMPAIGN_NAME,
        JUDGES,
        REPETITIONS,
        RUN_CONFIG,
        SEPARATE_OLLAMA_ATTACKER,
        TARGET_CONFIG,
        TARGETS,
    )
    from case_selection import CASE_SET_ID, cases_by_batch, load_frozen_cases  # type: ignore[no-redef]
    from sharding import (  # type: ignore[no-redef]
        DEFAULT_EXECUTION_SCOPE_ID,
        cell_id_for_run,
        load_shard_manifest,
        validate_shard_manifest,
    )


# ---------------------------------------------------------------------------
# Active configuration and matrix constants
# ---------------------------------------------------------------------------
#
# The source lists remain the complete inventory.  These four filtered lists
# are the active campaign definition used by preview, staging, and execution.
# A selector later narrows these active sets for one invocation; it does not
# mutate the versioned configuration.
targets = [
    target
    for target in TARGETS
    if target["use"]
    or (ACTIVE_TARGET_KEY is not None and target["key"] == ACTIVE_TARGET_KEY)
]
if ACTIVE_TARGET_KEY is not None:
    targets = [target for target in targets if target["key"] == ACTIVE_TARGET_KEY]


def _materialize_attacker(model: dict[str, object]) -> dict[str, object]:
    if not SEPARATE_OLLAMA_ATTACKER or model.get("serving_framework") != "ollama":
        return model
    selected = dict(model)
    selected["endpoint_env"] = "OLLAMA_ATTACKER_ENDPOINT"
    return selected


attackers = [
    _materialize_attacker(attacker)
    for attacker in ATTACKERS
    if attacker["use"]
    and (ACTIVE_ATTACKER_KEYS is None or attacker["key"] in ACTIVE_ATTACKER_KEYS)
]
judges = [judge for judge in JUDGES if judge["use"]]
attacks = [attack for attack in ATTACKS if attack["use"]]

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11435"
DEFAULT_VLLM_ENDPOINT = "http://127.0.0.1:8316/v1"
WARMUP_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class ShardContext:
    """Validated paths and identity for one immutable shard manifest."""

    manifest_path: Path
    campaign_root: Path
    shard_id: str
    manifest: dict[str, Any]
    shard_dir: Path
    database_path: Path
    planned_path: Path
    harvested_path: Path
    runs_csv_path: Path
    results_csv_path: Path


def load_shard_context(
    manifest_path: str | Path, shard_id: str
) -> ShardContext:
    """Load and validate one shard before selecting or executing cells."""
    source = Path(manifest_path).expanduser().resolve()
    manifest = load_shard_manifest(source)
    validate_shard_manifest(manifest)
    shard = next(
        (entry for entry in manifest["shards"] if entry.get("shard_id") == shard_id),
        None,
    )
    if shard is None:
        raise ValueError(f"unknown shard_id: {shard_id}")
    paths = shard.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError(f"shard {shard_id} has no path contract")
    root = source.parent
    relative_paths = {
        key: Path(str(value))
        for key, value in paths.items()
        if isinstance(value, str)
    }
    shard_dir = (root / relative_paths["relative_path"]).resolve()
    if root not in shard_dir.parents:
        raise ValueError("shard paths must remain under the campaign root")
    return ShardContext(
        manifest_path=source,
        campaign_root=root,
        shard_id=shard_id,
        manifest=manifest,
        shard_dir=shard_dir,
        database_path=(root / relative_paths["hackagent_db"]).resolve(),
        planned_path=(root / relative_paths["planned_runs"]).resolve(),
        harvested_path=(root / relative_paths["harvested_runs"]).resolve(),
        runs_csv_path=(root / relative_paths["runs_csv"]).resolve(),
        results_csv_path=(root / relative_paths["results_csv"]).resolve(),
    )


def validate_configuration() -> list[dict[str, str]]:
    """Validate invariants needed to expand an unambiguous active matrix.

    The return value is deliberately not a failure: unresolved model entries
    are useful information during a dry run.  The live paths use the same
    validation, then refuse to execute a cell whose required serving identity
    is unresolved.
    """
    if not targets:
        raise ValueError("at least one active target is required")
    if not attackers:
        raise ValueError("at least one active attacker is required")
    if len(judges) != 1:
        raise ValueError("exactly one active judge is required")
    if not attacks:
        raise ValueError("at least one active attack is required")
    if REPETITIONS < 1:
        raise ValueError("repetitions must be at least 1")

    for attack in attacks:
        missing = {"name", "use", "uses_attacker", "config"} - attack.keys()
        if missing:
            missing_names = ", ".join(sorted(missing))
            raise ValueError(f"attack is missing required fields: {missing_names}")

    unresolved: list[dict[str, str]] = []
    for model_kind, models in (
        ("target", targets),
        ("attacker", attackers),
        ("judge", judges),
    ):
        for model in models:
            if not model.get("key"):
                raise ValueError(f"active {model_kind} has an empty key")
            try:
                attack_adapters.model_identifier(model, model_kind)
            except ValueError:
                unresolved.append(
                    {
                        "kind": model_kind,
                        "key": model["key"],
                        "label": model.get("label", model["key"]),
                    }
                )

    return unresolved


def build_run_descriptions() -> list[dict[str, object]]:
    """Expand the active configuration into logical matrix-cell descriptions.

    A description is not yet a HackAgent attempt.  It is the deterministic
    identity and input bundle for one cell:

    ``target + attacker-or-None + attack + repetition + configured cases``.

    When a description is executed, run_one() creates a new physical
    ``experiment_run_id``.  Keeping those concepts separate lets the matrix
    resume code distinguish the configured cell from retries or historical
    attempts of that cell.
    """
    run_descriptions: list[dict[str, object]] = []
    judge = judges[0]
    manifest = load_frozen_cases()
    batches = cases_by_batch(manifest)

    for target in targets:
        for attack in attacks:
            # Non-attacker techniques have one cell per target, not one per attacker.
            attackers_for_attack = attackers if attack["uses_attacker"] else [None]
            for attacker in attackers_for_attack:
                for repetition in range(1, REPETITIONS + 1):
                    for batch_id, batch_goals in batches.items():
                        first_goal = batch_goals[0]
                        run = {
                            "campaign_name": CAMPAIGN_NAME,
                            "execution_scope_id": DEFAULT_EXECUTION_SCOPE_ID,
                            "target": target,
                            "attack": attack,
                            "attacker": attacker,
                            "attacker_applicability": (
                                "driven" if attacker is not None else "not_applicable"
                            ),
                            "judge": judge,
                            "repetition": repetition,
                            "case_set_id": manifest["case_set_id"],
                            "goal_batch_id": batch_id,
                            "category_id": first_goal["category_id"],
                            "subcategory_id": first_goal["subcategory_id"],
                            "goal_ids": [goal["goal_id"] for goal in batch_goals],
                            "goals": [goal["goal"] for goal in batch_goals],
                            "manifest_sha256": manifest["manifest_sha256"],
                        }
                        run["cell_id"] = cell_id_for_run(run)
                        run_descriptions.append(run)

    return run_descriptions


def _model_text(model: dict[str, object]) -> str:
    try:
        return attack_adapters.model_identifier(model, "model")
    except ValueError:
        return "UNRESOLVED (serving identity is not configured)"


def create_correlation_string(campaign_name: str, experiment_run_id: str) -> str:
    """Build the output path used to carry our ID into HackAgent evidence.

    Despite its historical name, this helper is only a pure path constructor:
    it performs no filesystem I/O and does not create the directory.  The
    adapter places this value in the attack configuration; HackAgent persists
    it, allowing results.py to find the exact run later.
    """
    # HackAgent persists output_dir, so it carries our ID into the evidence database.
    return str(Path("logs/campaigns") / campaign_name / "runs" / experiment_run_id)


def _campaign_output_dir() -> Path:
    return Path("logs/campaigns") / CAMPAIGN_NAME


def _output_paths(shard_context: ShardContext | None) -> tuple[Path, Path, Path, Path]:
    if shard_context is not None:
        return (
            shard_context.planned_path,
            shard_context.harvested_path,
            shard_context.runs_csv_path,
            shard_context.results_csv_path,
        )
    campaign_dir = _campaign_output_dir()
    return (
        campaign_dir / "planned_runs.jsonl",
        campaign_dir / "harvested_runs.jsonl",
        campaign_dir / "runs.csv",
        campaign_dir / "results.csv",
    )


def _shard_database_path(shard_context: ShardContext | None) -> Path:
    if shard_context is not None:
        return shard_context.database_path
    return _hackagent_db_path()


def _owned_cells(
    cells: list[dict[str, object]], shard_context: ShardContext
) -> list[dict[str, object]]:
    """Apply immutable shard ownership after ordinary matrix selectors."""
    shard = next(
        entry
        for entry in shard_context.manifest["shards"]
        if entry.get("shard_id") == shard_context.shard_id
    )
    manifest_cells = {
        str(cell["cell_id"]): cell for cell in shard_context.manifest["cells"]
    }
    owned_ids = {str(cell_id) for cell_id in shard["cell_ids"]}
    owned: list[dict[str, object]] = []
    for cell in cells:
        cell_id = str(
            cell_id_for_run(cell, shard_context.manifest["execution_scope_id"])
        )
        if cell_id not in owned_ids:
            continue
        manifest_cell = manifest_cells.get(cell_id)
        if manifest_cell is None or manifest_cell.get("shard_id") != shard_context.shard_id:
            raise ValueError(f"cell {cell_id} is not owned by {shard_context.shard_id}")
        selected = dict(cell)
        selected["cell_id"] = cell_id
        selected["execution_scope_id"] = shard_context.manifest[
            "execution_scope_id"
        ]
        selected["shard_id"] = shard_context.shard_id
        selected["shard_manifest_sha256"] = shard_context.manifest[
            "shard_manifest_sha256"
        ]
        owned.append(selected)
    return owned


def _endpoint() -> str:
    return os.environ.get("OLLAMA_ENDPOINT", DEFAULT_OLLAMA_ENDPOINT)


def _vllm_endpoint(*, required: bool = False) -> str:
    endpoint = os.environ.get("VLLM_ENDPOINT")
    if endpoint:
        return endpoint.rstrip("/")
    if required:
        raise ValueError("VLLM_ENDPOINT is required for active vLLM attacker roles")
    return DEFAULT_VLLM_ENDPOINT


def _role_endpoints(*, require_vllm: bool = False) -> dict[str, str]:
    endpoints = {
        "ollama": _endpoint().rstrip("/"),
        "vllm": _vllm_endpoint(required=require_vllm),
        "OLLAMA_ENDPOINT": _endpoint().rstrip("/"),
        "VLLM_ENDPOINT": _vllm_endpoint(required=require_vllm),
    }
    attacker_endpoint = os.environ.get("OLLAMA_ATTACKER_ENDPOINT")
    if attacker_endpoint:
        endpoints["OLLAMA_ATTACKER_ENDPOINT"] = attacker_endpoint.rstrip("/")
        endpoints["ollama_attacker"] = attacker_endpoint.rstrip("/")
    for model in [*targets, *attackers, *judges]:
        env_name = model.get("endpoint_env")
        if not env_name or env_name in endpoints:
            continue
        value = os.environ.get(str(env_name))
        if value:
            endpoints[str(env_name)] = value.rstrip("/")
    return endpoints


def _hackagent_db_path() -> Path:
    configured = os.environ.get("HACKAGENT_DB")
    return Path(configured).expanduser() if configured else Path(
        "~/.local/share/hackagent/hackagent.db"
    ).expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Cell selection and resumable state
# ---------------------------------------------------------------------------
def _find_selected_cell(
    target_key: str,
    attacker_key: str | None,
    attack_name: str,
    repetition: int,
) -> dict[str, object]:
    """Select one active cell using the configured attacker-dimension flag."""
    base_matches = [
        run
        for run in build_run_descriptions()
        if run["target"]["key"] == target_key
        and run["attack"]["name"] == attack_name
        and run["repetition"] == repetition
    ]
    if not base_matches:
        raise ValueError(
            "selected cell is not an active matrix cell: "
            f"target={target_key!r} attacker={attacker_key!r} "
            f"attack={attack_name!r} repetition={repetition}"
        )

    uses_attacker = base_matches[0]["attack"]["uses_attacker"]
    if uses_attacker and attacker_key is None:
        raise ValueError(
            f"attack {attack_name!r} is attacker-driven and requires --attacker"
        )
    if not uses_attacker and attacker_key is not None:
        raise ValueError(
            f"attack {attack_name!r} is non-attacker-driven; do not pass --attacker"
        )

    matches = [
        run
        for run in base_matches
        if (
            (
                run["attacker"] is not None
                and run["attacker"]["key"] == attacker_key
            )
            if uses_attacker
            else run["attacker"] is None
        )
    ]
    if len(matches) != 1:
        raise ValueError("selected cell did not resolve to exactly one matrix cell")
    return matches[0]


def _cell_key(run: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the stable logical identity of a configured matrix cell.

    This key is for resume matching, not for identifying a physical attempt.
    A retry gets a new experiment_run_id but maps back to the same logical key.
    The configuration hash is intentionally not part of this identity: the
    current continuation contract matches the selected dimensions and
    repetition only, while config_hash remains audit metadata on each record.
    """
    if run.get("cell_id"):
        return ("cell_id", str(run["cell_id"]))
    target = run.get("target")
    attack = run.get("attack")
    attacker = run.get("attacker")
    judge = run.get("judge")
    return (
        run.get("campaign_name", CAMPAIGN_NAME),
        run.get("case_set_id", CASE_SET_ID),
        target.get("key") if isinstance(target, Mapping) else run.get("target_key"),
        attacker.get("key")
        if isinstance(attacker, Mapping)
        else run.get("attacker_key"),
        judge.get("key") if isinstance(judge, Mapping) else run.get("judge_key"),
        attack.get("name") if isinstance(attack, Mapping) else run.get("attack"),
        run.get("repetition"),
        run.get("goal_batch_id"),
    )


def _cell_text(run: Mapping[str, Any]) -> str:
    target = run.get("target")
    attack = run.get("attack")
    attacker = run.get("attacker")
    target_key = target.get("key") if isinstance(target, Mapping) else "?"
    attack_name = attack.get("name") if isinstance(attack, Mapping) else attack
    attacker_key = attacker.get("key") if isinstance(attacker, Mapping) else "-"
    return (
        f"target={target_key} attacker={attacker_key} attack={attack_name} "
        f"repetition={run.get('repetition')}"
    )


def _validate_matrix_filters(
    target_filter: set[str] | None,
    attacker_filter: set[str] | None,
    attack_filter: set[str] | None,
    repetition_filter: set[int] | None,
) -> None:
    selectors = (
        ("target", target_filter, {str(value["key"]) for value in targets}),
        ("attacker", attacker_filter, {str(value["key"]) for value in attackers}),
        ("attack", attack_filter, {str(value["name"]) for value in attacks}),
    )
    for name, selected, active in selectors:
        if selected and not selected <= active:
            unknown = ", ".join(sorted(selected - active))
            raise ValueError(f"unknown or inactive {name} selector(s): {unknown}")
    if repetition_filter:
        invalid = sorted(
            repetition
            for repetition in repetition_filter
            if repetition < 1 or repetition > REPETITIONS
        )
        if invalid:
            raise ValueError(
                "repetition selector(s) are outside the configured range: "
                + ", ".join(str(repetition) for repetition in invalid)
            )


def select_matrix_cells(
    target_filter: set[str] | None = None,
    attacker_filter: set[str] | None = None,
    attack_filter: set[str] | None = None,
    repetition_filter: set[int] | None = None,
) -> list[dict[str, object]]:
    """Filter the deterministic active matrix without changing campaign config."""
    _validate_matrix_filters(
        target_filter, attacker_filter, attack_filter, repetition_filter
    )
    selected: list[dict[str, object]] = []
    for run in build_run_descriptions():
        target = run["target"]
        attack = run["attack"]
        attacker = run["attacker"]
        if target_filter and target["key"] not in target_filter:
            continue
        if attack_filter and attack["name"] not in attack_filter:
            continue
        if repetition_filter and run["repetition"] not in repetition_filter:
            continue
        # Controls have no attacker dimension, so retain one None-attacker cell
        # rather than multiplying or discarding them for an attacker filter.
        if (
            attacker_filter
            and attacker is not None
            and attacker["key"] not in attacker_filter
        ):
            continue
        selected.append(run)
    return selected


def _optional_jsonl(path: Path) -> list[dict[str, Any]]:
    results = _results_module()
    if not path.exists():
        return []
    return results.read_jsonl(path)


def _resume_index(
    shard_context: ShardContext | None = None,
) -> dict[tuple[Any, ...], str]:
    """Return the strongest known resume state for each logical cell.

    State is reconstructed from the append-only campaign JSONL files rather
    than from a second ledger.  The precedence is ``completed`` over
    ``incomplete`` over ``failed``: a completed historical attempt means the
    cell is done, while a planned record with no harvest remains an abandoned
    attempt that requires the explicit incomplete-retry flag.

    "Completed" here means a harvested status of ``completed`` with no
    structured failure.  It is an execution state, not a claim that every
    configured case was scientifically valid or manually reviewed.
    """
    planned_path, harvested_path, _, _ = _output_paths(shard_context)
    plans = _optional_jsonl(planned_path)
    harvests = _optional_jsonl(harvested_path)
    harvest_by_id = {
        str(record.get("experiment_run_id")): record
        for record in harvests
        if record.get("experiment_run_id") is not None
    }
    state_rank = {"failed": 0, "incomplete": 1, "completed": 2}
    states: dict[tuple[Any, ...], str] = {}
    for plan in plans:
        experiment_run_id = plan.get("experiment_run_id")
        harvest = (
            harvest_by_id.get(str(experiment_run_id))
            if experiment_run_id is not None
            else None
        )
        if harvest is None:
            state = "incomplete"
        else:
            status = str(harvest.get("status") or "").lower()
            state = (
                "completed"
                if status == "completed" and harvest.get("failure") is None
                else "failed"
            )
        cell_key = _cell_key(plan)
        if state_rank[state] > state_rank.get(states.get(cell_key), -1):
            states[cell_key] = state
    return states


def _model_ids(required_roles: list[dict[str, str]]) -> dict[str, str]:
    role_models: dict[str, str] = {}
    for required_role in required_roles:
        role = required_role.get("role")
        model = required_role.get("model")
        if not role or not model:
            raise ValueError("adapter returned an incomplete required role")
        if role in role_models:
            raise ValueError(f"adapter returned duplicate required role {role!r}")
        role_models[role] = model

    missing = {"target", "judge"} - role_models.keys()
    if missing:
        raise ValueError(
            "adapter did not return required campaign roles: "
            + ", ".join(sorted(missing))
        )
    return {f"{role}_model": role_models[role] for role in role_models}


def _config_hash(
    run: dict[str, object],
    resolved_attack_config: dict[str, Any],
    target_config: dict[str, Any],
    run_config: dict[str, Any],
    endpoint: str | Mapping[str, str],
) -> str:
    """Hash the materialized runtime inputs for audit and comparison.

    ``output_dir`` is excluded because it contains the per-attempt ID and must
    differ for every retry.  The hash records the selected campaign dimensions,
    endpoint, shared settings, and adapter-resolved attack configuration; it
    is not itself the resume identity and does not replace provenance metadata.
    """
    hash_config = copy.deepcopy(resolved_attack_config)
    hash_config.pop("output_dir", None)
    canonical = {
        "campaign_name": CAMPAIGN_NAME,
        "target_key": run["target"]["key"],
        "attacker_key": (
            run["attacker"]["key"] if run["attacker"] is not None else None
        ),
        "judge_key": run["judge"]["key"],
        "attack": run["attack"]["name"],
        "endpoints": endpoint,
        "target_config": target_config,
        "run_config": run_config,
        "resolved_attack_config": hash_config,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _planned_record(
    run: dict[str, object],
    experiment_run_id: str,
    output_dir: str,
    resolved_attack_config: dict[str, Any],
    target_config: dict[str, Any],
    run_config: dict[str, Any],
    config_hash: str,
    model_ids: dict[str, str],
    shard_context: ShardContext | None = None,
) -> dict[str, Any]:
    target = run["target"]
    attacker = run["attacker"]
    attack = run["attack"]
    judge = run["judge"]
    campaign_attacker_model = model_ids.get("attacker_model") or model_ids.get(
        "generator_model"
    )
    goal_ids = run.get("goal_ids")
    if not isinstance(goal_ids, list) or not all(
        isinstance(goal_id, str) for goal_id in goal_ids
    ):
        raise ValueError("selected run has no valid frozen goal IDs")
    return {
        "experiment_run_id": experiment_run_id,
        "campaign_name": CAMPAIGN_NAME,
        "execution_scope_id": run.get("execution_scope_id"),
        "cell_id": run.get("cell_id"),
        "shard_id": shard_context.shard_id if shard_context is not None else None,
        "shard_manifest_sha256": (
            shard_context.manifest["shard_manifest_sha256"]
            if shard_context is not None
            else None
        ),
        "evidence_db_relpath": (
            str(shard_context.database_path.relative_to(shard_context.campaign_root))
            if shard_context is not None
            else None
        ),
        "target_key": target["key"],
        "target_model": model_ids["target_model"],
        "attacker_key": attacker["key"] if attacker is not None else None,
        "attacker_model": campaign_attacker_model,
        "judge_key": judge["key"],
        "judge_model": model_ids["judge_model"],
        "judge_keys": [judge["key"]],
        "judge_models": [model_ids["judge_model"]],
        "attack": attack["name"],
        "attacker_applicability": run["attacker_applicability"],
        "case_set_id": run["case_set_id"],
        "goal_batch_id": run.get("goal_batch_id"),
        "category_id": run.get("category_id"),
        "subcategory_id": run.get("subcategory_id"),
        "goal_ids": list(goal_ids),
        "manifest_sha256": run.get("manifest_sha256"),
        # Keep the historical field name for pilot readers while using the
        # selected frozen batch IDs for the expanded campaign.
        "case_ids": list(goal_ids),
        "repetition": run["repetition"],
        "target_config": target_config,
        "run_config": run_config,
        "resolved_attack_config": resolved_attack_config,
        "config_hash": config_hash,
        "output_dir": output_dir,
        "status": "planned",
        "planned_at": _now(),
    }


def warmup(
    model: str, endpoint: str, timeout: float = WARMUP_TIMEOUT_SECONDS
) -> tuple[bool, float, str]:
    """Warm one already-installed model without invoking a model pull command."""
    payload = json.dumps(
        {
            "model": model,
            "prompt": "ping",
            "stream": False,
            "options": {"num_predict": 1},
        }
    ).encode("utf-8")
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


def warmup_openai(
    model: str, endpoint: str, timeout: float = WARMUP_TIMEOUT_SECONDS
) -> tuple[bool, float, str]:
    """Check a private OpenAI-compatible service without sending a campaign prompt."""
    request = urllib.request.Request(f"{endpoint.rstrip('/')}/models")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data", []) if isinstance(payload, Mapping) else []
        identifiers = {
            str(item.get("id"))
            for item in models
            if isinstance(item, Mapping) and item.get("id")
        }
        if model not in identifiers:
            return (
                False,
                time.monotonic() - started,
                f"served model ID {model!r} was not listed by {endpoint}/models",
            )
        return True, time.monotonic() - started, ""
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def _warm_required_models(
    required_roles: list[dict[str, str]], endpoint: str
) -> dict[str, dict[str, Any]]:
    roles_by_model: dict[tuple[str, str, str], list[str]] = {}
    for required_role in required_roles:
        model = required_role["model"]
        role = required_role["role"]
        role_endpoint = required_role.get("endpoint", endpoint)
        agent_type = required_role.get("agent_type", "OLLAMA")
        roles_by_model.setdefault((model, role_endpoint, agent_type), []).append(role)

    warmups: dict[str, dict[str, Any]] = {}
    for (model, role_endpoint, agent_type), roles in roles_by_model.items():
        is_openai = str(agent_type).upper() != "OLLAMA"
        warmup_fn = warmup_openai if is_openai else warmup
        ok, seconds, error = warmup_fn(model, role_endpoint)
        warmup_key = f"{agent_type}:{role_endpoint}:{model}"
        warmups[warmup_key] = {
            "model": model,
            "roles": roles,
            "endpoint": role_endpoint,
            "agent_type": agent_type,
            "ok": ok,
            "seconds": round(seconds, 3),
            "error": error,
        }
        print(
            f"warmup roles={','.join(roles)} model={model} "
            f"{'ok' if ok else 'FAILED'} {seconds:.1f}s",
            flush=True,
        )
        if not ok:
            break
    return warmups


def _warmup_seconds(warmups: dict[str, dict[str, Any]]) -> float | None:
    seconds = [
        value["seconds"]
        for value in warmups.values()
        if isinstance(value.get("seconds"), (int, float))
    ]
    return round(sum(seconds), 3) if seconds else None


def _copy_plan_metadata(record: dict[str, Any], planned: dict[str, Any]) -> None:
    for key in (
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
        "judge_key",
        "judge_model",
        "judge_keys",
        "judge_models",
        "attack",
        "attacker_applicability",
        "case_set_id",
        "case_ids",
        "goal_ids",
        "goal_batch_id",
        "category_id",
        "subcategory_id",
        "manifest_sha256",
        "repetition",
        "config_hash",
        "output_dir",
    ):
        if key in planned:
            record[key] = planned[key]


def _record_warmups(
    record: dict[str, Any], warmups: dict[str, dict[str, Any]]
) -> None:
    record["warmups"] = warmups
    record["warmup_seconds"] = _warmup_seconds(warmups)


# ---------------------------------------------------------------------------
# One-cell execution lifecycle
# ---------------------------------------------------------------------------
def _results_module():
    try:
        from . import results
    except ImportError:  # Allow the documented path-based CLI invocation.
        import results  # type: ignore[no-redef]

    return results


def run_one(
    run: dict[str, object], shard_context: ShardContext | None = None
) -> dict[str, Any]:
    """Execute one logical cell and return its planned/harvested records.

    The lifecycle is intentionally linear and observable:

    1. materialize the adapter-specific configuration and required roles;
    2. append the plan before any warmup or model call;
    3. warm each unique physical model once;
    4. construct HackAgent and execute the technique;
    5. harvest correlated SQLite evidence read-only, even after execution error;
    6. append one harvest record and regenerate normalized CSVs.

    The returned record describes one physical attempt.  Matrix continuation
    decides whether another physical attempt should be made for the same
    logical cell.
    """
    if shard_context is not None:
        owned = _owned_cells([run], shard_context)
        if not owned:
            raise ValueError(
                f"cell {run.get('cell_id')!r} is not owned by {shard_context.shard_id}"
            )
        run = owned[0]
    results = _results_module()
    experiment_run_id = f"campaign-{uuid.uuid4()}"
    if shard_context is None:
        output_dir = create_correlation_string(CAMPAIGN_NAME, experiment_run_id)
    else:
        output_dir = str((shard_context.shard_dir / "runs" / experiment_run_id).resolve())
    require_vllm = any(
        isinstance(model, Mapping)
        and attack_adapters.serving_framework(model) == "vllm"
        for role in ("target", "attacker", "judge")
        for model in (run.get(role),)
    )
    endpoints = _role_endpoints(require_vllm=require_vllm)

    # The adapter owns attack-specific field names and role contracts.  The
    # runner only consumes the uniform resolved-config/required-role interface.
    resolved_attack_config = attack_adapters.build_attack_config(
        run, output_dir, endpoints
    )
    required_roles = attack_adapters.required_roles(
        run, resolved_attack_config, endpoints
    )
    model_ids = _model_ids(required_roles)
    target_config = copy.deepcopy(TARGET_CONFIG)
    run_config = copy.deepcopy(RUN_CONFIG)
    config_hash = _config_hash(
        run,
        resolved_attack_config,
        target_config,
        run_config,
        endpoints,
    )
    planned = _planned_record(
        run,
        experiment_run_id,
        output_dir,
        resolved_attack_config,
        target_config,
        run_config,
        config_hash,
        model_ids,
        shard_context,
    )

    planned_path, harvested_path, runs_csv_path, results_csv_path = _output_paths(
        shard_context
    )

    # This append is the durable statement that the attempt was intended.  It
    # must happen before creating the run directory, warming a model, or
    # constructing HackAgent so a hard process kill still leaves a visible plan.
    results.append_planned_run(planned_path, planned)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    warmups: dict[str, dict[str, Any]] = {}
    try:
        # Warmup is diagnostic only.  It checks already-installed model access;
        # it is not a goal result and is excluded from ASR and run duration.
        if os.environ.get("HACKAGENT_SKIP_CAMPAIGN_WARMUP") == "1":
            for role in required_roles:
                warmup_key = (
                    f"{role.get('agent_type', 'OLLAMA')}:{role.get('endpoint', endpoints['ollama'])}:"
                    f"{role['model']}"
                )
                entry = warmups.setdefault(
                    warmup_key,
                    {
                        "model": role["model"],
                        "roles": [],
                        "endpoint": role.get("endpoint", endpoints["ollama"]),
                        "agent_type": role.get("agent_type", "OLLAMA"),
                        "ok": True,
                        "seconds": 0.0,
                        "error": "",
                        "skipped": True,
                    },
                )
                entry["roles"].append(role["role"])
            print("campaign warmup skipped by shared-GPU launcher", flush=True)
        else:
            warmups = _warm_required_models(required_roles, endpoints["ollama"])
        failed_warmup = next(
            (value for value in warmups.values() if not value["ok"]), None
        )
        if failed_warmup is not None:
            raise RuntimeError(
                f"{','.join(failed_warmup['roles'])} warmup failed for "
                f"{failed_warmup['model']}: {failed_warmup['error']}"
            )
    except Exception as exc:
        failure_record = results.build_error_record(
            experiment_run_id,
            "warmup",
            exc,
            traceback_text=traceback.format_exc(),
        )
        failure_record["status"] = "blocked"
        _copy_plan_metadata(failure_record, planned)
        _record_warmups(failure_record, warmups)
        results.append_harvested_run(harvested_path, failure_record)
        csv_rows = results.flatten_jsonl(
            planned_path,
            harvested_path,
            runs_csv_path,
            results_csv_path,
        )
        return {
            "planned": planned,
            "harvested": failure_record,
            "csv_rows": csv_rows,
        }

    # Disable HackAgent's automatic Ollama pulls at the environment boundary
    # instead of leaking an orchestration option into strict technique
    # configuration.  Assignment here intentionally overrides a caller's
    # value for the remainder of this process.
    os.environ["HACKAGENT_AUTO_PULL_MODELS"] = "0"
    execution_failure: dict[str, Any] | None = None
    backend = None
    database_path = _shard_database_path(shard_context)
    try:
        # HackAgent construction is separated from hack() so failures identify
        # whether the target agent could be built or the attack itself failed.
        from hackagent import HackAgent

        target_model = model_ids["target_model"]
        agent_kwargs: dict[str, Any] = {
            "name": target_model,
            "endpoint": endpoints["ollama"],
            "agent_type": "OLLAMA",
            "target_config": target_config,
            "thinking": target_config.get("thinking"),
        }
        if shard_context is not None:
            from hackagent import LocalBackend

            shard_context.shard_dir.mkdir(parents=True, exist_ok=True)
            os.environ["HACKAGENT_DB"] = str(database_path)
            backend = LocalBackend(db_path=str(database_path))
            agent_kwargs["backend"] = backend
        agent = HackAgent(**agent_kwargs)
    except Exception as exc:
        execution_failure = results.build_error_record(
            experiment_run_id,
            "hackagent_construction",
            exc,
            traceback_text=traceback.format_exc(),
        )["failure"]
    else:
        try:
            # The adapter-resolved configuration is passed unchanged.  The
            # runner does not add technique-specific keys at this point.
            agent.hack(
                attack_config=resolved_attack_config,
                run_config_override=run_config,
                fail_on_run_error=True,
            )
        except Exception as exc:
            execution_failure = results.build_error_record(
                experiment_run_id,
                "hackagent_execution",
                exc,
                traceback_text=traceback.format_exc(),
            )["failure"]

    # Harvest after both success and failure.  A failed HackAgent call may have
    # persisted a partial run, and results.py can preserve that evidence while
    # also retaining the campaign-side execution exception below.
    try:
        harvested = results.harvest_run(
            experiment_run_id,
            database_path,
            goal_to_case_mapping={
                goal["goal"]: {
                    "case_id": goal["goal_id"],
                    "intent_id": goal["goal_id"],
                    "goal_id": goal["goal_id"],
                    "goal_batch_id": goal["goal_batch_id"],
                    "category_id": goal["category_id"],
                    "subcategory_id": goal["subcategory_id"],
                }
                for goal in load_frozen_cases()["goals"]
            },
            planned_record=planned,
            warmups=warmups,
            harvested_path=None,
        )
        if execution_failure is not None:
            if harvested.get("failure") is not None:
                harvested["harvest_failure"] = harvested["failure"]
            harvested["failure"] = execution_failure
            harvested["status"] = "crashed"
        results.append_harvested_run(harvested_path, harvested)
        csv_rows = results.flatten_jsonl(
            planned_path,
            harvested_path,
            runs_csv_path,
            results_csv_path,
        )
    finally:
        if backend is not None:
            backend.close()
    return {"planned": planned, "harvested": harvested, "csv_rows": csv_rows}


def _unresolved_model(run: Mapping[str, Any]) -> str | None:
    for role in ("target", "attacker", "judge"):
        model = run.get(role)
        if isinstance(model, Mapping):
            try:
                attack_adapters.model_identifier(model, role)
            except ValueError as exc:
                return str(exc)
    return None


# ---------------------------------------------------------------------------
# Matrix continuation
# ---------------------------------------------------------------------------
def _execute_matrix(
    cells: list[dict[str, object]],
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    retry_incomplete: bool = False,
    shard_context: ShardContext | None = None,
) -> list[str]:
    """Execute a resumable matrix slice, isolating ordinary cell failures.

    Filtering happens first, then resume state removes already-addressed cells,
    and only then does ``limit`` cap new work.  This ordering matters for safe
    bounded continuation: a limit describes fresh eligible actions, not the
    first N configured cells.

    W9-R deliberately keeps unresolved-model preflight non-durable.  Failures
    after run_one() has written a plan are durable through its normal lifecycle;
    a cell blocked here is printed and counted but does not create a second
    preflight ledger or synthetic harvested record.
    """
    if not cells:
        raise ValueError("matrix selectors matched zero active cells")
    if limit is not None and limit < 0:
        raise ValueError("matrix execution --limit must be non-negative")

    states = _resume_index(shard_context)
    pending: list[dict[str, object]] = []
    statuses: list[str] = []
    for run in cells:
        state = states.get(_cell_key(run), "new")
        if state == "completed":
            statuses.append("skipped:completed")
        elif state == "failed" and not retry_failed:
            statuses.append("skipped:failed")
        elif state == "incomplete" and not retry_incomplete:
            statuses.append("skipped:incomplete")
        else:
            pending.append(run)

    selected = pending if limit is None else pending[:limit]
    skipped_for_limit = len(pending) - len(selected)

    print(
        f"matrix cells={len(cells)} pending={len(pending)} "
        f"actions={len(selected)} skipped_limit={skipped_for_limit}",
        flush=True,
    )

    statuses.extend("skipped:limit" for _ in range(skipped_for_limit))
    for run in selected:
        print(f"\nCell {_cell_text(run)}", flush=True)
        unresolved = _unresolved_model(run)
        if unresolved is not None:
            print(f"cell status: blocked ({unresolved})", flush=True)
            statuses.append("blocked")
            continue
        try:
            record = run_one(run, shard_context)
        except Exception as exc:
            status = "crashed"
            print(
                f"cell status: {status} ({type(exc).__name__}: {exc})",
                flush=True,
            )
        else:
            harvested = record.get("harvested")
            status = (
                str(harvested.get("status") or "crashed").lower()
                if isinstance(harvested, Mapping)
                else "crashed"
            )
            if status == "completed" and harvested.get("failure") is not None:
                status = "crashed"
            print(f"cell status: {status}", flush=True)
        statuses.append(status)

    results = _results_module()
    planned_path, harvested_path, runs_csv_path, results_csv_path = _output_paths(
        shard_context
    )
    if planned_path.exists() and harvested_path.exists():
        csv_rows = results.flatten_jsonl(
            planned_path,
            harvested_path,
            runs_csv_path,
            results_csv_path,
        )
        print(f"matrix CSV rows: runs={csv_rows[0]} results={csv_rows[1]}", flush=True)
    return statuses


def run_matrix(
    target_filter: set[str] | None = None,
    attacker_filter: set[str] | None = None,
    attack_filter: set[str] | None = None,
    repetition_filter: set[int] | None = None,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    retry_incomplete: bool = False,
    shard_context: ShardContext | None = None,
) -> list[str]:
    """Execute the active matrix or a filtered, resumable matrix slice."""
    validate_configuration()
    selected = select_matrix_cells(
        target_filter=target_filter,
        attacker_filter=attacker_filter,
        attack_filter=attack_filter,
        repetition_filter=repetition_filter,
    )
    if shard_context is not None:
        selected = _owned_cells(selected, shard_context)
    return _execute_matrix(
        selected,
        limit=limit,
        retry_failed=retry_failed,
        retry_incomplete=retry_incomplete,
        shard_context=shard_context,
    )


# ---------------------------------------------------------------------------
# Preview and command-line interface
# ---------------------------------------------------------------------------
def print_preview(
    limit: int = 5, shard_context: ShardContext | None = None
) -> None:
    """Print the active definition without contacting models or writing state."""
    unresolved = validate_configuration()
    run_descriptions = build_run_descriptions()
    if shard_context is not None:
        run_descriptions = _owned_cells(run_descriptions, shard_context)
    attacker_driven_cells = sum(
        1
        for run in run_descriptions
        if run["attacker_applicability"] == "driven"
    )
    non_attacker_driven_cells = len(run_descriptions) - attacker_driven_cells

    print(f"campaign: {CAMPAIGN_NAME}")
    print(f"active targets: {len(targets)}")
    for target in targets:
        print(f"  {target['key']} ({target.get('label', target['key'])}): {_model_text(target)}")
    print(f"active attackers: {len(attackers)}")
    print(f"active judges: {len(judges)}")
    judge = judges[0]
    print(
        f"active judge: {judge['key']} ({judge.get('label', judge['key'])})"
        f" / {_model_text(judge)}"
    )
    print(f"active attacks: {len(attacks)}")
    print(f"goals: {sum(len(batch) for batch in cases_by_batch(load_frozen_cases()).values())}")
    print(f"repetitions: {REPETITIONS}")
    print(f"attacker-driven cells: {attacker_driven_cells}")
    print(f"non-attacker-driven cells: {non_attacker_driven_cells}")
    print(f"total cells: {len(run_descriptions)}")
    if shard_context is not None:
        print(f"shard: {shard_context.shard_id}")

    if unresolved:
        print("unresolved active models:")
        for model in unresolved:
            print(f"  {model['kind']} {model['key']} ({model['label']})")

    if limit > 0:
        print(f"first cells (limit {limit}):")
        for run in run_descriptions[:limit]:
            target = run["target"]
            attack = run["attack"]
            attacker = run["attacker"]
            attacker_text = "not applicable"
            if attacker is not None:
                attacker_text = f"{attacker['key']} / {_model_text(attacker)}"
            print(
                f"  target={target['key']} attack={attack['name']} "
                f"attacker={attacker_text} repetition={run['repetition']}"
            )

    # Resolve one preview cell through the same adapter contract used by live
    # execution.  This is offline and uses a placeholder vLLM endpoint when a
    # worker-local VLLM_ENDPOINT has not been exported yet.
    if run_descriptions:
        preview_endpoints = _role_endpoints()
        resolved = attack_adapters.build_attack_config(
            run_descriptions[0], "preview://campaign", preview_endpoints
        )
        roles = attack_adapters.required_roles(
            run_descriptions[0], resolved, preview_endpoints
        )
        print("resolved role endpoints:")
        for role in roles:
            print(
                f"  {role['role']}: {role['model']} / {role['agent_type']} / "
                f"{role['endpoint']}"
            )


def main() -> int:
    """Parse the safety boundary between preview, one-cell, and matrix modes."""
    parser = argparse.ArgumentParser(
        description="Preview or execute configured campaign matrix cells."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the matrix without executing any attacks (the default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="execute one selected cell or a matrix slice",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="execute the filtered matrix slice instead of one cell",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="active target key; repeat for matrix filtering",
    )
    parser.add_argument(
        "--attacker",
        action="append",
        help="active attacker key; repeat for matrix filtering",
    )
    parser.add_argument(
        "--attack",
        action="append",
        help="active attack name; repeat for matrix filtering",
    )
    parser.add_argument(
        "--repetition",
        action="append",
        type=int,
        help="repetition number; repeat for matrix filtering",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="preview rows, or maximum eligible matrix cells to execute",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry matrix cells with failed, blocked, or uncorrelated harvests",
    )
    parser.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="retry matrix cells whose planned records have no harvest",
    )
    parser.add_argument(
        "--shard-manifest",
        metavar="PATH",
        help="validated immutable shard manifest",
    )
    parser.add_argument(
        "--shard-id",
        metavar="SHARD_ID",
        help="one shard ID from --shard-manifest",
    )
    args = parser.parse_args()
    if (args.shard_manifest is None) != (args.shard_id is None):
        parser.error("--shard-manifest and --shard-id must be supplied together")
    shard_context = None
    if args.shard_manifest is not None:
        try:
            shard_context = load_shard_context(args.shard_manifest, args.shard_id)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.matrix and not args.execute:
        parser.error("--matrix requires --execute")
    if not args.execute:
        if args.retry_failed or args.retry_incomplete:
            parser.error("retry options require --execute --matrix")
        print_preview(
            limit=5 if args.limit is None else args.limit,
            shard_context=shard_context,
        )
        return 0

    if (args.retry_failed or args.retry_incomplete) and not args.matrix:
        parser.error("retry options require --matrix")
    if shard_context is not None and not args.matrix:
        parser.error("sharded execution requires --matrix")
    if args.matrix:
        try:
            summaries = run_matrix(
                target_filter=set(args.target) if args.target else None,
                attacker_filter=set(args.attacker) if args.attacker else None,
                attack_filter=set(args.attack) if args.attack else None,
                repetition_filter=(set(args.repetition) if args.repetition else None),
                limit=args.limit,
                retry_failed=args.retry_failed,
                retry_incomplete=args.retry_incomplete,
                shard_context=shard_context,
            )
        except ValueError as exc:
            parser.error(str(exc))

        attempted = [status for status in summaries if not status.startswith("skipped:")]
        attempted_failures = [status for status in attempted if status != "completed"]
        print(
            f"matrix attempted={len(attempted)} failures={len(attempted_failures)} "
            f"skipped={len(summaries) - len(attempted)}",
            flush=True,
        )
        return 1 if attempted_failures else 0

    missing = [
        name
        for name in ("target", "attack", "repetition")
        if getattr(args, name) is None
    ]
    for name in ("target", "attacker", "attack", "repetition"):
        value = getattr(args, name)
        if value is not None and len(value) != 1:
            parser.error(f"one-cell --execute accepts exactly one --{name}")
    if missing:
        parser.error(
            "--execute requires "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )
    if args.repetition[0] < 1:
        parser.error("--repetition must be at least 1")

    validate_configuration()
    try:
        selected = _find_selected_cell(
            args.target[0],
            args.attacker[0] if args.attacker else None,
            args.attack[0],
            args.repetition[0],
        )
        if shard_context is not None:
            owned = _owned_cells([selected], shard_context)
            if not owned:
                parser.error("selected cell is not owned by the requested shard")
            selected = owned[0]
        record = run_one(selected, shard_context)
    except ValueError as exc:
        parser.error(str(exc))

    harvested = record["harvested"]
    print(
        f"experiment_run_id: {record['planned']['experiment_run_id']}\n"
        f"status: {harvested.get('status')}\n"
        f"hackagent_run_id: {harvested.get('hackagent_run_id')}\n"
        f"result_count: {harvested.get('result_count')}\n"
        f"trace_count: {harvested.get('trace_count')}\n"
        f"output_dir: {record['planned']['output_dir']}\n"
        f"csv_rows: runs={record['csv_rows'][0]} results={record['csv_rows'][1]}"
    )
    return 0 if harvested.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
