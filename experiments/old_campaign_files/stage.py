"""Resolve and optionally stage the local Ollama models named by a campaign."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import manifest as campaign_manifest  # noqa: E402


def required_model_ids(manifest: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for model_id in campaign_manifest._required_model_ids(manifest):
        if model_id not in ids:
            ids.append(model_id)
    return ids


def _aliases(model_id: str) -> set[str]:
    aliases = {model_id}
    if ":" not in model_id:
        aliases.add(f"{model_id}:latest")
    elif model_id.endswith(":latest"):
        aliases.add(model_id.removesuffix(":latest"))
    return aliases


def installed_models() -> set[str]:
    if shutil.which("ollama") is None:
        raise RuntimeError("ollama is not installed or is not in PATH")
    result = subprocess.run(
        ["ollama", "list"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ollama list failed")
    models: set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if fields:
            models.add(fields[0])
    return models


def stage(
    manifest_path: Path,
    *,
    pull: bool = False,
    output_path: Optional[Path] = None,
) -> dict[str, Any]:
    manifest = campaign_manifest.load_manifest(manifest_path)
    models = campaign_manifest.load_models(manifest, manifest_path)
    campaign_manifest.validate_manifest(manifest, models)
    installed = installed_models()
    records: list[dict[str, Any]] = []

    for model_id in required_model_ids(manifest):
        model = models[model_id]
        local = model.execution_mode == "ollama-local"
        present = bool(_aliases(model_id) & installed) if local else False
        action = "cloud-only" if not local else ("present" if present else "missing")
        if local and not present and pull:
            print(f"pulling {model_id}", flush=True)
            result = subprocess.run(["ollama", "pull", model_id], check=False)
            if result.returncode != 0:
                action = "pull-failed"
            else:
                action = "pulled"
                installed = installed_models()
                present = bool(_aliases(model_id) & installed)
        records.append(
            {
                "model_id": model_id,
                "role": model.role,
                "execution_mode": model.execution_mode,
                "revision": model.revision,
                "local_present": present,
                "action": action,
                "metadata": model.metadata,
            }
        )
        print(f"{action:12} {model_id}", flush=True)

    report = {
        "manifest": str(manifest_path),
        "campaign_id": manifest["campaign_id"],
        "endpoint": os.environ.get("OLLAMA_HOST", "127.0.0.1:11435"),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pull_requested": pull,
        "models": records,
    }
    destination = output_path or PROJECT / "logs" / "campaign" / "staging.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"staging report: {destination}", flush=True)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pull", action="store_true", help="pull missing local models")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = stage(args.manifest.resolve(), pull=args.pull, output_path=args.output)
    return 1 if any(item["action"] == "pull-failed" for item in report["models"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
