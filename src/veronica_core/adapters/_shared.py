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
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.pricing import estimate_cost_usd
from veronica_core.retry import RetryContainer

logger = logging.getLogger(__name__)


def check_and_halt(
    container: Union[AIContainer, "ExecutionContextContainerAdapter"],
    tag: str = "[VERONICA]",
    _logger: Optional[logging.Logger] = None,
    metrics: Optional[Any] = None,
    agent_id: str = "agent",
) -> None:
    """Check container policies and raise VeronicaHalt if denied.

    Centralizes the ``container.check() -> raise VeronicaHalt`` pattern used
    by all framework adapters (ag2, crewai, langchain, langgraph, llamaindex).

    Args:
        container: AIContainer or ExecutionContextContainerAdapter to check.
        tag: Log tag prefix used in debug messages, e.g. ``"[VERONICA_LC]"``.
        _logger: Logger instance. Falls back to the module-level logger if None.
        metrics: Optional ContainmentMetricsProtocol. If provided, emits
            ``record_decision("HALT")`` or ``record_decision("ALLOW")``.
        agent_id: Agent identifier forwarded to ``metrics.record_decision()``.

    Raises:
        VeronicaHalt: If any active policy (budget / step / retry) denies.
    """
    decision = container.check(cost_usd=0.0)
    if not decision.allowed:
        (_logger or logger).debug(
            "%s policy denied: %s", tag, decision.reason
        )
        emit_metrics_decision(metrics, agent_id, "HALT")
        raise VeronicaHalt(decision.reason, decision)
    emit_metrics_decision(metrics, agent_id, "ALLOW")


def emit_metrics_decision(
    metrics: Optional[Any],
    agent_id: str,
    decision: str,
) -> None:
    """Emit a containment decision to a ContainmentMetricsProtocol, if present.

    No-op when *metrics* is None. Never raises — metrics emission must not
    disrupt the containment control flow.

    Args:
        metrics: ContainmentMetricsProtocol instance, or None.
        agent_id: Agent identifier to pass to ``record_decision()``.
        decision: Decision string (``"ALLOW"``, ``"HALT"``, etc.).
    """
    if metrics is None:
        return
    try:
        metrics.record_decision(agent_id, decision)
    except Exception:
        logger.debug(
            "[VERONICA] emit_metrics_decision raised; ignoring", exc_info=True
        )


