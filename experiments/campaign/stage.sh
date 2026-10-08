#!/usr/bin/env bash
set -euo pipefail

# W10 login-node staging boundary.
#
# This wrapper resolves the selected matrix slice through the existing Python
# configuration and adapter contract, checks exact Ollama identities, and emits
# a report.  It is report-only by default; only --pull may invoke ollama pull.
# The Python campaign runner is still the owner of execution and resume state.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PULL=0
OUTPUT_PATH=""
SELECTOR_ARGS=()

# ---------------------------------------------------------------------------
# Shell argument parsing
# ---------------------------------------------------------------------------
usage() {
    printf 'Usage: %s [--pull] [--output PATH] [matrix selectors]\n' "$0" >&2
    printf '  --pull       pull missing resolved models before rechecking presence\n' >&2
    printf '  --output     write the JSON staging report to PATH\n' >&2
    printf '  --target KEY repeatable target selector\n' >&2
    printf '  --attacker KEY repeatable attacker selector\n' >&2
    printf '  --attack NAME repeatable attack selector\n' >&2
    printf '  --repetition N repeatable repetition selector\n' >&2
    printf '  --limit N    inspect only the first N selected cells\n' >&2
    printf '  MML requires HACKAGENT_MML_CAPABILITY_VERIFIED=1\n' >&2
}

require_value() {
    if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        usage
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --pull)
            PULL=1
            shift
            ;;
        --output)
            require_value "$@"
            OUTPUT_PATH="$2"
            shift 2
            ;;
        --target|--attacker|--attack|--repetition|--limit)
            require_value "$@"
            SELECTOR_ARGS+=("$1" "$2")
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown staging option: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
done

# Keep staging's environment explicit and local to this process.  In
# particular, OLLAMA_MODELS identifies the already-prepared model store; it is
# not a reason to pull models unless the operator supplied --pull.
export PYTHONDONTWRITEBYTECODE=1
export STAGE_PULL="$PULL"
export STAGE_OUTPUT_PATH="$OUTPUT_PATH"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11435}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/models}"

cd "$PROJECT_DIR"

# The inline Python code deliberately reuses campaign.py's selectors and
# attack_adapters.py's role contract instead of duplicating attack-specific
# staging logic in this shell wrapper.
uv run --no-sync python - "${SELECTOR_ARGS[@]}" <<'PY'
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments.campaign import attack_adapters, campaign


def endpoint_for(host: str) -> str:
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    return f"http://{host.rstrip('/')}"


def cell_metadata(run: dict[str, Any]) -> dict[str, Any]:
    target = run.get("target") or {}
    attacker = run.get("attacker") or {}
    judge = run.get("judge") or {}
    attack = run.get("attack") or {}
    return {
        "case_set_id": campaign.CASE_SET_ID,
        "goal_batch_id": run.get("goal_batch_id"),
        "category_id": run.get("category_id"),
        "subcategory_id": run.get("subcategory_id"),
        "goal_ids": run.get("goal_ids", []),
        "manifest_sha256": run.get("manifest_sha256"),
        "target_key": target.get("key"),
        "attacker_key": attacker.get("key") if attacker else None,
        "judge_key": judge.get("key"),
        "attack": attack.get("name"),
        "repetition": run.get("repetition"),
    }


def model_entries(model_id: str) -> list[dict[str, Any]]:
    entries = []
    for kind, models in (
        ("target", campaign.targets),
        ("attacker", campaign.attackers),
        ("judge", campaign.judges),
    ):
        for model in models:
            if model.get("ollama_model") == model_id:
                entries.append(
                    {
                        "kind": kind,
                        "key": model.get("key"),
                        "label": model.get("label"),
                        "ollama_model": model.get("ollama_model"),
                        "hf_repo": model.get("hf_repo"),
                        "revision": model.get("revision"),
                        "use": model.get("use"),
                    }
                )
    return entries


