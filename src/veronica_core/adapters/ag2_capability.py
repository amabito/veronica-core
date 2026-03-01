"""CircuitBreakerCapability — AG2 AgentCapability-compatible circuit breaker.

Follows AG2's AgentCapability.add_to_agent() pattern.
Does NOT require ag2 to be installed — works with any object that has
a generate_reply() method (autogen.ConversableAgent, stub agents, etc.).

Public API:
    CircuitBreakerCapability -- add_to_agent() injects a circuit breaker
        into generate_reply() without requiring the caller to change
        how they invoke the agent.

Example::

    from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

    cap = CircuitBreakerCapability(failure_threshold=3, recovery_timeout=60)
    cap.add_to_agent(planner)
    cap.add_to_agent(executor)

    # Calling code is unchanged:
    reply = agent.generate_reply(messages)

    # SAFE_MODE support (optional):
    from veronica_core import VeronicaIntegration
    from veronica_core.backends import MemoryBackend

    veronica = VeronicaIntegration(backend=MemoryBackend())
    cap = CircuitBreakerCapability(failure_threshold=3, veronica=veronica)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.state import VeronicaState

if TYPE_CHECKING:
    from veronica_core.shield.token_budget import TokenBudgetHook

logger = logging.getLogger(__name__)

__all__ = ["CircuitBreakerCapability"]


class CircuitBreakerCapability:
    """Circuit breaker capability for AG2 (and compatible) agents.

    Follows AG2's AgentCapability pattern: call ``add_to_agent(agent)``
    once per agent.  Subsequent calls to ``agent.generate_reply()`` go
    through the circuit breaker transparently — no changes needed at the
    call site.

    Each agent gets its own independent ``CircuitBreaker`` instance.
    A shared ``VeronicaIntegration`` can be passed for SAFE_MODE support:
    when the system-wide state is SAFE_MODE, all agents are blocked
    regardless of their individual circuit state.

    Args:
        failure_threshold: Consecutive ``None`` replies before the circuit
            opens.  Defaults to 3.
        recovery_timeout: Seconds before transitioning from OPEN to
            HALF_OPEN and attempting one test call.  Defaults to 60.0.
        veronica: Optional ``VeronicaIntegration`` instance.  When
            provided, SAFE_MODE checks are applied before the per-agent
            circuit check.

    Example::

        cap = CircuitBreakerCapability(failure_threshold=3, recovery_timeout=60)
        cap.add_to_agent(planner)
        cap.add_to_agent(executor)

        # Ordinary call — circuit breaker is transparent:
        reply = planner.generate_reply(messages)

        # Inspect circuit state:
        breaker = cap.get_breaker("planner")
        print(breaker.state)   # CircuitState.CLOSED / OPEN / HALF_OPEN
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        veronica: Optional[Any] = None,
        token_budget_hook: Optional["TokenBudgetHook"] = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._veronica = veronica
        self._token_budget_hook = token_budget_hook
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._originals: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_to_agent(self, agent: Any) -> CircuitBreaker:
        """Inject a circuit breaker into *agent*.

        Wraps ``agent.generate_reply`` so that:

        * When the system is in SAFE_MODE (and ``veronica`` was provided),
          the call is blocked and ``None`` is returned immediately.
        * When the agent's circuit is OPEN, the call is blocked and
          ``None`` is returned without invoking the original method.
        * Otherwise, the original ``generate_reply`` is called and its
          result is used to update the circuit state (``record_success``
          or ``record_failure``).

        Calling ``add_to_agent`` on the same agent a second time is a
        no-op (a warning is logged and the existing breaker is returned).

        AG2 equivalent::

            # This method performs the equivalent of:
            agent.register_reply(
                trigger=lambda _: True,
                reply_func=self._circuit_breaker_reply,
                position=0,
            )
            # plus the post-call result recording that register_reply
            # alone cannot provide.

        Args:
            agent: Any object with a ``generate_reply`` method.
                   Compatible with ``autogen.ConversableAgent``.

        Returns:
            The ``CircuitBreaker`` instance bound to this agent.
        """
        name = getattr(agent, "name", repr(agent))

        if name in self._breakers:
            logger.warning(
                "[VERONICA_CAP] add_to_agent called twice on '%s' -- skipping", name
            )
            return self._breakers[name]

        breaker = CircuitBreaker(
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
        )
        self._breakers[name] = breaker

        original_generate_reply = agent.generate_reply
        self._originals[name] = original_generate_reply
        cap = self  # explicit capture to avoid late-binding

        def _guarded_generate_reply(*args: Any, **kwargs: Any) -> Optional[Any]:
            # System-wide halt check (SAFE_MODE)
            if cap._veronica is not None:
                if cap._veronica.state.current_state == VeronicaState.SAFE_MODE:
                    logger.debug(
                        "[VERONICA_CAP] %s blocked: SAFE_MODE active", name
                    )
                    return None

            # Per-agent circuit check (uses check() to enforce HALF_OPEN
            # single-request limit via _half_open_in_flight counter)
            cb_decision = breaker.check(PolicyContext())
            if not cb_decision.allowed:
                logger.debug(
                    "[VERONICA_CAP] %s blocked: %s",
                    name,
                    cb_decision.reason,
                )
                return None

            # Token budget check
            if cap._token_budget_hook is not None:
                ctx = ToolCallContext(request_id=str(uuid4()), tool_name="llm")
                decision = cap._token_budget_hook.before_llm_call(ctx)
                if decision == Decision.HALT:
                    logger.debug(
                        "[VERONICA_CAP] %s blocked: token budget HALT", name
                    )
                    return None

            # Invoke the original generate_reply
            reply = original_generate_reply(*args, **kwargs)

            # Record token usage after successful reply
            if reply is not None and cap._token_budget_hook is not None:
                cap._token_budget_hook.record_usage(
                    output_tokens=len(str(reply)) // 4
                )

            # Record result to drive state transitions
            if reply is None:
                breaker.record_failure()
            else:
                breaker.record_success()

            return reply

        agent.generate_reply = _guarded_generate_reply

        logger.debug("[VERONICA_CAP] Circuit breaker injected into '%s'", name)
        return breaker

    def remove_from_agent(self, agent: Any) -> None:
        """Remove the circuit breaker previously injected into *agent*.

        Restores ``agent.generate_reply`` to its original implementation and
        removes the associated ``CircuitBreaker`` from this capability.

        If *agent* was not registered with this capability, a warning is logged
        and the call is a no-op.

        Args:
            agent: The agent from which to remove the circuit breaker.
                   Must be the same object that was passed to ``add_to_agent``.
        """
        name = getattr(agent, "name", repr(agent))
        if name not in self._originals:
            logger.warning(
                "[VERONICA_CAP] remove_from_agent called on unregistered agent '%s' -- skipping",
                name,
            )
            return
        agent.generate_reply = self._originals.pop(name)
        del self._breakers[name]
        logger.debug("[VERONICA_CAP] Circuit breaker removed from '%s'", name)

    def get_breaker(self, agent_name: str) -> Optional[CircuitBreaker]:
        """Return the ``CircuitBreaker`` for *agent_name*, or ``None``."""
        return self._breakers.get(agent_name)

    @property
    def breakers(self) -> Dict[str, CircuitBreaker]:
        """Snapshot of all agent-name → CircuitBreaker mappings."""
        return dict(self._breakers)
