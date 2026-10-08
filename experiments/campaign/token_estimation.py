"""Explicit tokenizer-based estimates for derived campaign exports.

The provider token counts from historical runs are not recoverable after trace
sanitization.  This module therefore keeps tokenizer estimates deliberately
separate from exact usage accounting.  It accepts a tokenizer object in tests
and loads a Hugging Face tokenizer only when an operator explicitly requests an
estimate during reharvesting.

The current historical evidence is target-facing.  The exported field called
``attacker_prompt`` is the adversarial prompt sent to the target, not the
attacker model's own request.  Consequently this module leaves attacker and
combined estimates empty unless a future evidence producer supplies those role
texts explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


TOKEN_ESTIMATION_VERSION = "E1"
LOCAL_TIKTOKEN_ENCODINGS = frozenset(
    {"cl100k_base", "o200k_base", "p50k_base", "p50k_edit", "r50k_base", "gpt2"}
)

ESTIMATED_RESULT_TOKEN_COLUMNS = [
    "goal_target_prompt_tokens_estimated",
    "goal_target_completion_tokens_estimated",
    "goal_target_total_tokens_estimated",
    "goal_attacker_prompt_tokens_estimated",
    "goal_attacker_completion_tokens_estimated",
    "goal_attacker_total_tokens_estimated",
    "goal_combined_prompt_tokens_estimated",
    "goal_combined_completion_tokens_estimated",
    "goal_combined_total_tokens_estimated",
    "goal_token_call_count_estimated",
    "goal_token_calls_missing_count",
    "goal_token_evidence_status",
    "token_estimation_scope",
    "token_estimation_role_coverage",
    "token_estimation_quality",
    "token_estimation_version",
    "tokenizer_name",
    "tokenizer_revision",
    "tokenizer_class",
    "tokenizer_backend",
    "tokenization_method",
]

ESTIMATED_RUN_TOKEN_COLUMNS = [
    "target_prompt_tokens_estimated",
    "target_completion_tokens_estimated",
    "target_total_tokens_estimated",
    "attacker_prompt_tokens_estimated",
    "attacker_completion_tokens_estimated",
    "attacker_total_tokens_estimated",
    "combined_prompt_tokens_estimated",
    "combined_completion_tokens_estimated",
    "combined_total_tokens_estimated",
    "token_estimation_goal_count",
    "token_estimation_missing_count",
    "token_estimation_partial_count",
    "token_evidence_status",
    "token_estimation_scope",
    "token_estimation_role_coverage",
    "token_estimation_quality",
    "token_estimation_version",
    "tokenizer_name",
    "tokenizer_revision",
    "tokenizer_class",
    "tokenizer_backend",
    "tokenization_method",
]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    converted = str(value)
    return converted if converted.strip() else None


def _token_length(encoded: Any) -> int:
    """Extract one sequence length from common tokenizer return shapes."""
    if isinstance(encoded, Mapping):
        encoded = encoded.get("input_ids")
    if encoded is None:
        return 0
    if hasattr(encoded, "shape"):
        shape = getattr(encoded, "shape")
        if len(shape) == 2:
            return int(shape[-1])
        if len(shape) == 1:
            return int(shape[0])
    if isinstance(encoded, Sequence) and not isinstance(encoded, (str, bytes)):
        if encoded and isinstance(encoded[0], Sequence) and not isinstance(
            encoded[0], (str, bytes)
        ):
            return len(encoded[0])
        return len(encoded)
    raise TypeError(f"unsupported tokenizer output: {type(encoded).__name__}")


class TokenEstimator:
    """Count observed text with one explicitly selected tokenizer.

    ``prompt_mode=chat_template`` counts a target prompt as one user message
    with a generation prompt when the tokenizer supports chat templates.  The
    completion is counted as generated text without special tokens.  This is a
    reproducible proxy for provider usage, not a claim about the provider's
    serialized request or response accounting.
    """

    def __init__(
        self,
        tokenizer_name: str,
        *,
        revision: str | None = None,
        prompt_mode: str = "chat_template",
        tokenizer: Any | None = None,
        trust_remote_code: bool = False,
    ) -> None:
        if prompt_mode not in {"chat_template", "text"}:
            raise ValueError("prompt_mode must be 'chat_template' or 'text'")
        if not tokenizer_name:
            raise ValueError("tokenizer_name must not be empty")

        self.tokenizer_name = str(tokenizer_name)
        self.tokenizer_revision = str(revision) if revision else ""
        self.prompt_mode = prompt_mode
        self.tokenizer_backend = "custom"
        self.tokenizer = (
            tokenizer
            if tokenizer is not None
            else self._load_tokenizer(
                self.tokenizer_name,
                revision=revision,
                trust_remote_code=trust_remote_code,
            )
        )
        self.tokenizer_class = type(self.tokenizer).__name__
        self._cache: dict[tuple[str, str], tuple[int, str]] = {}

    @staticmethod
    def _load_tokenizer(
        tokenizer_name: str,
        *,
        revision: str | None,
        trust_remote_code: bool,
    ) -> Any:
        if tokenizer_name in LOCAL_TIKTOKEN_ENCODINGS:
            try:
                import tiktoken
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    f"local tokenizer {tokenizer_name!r} requires the 'tiktoken' package"
                ) from exc
            return tiktoken.get_encoding(tokenizer_name)
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "tokenizer estimates require the 'transformers' package"
            ) from exc
        return AutoTokenizer.from_pretrained(
            tokenizer_name,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )

    def _is_tiktoken(self) -> bool:
        return self.tokenizer_name in LOCAL_TIKTOKEN_ENCODINGS or (
            type(self.tokenizer).__module__.startswith("tiktoken")
        )

    @property
    def metadata(self) -> dict[str, Any]:
        """Return JSON-safe provenance for reports and every derived row."""
        return {
            "token_estimation_version": TOKEN_ESTIMATION_VERSION,
            "tokenizer_name": self.tokenizer_name,
            "tokenizer_revision": self.tokenizer_revision,
            "tokenizer_class": self.tokenizer_class,
            "tokenizer_backend": (
                "tiktoken_local" if self._is_tiktoken() else "transformers_or_custom"
            ),
            "tokenization_method": (
                "tiktoken_text"
                if self._is_tiktoken()
                else "chat_template_user_generation_prompt"
                if self.prompt_mode == "chat_template"
                else "text_with_special_tokens_prompt_and_text_completion"
            ),
        }

    def _count_prompt(self, text: str) -> tuple[int, str]:
        cache_key = ("prompt", text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        if self._is_tiktoken():
            result = (len(self.tokenizer.encode(text, disallowed_special=())), "tiktoken_text")
            self._cache[cache_key] = result
            return result

        if self.prompt_mode == "chat_template":
            apply_chat_template = getattr(self.tokenizer, "apply_chat_template", None)
            if callable(apply_chat_template):
                try:
                    encoded = apply_chat_template(
                        [{"role": "user", "content": text}],
                        tokenize=True,
                        add_generation_prompt=True,
                    )
                    result = (_token_length(encoded), "chat_template")
                    self._cache[cache_key] = result
                    return result
                except (AttributeError, KeyError, TypeError, ValueError):
                    # Some older or custom tokenizers have no usable template.
                    # Falling back is recorded in the row-level method.
                    pass

        encoded = self.tokenizer(text, add_special_tokens=True)
        method = (
            "text_fallback_no_chat_template"
            if self.prompt_mode == "chat_template"
            else "text"
        )
        result = (_token_length(encoded), method)
        self._cache[cache_key] = result
        return result

    def _count_completion(self, text: str) -> tuple[int, str]:
        cache_key = ("completion", text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if self._is_tiktoken():
            result = (len(self.tokenizer.encode(text, disallowed_special=())), "tiktoken_text")
        else:
            encoded = self.tokenizer(text, add_special_tokens=False)
            result = (_token_length(encoded), "text_completion")
        self._cache[cache_key] = result
        return result

    def estimate_target_events(
        self,
        events: Sequence[Mapping[str, Any]] | None = None,
        *,
        fallback_prompt: Any = None,
        fallback_response: Any = None,
        expected_call_count: int | None = None,
        scope: str = "observed_target_calls_through_terminal",
    ) -> dict[str, Any]:
        """Estimate target-side counts without retaining source text.

        ``events`` should contain only compact ``prompt``/``response`` values
        selected by the evidence adapter.  If no events are available, the
        selected exported prompt/response pair is used as a legacy fallback.
        Numeric partial sums are retained, while total fields remain blank when
        one or more calls lack either side of the pair.
        """
        normalized_events = [
            event for event in (events or []) if isinstance(event, Mapping)
        ]
        effective_scope = scope
        if not normalized_events:
            prompt = _text(fallback_prompt)
            response = _text(fallback_response)
            if prompt is not None or response is not None:
                normalized_events = [
                    {
                        "prompt": prompt,
                        "response": response,
                        "prompt_truncated": bool(prompt and len(prompt) >= 500),
                        "response_truncated": bool(response and len(response) >= 2000),
                    }
                ]
                effective_scope = "selected_target_interaction_fallback"

        prompt_counts: list[int] = []
        completion_counts: list[int] = []
        missing_calls = 0
        text_truncated = False
        request_context_incomplete = False
        prompt_methods: list[str] = []
        for event in normalized_events:
            prompt = _text(event.get("prompt"))
            response = _text(event.get("response"))
            text_truncated = text_truncated or bool(
                event.get("prompt_truncated") or event.get("response_truncated")
            )
            request_context_incomplete = request_context_incomplete or bool(
                event.get("request_context_incomplete")
            )
            call_missing = False
            if prompt is not None:
                count, method = self._count_prompt(prompt)
                prompt_counts.append(count)
                prompt_methods.append(method)
            else:
                call_missing = True
            if response is not None:
                count, _method = self._count_completion(response)
                completion_counts.append(count)
            else:
                call_missing = True
            if call_missing:
                missing_calls += 1

        if expected_call_count is not None and expected_call_count >= 0:
            missing_calls += max(0, expected_call_count - len(normalized_events))

        prompt_total = sum(prompt_counts) if prompt_counts else None
        completion_total = sum(completion_counts) if completion_counts else None
        total = (
            prompt_total + completion_total
            if prompt_total is not None
            and completion_total is not None
            and missing_calls == 0
            else None
        )
        if not normalized_events:
            status = "missing"
            quality = "missing_target_text"
        elif (
            prompt_total is not None
            and completion_total is not None
            and not missing_calls
            and not text_truncated
        ):
            status = "estimated"
            quality = "observed_target_text"
        else:
            status = "partial"
            if request_context_incomplete:
                quality = "incomplete_target_context"
            elif text_truncated:
                quality = "truncated_target_text"
            else:
                quality = "partial_target_text"
        if (
            effective_scope == "selected_target_interaction_fallback"
            and status == "estimated"
        ):
            quality = "selected_target_interaction_fallback"

        method_values = sorted(set(prompt_methods))
        tokenization_method = "+".join(method_values) if method_values else self.metadata[
            "tokenization_method"
        ]
        result = {
            "goal_target_prompt_tokens_estimated": prompt_total,
            "goal_target_completion_tokens_estimated": completion_total,
            "goal_target_total_tokens_estimated": total,
            # No historical attacker/generator request/response ledger is
            # available.  Do not infer these values from the target prompt.
            "goal_attacker_prompt_tokens_estimated": None,
            "goal_attacker_completion_tokens_estimated": None,
            "goal_attacker_total_tokens_estimated": None,
            "goal_combined_prompt_tokens_estimated": None,
            "goal_combined_completion_tokens_estimated": None,
            "goal_combined_total_tokens_estimated": None,
            "goal_token_call_count_estimated": len(normalized_events) or None,
            "goal_token_calls_missing_count": (
                missing_calls
                if normalized_events or expected_call_count is not None
                else None
            ),
            "goal_token_evidence_status": status,
            "token_estimation_scope": effective_scope,
            "token_estimation_role_coverage": "target_only",
            "token_estimation_quality": quality,
            "token_estimation_version": TOKEN_ESTIMATION_VERSION,
            "tokenizer_name": self.tokenizer_name,
            "tokenizer_revision": self.tokenizer_revision,
            "tokenizer_class": self.tokenizer_class,
            "tokenizer_backend": (
                "tiktoken_local" if self._is_tiktoken() else "transformers_or_custom"
            ),
            "tokenization_method": tokenization_method,
        }
        return result


def empty_estimated_result_fields() -> dict[str, Any]:
    """Return blank fields for exports produced without estimation."""
    return {column: None for column in ESTIMATED_RESULT_TOKEN_COLUMNS}


def empty_estimated_run_fields() -> dict[str, Any]:
    """Return blank fields for exports produced without estimation."""
    return {column: None for column in ESTIMATED_RUN_TOKEN_COLUMNS}


def _sum_numeric(values: Sequence[Any]) -> int | None:
    numeric = [
        value
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return int(sum(numeric)) if numeric else None


def aggregate_goal_estimates(goals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate goal-grain estimates into one run-grain row."""
    output = empty_estimated_run_fields()
    statuses = [
        str(goal.get("goal_token_evidence_status"))
        for goal in goals
        if goal.get("goal_token_evidence_status") not in (None, "")
    ]
    if not statuses:
        return output

    complete = len(statuses) == len(goals) and all(
        status == "estimated" for status in statuses
    )

    def total(column: str) -> int | None:
        return _sum_numeric([goal.get(column) for goal in goals]) if complete else None

    output.update(
        {
            "target_prompt_tokens_estimated": total(
                "goal_target_prompt_tokens_estimated"
            ),
            "target_completion_tokens_estimated": total(
                "goal_target_completion_tokens_estimated"
            ),
            "target_total_tokens_estimated": total("goal_target_total_tokens_estimated"),
            "attacker_prompt_tokens_estimated": total(
                "goal_attacker_prompt_tokens_estimated"
            ),
            "attacker_completion_tokens_estimated": total(
                "goal_attacker_completion_tokens_estimated"
            ),
            "attacker_total_tokens_estimated": total(
                "goal_attacker_total_tokens_estimated"
            ),
            "combined_prompt_tokens_estimated": total(
                "goal_combined_prompt_tokens_estimated"
            ),
            "combined_completion_tokens_estimated": total(
                "goal_combined_completion_tokens_estimated"
            ),
            "combined_total_tokens_estimated": total(
                "goal_combined_total_tokens_estimated"
            ),
            "token_estimation_goal_count": len(goals),
            "token_estimation_missing_count": statuses.count("missing"),
            "token_estimation_partial_count": statuses.count("partial"),
            "token_estimation_scope": ";".join(
                sorted(
                    {
                        str(goal.get("token_estimation_scope"))
                        for goal in goals
                        if goal.get("token_estimation_scope") not in (None, "")
                    }
                )
            ),
            "token_estimation_role_coverage": ";".join(
                sorted(
                    {
                        str(goal.get("token_estimation_role_coverage"))
                        for goal in goals
                        if goal.get("token_estimation_role_coverage") not in (None, "")
                    }
                )
            ),
            "token_estimation_quality": ";".join(
                sorted(
                    {
                        str(goal.get("token_estimation_quality"))
                        for goal in goals
                        if goal.get("token_estimation_quality") not in (None, "")
                    }
                )
            ),
            "token_estimation_version": ";".join(
                sorted(
                    {
                        str(goal.get("token_estimation_version"))
                        for goal in goals
                        if goal.get("token_estimation_version") not in (None, "")
                    }
                )
            ),
            "tokenizer_name": ";".join(
                sorted(
                    {
                        str(goal.get("tokenizer_name"))
                        for goal in goals
                        if goal.get("tokenizer_name") not in (None, "")
                    }
                )
            ),
            "tokenizer_revision": ";".join(
                sorted(
                    {
                        str(goal.get("tokenizer_revision"))
                        for goal in goals
                        if goal.get("tokenizer_revision") not in (None, "")
                    }
                )
            ),
            "tokenizer_class": ";".join(
                sorted(
                    {
                        str(goal.get("tokenizer_class"))
                        for goal in goals
                        if goal.get("tokenizer_class") not in (None, "")
                    }
                )
            ),
            "tokenizer_backend": ";".join(
                sorted(
                    {
                        str(goal.get("tokenizer_backend"))
                        for goal in goals
                        if goal.get("tokenizer_backend") not in (None, "")
                    }
                )
            ),
            "tokenization_method": ";".join(
                sorted(
                    {
                        str(goal.get("tokenization_method"))
                        for goal in goals
                        if goal.get("tokenization_method") not in (None, "")
                    }
                )
            ),
        }
    )
    if complete:
        output["token_evidence_status"] = "estimated"
    elif any(status in {"estimated", "partial"} for status in statuses):
        output["token_evidence_status"] = "partial"
    else:
        output["token_evidence_status"] = "missing"
    return output


