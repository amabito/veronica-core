"""veronica_core.adapters.ag2 — AG2 (AutoGen2) ConversableAgent adapter.

Integrates VERONICA policy enforcement into AG2 pipelines via two
complementary approaches: subclassing and hook-based registration.

This module raises ImportError on import if ag2 is not installed.

Public API:
    VeronicaConversableAgent — ConversableAgent subclass that enforces
        budget, step count, and retry limits on every generate_reply() call.
    register_veronica_hook — Function-based alternative; registers a reply
        function via ag2's register_reply() for users who prefer composition
        over inheritance.

Usage (subclass)::

    from ag2 import ConversableAgent
    from veronica_core.adapters.ag2 import VeronicaConversableAgent
    from veronica_core import GuardConfig

    agent = VeronicaConversableAgent(
        "assistant",
        config=GuardConfig(max_cost_usd=1.0, max_steps=20),
        system_message="You are a helpful assistant.",
    )
    reply = agent.generate_reply(messages=[{"role": "user", "content": "Hello!"}])

Usage (hook)::

    import ag2
    from veronica_core.adapters.ag2 import register_veronica_hook
    from veronica_core import GuardConfig

    agent = ag2.ConversableAgent("assistant")
    register_veronica_hook(agent, GuardConfig(max_cost_usd=1.0, max_steps=20))
"""
from __future__ import annotations

try:
    import ag2  # type: ignore[import]
    from ag2 import ConversableAgent  # type: ignore[import]
except ImportError as _exc:
    raise ImportError(
        "veronica_core.adapters.ag2 requires ag2. "
        "Install with: pip install ag2"
    ) from _exc

import logging
from typing import Any, Dict, List, Optional, Union

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.budget import BudgetEnforcer
from veronica_core.container import AIcontainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.retry import RetryContainer

logger = logging.getLogger(__name__)

__all__ = ["VeronicaConversableAgent", "register_veronica_hook"]


def _build_container(config: Union[GuardConfig, ExecutionConfig]) -> AIcontainer:
    """Build an AIcontainer from a GuardConfig or ExecutionConfig."""
    return AIcontainer(
        budget=BudgetEnforcer(limit_usd=config.max_cost_usd),
        retry=RetryContainer(max_retries=config.max_retries_total),
        step_guard=AgentStepGuard(max_steps=config.max_steps),
    )


class VeronicaConversableAgent(ConversableAgent):
    """AG2 ConversableAgent subclass that enforces VERONICA policies.

    Wraps ``generate_reply()`` with a pre-call policy check via AIcontainer.
    Raises VeronicaHalt before the LLM is invoked if any policy denies.

    On each ``generate_reply()`` call:
    - **Pre-call**: policy check via AIcontainer.check().
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
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._container = _build_container(config)

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
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

        reply = super().generate_reply(messages=messages, sender=sender, **kwargs)

        # Increment step counter after successful call
        if self._container.step_guard is not None:
            self._container.step_guard.step()

        return reply

    @property
    def container(self) -> AIcontainer:
        """The underlying AIcontainer (for testing and introspection)."""
        return self._container


def register_veronica_hook(
    agent: ConversableAgent,
    config: Union[GuardConfig, ExecutionConfig],
) -> AIcontainer:
    """Register a VERONICA policy-enforcement reply function on an existing agent.

    Alternative to subclassing: injects a policy-checking reply function into
    *agent* via ``agent.register_reply()``. The hook is registered at the
    highest priority (position 0) so policies are checked before any other
    reply function.

    Args:
        agent: An existing ``ag2.ConversableAgent`` instance to instrument.
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.

    Returns:
        The AIcontainer created for this agent (for testing and introspection).

    Raises:
        VeronicaHalt: When a policy denies execution inside the registered hook.

    Example::

        import ag2
        from veronica_core.adapters.ag2 import register_veronica_hook
        from veronica_core import GuardConfig

        agent = ag2.ConversableAgent("assistant")
        container = register_veronica_hook(agent, GuardConfig(max_cost_usd=1.0))
    """
    container = _build_container(config)

    def _veronica_reply_fn(
        recipient: ConversableAgent,
        messages: Optional[List[Dict[str, Any]]] = None,
        sender: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> tuple[bool, None]:
        """Policy-check reply function registered via register_reply().

        Returns (False, None) to indicate it did not produce a reply, allowing
        subsequent reply functions to run. Raises VeronicaHalt to abort.
        """
        decision = container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

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
