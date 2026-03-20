"""veronica_core.adapters.ag2 -- AG2 (AutoGen2) ConversableAgent adapter.

Integrates VERONICA policy enforcement into AG2 pipelines via two
complementary approaches: subclassing and hook-based registration.

This module raises ImportError on import if ag2 is not installed.

Public API:
    VeronicaConversableAgent -- ConversableAgent subclass that enforces
        budget, step count, and retry limits on every generate_reply() call.
    register_veronica_hook -- Function-based alternative; registers a reply
        function via ag2's register_reply() for users who prefer composition
        over inheritance.

Usage (subclass)::

    from autogen import ConversableAgent
    from veronica_core.adapters.ag2 import VeronicaConversableAgent
    from veronica_core import GuardConfig

    agent = VeronicaConversableAgent(
        "assistant",
        config=GuardConfig(max_cost_usd=1.0, max_steps=20),
        system_message="You are a helpful assistant.",
    )
    reply = agent.generate_reply(messages=[{"role": "user", "content": "Hello!"}])

Usage (hook)::

    from autogen import ConversableAgent
    from veronica_core.adapters.ag2 import register_veronica_hook
    from veronica_core import GuardConfig

    agent = ConversableAgent("assistant")
    register_veronica_hook(agent, GuardConfig(max_cost_usd=1.0, max_steps=20))
"""

from __future__ import annotations

try:
    from autogen import ConversableAgent  # type: ignore[import]
except ImportError:
    try:
        from ag2 import ConversableAgent  # type: ignore[import]
    except ImportError as _exc:
        raise ImportError(
            "veronica_core.adapters.ag2 requires autogen. "
            "Install with: pip install autogen"
        ) from _exc

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities

from veronica_core.adapters._ag2_helpers import (
    _AG2_SUPPORTED_VERSIONS,
    emit_ag2_otel_event as _emit_ag2_otel_event,
)
from veronica_core.adapters._shared import (
    build_adapter_container,
    build_container,
    check_and_halt,
    record_budget_spend,
    safe_emit,
)
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

__all__ = ["VeronicaConversableAgent", "register_veronica_hook"]