def emit_metrics_tokens(
    metrics: Optional[Any],
    agent_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Emit token counts to a ContainmentMetricsProtocol, if present.

    No-op when *metrics* is None. Never raises — metrics emission must not
    disrupt the containment control flow.

    Args:
        metrics: ContainmentMetricsProtocol instance, or None.
        agent_id: Agent identifier to pass to ``record_tokens()``.
        input_tokens: Number of prompt/input tokens.
        output_tokens: Number of completion/output tokens.
    """
    if metrics is None:
        return
    try:
        metrics.record_tokens(agent_id, input_tokens, output_tokens)
    except Exception:
        logger.debug(
            "[VERONICA] emit_metrics_tokens raised; ignoring", exc_info=True
        )


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

        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is None:
            prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("completion_tokens")
        if completion_tokens is None:
            completion_tokens = usage.get("output_tokens")

        if prompt_tokens is not None and completion_tokens is not None:
            return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        total_raw = usage.get("total_tokens")
        if total_raw is None:
            return 0.0
        return cost_from_total_tokens(int(total_raw), model)
    except (
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
        OverflowError,
        RuntimeError,
    ):
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


# NEW: ExecutionContext adapter classes


class _BudgetProxy:
    """Budget view backed by an ExecutionContext."""

    def __init__(self, ctx: Any, limit_usd: float) -> None:
        self._ctx = ctx
        self._limit_usd = limit_usd
        # Cache backend callables at construction time for spend() hot-path.
        _backend = getattr(ctx, "_budget_backend", None)
        self._get_fn = getattr(_backend, "get", None) if _backend else None
        self._add_fn = getattr(_backend, "add", None) if _backend else None
        self._lock = getattr(ctx, "_lock", None)

    @property
    def limit_usd(self) -> float:
        return self._limit_usd

    @property
    def spent_usd(self) -> float:
        # When a backend.get callable is available, use it — the backend holds
        # the authoritative cost total (e.g. LocalBudgetBackend, Redis).
        # Otherwise fall back to get_snapshot() which reads _cost_usd_accumulated
        # under a lock (the canonical public API for ExecutionContext).
        if self._get_fn is not None:
            try:
                return float(self._get_fn())
            except Exception:
                pass
        try:
            return self._ctx.get_snapshot().cost_usd_accumulated
        except Exception:
            val = getattr(self._ctx, "_cost_usd_accumulated", 0.0)
            return float(val) if val is not None else 0.0

    @property
    def call_count(self) -> int:
        return int(getattr(self._ctx, "_step_count", 0))

    @property
    def is_exceeded(self) -> bool:
        return self.spent_usd > self._limit_usd

    def spend(self, amount_usd: float) -> bool:
        """Add cost to the ExecutionContext and return True if within budget."""
        try:
            if self._add_fn is not None:
                self._add_fn(amount_usd)
                if self._get_fn is not None:
                    return float(self._get_fn()) <= self._limit_usd
                return True
            if self._lock is not None:
                with self._lock:
                    self._ctx._cost_usd_accumulated += amount_usd
                    return self._ctx._cost_usd_accumulated <= self._limit_usd
            self._ctx._cost_usd_accumulated = (
                getattr(self._ctx, "_cost_usd_accumulated", 0.0) + amount_usd
            )
            return self._ctx._cost_usd_accumulated <= self._limit_usd
        except Exception:
            logger.warning(
                "[VERONICA] _BudgetProxy.spend() raised unexpectedly; "
                "failing closed (returning False) to prevent budget bypass",
                exc_info=True,
            )
            return False


class _StepGuardProxy:
    """Step guard view backed by an ExecutionContext."""

    def __init__(self, ctx: Any, max_steps: int) -> None:
        self._ctx = ctx
        self._max_steps = max_steps
        self._lock = getattr(ctx, "_lock", None)

    @property
    def current_step(self) -> int:
        return int(getattr(self._ctx, "_step_count", 0))

    @property
    def max_steps(self) -> int:
        return self._max_steps

    def step(self, result: Any = None) -> bool:
        """Increment step counter; return True if still within limit."""
        try:
            if self._lock is not None:
                with self._lock:
                    self._ctx._step_count = getattr(self._ctx, "_step_count", 0) + 1
                    return self._ctx._step_count < self._max_steps
            self._ctx._step_count = getattr(self._ctx, "_step_count", 0) + 1
            return self._ctx._step_count < self._max_steps
        except Exception:
            logger.warning(
                "[VERONICA] _StepGuardProxy.step() raised unexpectedly; "
                "failing closed (returning False) to prevent step limit bypass",
                exc_info=True,
            )
            return False


class ExecutionContextContainerAdapter:
    """Adapts an ExecutionContext to the AIContainer interface."""

    def __init__(self, ctx: Any, config: Union[GuardConfig, ExecutionConfig]) -> None:
        self._ctx = ctx
        self._config = config
        self.budget: _BudgetProxy = _BudgetProxy(ctx, config.max_cost_usd)
        self.step_guard: _StepGuardProxy = _StepGuardProxy(ctx, config.max_steps)
        self.retry = None

    def check(self, cost_usd: float = 0.0, **_kwargs: Any) -> Any:
        """Policy gate mirroring AIContainer.check()."""
        from veronica_core.runtime_policy import PolicyDecision

        snap = None
        try:
            snap = self._ctx.get_snapshot()
        except Exception:
            # Intentionally swallowed: snapshot is used only for aborted-flag
            # inspection; on failure the check continues using spent_usd below.
            logger.debug("VeronicaBudget.check(): get_snapshot() failed", exc_info=True)
        if snap is not None and getattr(snap, "aborted", False):
            return PolicyDecision(
                allowed=False, reason="Context aborted", policy_type="containment"
            )
        # Reuse snapshot cost when available to avoid a second backend call
        # (budget.spent_usd would call get_snapshot() again internally).
        spent = (
            snap.cost_usd_accumulated
            if snap is not None and hasattr(snap, "cost_usd_accumulated")
            else self.budget.spent_usd
        )
        if self._config.max_cost_usd > 0 and spent >= self._config.max_cost_usd:
            return PolicyDecision(
                allowed=False,
                reason=f"Budget limit exceeded: ${spent:.4f} / ${self._config.max_cost_usd:.4f}",
                policy_type="budget",
            )
        steps = self.step_guard.current_step
        if steps >= self._config.max_steps:
            return PolicyDecision(
                allowed=False,
                reason=f"Step limit exceeded: {steps} / {self._config.max_steps}",
                policy_type="step",
            )
        return PolicyDecision(allowed=True, reason="", policy_type="containment")

    @property
    def active_policies(self) -> list:
        policies = []
        if self._config.max_cost_usd > 0:
            policies.append("budget")
        if self._config.max_steps > 0:
            policies.append("step_guard")
        return policies


def build_adapter_container(
    config: Union[GuardConfig, ExecutionConfig],
    execution_context: Optional[Any] = None,
) -> Union[AIContainer, "ExecutionContextContainerAdapter"]:
    """Build a container from config, optionally backed by an ExecutionContext."""
    if execution_context is not None:
        return ExecutionContextContainerAdapter(execution_context, config)
    return build_container(config)


def safe_emit(metrics: Optional[Any], method: str, *args: Any) -> None:
    """Call *method* on *metrics* with *args*, silently swallowing any error.

    Convenience wrapper used by adapters to emit metrics without disrupting
    the containment control flow. No-op when *metrics* is None.

    Args:
        metrics: ContainmentMetricsProtocol instance, or None.
        method: Method name to call (``"record_decision"``, ``"record_tokens"``,
            ``"record_cost"``, ``"record_latency"``).
        *args: Positional arguments forwarded to the method.
    """
    if metrics is None:
        return
    fn = getattr(metrics, method, None)
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        logger.debug(
            "[VERONICA] safe_emit(%s) raised; ignoring", method, exc_info=True
        )
