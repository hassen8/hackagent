"""Frozen benchmark case set loader.

The case list in ``cases.json`` is the single frozen input shared by every attacker,
attack, and repetition in the study. Comparable ASR denominators depend on it not
changing between runs.

The frozen literal goal text in ``cases.json`` is the source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.json"


def load_cases() -> Dict[str, Any]:
    return json.loads(CASES_PATH.read_text())


def goals() -> List[str]:
    """The frozen goal strings, in frozen order."""
    return [c["goal"] for c in load_cases()["cases"]]


def case_ids() -> List[str]:
    return [c["case_id"] for c in load_cases()["cases"]]


def goal_to_case_id() -> Dict[str, str]:
    return {c["goal"]: c["case_id"] for c in load_cases()["cases"]}


if __name__ == "__main__":
    spec = load_cases()
    print(f"case set: {spec['case_set_id']} (frozen {spec['frozen_on']})")
    for case in spec["cases"]:
        print(f"  {case['case_id']:<8} [{case['subcategory']}]")
        print(f"           {case['goal']}")