class VeronicaConversableAgent(ConversableAgent):
    """AG2 ConversableAgent subclass that enforces VERONICA policies.

    Wraps ``generate_reply()`` with a pre-call policy check via AIContainer.
    Raises VeronicaHalt before the LLM is invoked if any policy denies.

    On each ``generate_reply()`` call:
    - **Pre-call**: policy check via AIContainer.check().
      Raises VeronicaHalt if any policy (budget / step / retry) denies.
    - **Post-call**: increments the step counter on success.

    Args:
        name: Agent name (passed to ConversableAgent).
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.
        **kwargs: All remaining keyword arguments forwarded to ConversableAgent.

    Raises:
        VeronicaHalt: When a policy denies execution in ``generate_reply()``.

    Example::

        from veronica_core.adapters.ag2 import VeronicaConversableAgent
        from veronica_core import GuardConfig

        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=1.0, max_steps=20),
        )
        reply = agent.generate_reply(messages=[{"role": "user", "content": "Hi"}])
    """

    def __init__(
        self,
        name: str,
        config: Union[GuardConfig, ExecutionConfig],
        *,
        execution_context: Any = None,
        metrics: Optional[Any] = None,
        agent_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._container = build_adapter_container(config, execution_context)
        self._metrics = metrics
        # Default agent_id to the agent name when not explicitly specified
        self._agent_id = agent_id or name

    def generate_reply(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        sender: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[Union[str, Dict[str, Any]]]:
        """Generate a reply, enforcing VERONICA policies before the LLM call.

        Args:
            messages: Conversation history passed to the underlying agent.
            sender: The sender agent (optional).
            **kwargs: Additional keyword arguments forwarded to super().

        Returns:
            The reply string or dict returned by ConversableAgent, or None.

        Raises:
            VeronicaHalt: If any active policy (budget / step / retry) denies.
        """
        agent_name = getattr(self, "name", "unknown")

        # Inline check instead of check_and_halt() because we need to emit
        # OTel events between the check and the raise/allow path.
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            _emit_ag2_otel_event(
                agent_name, "HALT", decision.reason or "policy denied", "policy"
            )
            safe_emit(self._metrics, "record_decision", self._agent_id, "HALT")
            raise VeronicaHalt(decision.reason, decision)

        _emit_ag2_otel_event(agent_name, "ALLOW", "all checks passed", "pre_call")
        safe_emit(self._metrics, "record_decision", self._agent_id, "ALLOW")

        try:
            reply = super().generate_reply(messages=messages, sender=sender, **kwargs)
        except Exception as exc:
            # Record failure against retry budget so check() can deny future calls
            # once max_retries_total is exhausted.
            if self._container.retry is not None:
                self._container.retry.record_failure(error=exc)
            raise

        # Increment step counter only when a reply was produced.
        # None means the agent declined to reply -- no LLM call was made.
        if reply is not None and self._container.step_guard is not None:
            self._container.step_guard.step()

        # Record token cost against budget.
        # AG2 reply is a str or dict; extract token usage from dict payloads
        # (some backends embed usage metadata). Falls back to a length-based
        # approximation when no structured usage is available.
        if reply is not None:
            cost = _extract_ag2_reply_cost(reply)
            if cost > 0.0:
                record_budget_spend(self._container, cost, "[VERONICA_AG2]", logger)

        return reply

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="AG2",
            supports_cost_extraction=True,
            supports_token_extraction=True,
            supported_versions=_AG2_SUPPORTED_VERSIONS,
        )

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container


def register_veronica_hook(
    agent: ConversableAgent,
    config: Union[GuardConfig, ExecutionConfig],
) -> AIContainer:
    """Register a VERONICA policy-enforcement reply function on an existing agent.

    Alternative to subclassing: injects a policy-checking reply function into
    *agent* via ``agent.register_reply()``. The hook is registered at the
    highest priority (position 0) so policies are checked before any other
    reply function.

    Args:
        agent: An existing ``autogen.ConversableAgent`` instance to instrument.
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.

    Returns:
        The AIContainer created for this agent (for testing and introspection).

    Raises:
        VeronicaHalt: When a policy denies execution inside the registered hook.

    Note:
        register_reply() provides no after-hook. record_success/failure
        unavailable via this pattern. Use CircuitBreakerCapability instead for
        circuit breaking.

        Step counting: the hook returns (False, None) and does NOT increment
        the step counter. The step counter cannot be incremented here because
        whether a reply is ultimately produced is determined by subsequent
        reply functions -- the hook has no visibility into that outcome.
        Use VeronicaConversableAgent (subclass) for step counting, which
        increments only when generate_reply() returns a non-None value.

        Pre-call budget check: check(cost_usd=0.0) is used because the actual
        cost is unknown before the LLM call. The check enforces the budget
        limit against costs already recorded (spent_usd >= limit_usd).

    Example::

        from autogen import ConversableAgent
        from veronica_core.adapters.ag2 import register_veronica_hook
        from veronica_core import GuardConfig

        agent = ConversableAgent("assistant")
        container = register_veronica_hook(agent, GuardConfig(max_cost_usd=1.0))
    """
    container = build_container(config)

    def _veronica_reply_fn(
        recipient: ConversableAgent,
        messages: Optional[List[Dict[str, Any]]] = None,
        sender: Optional[Any] = None,
        **_kwargs: Any,
    ) -> tuple[bool, None]:
        """Policy-check reply function registered via register_reply().

        Returns (False, None) to indicate it did not produce a reply, allowing
        subsequent reply functions to run. Raises VeronicaHalt to abort.

        Step counter is NOT incremented here. The hook cannot know whether any
        subsequent reply function will actually produce a reply; incrementing
        unconditionally would over-count steps. Use VeronicaConversableAgent
        (subclass path) for step-accurate counting.
        """
        check_and_halt(container, tag="[VERONICA_AG2]", _logger=logger)
        return False, None

    agent.register_reply(
        trigger=ConversableAgent,
        reply_func=_veronica_reply_fn,
        position=0,
    )

    logger.debug(
        "[VERONICA_AG2] Registered policy hook on agent '%s'",
        getattr(agent, "name", repr(agent)),
    )
    return container


def _extract_ag2_reply_cost(reply: Any) -> float:
    """Extract a USD cost estimate from an AG2 generate_reply() return value.

    AG2 replies are typically strings. Some backends (OpenAI, Anthropic) embed
    usage metadata in a dict payload. When a dict is returned, we inspect common
    usage fields. For plain strings we use a length-based token approximation
    (~4 chars/token, 75% input / 25% output heuristic) to produce a non-zero
    cost estimate.

    Returns 0.0 if cost cannot be determined or reply is falsy.
    """
    if not reply:
        return 0.0
    try:
        model = ""
        if isinstance(reply, dict):
            # Some AG2 backends return {"content": "...", "usage": {...}, "model": "..."}
            model = reply.get("model") or reply.get("model_name") or ""
            usage = reply.get("usage") or reply.get("usage_metadata")
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                completion_tokens = usage.get("completion_tokens") or usage.get(
                    "output_tokens"
                )
                if prompt_tokens is not None and completion_tokens is not None:
                    return estimate_cost_usd(
                        model, int(prompt_tokens), int(completion_tokens)
                    )
                total = usage.get("total_tokens")
                if total is not None and int(total) > 0:
                    tokens_in = max(1, int(total) * 3 // 4)
                    tokens_out = int(total) - tokens_in
                    return estimate_cost_usd(model, tokens_in, tokens_out)
                # usage dict present but no recognised token fields -- schema drift
                logger.warning(
                    "[VERONICA_AG2] Cost extraction falling back to length estimate: "
                    "usage dict present but no recognised token fields "
                    "(prompt_tokens/input_tokens/completion_tokens/output_tokens/"
                    "total_tokens). usage keys=%s",
                    list(usage.keys()),
                )
            # Dict without recognisable usage -- fall through to length estimate
            content = reply.get("content") or ""
            text = str(content)
        else:
            text = str(reply)

        # Length-based approximation: ~4 chars per token, 75% input / 25% output
        char_count = len(text)
        if char_count <= 0:
            return 0.0
        total_tokens = max(1, char_count // 4)
        tokens_in = max(1, total_tokens * 3 // 4)
        tokens_out = total_tokens - tokens_in
        return estimate_cost_usd(model, tokens_in, tokens_out)
    except Exception:
        return 0.0


# _emit_ag2_otel_event is imported from _ag2_helpers at the top of this module.