def run_ollama(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["ollama", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
    except OSError as exc:
        raise RuntimeError(f"could not run ollama {' '.join(arguments)}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"ollama {' '.join(arguments)} failed with exit {completed.returncode}: "
            f"{detail}"
        )
    return completed.stdout


def listed_models(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        fields = line.strip().split()
        if fields and fields[0] not in {"NAME", "name"}:
            names.add(fields[0])
    return names


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


parser = argparse.ArgumentParser(description="Inspect and optionally stage campaign models.")
parser.add_argument("--target", action="append")
parser.add_argument("--attacker", action="append")
parser.add_argument("--attack", action="append")
parser.add_argument("--repetition", action="append", type=int)
parser.add_argument("--limit", type=int)
args = parser.parse_args()

# These filters are reported for auditability.  --limit is intentionally used
# only to limit inspected role rows here; submit_sharded.sh forwards its
# execution limit separately so the runner applies it after resume-state filtering.
filters = {
    "target": args.target or [],
    "attacker": args.attacker or [],
    "attack": args.attack or [],
    "repetition": args.repetition or [],
}
pull = os.environ.get("STAGE_PULL") == "1"
host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11435")
models_path = Path(os.environ.get("OLLAMA_MODELS", str(Path.home() / "models"))).expanduser()
endpoint = endpoint_for(host)
output_path = os.environ.get("STAGE_OUTPUT_PATH")

campaign.validate_configuration()
if args.limit is not None and args.limit < 0:
    raise SystemExit("--limit must be non-negative")

try:
    selected = campaign.select_matrix_cells(
        target_filter=set(args.target) if args.target else None,
        attacker_filter=set(args.attacker) if args.attacker else None,
        attack_filter=set(args.attack) if args.attack else None,
        repetition_filter=set(args.repetition) if args.repetition else None,
    )
except ValueError as exc:
    raise SystemExit(str(exc)) from exc
if not selected:
    raise SystemExit("matrix selectors matched zero active cells")

# Resolve every selected cell's required roles before presence checks.  This is
# what catches unresolved active models and auxiliary roles such as AutoDAN's
# audited embedder without teaching the staging layer each attack's schema.
inspected = selected if args.limit is None else selected[: args.limit]
report: dict[str, Any] = {
    "campaign_name": campaign.CAMPAIGN_NAME,
    "selected_filters": filters,
    "selected_cell_count": len(selected),
    "inspected_cell_count": len(inspected),
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "pull_requested": pull,
    "endpoint": endpoint,
    "ollama_host": host,
    "ollama_models": str(models_path),
    "model_store": {
        "path": str(models_path),
        "exists": models_path.is_dir(),
        "blobs": (models_path / "blobs").is_dir(),
        "manifests": (models_path / "manifests").is_dir(),
    },
    "cells": [],
    "models": {},
    "unresolved": [],
    "errors": [],
}
report["mml_capability_verified"] = os.environ.get(
    "HACKAGENT_MML_CAPABILITY_VERIFIED"
) == "1"
report["model_store"]["complete"] = all(
    report["model_store"][key] for key in ("exists", "blobs", "manifests")
)
report["limit_scope"] = "role inspection only; runner --limit is forwarded unchanged"

for run in inspected:
    cell = cell_metadata(run)
    cell["roles"] = []
    if (
        cell["attack"] == "mml"
        and not report["mml_capability_verified"]
    ):
        report["errors"].append(
            {
                "phase": "capability",
                "type": "UnverifiedImageInputCapability",
                "message": (
                    "MML target image-input capability requires explicit operator "
                    "acknowledgment via HACKAGENT_MML_CAPABILITY_VERIFIED=1"
                ),
                "cell": cell,
            }
        )
    try:
        staging_output = (
            f"staging://{campaign.CAMPAIGN_NAME}/{len(report['cells']) + 1}"
        )
        resolved = attack_adapters.build_attack_config(run, staging_output, endpoint)
        required_roles = attack_adapters.required_roles(run, resolved)
        for required_role in required_roles:
            role = required_role.get("role")
            model_id = required_role.get("model")
            if not role or not model_id:
                report["unresolved"].append(
                    {
                        "role": role,
                        "model": model_id,
                        "cell": cell,
                    }
                )
                raise ValueError("adapter returned an incomplete required role")
            role_cell = {"role": role, "model": model_id, **cell_metadata(run)}
            cell["roles"].append(role_cell)
            configured_entries = model_entries(model_id)
            model = report["models"].setdefault(
                model_id,
                {
                    "model": model_id,
                    "roles": [],
                    "configured_entries": configured_entries,
                    "auxiliary": not configured_entries,
                    "local_present": None,
                    "presence_error": None,
                    "pulled": False,
                },
            )
            model["roles"].append(role_cell)
    except Exception as exc:
        report["errors"].append(
            {
                "phase": "adapter",
                "cell": cell,
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
        for kind in ("target", "attacker", "judge"):
            entry = run.get(kind)
            if isinstance(entry, dict) and not entry.get("ollama_model"):
                report["unresolved"].append(
                    {
                        "role": kind,
                        "key": entry.get("key"),
                        "label": entry.get("label"),
                        "cell": cell,
                    }
                )
    report["cells"].append(cell)

required_model_ids = sorted(report["models"])
try:
    # Never substitute a family nickname or a different local tag.  Staging,
    # presence checks, and optional pulls all use the exact adapter/config IDs.
    if required_model_ids and not report["model_store"]["complete"]:
        raise RuntimeError(
            f"Ollama model store is missing or incomplete: {models_path}"
        )
    listed = listed_models(run_ollama("list")) if required_model_ids else set()
    missing = [model_id for model_id in required_model_ids if model_id not in listed]
    if pull and not report["errors"] and not report["unresolved"]:
        for model_id in missing:
            run_ollama("pull", model_id)
            report["models"][model_id]["pulled"] = True
        listed = listed_models(run_ollama("list")) if required_model_ids else set()
    for model_id in required_model_ids:
        present = model_id in listed
        report["models"][model_id]["local_present"] = present
        if not present:
            report["models"][model_id]["presence_error"] = (
                "exact configured model ID is not present in ollama list"
            )
            report["errors"].append(
                {
                    "phase": "presence",
                    "model": model_id,
                    "type": "MissingModel",
                    "message": "exact configured model ID is not present",
                }
            )
except Exception as exc:
    report["errors"].append(
        {
            "phase": "presence",
            "type": type(exc).__name__,
            "message": str(exc),
        }
    )

if not output_path:
    output = Path("logs/campaigns") / campaign.CAMPAIGN_NAME / "staging-report.json"
else:
    output = Path(output_path).expanduser()
protected = {
    Path("logs/campaigns")
    / campaign.CAMPAIGN_NAME
    / filename
    for filename in ("planned_runs.jsonl", "harvested_runs.jsonl", "runs.csv", "results.csv")
}
if output.resolve() in {path.resolve() for path in protected}:
    raise SystemExit("staging report cannot overwrite campaign JSONL or CSV records")
report["report_path"] = str(output)
report["status"] = "ready" if not report["errors"] and not report["unresolved"] else "blocked"
# A blocked report is written before returning nonzero so an operator can
# inspect the exact unresolved, missing, or capability-gated selection.
write_report(output, report)
print(json.dumps(report, indent=2, sort_keys=True))
if report["status"] != "ready":
    raise SystemExit(1)
PY
