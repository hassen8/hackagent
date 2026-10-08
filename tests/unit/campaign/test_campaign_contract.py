from __future__ import annotations

import csv

from experiments.campaign import campaign
from experiments.campaign.campaign_config import (
    ATTACKERS,
    ATTACKS,
    CAMPAIGN_NAME,
    REPETITIONS,
)
from experiments.campaign.evidence_adapters import extract_goal_evidence
from experiments.campaign.results import RESULT_COLUMNS, RUN_COLUMNS, flatten_records
from experiments.analysis.analysis import build_aggregates, load_campaign_data


def test_expansion_matrix_and_serving_inventory():
    runs = campaign.build_run_descriptions()
    active_attackers = [attacker for attacker in ATTACKERS if attacker["use"]]
    active_attacks = {attack["name"] for attack in ATTACKS if attack["use"]}

    assert CAMPAIGN_NAME == "omnisafebench_af340_vllm_attackers_vs_gemma3_4b"
    assert REPETITIONS == 1
    assert len(active_attackers) == 8
    assert active_attacks == {"tap", "crescendo", "advprefix"}
    assert len(runs) == 816
    assert len({run["goal_batch_id"] for run in runs}) == 34
    assert all(len(run["goal_ids"]) == len(run["goals"]) == 10 for run in runs)

    assert {attacker["key"] for attacker in active_attackers} == {
        "qwen35_4b_abliterated",
        "gemma4_12b_abliterated",
        "qwen38_27b_abliterated",
        "qwen38_27b_abliterated_bf16",
        "gemma4_31b_instruct_abliterated",
        "deepseek_v4_flash_0731__crack_nvfp4",
        "glm47_abliterated",
        "glm53_flash_abliterated_fp8",
    }


def test_planned_and_csv_metadata_follow_frozen_batch(tmp_path):
    run = campaign.build_run_descriptions()[0]
    planned = campaign._planned_record(
        run,
        "verification-run",
        "verification-output",
        {"attack_type": run["attack"]["name"], "goals": run["goals"]},
        {},
        {},
        "verification-config-hash",
        {
            "target_model": "target",
            "attacker_model": "attacker",
            "judge_model": "judge",
        },
    )
    planned.update(
        {
            "shard_id": "shard-000",
            "shard_manifest_sha256": "test-shard-manifest",
            "evidence_db_relpath": "shards/shard-000/hackagent.db",
        }
    )

    assert planned["case_set_id"] == run["case_set_id"]
    assert planned["goal_batch_id"] == run["goal_batch_id"]
    assert planned["category_id"] == run["category_id"]
    assert planned["subcategory_id"] == run["subcategory_id"]
    assert planned["goal_ids"] == run["goal_ids"]
    assert planned["manifest_sha256"] == run["manifest_sha256"]
    assert planned["case_ids"] == run["goal_ids"]

    harvest = {
        "experiment_run_id": "verification-run",
        "hackagent_run_id": "hackagent-run",
        "run_status": "COMPLETED",
        "status": "completed",
        "goals": [
            {
                "case_id": run["goal_ids"][0],
                "intent_id": run["goal_ids"][0],
                "goal_id": run["goal_ids"][0],
                "goal": run["goals"][0],
                "goal_index": 0,
                "evaluation_status": "FAILED_JAILBREAK",
                "automated_success": False,
                "metadata": {},
                "evaluation_metrics": {},
            }
        ],
    }
    runs_path = tmp_path / "runs.csv"
    results_path = tmp_path / "results.csv"
    assert flatten_records([planned], [harvest], runs_path, results_path) == (1, 1)

    with runs_path.open(newline="", encoding="utf-8") as handle:
        run_row = next(csv.DictReader(handle))
    with results_path.open(newline="", encoding="utf-8") as handle:
        result_row = next(csv.DictReader(handle))

    assert set(
        (
            "case_set_id",
            "goal_batch_id",
            "category_id",
            "subcategory_id",
            "goal_ids",
            "manifest_sha256",
        )
    ) <= set(RUN_COLUMNS)
    assert set(
        (
            "case_set_id",
            "goal_batch_id",
            "category_id",
            "subcategory_id",
            "goal_id",
            "goal",
            "manifest_sha256",
        )
    ) <= set(RESULT_COLUMNS)
    assert run_row["goal_batch_id"] == run["goal_batch_id"]
    assert run_row["category_id"] == run["category_id"]
    assert run_row["subcategory_id"] == run["subcategory_id"]
    assert run_row["manifest_sha256"] == run["manifest_sha256"]
    assert result_row["goal_batch_id"] == run["goal_batch_id"]
    assert result_row["category_id"] == run["category_id"]
    assert result_row["subcategory_id"] == run["subcategory_id"]
    assert result_row["goal_id"] == run["goal_ids"][0]
    assert result_row["goal"] == run["goals"][0]


def test_no_active_model_identity_is_unresolved():
    assert campaign.validate_configuration() == []


