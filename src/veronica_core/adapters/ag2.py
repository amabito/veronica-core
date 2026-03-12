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

from veronica_core.adapters._shared import (
    build_adapter_container,
    build_container,
    check_and_halt,
    safe_emit,
)
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt

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

        reply = super().generate_reply(messages=messages, sender=sender, **kwargs)

        # Increment step counter only when a reply was produced.
        # None means the agent declined to reply -- no LLM call was made.
        if reply is not None and self._container.step_guard is not None:
            self._container.step_guard.step()

        return reply

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="AG2",
            supports_cost_extraction=True,
            supports_token_extraction=True,
            supported_versions=("0.4.0", "0.6.99"),
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
        NOTE: register_reply() provides no after-hook. record_success/failure
        unavailable via this pattern. Use CircuitBreakerCapability instead for
        circuit breaking.

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
        """
        check_and_halt(container, tag="[VERONICA_AG2]", _logger=logger)

        # Increment step counter; actual reply generated by next handler
        if container.step_guard is not None:
            container.step_guard.step()

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


# ---------------------------------------------------------------------------
# OTel helpers (no-op when OTel is not enabled)
# ---------------------------------------------------------------------------


def _emit_ag2_otel_event(
    agent_name: str, decision: str, reason: str, check_type: str
) -> None:
    """Add a veronica containment event to the current OTel span.

    No-op if OTel is not enabled. Never raises.
    """
    try:
        from veronica_core.otel import emit_containment_decision

        emit_containment_decision(
            decision_name=decision,
            reason=f"[{check_type}] {agent_name}: {reason}",
        )
    except Exception:
        # Intentionally swallowed: this helper is declared "Never raises";
        # OTel emission is best-effort telemetry that must not disrupt agent
        # containment logic.
        pass
