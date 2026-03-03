"""Shared utilities for veronica-core framework adapters.

Internal module — not part of the public API. Centralizes patterns
that all adapter modules (langchain, crewai, langgraph, etc.) share.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Union

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.budget import BudgetEnforcer
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig
from veronica_core.pricing import estimate_cost_usd
from veronica_core.retry import RetryContainer


def build_container(config: Union[GuardConfig, ExecutionConfig]) -> AIContainer:
    """Build an AIContainer from GuardConfig or ExecutionConfig.

    Centralizes the container construction pattern used by all adapters
    to avoid 5-way duplication of the same AIContainer(...) call.
    """
    return AIContainer(
        budget=BudgetEnforcer(limit_usd=config.max_cost_usd),
        retry=RetryContainer(max_retries=config.max_retries_total),
        step_guard=AgentStepGuard(max_steps=config.max_steps),
    )


def cost_from_total_tokens(total: int, model: str = "") -> float:
    """Estimate USD cost from total token count using 75/25 heuristic split.

    Assumes 75% input tokens and 25% output tokens when only the total
    is available. Returns 0.0 for non-positive totals.

    This centralizes the magic-number heuristic that was duplicated across
    langchain.py, crewai.py, and langgraph.py (4 call sites).
    """
    if total <= 0:
        return 0.0
    tokens_in = max(1, int(total * 0.75))
    tokens_out = total - tokens_in
    return estimate_cost_usd(model, tokens_in, tokens_out)


def extract_llm_result_cost(response: Any) -> float:
    """Extract USD cost from a LangChain LLMResult object.

    Handles both LangChain (langchain.py) and LangGraph (langgraph.py) usage
    patterns since both pass LLMResult objects to their on_llm_end callbacks.
    Tries prompt+completion token split first; falls back to 75/25 heuristic.
    Returns 0.0 if usage cannot be determined.
    """
    try:
        if response is None:
            return 0.0
        llm_output = getattr(response, "llm_output", None)
        if llm_output is None:
            # langchain.py passes LLMResult directly; llm_output may be a dict attr
            if isinstance(response, dict):
                llm_output = response
            else:
                return 0.0
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if not usage:
            return 0.0

        model = llm_output.get("model_name") or llm_output.get("model") or ""

        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

        if prompt_tokens is not None and completion_tokens is not None:
            return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        total_raw = usage.get("total_tokens")
        if total_raw is None:
            return 0.0
        return cost_from_total_tokens(int(total_raw), model)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, RuntimeError):
        return 0.0


def record_budget_spend(
    container: AIContainer,
    cost: float,
    tag: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Spend cost against the container's budget and warn if over limit.

    Returns True if within budget, False if the limit was exceeded.
    No-op (returns True) when the container has no budget enforcer.

    Args:
        container: AIContainer whose budget to charge.
        cost: USD cost to record.
        tag: Log tag prefix, e.g. "[VERONICA_LC]".
        logger: Logger to use for the warning. Uses module-level logger if None.
    """
    if container.budget is None:
        return True
    _logger = logger or logging.getLogger(__name__)
    within = container.budget.spend(cost)
    if not within:
        _logger.warning(
            "%s LLM call pushed budget over limit (spent $%.4f / $%.4f)",
            tag,
            container.budget.spent_usd,
            container.budget.limit_usd,
        )
    return within
