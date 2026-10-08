"""Attack-aware interpretation of HackAgent trace records.

The campaign exporter receives normalized trace dictionaries from ``results.py``
and this module turns them into evidence records at goal grain.  Trace rows all
use HackAgent's ``OTHER`` step type in several techniques, so role and terminal
selection must use the technique's explicit step names and metadata rather than
the first or last row in a result.

This module is deliberately read-only.  It does not open SQLite, call a model,
or know anything about campaign persistence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_SUCCESS_STATUS = "SUCCESSFUL_JAILBREAK"
_TARGET_CALL_ATTACKS = {
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
    "advprefix",
    "autodan_turbo",
}
_REASONING_KEYS = (
    "reasoning",
    "rationale",
    "scorer_explanation",
    "judge_explanation",
    "explanation",
)
_ATTACK_JUDGE_NAMES = {
    "baseline_llm_judge",
    "eval_single_judge",
    "pair_scorer",
    "pair_scorer_iteration",
    "tap_judge",
    "crescendo_judge",
    "crescendo_judge_turn",
    "autodan_turbo_scorer",
}
_SHORT_JUDGE_LABEL_RE = re.compile(
    r"^\s*(?:harmful|safe|yes|no)\b.*(?:harmbench|judge)\s*:",
    re.IGNORECASE,
)


def canonical_attack_name(value: Any) -> str:
    """Normalize HackAgent's persisted aliases to campaign attack names."""
    normalized = str(value or "").strip().lower().replace("-", "_")
    return {
        "statictemplate": "static_template",
        "static_template": "static_template",
        "autodan": "autodan_turbo",
        "autodan_turbo": "autodan_turbo",
    }.get(normalized, normalized)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    return str(value) if value else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        converted = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(converted) if converted.is_integer() else converted


def _first_present(*values: Any) -> Any:
    """Return the first value that is present, including numeric zero."""
    for value in values:
        if value is not None:
            return value
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "successful", "success", "1"}:
        return True
    if normalized in {"false", "no", "n", "failed", "failure", "0"}:
        return False
    return None


def _judge_success(
    content: Mapping[str, Any],
    metadata: Mapping[str, Any],
    result: Mapping[str, Any],
    judge_columns: Mapping[str, Any],
) -> bool | None:
    """Read an explicit verdict before interpreting a numeric score.

    HarmBench's scalar score is not a universal success flag: a safe label can
    still carry a numeric score.  Attack-specific result fields and labels are
    therefore preferred, with a positive score used only for evaluators whose
    traces expose no explicit verdict.
    """
    for candidate in (
        result.get("is_success"),
        result.get("success"),
        metadata.get("is_jailbreak"),
        judge_columns.get("is_jailbreak"),
        result.get("eval_hb"),
        judge_columns.get("eval_hb"),
    ):
        success = _boolean(candidate)
        if success is not None:
            return success

    labels = " ".join(
        str(value)
        for value in (
            content.get("explanation"),
            result.get("explanation"),
            metadata.get("explanation"),
        )
        if value is not None
    ).lower()
    if re.search(r"\b(?:harmbench|judge)\s*:\s*yes\b", labels):
        return True
    if re.search(r"\b(?:harmbench|judge)\s*:\s*no\b", labels):
        return False
    if re.search(r"\bharmful\b", labels) and "not harmful" not in labels:
        return True
    if re.search(r"\bsafe\b", labels):
        return False
    return None