def summarize_goal_estimates(goals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize estimate coverage without converting missing values to zero."""
    statuses = [
        str(goal.get("goal_token_evidence_status"))
        for goal in goals
        if goal.get("goal_token_evidence_status") not in (None, "")
    ]
    target_total_count = sum(
        isinstance(goal.get("goal_target_total_tokens_estimated"), int)
        and not isinstance(goal.get("goal_target_total_tokens_estimated"), bool)
        for goal in goals
    )
    return {
        "goal_rows": len(goals),
        "estimated_goal_rows": statuses.count("estimated"),
        "partial_goal_rows": statuses.count("partial"),
        "missing_goal_rows": statuses.count("missing"),
        "target_total_numeric_goal_rows": target_total_count,
        "target_total_numeric_coverage": (
            target_total_count / len(goals) if goals else None
        ),
        "role_coverage": ";".join(
            sorted(
                {
                    str(goal.get("token_estimation_role_coverage"))
                    for goal in goals
                    if goal.get("token_estimation_role_coverage") not in (None, "")
                }
            )
        ),
    }


def enrich_harvested_records(
    harvests: Sequence[Mapping[str, Any]], estimator: TokenEstimator
) -> list[dict[str, Any]]:
    """Attach estimate fields to derived harvested JSONL without changing sources."""
    enriched: list[dict[str, Any]] = []
    for harvest in harvests:
        record = dict(harvest)
        goals = record.get("goals") or record.get("results") or []
        if isinstance(goals, list):
            updated_goals: list[Any] = []
            for goal in goals:
                if not isinstance(goal, Mapping):
                    updated_goals.append(goal)
                    continue
                updated_goal = dict(goal)
                if "goal_token_evidence_status" not in updated_goal:
                    updated_goal.update(
                        estimator.estimate_target_events(
                            fallback_prompt=updated_goal.get("attacker_prompt"),
                            fallback_response=updated_goal.get("target_response"),
                            expected_call_count=updated_goal.get(
                                "goal_target_call_count"
                            ),
                            scope="selected_target_interaction_fallback",
                        )
                    )
                updated_goals.append(updated_goal)
            record["goals"] = updated_goals
            if "results" in record:
                record["results"] = updated_goals
        enriched.append(record)
    return enriched


__all__ = [
    "ESTIMATED_RESULT_TOKEN_COLUMNS",
    "ESTIMATED_RUN_TOKEN_COLUMNS",
    "TOKEN_ESTIMATION_VERSION",
    "LOCAL_TIKTOKEN_ENCODINGS",
    "TokenEstimator",
    "aggregate_goal_estimates",
    "summarize_goal_estimates",
    "empty_estimated_result_fields",
    "empty_estimated_run_fields",
    "enrich_harvested_records",
]
