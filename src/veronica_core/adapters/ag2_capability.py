"""CircuitBreakerCapability -- AG2 AgentCapability-compatible circuit breaker.

Follows AG2's AgentCapability.add_to_agent() pattern.
Does NOT require ag2 to be installed -- works with any object that has
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
import weakref
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

from veronica_core.adapters._ag2_helpers import (
    _AG2_SUPPORTED_VERSIONS,
    emit_ag2_otel_event as _emit_ag2_otel_event,
)
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.state import VeronicaState

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities
    from veronica_core.shield.token_budget import TokenBudgetHook

logger = logging.getLogger(__name__)

__all__ = ["CircuitBreakerCapability"]


class CircuitBreakerCapability:
    """Circuit breaker capability for AG2 (and compatible) agents.

    Follows AG2's AgentCapability pattern: call ``add_to_agent(agent)``
    once per agent.  Subsequent calls to ``agent.generate_reply()`` go
    through the circuit breaker transparently -- no changes needed at the
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

        # Ordinary call -- circuit breaker is transparent:
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
        # _originals stores weak references to the original bound methods so
        # that this capability does not prevent agent GC. WeakMethod is used
        # for bound methods; plain weakref.ref is used as fallback for
        # non-method callables (e.g. closures, lambda, function objects).
        self._originals: Dict[str, Any] = {}  # agent_key -> weakref
        self._agent_names: Dict[str, str] = {}  # agent_key -> display name

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
        # Use a UUID tag stored on the agent to avoid id() GC-reuse collisions.
        agent_key = getattr(agent, "_veronica_agent_key", None)
        name = getattr(agent, "name", repr(agent))

        if agent_key is not None and agent_key in self._breakers:
            logger.warning(
                "[VERONICA_CAP] add_to_agent called twice on '%s' -- skipping", name
            )
            return self._breakers[agent_key]

        agent_key = str(uuid4())
        agent._veronica_agent_key = agent_key

        breaker = CircuitBreaker(
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
        )
        self._breakers[agent_key] = breaker
        self._agent_names[agent_key] = name

        original_generate_reply = agent.generate_reply
        # Store a weak reference so this capability does not prevent agent GC.
        # WeakMethod works for bound methods; fall back to weakref.ref for
        # plain callables (closures, lambdas, unbound functions).
        # The closure uses this same weak reference when invoking the original,
        # so neither _originals nor the closure holds a strong ref to the agent.
        try:
            _original_ref: Any = weakref.WeakMethod(original_generate_reply)
        except TypeError:
            _original_ref = weakref.ref(original_generate_reply)
        self._originals[agent_key] = _original_ref
        cap = self  # explicit capture to avoid late-binding

        def _guarded_generate_reply(*args: Any, **kwargs: Any) -> Optional[Any]:
            # System-wide halt check (SAFE_MODE)
            if cap._veronica is not None:
                if cap._veronica.state.current_state == VeronicaState.SAFE_MODE:
                    logger.debug("[VERONICA_CAP] %s blocked: SAFE_MODE active", name)
                    _emit_ag2_otel_event(name, "HALT", "SAFE_MODE active", "safe_mode")
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
                _emit_ag2_otel_event(
                    name,
                    "HALT",
                    cb_decision.reason or "circuit open",
                    "circuit_breaker",
                )
                return None

            # Token budget check
            if cap._token_budget_hook is not None:
                ctx = ToolCallContext(request_id=str(uuid4()), tool_name="llm")
                decision = cap._token_budget_hook.before_llm_call(ctx)
                if decision == Decision.HALT:
                    logger.debug("[VERONICA_CAP] %s blocked: token budget HALT", name)
                    _emit_ag2_otel_event(
                        name, "HALT", "token budget exceeded", "token_budget"
                    )
                    return None

            # Emit ALLOW event before invoking the original
            _emit_ag2_otel_event(name, "ALLOW", "all checks passed", "pre_call")

            # Dereference the weak reference to obtain the original method.
            # If the agent was GC'd, fn is None; treat as a circuit failure.
            fn = _original_ref()
            if fn is None:
                logger.warning(
                    "[VERONICA_CAP] %s: original generate_reply was GC'd; "
                    "treating as failure",
                    name,
                )
                breaker.record_failure()
                return None

            # Invoke the original generate_reply
            try:
                reply = fn(*args, **kwargs)
            except Exception as exc:
                breaker.record_failure(error=exc)
                _emit_ag2_otel_event(
                    name,
                    "FAILURE",
                    "generate_reply raised exception",
                    "post_call",
                )
                raise

            # Record token usage after successful reply.
            # Approximation: len(str(reply)) // 4 estimates token count using
            # the ~4-chars-per-token heuristic.  This is known to underestimate
            # for multi-byte content (CJK, emoji: 2-4x error) and to overestimate
            # for structured JSON (str() overhead).  Acceptable for budget
            # tracking but not for billing-grade accuracy.
            if reply is not None and cap._token_budget_hook is not None:
                cap._token_budget_hook.record_usage(output_tokens=len(str(reply)) // 4)

            # Record result to drive state transitions
            if reply is None:
                breaker.record_failure()
                _emit_ag2_otel_event(
                    name, "FAILURE", "generate_reply returned None", "post_call"
                )
            else:
                breaker.record_success()

            return reply

        agent.generate_reply = _guarded_generate_reply

        logger.debug("[VERONICA_CAP] Circuit breaker injected into '%s'", name)
        return breaker

    def _remove_by_key(self, agent_key: str) -> None:
        """Remove all state for *agent_key*.

        Called directly by remove_from_agent().  Safe to call multiple times
        for the same key (idempotent).
        """
        self._originals.pop(agent_key, None)
        self._breakers.pop(agent_key, None)
        self._agent_names.pop(agent_key, None)

    def cleanup(self) -> int:
        """Prune entries for agents that have been garbage-collected.

        B3-H1: _breakers, _agent_names, and _originals are keyed by UUID
        strings and grow without bound when many short-lived agents are
        wrapped over a long-running process.  Callers should invoke cleanup()
        periodically (e.g. in a maintenance task or after each batch of
        add_to_agent calls) to reclaim memory.

        Returns the number of entries pruned.
        """
        return self._cleanup_dead_refs()

    def _cleanup_dead_refs(self) -> int:
        """Internal: prune entries whose weakref to the original method is dead.

        Note: the original method weakref dies as soon as add_to_agent()
        returns because the wrapped closure is the only reference held by the
        agent.  This method therefore prunes all registered agents that have
        since called remove_from_agent() or been cleaned up by other means.
        Use remove_from_agent() or cleanup() for the primary GC mechanism.

        Returns the number of entries pruned.
        """
        dead_keys = [k for k, ref in list(self._originals.items()) if ref() is None]
        for k in dead_keys:
            self._remove_by_key(k)
        return len(dead_keys)

    def remove_from_agent(self, agent: Any) -> None:
        """Remove the circuit breaker previously injected into *agent*.

        Restores ``agent.generate_reply`` to its original implementation and
        removes the associated ``CircuitBreaker`` from this capability.

        If *agent* was not registered with this capability, a warning is logged
        and the call is a no-op.

        Warning:
            Circuit breaker state is NOT preserved across remove/re-add cycles.
            Calling ``remove_from_agent`` followed by ``add_to_agent`` assigns a
            fresh ``CircuitBreaker`` with zero failure history. Any accumulated
            failure counts and state transitions (OPEN, HALF_OPEN) from before
            the removal are permanently discarded.

        Args:
            agent: The agent from which to remove the circuit breaker.
                   Must be the same object that was passed to ``add_to_agent``.
        """
        agent_key = getattr(agent, "_veronica_agent_key", None)
        name = getattr(agent, "name", repr(agent))
        if agent_key is None or agent_key not in self._originals:
            logger.warning(
                "[VERONICA_CAP] remove_from_agent called on unregistered agent '%s' -- skipping",
                name,
            )
            return
        # Dereference the weakref stored in _originals to restore the method.
        # If the weak reference is dead (agent was partially GC'd), fall back
        # to leaving generate_reply as-is rather than crashing.
        original_ref = self._originals.pop(agent_key)
        original = original_ref()  # dereference WeakMethod / weakref.ref
        if original is not None:
            agent.generate_reply = original
        else:
            logger.warning(
                "[VERONICA_CAP] remove_from_agent: original generate_reply for '%s' "
                "was GC'd before removal; generate_reply NOT restored",
                name,
            )
        self._remove_by_key(agent_key)
        try:
            del agent._veronica_agent_key
        except AttributeError:
            pass
        logger.debug("[VERONICA_CAP] Circuit breaker removed from '%s'", name)

    def get_breaker(self, agent_name: str) -> Optional[CircuitBreaker]:
        """Return the ``CircuitBreaker`` for *agent_name*, or ``None``.

        Searches by display name. If multiple agents share the same name
        (which is valid since v3.5), the first match is returned.

        Note: O(n) scan over registered agents -- acceptable because typical
        agent count is small (< 100).
        """
        for agent_key, name in self._agent_names.items():
            if name == agent_name:
                return self._breakers.get(agent_key)
        return None

    @property
    def breakers(self) -> Dict[str, CircuitBreaker]:
        """Snapshot of all agent-name -> CircuitBreaker mappings.

        When multiple agents share the same display name, the first-registered
        agent's breaker is returned for that name -- consistent with
        get_breaker() semantics. Subsequent same-named agents are still tracked
        and their breakers are accessible via get_breaker() (which also returns
        the first match).
        """
        result: Dict[str, CircuitBreaker] = {}
        for agent_key, name in self._agent_names.items():
            if name not in result and agent_key in self._breakers:
                result[name] = self._breakers[agent_key]
        return result

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="AG2",
            supports_cost_extraction=True,
            supports_token_extraction=True,
            supported_versions=_AG2_SUPPORTED_VERSIONS,
        )


# _emit_ag2_otel_event is imported from _ag2_helpers at the top of this module.