def _content(trace: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(trace.get("content"))


def _metadata(content: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(content.get("metadata"))


def _request(content: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(content.get("request"))


def _response_text(content: Mapping[str, Any]) -> str | None:
    response = content.get("response")
    if isinstance(response, Mapping):
        for key in ("generated_text", "content", "target_response", "response"):
            value = _text(response.get(key))
            if value is not None:
                return value
        return None
    return _text(response)


def _prompt_text(content: Mapping[str, Any]) -> str | None:
    request = _request(content)
    prompt = _text(request.get("prompt"))
    if prompt is not None:
        return prompt
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            message_map = _mapping(message)
            if message_map.get("role") != "user":
                continue
            prompt = _text(message_map.get("content"))
            if prompt is not None:
                return prompt
    for key in ("prompt", "generated_prompt", "jailbreak_prompt", "text_prompt"):
        prompt = _text(content.get(key))
        if prompt is not None:
            return prompt
    return None


def _step_name(content: Mapping[str, Any]) -> str:
    return str(content.get("step_name") or "")


def _display_type(content: Mapping[str, Any]) -> str:
    return str(_metadata(content).get("display_type") or "").lower()


def _sequence(trace: Mapping[str, Any]) -> tuple[int, str]:
    sequence = _integer(trace.get("sequence"))
    return (
        sequence if sequence is not None else 2**31,
        str(trace.get("trace_id") or ""),
    )


def _trace_id(trace: Mapping[str, Any]) -> str | None:
    value = trace.get("trace_id")
    return str(value) if value is not None else None


def _event(
    trace: Mapping[str, Any],
    *,
    prompt: str | None = None,
    response: str | None = None,
    prompt_truncated: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    event = {
        "trace": trace,
        "trace_id": _trace_id(trace),
        "sequence": _sequence(trace)[0],
        "prompt": prompt,
        "response": response,
        "prompt_truncated": prompt_truncated,
        "response_truncated": _boolean(
            _metadata(_content(trace)).get("response_preview_truncated")
        )
        is True
        or bool(response is not None and len(response) >= 2000),
    }
    event.update(fields)
    return event


def _target_event(trace: Mapping[str, Any], attack: str) -> dict[str, Any] | None:
    content = _content(trace)
    step = _step_name(content)
    metadata = _metadata(content)
    display = _display_type(content)
    prompt = _prompt_text(content)
    response = _response_text(content)

    if attack == "pair":
        match = re.match(r"^Iteration\s+(\d+),\s*Stream\s+(\d+)$", step)
        if match and prompt is not None:
            return _event(
                trace,
                prompt=prompt,
                response=response,
                prompt_truncated=True,
                iteration=int(match.group(1)),
                stream=int(match.group(2)),
            )
    elif attack == "tap":
        match = re.match(r"^Depth\s+(\d+)\s+Candidate$", step)
        if match and prompt is not None:
            return _event(
                trace,
                prompt=prompt,
                response=response,
                prompt_truncated=len(prompt) >= 500,
                iteration=int(match.group(1)),
                judge_score=_number(metadata.get("judge_score")),
                branch_index=_integer(metadata.get("branch_index")),
                stream_index=_integer(metadata.get("stream_index")),
            )
    elif attack == "crescendo":
        match = re.match(r"^Turn\s+(\d+)(?:\b|\s)", step)
        if match and prompt is not None and "Generation Failed" not in step:
            return _event(
                trace,
                prompt=prompt,
                response=response,
                prompt_truncated=len(prompt) >= 500,
                # The trace stores only the newest question, while Crescendo
                # sends the accumulated conversation to the target.
                request_context_incomplete=True,
                turn=int(match.group(1)),
            )
    elif attack == "pap":
        if display == "pap_candidate" and prompt is not None:
            return _event(
                trace,
                prompt=prompt,
                response=response,
                technique_index=_integer(metadata.get("technique_index")),
            )
    elif attack == "bon":
        if display == "bon_candidate" and prompt is not None:
            return _event(
                trace,
                prompt=prompt,
                response=response,
                step=_integer(metadata.get("step")),
                candidate_index=_integer(metadata.get("candidate_index")),
            )
    elif attack == "advprefix":
        if (
            step in {"Target Completion", "Target Prefix CE Probe"}
            and prompt is not None
        ):
            phase = (
                "cross_entropy_filter"
                if step == "Target Prefix CE Probe"
                else "completion"
            )
            return _event(
                trace,
                prompt=prompt,
                response=response,
                target_call_phase=phase,
            )
    elif attack == "autodan_turbo":
        if (
            str(content.get("subphase") or "").upper() == "TARGET_QUERY"
            or "Target Query" in step
        ):
            prompt = _text(content.get("prompt")) or _text(
                content.get("generated_prompt")
            )
            response = _text(content.get("target_response")) or response
            if prompt is not None:
                return _event(
                    trace,
                    prompt=prompt,
                    response=response,
                    iteration=_integer(content.get("iteration")),
                    epoch=_integer(content.get("epoch")),
                )
    if (
        attack in {"pair", "crescendo"}
        and isinstance(metadata.get("target_call"), Mapping)
        and prompt is not None
    ):
        return _event(
            trace,
            prompt=prompt,
            response=response,
            iteration=_integer(metadata.get("iteration")),
            stream=_integer(metadata.get("stream")),
            turn=_integer(metadata.get("turn")),
        )
    elif (
        attack == "baseline"
        and step == "Baseline: Direct Request"
        and prompt is not None
    ):
        return _event(trace, prompt=prompt, response=response)
    elif (
        attack == "static_template"
        and step.startswith("Template:")
        and prompt is not None
    ):
        return _event(trace, prompt=prompt, response=response)
    elif (
        attack == "flipattack"
        and step.startswith("FlipAttack Generation")
        and prompt is not None
    ):
        return _event(trace, prompt=prompt, response=response)
    elif (
        attack == "cipherchat"
        and step.startswith("CipherChat Generation")
        and prompt is not None
    ):
        return _event(trace, prompt=prompt, response=response)
    elif attack == "h4rm3l" and step == "h4rm3l Generation" and prompt is not None:
        return _event(trace, prompt=prompt, response=response)
    elif attack == "mml" and step.startswith("MML Generation") and prompt is not None:
        return _event(trace, prompt=prompt, response=response)
    return None


def _target_events(
    traces: Iterable[Mapping[str, Any]], attack: str
) -> list[dict[str, Any]]:
    if attack not in _TARGET_CALL_ATTACKS:
        return []
    events = []
    for trace in sorted(traces, key=_sequence):
        event = _target_event(trace, attack)
        if event is not None:
            events.append(event)
    return events


def _advprefix_generation_count(
    traces: Iterable[Mapping[str, Any]],
) -> int | None:
    """Return the number of target CE probes declared by AdvPrefix generation."""
    count: int | None = None
    for trace in traces:
        content = _content(trace)
        if "after_phase1_filtering" not in content:
            continue
        candidate_count = _integer(content.get("after_phase1_filtering"))
        if candidate_count is not None and candidate_count >= 0:
            count = candidate_count
    return count


def _target_call_count(
    attack: str,
    traces: list[Mapping[str, Any]],
    targets: list[Mapping[str, Any]],
    terminal: Mapping[str, Any] | None = None,
) -> tuple[int | None, str]:
    """Return the total target-call count and its evidence status.

    AdvPrefix has two target-call components.  The component-aware helper below
    is the source of truth; this compatibility wrapper keeps the older internal
    two-value contract available to callers and tests.
    """
    total, status, _ce_count, _completion_count = _target_call_breakdown(
        attack, traces, targets, terminal
    )
    return total, status


def _has_attacker_parse_failure(traces: Iterable[Mapping[str, Any]]) -> bool:
    """Return whether the attack explicitly recorded an attacker parse failure."""
    return any(
        _content(trace).get("error") == "Attacker parse failure"
        for trace in traces
    )


def _target_call_breakdown(
    attack: str,
    traces: list[Mapping[str, Any]],
    targets: list[Mapping[str, Any]],
    terminal: Mapping[str, Any] | None = None,
) -> tuple[int | None, str, int | None, int | None]:
    """Count target requests and, for AdvPrefix, preserve both components.

    Older AdvPrefix runs persisted only the per-goal generation summary, not
    each CE probe.  That summary records one target request per phase-one
    prefix, so it remains sufficient to recover the count while marking the
    provenance as summary-derived.  New runs persist each probe explicitly and
    are checked against the summary when one is available.
    """
    if attack != "advprefix":
        through = _through(targets, attack, terminal)
        if through:
            return len(through), "observed", None, None
        if attack == "crescendo" and _has_attacker_parse_failure(traces):
            # Crescendo records this exact marker when the attacker request
            # fails before a question is produced.  With no target events,
            # the target-call count is provably zero, not merely unknown.
            return 0, "observed_zero", None, None
        return None, "missing_evidence", None, None

    explicit_ce = [
        target
        for target in targets
        if target.get("target_call_phase") == "cross_entropy_filter"
    ]
    completion_targets = [
        target
        for target in targets
        if target.get("target_call_phase") != "cross_entropy_filter"
    ]
    completion_count = len(_through(completion_targets, attack, terminal))
    generation_count = _advprefix_generation_count(traces)
    if explicit_ce:
        if generation_count is not None and len(explicit_ce) != generation_count:
            return None, "incomplete_evidence", None, completion_count
        return len(explicit_ce) + completion_count, "observed", len(explicit_ce), completion_count
    if generation_count is not None:
        return (
            generation_count + completion_count,
            "observed_summary",
            generation_count,
            completion_count,
        )
    through = _through(targets, attack, terminal)
    return (
        (len(through), "observed", None, None)
        if through
        else (None, "missing_evidence", None, None)
    )


def _result_mapping(content: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(content.get("result"))


def _judge_event(trace: Mapping[str, Any], attack: str) -> dict[str, Any] | None:
    content = _content(trace)
    metadata = _metadata(content)
    result = _result_mapping(content)
    display = _display_type(content)
    evaluator = _text(content.get("evaluator"))
    if evaluator is not None and evaluator.lower() == "tracking_coordinator":
        return None
    if evaluator is None and display not in {"pap_evaluation", "bon_evaluation"}:
        return None

    judge_columns = _mapping(metadata.get("judge_columns"))
    score = _number(content.get("score"))
    if score is None:
        score = _number(result.get("score"))
    if score is None:
        score = _number(metadata.get("judge_score"))
    if score is None:
        score = _number(judge_columns.get("score"))

    success = _judge_success(content, metadata, result, judge_columns)
    threshold = _number(result.get("threshold"))
    if threshold is None:
        threshold = _number(metadata.get("threshold"))
    if success is None and score is not None:
        success = score > 0

    event = _event(
        trace,
        evaluator=evaluator,
        score=score,
        success=success,
        threshold=threshold,
        iteration=_integer(
            _first_present(result.get("iteration"), metadata.get("iteration"))
        ),
        stream=_integer(_first_present(result.get("stream"), metadata.get("stream"))),
        turn=_integer(_first_present(result.get("turn"), metadata.get("turn"))),
        technique_index=_integer(
            _first_present(
                metadata.get("technique_index"), result.get("technique_index")
            )
        ),
        step=_integer(_first_present(metadata.get("step"), result.get("step"))),
        candidate_index=_integer(
            _first_present(
                metadata.get("candidate_index"),
                result.get("candidate_index"),
                _request(content).get("best_candidate_index"),
            )
        ),
        iterations_completed=_integer(
            _first_present(
                result.get("iterations_completed"), metadata.get("iterations_completed")
            )
        ),
        turns_completed=_integer(
            _first_present(
                result.get("turns_completed"), metadata.get("turns_completed")
            )
        ),
        epoch=_integer(_first_present(result.get("epoch"), metadata.get("epoch"))),
        best_score=_number(
            _first_present(result.get("best_score"), metadata.get("best_score"))
        ),
        completion=_text(metadata.get("completion")),
        prefix=_text(metadata.get("prefix")),
        judge_columns=judge_columns,
        display_type=display,
        prompt=_text(result.get("jailbreak_prompt")) or _text(result.get("prompt")),
        response=_text(result.get("target_response")) or _text(result.get("response")),
    )
    return event


def _judge_events(
    traces: Iterable[Mapping[str, Any]], attack: str
) -> list[dict[str, Any]]:
    events = []
    for trace in sorted(traces, key=_sequence):
        event = _judge_event(trace, attack)
        if event is not None:
            events.append(event)
    return events


def classify_trace(trace: Mapping[str, Any], attack: str) -> str:
    """Classify one trace as target, attacker, judge, evaluator, or bookkeeping."""
    attack = canonical_attack_name(attack)
    if _judge_event(trace, attack) is not None:
        evaluator = str(_content(trace).get("evaluator") or "").strip().lower()
        display = _display_type(_content(trace))
        return (
            "judge"
            if display in {"pap_evaluation", "bon_evaluation"}
            or evaluator in _ATTACK_JUDGE_NAMES
            else "evaluator"
        )
    if _target_event(trace, attack) is not None:
        return "target"
    content = _content(trace)
    step = _step_name(content).lower()
    if (
        "attacker" in step
        or "generation" in step
        or "generated_prompt" in content
        or "attacker_raw_response" in content
    ):
        return "attacker"
    return "bookkeeping"


def classify_trace_events(
    traces: Iterable[Mapping[str, Any]], attack: str
) -> list[dict[str, Any]]:
    """Return trace IDs and normalized roles for diagnostics and tests."""
    attack = canonical_attack_name(attack)
    return [
        {
            "trace_id": _trace_id(trace),
            "sequence": _sequence(trace)[0],
            "role": classify_trace(trace, attack),
        }
        for trace in sorted(traces, key=_sequence)
    ]


def _same_text(left: Any, right: Any) -> bool:
    left_text = _text(left)
    right_text = _text(right)
    if left_text is None or right_text is None:
        return False
    if left_text == right_text:
        return True
    shorter, longer = sorted((left_text, right_text), key=len)
    return len(shorter) >= 40 and longer.startswith(shorter)


def _related(target: Mapping[str, Any], judge: Mapping[str, Any], attack: str) -> bool:
    if attack == "pair":
        return target.get("iteration") == judge.get("iteration") and (
            judge.get("stream") is None or target.get("stream") == judge.get("stream")
        )
    if attack == "crescendo":
        return target.get("turn") == judge.get("turn")
    if attack == "pap":
        return target.get("technique_index") == judge.get("technique_index")
    if attack == "bon":
        return target.get("step") == judge.get("step") and (
            judge.get("candidate_index") is None
            or target.get("candidate_index") == judge.get("candidate_index")
        )
    if attack == "autodan_turbo":
        return (
            judge.get("iteration") is None
            or target.get("iteration") == judge.get("iteration")
        ) and (judge.get("epoch") is None or target.get("epoch") == judge.get("epoch"))
    prompt_match = _same_text(target.get("prompt"), judge.get("prefix"))
    response_match = _same_text(target.get("response"), judge.get("completion"))
    return prompt_match or response_match


def _success_events(judges: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(judge) for judge in judges if judge.get("success") is True]


def _through(
    targets: list[Mapping[str, Any]], attack: str, terminal: Mapping[str, Any] | None
) -> list[Mapping[str, Any]]:
    if terminal is None:
        return targets
    if attack == "pair" and terminal.get("iteration") is not None:
        return [
            target
            for target in targets
            if target.get("iteration", 0) <= terminal["iteration"]
        ]
    if attack == "tap" and terminal.get("iteration") is not None:
        return [
            target
            for target in targets
            if target.get("iteration", 0) <= terminal["iteration"]
        ]
    if attack == "crescendo" and terminal.get("turn") is not None:
        return [
            target for target in targets if target.get("turn", 0) <= terminal["turn"]
        ]
    if attack == "pap" and terminal.get("technique_index") is not None:
        return [
            target
            for target in targets
            if target.get("technique_index", 0) <= terminal["technique_index"]
        ]
    if attack == "bon" and terminal.get("step") is not None:
        return [
            target for target in targets if target.get("step", 0) <= terminal["step"]
        ]
    if attack == "autodan_turbo" and terminal.get("iteration") is not None:
        return [
            target
            for target in targets
            if target.get("iteration") is None
            or target.get("iteration", 0) <= terminal["iteration"]
        ]
    return targets


def _best_target(
    targets: Iterable[Mapping[str, Any]],
    judges: Iterable[Mapping[str, Any]],
    attack: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    target_list = [dict(target) for target in targets]
    judge_list = [dict(judge) for judge in judges]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for judge in judge_list:
        related = [target for target in target_list if _related(target, judge, attack)]
        for target in related:
            pairs.append((target, judge))
    if pairs:
        pairs.sort(
            key=lambda pair: (pair[1].get("sequence", 0), pair[0].get("sequence", 0))
        )
        return pairs[-1]
    return (target_list[-1], None) if target_list else (None, None)


def _reasoning_text(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if "thinking process" in lowered and (
        "unknown response" in lowered or "retries exhausted" in lowered
    ):
        return None
    return text


def _find_reasoning(value: Any) -> list[str]:
    mapping = _mapping(value)
    if not mapping:
        return []
    candidates: list[str] = []
    for key in _REASONING_KEYS:
        candidate = _reasoning_text(mapping.get(key))
        if candidate is not None:
            candidates.append(candidate)
    for key in ("result", "metadata", "judge_columns"):
        nested_value = mapping.get(key)
        if not isinstance(nested_value, Mapping):
            continue
        candidates.extend(_find_reasoning(nested_value))
    return candidates


def _response_reasoning(content: Mapping[str, Any]) -> str | None:
    response = _text(content.get("response"))
    if response is None:
        return None
    match = re.search(
        r"(?:^|\n)\s*Explanation:\s*(.+?)(?=\n\s*Target response:|\Z)",
        response,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _reasoning_text(match.group(1).strip()) if match else None


def _judge_reasoning(judge: Mapping[str, Any]) -> str | None:
    """Return the richest explicit rationale, excluding parse-failure text."""
    content = _content(_mapping(judge.get("trace")))
    candidates = _find_reasoning(content)
    response_reasoning = _response_reasoning(content)
    if response_reasoning is not None:
        candidates.append(response_reasoning)
    return max(candidates, key=len) if candidates else None


def normalize_judge_evaluation(
    judge: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize one selected attack-time judge trace into a JSON-safe object."""
    if judge is None:
        return None
    output: dict[str, Any] = {}
    for source, destination in (
        ("evaluator", "evaluator"),
        ("score", "score"),
        ("success", "success"),
        ("threshold", "threshold"),
        ("iteration", "iteration"),
        ("turn", "turn"),
        ("stream", "stream"),
        ("technique_index", "technique_index"),
        ("step", "step"),
        ("candidate_index", "candidate_index"),
        ("epoch", "epoch"),
    ):
        value = judge.get(source)
        if value is not None:
            output[destination] = value
    if judge.get("display_type"):
        output["display_type"] = judge["display_type"]
    if isinstance(judge.get("judge_columns"), Mapping) and judge["judge_columns"]:
        output["judge_columns"] = dict(judge["judge_columns"])
    return output or None


def _evidence_result(
    targets: list[dict[str, Any]],
    target: Mapping[str, Any] | None,
    judge: Mapping[str, Any] | None,
    *,
    attack: str,
    terminal: Mapping[str, Any] | None,
    selection: str,
) -> dict[str, Any]:
    through = _through(targets, attack, terminal)
    target_ids = [event.get("trace_id") for event in through if event.get("trace_id")]
    count = len(through) if through else None
    status = "observed" if count is not None else "missing_evidence"
    result: dict[str, Any] = {
        "goal_target_call_count": count,
        "advprefix_ce_call_count": None,
        "advprefix_completion_call_count": None,
        "target_call_evidence_status": status,
        "attacker_prompt": None,
        "target_response": None,
        "judge_evaluation": None,
        "judge_reasoning": None,
        "evidence_trace_ids": {},
        "evidence_selection": selection,
    }
    if target is None or judge is None:
        if target is not None:
            result["target_call_evidence_status"] = "observed_selection_missing"
        return result

    result["attacker_prompt"] = target.get("prompt")
    result["target_response"] = target.get("response")
    result["judge_evaluation"] = normalize_judge_evaluation(judge)
    content = _content(_mapping(judge.get("trace")))
    result["judge_reasoning"] = _judge_reasoning(judge)
    if result["judge_reasoning"] is None and _SHORT_JUDGE_LABEL_RE.match(
        _text(content.get("explanation")) or ""
    ):
        result["judge_reasoning"] = _text(content.get("explanation"))
    result["evidence_trace_ids"] = {
        "target_trace_id": target.get("trace_id"),
        "judge_trace_ids": [judge.get("trace_id")],
        "target_call_trace_ids": target_ids,
    }
    return result


def _estimate_target_tokens(
    token_estimator: Any | None,
    events: Iterable[Mapping[str, Any]],
    *,
    expected_call_count: int | None,
    scope: str,
) -> dict[str, Any]:
    """Estimate target text while the complete selected trace set is available."""
    if token_estimator is None:
        return {}
    compact_events = [
        {
            "prompt": event.get("prompt"),
            "response": event.get("response"),
            "prompt_truncated": bool(event.get("prompt_truncated")),
            "response_truncated": bool(event.get("response_truncated")),
            "request_context_incomplete": bool(
                event.get("request_context_incomplete")
            ),
        }
        for event in events
    ]
    return token_estimator.estimate_target_events(
        compact_events,
        expected_call_count=expected_call_count,
        scope=scope,
    )


def _terminal_for_attack(
    attack: str,
    targets: list[dict[str, Any]],
    judges: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    successes = _success_events(judges)
    if not successes:
        return None, []
    if attack in {"pair", "crescendo", "pap", "bon"}:
        field = {
            "pair": "iteration",
            "crescendo": "turn",
            "pap": "technique_index",
            "bon": "step",
        }[attack]
        values = [
            event.get(field) for event in successes if event.get(field) is not None
        ]
        if values:
            terminal_value = min(values)
            terminal = next(
                event for event in successes if event.get(field) == terminal_value
            )
            associated = [
                event for event in successes if event.get(field) == terminal_value
            ]
            return terminal, associated
    if attack == "tap":
        final = [event for event in successes if event.get("evaluator") == "tap_judge"]
        if final:
            event = final[-1]
            completed = event.get("iterations_completed")
            if completed is not None:
                event = dict(event)
                event["iteration"] = completed
            return event, final
    if attack == "autodan_turbo":
        values = [
            event.get("iteration")
            for event in successes
            if event.get("iteration") is not None
        ]
        if values:
            terminal_value = min(values)
            associated = [
                event for event in successes if event.get("iteration") == terminal_value
            ]
            return associated[0], associated
    if attack in {
        "baseline",
        "static_template",
        "flipattack",
        "cipherchat",
        "h4rm3l",
        "mml",
        "advprefix",
    }:
        paired = [
            event
            for event in successes
            if event.get("completion") is not None or event.get("prefix") is not None
        ]
        if paired:
            return paired[-1], paired
        explicit = [
            event
            for event in successes
            if event.get("evaluator", "").lower()
            in {"eval_single_judge", "baseline_llm_judge"}
        ]
        if explicit:
            return explicit[-1], explicit

    # Direct/control and AdvPrefix attacks do not expose a terminal index. Prefer
    # a judge trace carrying the prompt/response pair over a later run summary.
    paired = [
        event
        for event in successes
        if event.get("completion") is not None or event.get("prefix") is not None
    ]
    selected = paired[-1] if paired else successes[-1]
    return selected, [selected]


def extract_goal_evidence(
    attack: str,
    traces: Iterable[Mapping[str, Any]],
    *,
    evaluation_status: str | None,
    token_estimator: Any | None = None,
) -> dict[str, Any]:
    """Classify calls, count calls through termination, and select evidence.

    The returned dictionary is stable campaign-export data.  Successful-goal
    prompt/response/judge fields are populated only when a target interaction is
    linked to a successful judge event.  Failed goals still receive a target-call
    count but deliberately receive empty selected-evidence fields.
    """
    attack = canonical_attack_name(attack)
    trace_list = [dict(trace) for trace in traces]
    targets = _target_events(trace_list, attack)
    judges = _judge_events(trace_list, attack)
    status = str(evaluation_status or "").upper()
    if status != _SUCCESS_STATUS:
        (
            target_call_count,
            target_call_status,
            ce_count,
            completion_count,
        ) = _target_call_breakdown(
            attack, trace_list, targets
        )
        evidence = {
            "goal_target_call_count": target_call_count,
            "advprefix_ce_call_count": ce_count,
            "advprefix_completion_call_count": completion_count,
            "target_call_evidence_status": target_call_status,
            "attacker_prompt": None,
            "target_response": None,
            "judge_evaluation": None,
            "judge_reasoning": None,
            "evidence_trace_ids": {},
            "evidence_selection": "not_selected_unsuccessful_goal",
        }
        evidence.update(
            _estimate_target_tokens(
                token_estimator,
                _through(targets, attack, None),
                expected_call_count=target_call_count,
                scope="observed_target_calls_all",
            )
        )
        return evidence

    completion_targets = [
        target
        for target in targets
        if target.get("target_call_phase") != "cross_entropy_filter"
    ]
    terminal, terminal_judges = _terminal_for_attack(attack, targets, judges)
    (
        target_call_count,
        target_call_status,
        ce_count,
        completion_count,
    ) = _target_call_breakdown(
        attack, trace_list, targets, terminal
    )
    through = _through(completion_targets, attack, terminal)
    if attack == "tap" and terminal is not None:
        score = terminal.get("score")
        if score is None:
            score = terminal.get("best_score")
        candidate_targets = [
            target
            for target in through
            if terminal.get("iteration") is None
            or target.get("iteration") == terminal.get("iteration")
        ]
        if score is not None:
            matching = [
                target
                for target in candidate_targets
                if target.get("judge_score") == score
            ]
            candidate_targets = matching or candidate_targets
        target = (
            max(
                candidate_targets or through,
                key=lambda event: (
                    event.get("judge_score") is not None,
                    event.get("judge_score") or -1,
                    event.get("sequence", 0),
                ),
            )
            if (candidate_targets or through)
            else None
        )
        judge = terminal_judges[-1] if terminal_judges else terminal
    elif attack == "pair" and terminal is not None:
        associated = [
            target
            for target in through
            if target.get("iteration") == terminal.get("iteration")
            and (
                terminal.get("stream") is None
                or target.get("stream") == terminal.get("stream")
            )
        ]
        target = associated[-1] if associated else None
        if target is None:
            same_iteration = [
                candidate
                for candidate in through
                if candidate.get("iteration") == terminal.get("iteration")
            ]
            target = (
                max(
                    same_iteration or through,
                    key=lambda event: (
                        event.get("judge_score") or -1,
                        event.get("sequence", 0),
                    ),
                )
                if (same_iteration or through)
                else None
            )
        judge = terminal
    elif attack == "crescendo" and terminal is not None:
        same_turn = [
            target for target in through if target.get("turn") == terminal.get("turn")
        ]
        target = (
            max(
                same_turn or through,
                key=lambda event: (
                    _boolean(
                        _metadata(_content(_mapping(event.get("trace")))).get("is_best")
                    )
                    is True,
                    event.get("sequence", 0),
                ),
            )
            if (same_turn or through)
            else None
        )
        judge = terminal
    elif attack in {"pap", "bon"} and terminal is not None:
        target, judge = _best_target(through, terminal_judges or [terminal], attack)
    elif attack == "autodan_turbo" and terminal is not None:
        target, judge = _best_target(through, terminal_judges or [terminal], attack)
    else:
        # Direct/control and AdvPrefix results can have a final aggregate judge
        # plus one evaluator per target response. Use all successful evaluator
        # events so prompt/response linkage is explicit instead of selecting a
        # summary trace with no target-facing fields.
        successful_judges = (
            [
                judge_event
                for judge_event in judges
                if judge_event.get("success") is True
            ]
            if attack
            in {
                "baseline",
                "static_template",
                "flipattack",
                "cipherchat",
                "h4rm3l",
                "mml",
                "advprefix",
            }
            else (terminal_judges or [terminal] if terminal else [])
        )
        target, judge = _best_target(through, successful_judges, attack)

    selection = (
        f"{attack}:terminal_success"
        if terminal is not None
        else f"{attack}:successful_judge_without_terminal_marker"
    )
    evidence = _evidence_result(
        completion_targets,
        target,
        judge,
        attack=attack,
        terminal=terminal,
        selection=selection,
    )
    evidence["goal_target_call_count"] = target_call_count
    evidence["advprefix_ce_call_count"] = ce_count
    evidence["advprefix_completion_call_count"] = completion_count
    evidence["target_call_evidence_status"] = target_call_status
    evidence.update(
        _estimate_target_tokens(
            token_estimator,
            _through(targets, attack, terminal),
            expected_call_count=target_call_count,
            scope="observed_target_calls_through_terminal",
        )
    )
    return evidence


def count_target_calls(
    attack: str,
    traces: Iterable[Mapping[str, Any]],
    *,
    evaluation_status: str | None,
) -> int | None:
    """Return the target-call count using the same terminal policy as export."""
    return extract_goal_evidence(
        attack,
        traces,
        evaluation_status=evaluation_status,
    ).get("goal_target_call_count")


__all__ = [
    "canonical_attack_name",
    "classify_trace",
    "classify_trace_events",
    "count_target_calls",
    "extract_goal_evidence",
    "normalize_judge_evaluation",
]
