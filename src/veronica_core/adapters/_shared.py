"""Shared utilities for veronica-core framework adapters.

Internal module — not part of the public API. Centralizes patterns
that all adapter modules (langchain, crewai, langgraph, etc.) share.
"""
from __future__ import annotations

from typing import Union

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