def test_vllm_attacker_roles_enable_reasoning():
    run = next(
        run
        for run in campaign.build_run_descriptions()
        if run["attacker"]["key"] == "qwen38_27b_abliterated_bf16"
    )
    resolved = campaign.attack_adapters.build_attack_config(
        run,
        "preview://campaign",
        {
            "ollama": "http://127.0.0.1:11435",
            "vllm": "http://127.0.0.1:8316/v1",
        },
    )
    vllm_attackers = [
        value
        for key, value in resolved.items()
        if key in {"attacker", "generator"}
        and isinstance(value, dict)
        and value.get("agent_type") == "OPENAI_SDK"
    ]
    assert vllm_attackers
    assert all(
        role["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        for role in vllm_attackers
    )


def test_vllm_target_and_judge_roles_disable_reasoning():
    from experiments.campaign.attack_adapters import _role_for_model

    vllm_model = {
        "key": "probe",
        "model_id": "probe-model",
        "serving_framework": "vllm",
        "agent_type": "OPENAI_SDK",
        "endpoint_env": "VLLM_ENDPOINT",
    }
    endpoints = {"VLLM_ENDPOINT": "http://127.0.0.1:8316/v1"}
    for kind in ("target", "judge"):
        role = _role_for_model(dict(vllm_model), endpoints, kind)
        assert role["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_advprefix_generation_summary_counts_ce_probes():
    traces = [
        {
            "sequence": 1,
            "content": {
                "raw_generated": 40,
                "after_phase1_filtering": 15,
                "after_phase2_filtering": 0,
                "candidates": [],
            },
        }
    ]

    evidence = extract_goal_evidence(
        "advprefix",
        traces,
        evaluation_status="FAILED_JAILBREAK",
    )

    assert evidence["goal_target_call_count"] == 15
    assert evidence["target_call_evidence_status"] == "observed_summary"


def test_advprefix_counts_persisted_ce_probes_and_completions():
    traces = [
        {
            "sequence": 1,
            "content": {
                "after_phase1_filtering": 2,
                "after_phase2_filtering": 1,
                "candidates": [{"prefix": "kept"}],
            },
        },
        {
            "sequence": 2,
            "content": {
                "step_name": "Target Prefix CE Probe",
                "request": {"prompt": "probe one"},
                "response": "accepted",
            },
        },
        {
            "sequence": 3,
            "content": {
                "step_name": "Target Prefix CE Probe",
                "request": {"prompt": "probe two"},
                "response": "accepted",
            },
        },
        {
            "sequence": 4,
            "content": {
                "step_name": "Target Completion",
                "request": {"prompt": "kept"},
                "response": "completion",
            },
        },
    ]

    evidence = extract_goal_evidence(
        "advprefix",
        traces,
        evaluation_status="FAILED_JAILBREAK",
    )

    assert evidence["goal_target_call_count"] == 3
    assert evidence["target_call_evidence_status"] == "observed"


def test_empty_failed_goal_without_terminal_trace_remains_unknown():
    evidence = extract_goal_evidence(
        "advprefix",
        [],
        evaluation_status="FAILED_JAILBREAK",
    )

    assert evidence["goal_target_call_count"] is None
    assert evidence["target_call_evidence_status"] == "missing_evidence"


def test_crescendo_attacker_parse_failure_without_target_call_is_zero():
    evidence = extract_goal_evidence(
        "crescendo",
        [
            {
                "sequence": 1,
                "content": {
                    "attack_type": "crescendo",
                    "goal": "goal",
                },
            },
            {
                "sequence": 2,
                "content": {
                    "error": "Attacker parse failure",
                    "turn": 1,
                },
            },
        ],
        evaluation_status="FAILED_JAILBREAK",
    )

    assert evidence["goal_target_call_count"] == 0
    assert evidence["target_call_evidence_status"] == "observed_zero"


def test_nonterminal_partial_evidence_is_preserved_but_excluded_from_asr(tmp_path):
    run = campaign.build_run_descriptions()[0]
    planned = campaign._planned_record(
        run,
        "nonterminal-run",
        "nonterminal-output",
        {"attack_type": run["attack"]["name"], "goals": run["goals"]},
        {},
        {},
        "nonterminal-config-hash",
        {
            "target_model": "target",
            "attacker_model": "attacker",
            "judge_model": "judge",
        },
    )
    planned.update(
        {
            "shard_id": "shard-000",
            "shard_manifest_sha256": "test-shard-manifest",
            "evidence_db_relpath": "shards/shard-000/hackagent.db",
        }
    )
    harvest = {
        "experiment_run_id": "nonterminal-run",
        "shard_id": planned["shard_id"],
        "shard_manifest_sha256": planned["shard_manifest_sha256"],
        "evidence_db_relpath": planned["evidence_db_relpath"],
        "status": "incomplete",
        "run_status": "RUNNING",
        "hackagent_run_id": "hackagent-run",
        "result_count": 1,
        "trace_count": 1,
        "goals": [
            {
                "case_id": run["goal_ids"][0],
                "intent_id": run["goal_ids"][0],
                "goal_id": run["goal_ids"][0],
                "goal": run["goals"][0],
                "goal_index": 0,
                "result_id": "partial-result",
                "evaluation_status": "FAILED_JAILBREAK",
                "automated_success": False,
                "metadata": {},
                "evaluation_metrics": {},
            }
        ],
    }
    runs_path = tmp_path / "runs.csv"
    results_path = tmp_path / "results.csv"
    assert flatten_records([planned], [harvest], runs_path, results_path) == (1, 1)

    runs, results, _inputs, findings = load_campaign_data([tmp_path])
    assert runs.loc[0, "status"] == "incomplete"
    assert runs.loc[0, "hackagent_run_status"] == "RUNNING"
    assert results.loc[0, "run_status"] == "incomplete"
    assert results.loc[0, "hackagent_run_status"] == "RUNNING"
    assert any(
        finding["code"] == "result_rows_on_noncompleted_run"
        and finding["severity"] == "warning"
        for finding in findings
    )

    aggregates = build_aggregates(runs, results)
    run_rows = aggregates[aggregates["aggregation_level"].eq("run")]
    assert int(run_rows["incomplete_attempt_count"].max()) == 1
    assert aggregates[aggregates["aggregation_level"].eq("goal")].empty
