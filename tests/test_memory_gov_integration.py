"""Integration tests for v3.3 MemoryGovernor wiring in ExecutionContext and AIContainer.

Tests:
1.  ExecutionContext with MemoryGovernor that ALLOWs -- wrap succeeds
2.  ExecutionContext with MemoryGovernor that DENYs -- wrap returns HALT
3.  ExecutionContext with no MemoryGovernor (None) -- wrap succeeds (backward compat)
4.  MemoryGovernor with QUARANTINE verdict -- wrap still succeeds (non-DENY)
5.  MemoryGovernor.notify_after() is called on success path
6.  AIContainer with MemoryGovernor that DENYs -- check returns denied
7.  AIContainer with MemoryGovernor that ALLOWs -- check returns allowed
8.  Child context (spawn_child) inherits memory_governor from parent
9.  Child context (create_child) inherits policy_view_holder from parent
"""

from __future__ import annotations

from typing import Any


from veronica_core.container.aicontainer import AIContainer
from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook, DenyAllMemoryGovernanceHook
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(max_cost: float = 10.0, max_steps: int = 50, max_retries: int = 10) -> ExecutionConfig:
    return ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=max_steps,
        max_retries_total=max_retries,
    )


def _allow_governor() -> MemoryGovernor:
    """MemoryGovernor that ALLOWs all operations."""
    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(DefaultMemoryGovernanceHook())
    return gov


def _deny_governor() -> MemoryGovernor:
    """MemoryGovernor that DENYs all operations."""
    gov = MemoryGovernor(fail_closed=True)
    gov.add_hook(DenyAllMemoryGovernanceHook())
    return gov


def _make_verdict_hook(verdict: GovernanceVerdict) -> Any:
    """Return a hook that always returns the given verdict."""

    class _VerdictHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=verdict,
                reason=f"forced {verdict.value}",
                policy_id="test_hook",
                operation=operation,
            )

        def after_op(
            self,
            operation: MemoryOperation,
            decision: MemoryGovernanceDecision,
            result: Any = None,
            error: BaseException | None = None,
        ) -> None:
            pass

    return _VerdictHook()


# ---------------------------------------------------------------------------
# Category 1-4: ExecutionContext + MemoryGovernor
# ---------------------------------------------------------------------------


def test_execution_context_with_allow_governor_wrap_succeeds():
    """ExecutionContext with ALLOW governor -- wrap_llm_call returns ALLOW."""
    gov = _allow_governor()
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.ALLOW
    assert called == [1], "fn must be called when governor ALLOWs"


def test_execution_context_with_deny_governor_wrap_returns_halt():
    """ExecutionContext with DENY governor -- wrap_llm_call returns HALT before fn."""
    gov = _deny_governor()
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.HALT
    assert called == [], "fn must NOT be called when governor DENYs"


def test_execution_context_without_governor_wrap_succeeds():
    """ExecutionContext with no MemoryGovernor -- wrap_llm_call succeeds (backward compat)."""
    ctx = ExecutionContext(config=_cfg(), memory_governor=None)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.ALLOW
    assert called == [1]


def test_execution_context_with_quarantine_governor_wrap_succeeds():
    """QUARANTINE verdict is non-DENY -- wrap should not halt."""
    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.ALLOW
    assert called == [1], "QUARANTINE is not DENY -- fn must be called"


def test_execution_context_with_degrade_governor_wrap_succeeds():
    """DEGRADE verdict is non-DENY -- wrap should not halt."""
    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    called = []
    decision = ctx.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.ALLOW
    assert called == [1], "DEGRADE is not DENY -- fn must be called"


# ---------------------------------------------------------------------------
# Category 5: notify_after called on success path
# ---------------------------------------------------------------------------


def test_execution_context_notify_after_called_on_success():
    """MemoryGovernor.notify_after() is invoked once per successful wrap call."""
    after_calls: list[tuple[str, str]] = []

    class _SpyHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                reason="allow",
                policy_id="spy",
                operation=operation,
            )

        def after_op(
            self,
            operation: MemoryOperation,
            decision: MemoryGovernanceDecision,
            result: Any = None,
            error: BaseException | None = None,
        ) -> None:
            after_calls.append((operation.action.value, decision.verdict.value))

    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_SpyHook())
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    ctx.wrap_llm_call(fn=lambda: None)

    assert len(after_calls) == 1
    # The post-dispatch notification is always sent with ALLOW verdict.
    assert after_calls[0][1] == GovernanceVerdict.ALLOW.value


def test_execution_context_notify_after_not_called_on_deny():
    """MemoryGovernor.notify_after() is NOT called when pre-dispatch is denied."""
    after_calls: list[str] = []

    class _DenySpyHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="deny",
                policy_id="deny_spy",
                operation=operation,
            )

        def after_op(
            self,
            operation: MemoryOperation,
            decision: MemoryGovernanceDecision,
            result: Any = None,
            error: BaseException | None = None,
        ) -> None:
            after_calls.append("called")

    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_DenySpyHook())
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    ctx.wrap_llm_call(fn=lambda: None)

    # Bug #10 fix: notify_after is now called symmetrically even on DENY,
    # matching evaluate_message() behavior. Hooks need after-notification
    # for cleanup regardless of verdict.
    assert after_calls == ["called"], "notify_after must fire even when governor DENYs"


