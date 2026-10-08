"""Load and expand a campaign manifest into independent run specifications."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Model:
    model_id: str
    role: str
    revision: Optional[str]
    execution_mode: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class RunSpec:
    campaign_id: str
    case_set_id: str
    target: Model
    attacker: Optional[Model]
    judge: Model
    attack: str
    attack_config: Dict[str, Any]
    case_ids: list[str]
    goals: list[str]
    repetition: int


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open() as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def load_manifest(path: Path) -> Dict[str, Any]:
    manifest = _read_json(path)
    if manifest.get("serving_framework") != "ollama":
        raise ValueError("The campaign currently supports only serving_framework='ollama'")
    return manifest


def load_models(manifest: Dict[str, Any], manifest_path: Path) -> Dict[str, Model]:
    models_path = (manifest_path.parent / manifest["models_file"]).resolve()
    inventory = _read_json(models_path)
    if inventory.get("serving_framework") != "ollama":
        raise ValueError("The model inventory must use serving_framework='ollama'")

    models: Dict[str, Model] = {}
    for raw in inventory.get("models", []):
        if not isinstance(raw, dict) or not raw.get("model_id"):
            continue
        model_id = str(raw["model_id"])
        models[model_id] = Model(
            model_id=model_id,
            role=str(raw.get("role") or "other"),
            revision=raw.get("revision"),
            execution_mode=str(raw.get("execution_mode") or "ollama-local"),
            metadata=copy.deepcopy(raw),
        )
    return models


def load_cases(manifest: Dict[str, Any], manifest_path: Path) -> tuple[str, list[str], list[str]]:
    cases_path = (manifest_path.parent / manifest["cases_file"]).resolve()
    cases = _read_json(cases_path)
    case_set_id = str(cases.get("case_set_id") or "unknown")
    requested_id = str(manifest.get("cases", {}).get("case_set_id") or case_set_id)
    if requested_id != case_set_id:
        raise ValueError(
            f"Manifest requests case set {requested_id!r}, but {cases_path} is {case_set_id!r}"
        )

    raw_cases = cases.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"No cases found in {cases_path}")
    selected = raw_cases[: int(manifest.get("cases", {}).get("case_count", len(raw_cases)))]
    case_ids = [str(case["case_id"]) for case in selected]
    goals = [str(case["goal"]) for case in selected]
    return case_set_id, case_ids, goals


def _required_model_ids(manifest: Dict[str, Any]) -> set[str]:
    required = set(manifest.get("target_ids", []))
    required.update(manifest.get("attacker_ids", []))
    if manifest.get("judge_id"):
        required.add(manifest["judge_id"])
    for attack in manifest.get("attacks", []):
        config = attack.get("config", {}) if isinstance(attack, dict) else {}
        if attack.get("name") == "autodan_turbo":
            # The campaign keeps the embedder explicit in the inventory even though
            # it is not a target/attacker comparison factor.
            required.add(str(config.get("embedder_model", "embeddinggemma:300m")))
    return required


def validate_manifest(manifest: Dict[str, Any], models: Dict[str, Model]) -> None:
    required = _required_model_ids(manifest)
    missing = sorted(model_id for model_id in required if model_id not in models)
    if missing:
        raise ValueError(f"Models missing from inventory: {', '.join(missing)}")
    if not manifest.get("target_ids"):
        raise ValueError("Manifest target_ids must not be empty")
    if not manifest.get("attacker_ids"):
        raise ValueError("Manifest attacker_ids must not be empty")
    if not manifest.get("attacks"):
        raise ValueError("Manifest attacks must not be empty")
    configured_attacks = {name for name, _ in attack_specs(manifest)}
    unknown_attacks = configured_attacks - {
        "baseline",
        "static_template",
        "flipattack",
        "cipherchat",
        "bon",
        "h4rm3l",
        "mml",
        "pair",
        "tap",
        "crescendo",
        "pap",
        "autodan_turbo",
        "advprefix",
    }
    if unknown_attacks:
        raise ValueError(f"Unsupported campaign attacks: {', '.join(sorted(unknown_attacks))}")
    if int(manifest.get("repetitions", 0)) < 1:
        raise ValueError("Manifest repetitions must be at least 1")


def attack_specs(manifest: Dict[str, Any]) -> Iterable[tuple[str, Dict[str, Any]]]:
    for raw in manifest.get("attacks", []):
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError("Each manifest attack must contain a name")
        yield str(raw["name"]), copy.deepcopy(raw.get("config") or {})


def expand_runs(
    manifest: Dict[str, Any],
    models: Dict[str, Model],
    *,
    case_set_id: str,
    case_ids: list[str],
    goals: list[str],
    targets: Optional[set[str]] = None,
    attackers: Optional[set[str]] = None,
    attacks: Optional[set[str]] = None,
    repetitions: Optional[set[int]] = None,
) -> list[RunSpec]:
    """Expand the Cartesian product while avoiding irrelevant transform duplicates."""
    selected_targets = [
        models[model_id]
        for model_id in manifest["target_ids"]
        if targets is None or model_id in targets
    ]
    selected_attackers = [
        models[model_id]
        for model_id in manifest["attacker_ids"]
        if attackers is None or model_id in attackers
    ]
    judge = models[manifest["judge_id"]]
    repetitions_count = int(manifest["repetitions"])
    selected_repetitions = repetitions or set(range(1, repetitions_count + 1))

    specs: list[RunSpec] = []
    for target in selected_targets:
        for attack, config in attack_specs(manifest):
            if attacks is not None and attack not in attacks:
                continue
            # The registry marks attacker-driven methods. The runner applies this
            # distinction; keeping transforms to one row avoids fake attacker wins.
            attacker_driven = attack in {
                "pair",
                "tap",
                "crescendo",
                "pap",
                "autodan_turbo",
                "advprefix",
            }
            candidates = selected_attackers if attacker_driven else [None]
            for attacker in candidates:
                for repetition in sorted(selected_repetitions):
                    if repetition < 1 or repetition > repetitions_count:
                        raise ValueError(f"Invalid repetition {repetition}")
                    specs.append(
                        RunSpec(
                            campaign_id=str(manifest["campaign_id"]),
                            case_set_id=case_set_id,
                            target=target,
                            attacker=attacker,
                            judge=judge,
                            attack=attack,
                            attack_config=copy.deepcopy(config),
                            case_ids=list(case_ids),
                            goals=list(goals),
                            repetition=repetition,
                        )
                    )
    return specs


def config_hash(config: Dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
