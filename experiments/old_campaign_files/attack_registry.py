"""Small registry of the attack configurations used by the campaign runner."""

from __future__ import annotations

import copy
from typing import Any, Dict

from hackagent.attacks.techniques.advprefix.config import (
    DEFAULT_PREFIX_GENERATION_CONFIG,
)
from hackagent.attacks.techniques.autodan_turbo.config import (
    DEFAULT_AUTODAN_TURBO_CONFIG,
)
from hackagent.attacks.techniques.baseline.config import DEFAULT_BASELINE_CONFIG
from hackagent.attacks.techniques.bon.config import DEFAULT_BON_CONFIG
from hackagent.attacks.techniques.cipherchat.config import DEFAULT_CIPHERCHAT_CONFIG
from hackagent.attacks.techniques.config import DEFAULT_CONFIG_BASE
from hackagent.attacks.techniques.crescendo.config import DEFAULT_CRESCENDO_CONFIG
from hackagent.attacks.techniques.flipattack.config import DEFAULT_FLIPATTACK_CONFIG
from hackagent.attacks.techniques.h4rm3l.config import DEFAULT_H4RM3L_CONFIG
from hackagent.attacks.techniques.mml.config import DEFAULT_MML_CONFIG
from hackagent.attacks.techniques.pair.config import DEFAULT_PAIR_CONFIG
from hackagent.attacks.techniques.pap.config import DEFAULT_PAP_CONFIG
from hackagent.attacks.techniques.static_template.config import DEFAULT_TEMPLATE_CONFIG
from hackagent.attacks.techniques.tap.config import DEFAULT_TAP_CONFIG
from hackagent.router.types import AgentTypeEnum


def _deep_update(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _role(
    model: str,
    endpoint: str,
    *,
    agent_type: Any = "OLLAMA",
    **overrides: Any,
) -> Dict[str, Any]:
    role = {
        "identifier": model,
        "endpoint": endpoint,
        "agent_type": agent_type,
    }
    role.update(overrides)
    return role


def _set_role_model(
    config: Dict[str, Any],
    key: str,
    model: str,
    endpoint: str,
    *,
    agent_type: Any = "OLLAMA",
) -> None:
    current = config.get(key)
    role = dict(current) if isinstance(current, dict) else {}
    role.update(_role(model, endpoint, agent_type=agent_type))
    config[key] = role


def _set_judge(config: Dict[str, Any], model: str, endpoint: str) -> None:
    current = config.get("judge")
    judge = dict(current) if isinstance(current, dict) else {}
    judge.update(_role(model, endpoint, type=judge.get("type", "harmbench")))
    config["judge"] = judge

    current_judges = config.get("judges")
    if isinstance(current_judges, list) and current_judges:
        judges = []
        for current in current_judges:
            item = dict(current) if isinstance(current, dict) else {}
            item.update(_role(model, endpoint, type=item.get("type", "harmbench")))
            judges.append(item)
    else:
        judges = [dict(judge)]
    config["judges"] = judges


_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "baseline": {**copy.deepcopy(DEFAULT_CONFIG_BASE), **DEFAULT_BASELINE_CONFIG},
    "static_template": copy.deepcopy(DEFAULT_TEMPLATE_CONFIG),
    "flipattack": copy.deepcopy(DEFAULT_FLIPATTACK_CONFIG),
    "cipherchat": copy.deepcopy(DEFAULT_CIPHERCHAT_CONFIG),
    "bon": copy.deepcopy(DEFAULT_BON_CONFIG),
    "h4rm3l": copy.deepcopy(DEFAULT_H4RM3L_CONFIG),
    "mml": copy.deepcopy(DEFAULT_MML_CONFIG),
    "pair": copy.deepcopy(DEFAULT_PAIR_CONFIG),
    "tap": copy.deepcopy(DEFAULT_TAP_CONFIG),
    "crescendo": copy.deepcopy(DEFAULT_CRESCENDO_CONFIG),
    "pap": copy.deepcopy(DEFAULT_PAP_CONFIG),
    "autodan_turbo": copy.deepcopy(DEFAULT_AUTODAN_TURBO_CONFIG),
    "advprefix": copy.deepcopy(DEFAULT_PREFIX_GENERATION_CONFIG),
}


# This is deliberately data, not a plugin system. A new technique needs one registry
# entry and a small role/config branch in build_attack_config.
ATTACKS: Dict[str, Dict[str, Any]] = {
    "baseline": {"uses_attacker": False, "roles": ("judge",)},
    "static_template": {"uses_attacker": False, "roles": ("judge",)},
    "flipattack": {"uses_attacker": False, "roles": ("judge",)},
    "cipherchat": {"uses_attacker": False, "roles": ("judge",)},
    "bon": {"uses_attacker": False, "roles": ("judge",)},
    "h4rm3l": {"uses_attacker": False, "roles": ("judge",)},
    "mml": {"uses_attacker": False, "roles": ("judge",)},
    "pair": {"uses_attacker": True, "roles": ("attacker", "judge")},
    "tap": {"uses_attacker": True, "roles": ("attacker", "judge")},
    "crescendo": {"uses_attacker": True, "roles": ("attacker", "judge")},
    "pap": {"uses_attacker": True, "roles": ("attacker", "judge")},
    "autodan_turbo": {
        "uses_attacker": True,
        "roles": ("attacker", "judge", "summarizer", "embedder"),
    },
    "advprefix": {"uses_attacker": True, "roles": ("generator", "judge")},
}


def build_attack_config(
    attack: str,
    overrides: Dict[str, Any],
    *,
    target_model: str,
    attacker_model: str | None,
    judge_model: str,
    embedder_model: str,
    endpoint: str,
    output_dir: str,
    goals: list[str],
) -> Dict[str, Any]:
    """Return one fully materialized attack config for a matrix cell."""
    if attack not in ATTACKS:
        raise ValueError(f"Unknown campaign attack {attack!r}")
    if ATTACKS[attack]["uses_attacker"] and not attacker_model:
        raise ValueError(f"Attack {attack!r} requires an attacker model")

    config = copy.deepcopy(_DEFAULTS[attack])
    _deep_update(config, overrides)
    config["attack_type"] = attack
    config["goals"] = list(goals)
    config["output_dir"] = output_dir
    config["auto_pull_models"] = False

    if ATTACKS[attack]["uses_attacker"]:
        attacker_type: Any = AgentTypeEnum.OLLAMA if attack == "pap" else "OLLAMA"
        if attack == "advprefix":
            _set_role_model(
                config, "generator", attacker_model or "", endpoint,
                agent_type=attacker_type,
            )
        else:
            _set_role_model(
                config, "attacker", attacker_model or "", endpoint,
                agent_type=attacker_type,
            )
        if attack == "autodan_turbo":
            _set_role_model(config, "summarizer", judge_model, endpoint)
            _set_role_model(config, "embedder", embedder_model, endpoint)
    else:
        # Do not leave the default attacker in the serialized config for transforms.
        config.pop("attacker", None)

    _set_judge(config, judge_model, endpoint)
    if attack == "pap":
        config["judge"]["agent_type"] = AgentTypeEnum.OLLAMA
        config["judges"] = [
            {**judge, "agent_type": AgentTypeEnum.OLLAMA}
            for judge in config["judges"]
        ]
    if attack == "tap" and not isinstance(config.get("on_topic_judge"), dict):
        config["on_topic_judge"] = None
    if attack == "baseline":
        config["judge_config"] = None

    return config


def attack_names() -> list[str]:
    return list(ATTACKS)