def test_execution_context_notify_after_called_for_tool_call_success():
    """notify_after is called for successful tool (non-llm) calls too."""
    after_actions: list[str] = []

    class _ToolSpyHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                reason="allow",
                policy_id="tool_spy",
                operation=operation,
            )

        def after_op(
            self,
            operation: MemoryOperation,
            decision: MemoryGovernanceDecision,
            result: Any = None,
            error: BaseException | None = None,
        ) -> None:
            after_actions.append(operation.action.value)

    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_ToolSpyHook())
    ctx = ExecutionContext(config=_cfg(), memory_governor=gov)

    ctx.wrap_tool_call(fn=lambda: None)

    assert len(after_actions) == 1
    # Tool calls use READ action.
    assert after_actions[0] == MemoryAction.READ.value


# ---------------------------------------------------------------------------
# Category 6-7: AIContainer + MemoryGovernor
# ---------------------------------------------------------------------------


def test_aicontainer_with_deny_governor_check_returns_denied():
    """AIContainer with DENY governor -- check() returns PolicyDecision(allowed=False)."""
    gov = _deny_governor()
    container = AIContainer(memory_governor=gov)

    decision = container.check(cost_usd=0.0, step_count=0)

    assert not decision.allowed
    assert "memory governance" in decision.reason.lower()


def test_aicontainer_with_allow_governor_check_returns_allowed():
    """AIContainer with ALLOW governor -- check() returns PolicyDecision(allowed=True)."""
    gov = _allow_governor()
    container = AIContainer(memory_governor=gov)

    decision = container.check(cost_usd=0.0, step_count=0)

    assert decision.allowed


def test_aicontainer_without_governor_check_returns_allowed():
    """AIContainer with no governor (None) -- check() still returns allowed."""
    container = AIContainer(memory_governor=None)

    decision = container.check(cost_usd=0.0, step_count=0)

    assert decision.allowed


def test_aicontainer_with_deny_governor_policy_type_is_memory_governance():
    """Denied decision from governor has policy_type='memory_governance'."""
    gov = _deny_governor()
    container = AIContainer(memory_governor=gov)

    decision = container.check()

    assert not decision.allowed
    assert decision.policy_type == "memory_governance"


def test_aicontainer_governor_not_evaluated_when_pipeline_denies():
    """Governor is not evaluated if pipeline already denied."""
    from veronica_core.budget import BudgetEnforcer

    # BudgetEnforcer with limit=0.0: any positive cost triggers a denial.
    budget = BudgetEnforcer(limit_usd=0.0)

    # Governor would DENY too -- but it should not be reached.
    after_calls: list[str] = []

    class _SpyGovernorHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            after_calls.append("evaluated")
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="deny",
                policy_id="spy",
                operation=operation,
            )

        def after_op(
            self,
            operation: MemoryOperation,
            decision: MemoryGovernanceDecision,
            result: Any = None,
            error: BaseException | None = None,
        ) -> None:
            pass

    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(_SpyGovernorHook())
    container = AIContainer(budget=budget, memory_governor=gov)

    # cost_usd=0.01 triggers budget denial before governor is evaluated.
    decision = container.check(cost_usd=0.01)

    assert not decision.allowed
    # Governor hook must not have been called (pipeline short-circuited first).
    assert after_calls == [], "governor must not be evaluated when pipeline denies"


# ---------------------------------------------------------------------------
# Category 8: spawn_child inherits memory_governor
# ---------------------------------------------------------------------------


def test_spawn_child_inherits_memory_governor():
    """Child created via spawn_child has the same memory_governor as parent."""
    gov = _allow_governor()
    parent = ExecutionContext(config=_cfg(), memory_governor=gov)

    child = parent.spawn_child(max_cost_usd=1.0)

    assert child._memory_governor is gov


def test_spawn_child_inherits_none_memory_governor():
    """spawn_child with no parent governor yields child with no governor."""
    parent = ExecutionContext(config=_cfg(), memory_governor=None)

    child = parent.spawn_child(max_cost_usd=1.0)

    assert child._memory_governor is None


def test_spawn_child_deny_governor_halts_child_wrap():
    """Child inherits DENY governor and its wrap calls are halted."""
    gov = _deny_governor()
    parent = ExecutionContext(config=_cfg(), memory_governor=gov)
    child = parent.spawn_child(max_cost_usd=5.0)

    called = []
    decision = child.wrap_llm_call(fn=lambda: called.append(1))

    assert decision == Decision.HALT
    assert called == []


# ---------------------------------------------------------------------------
# Category 9: create_child inherits policy_view_holder
# ---------------------------------------------------------------------------


def test_create_child_inherits_policy_view_holder():
    """Child created via create_child has the same policy_view_holder as parent."""
    from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata
    from veronica_core.policy.verifier import PolicyVerifier

    bundle = PolicyBundle(
        metadata=PolicyMetadata(policy_id="test-policy"),
        rules=(),
    )
    result = PolicyVerifier().verify(bundle)
    view = FrozenPolicyView(bundle, result)
    holder = PolicyViewHolder(initial=view)

    parent = ExecutionContext(config=_cfg(), policy_view_holder=holder)
    child = parent.create_child(
        agent_name="agent-a",
        agent_names=["agent-a", "agent-b"],
    )

    assert child._policy_view_holder is holder


def test_create_child_inherits_none_policy_view_holder():
    """create_child with no holder yields child with no holder."""
    parent = ExecutionContext(config=_cfg(), policy_view_holder=None)
    child = parent.create_child(
        agent_name="agent-a",
        agent_names=["agent-a"],
    )

    assert child._policy_view_holder is None


def test_spawn_child_inherits_policy_view_holder():
    """Child created via spawn_child also inherits policy_view_holder."""
    holder = PolicyViewHolder(initial=None)
    parent = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    child = parent.spawn_child(max_cost_usd=1.0)

    assert child._policy_view_holder is holder
