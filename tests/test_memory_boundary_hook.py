"""Tests for MemoryBoundaryHook (Issue #71).

Covers:
- Allow-all default (no config)
- Deny specific agent from writing to a namespace
- Deny specific agent from reading a namespace
- Wildcard rules
- Rule specificity (exact beats wildcard)
- MemoryGovernor integration (before_op / after_op protocols)
- PostDispatchHook path (after_llm_call with memory kind metadata)
- Concurrent access (5 threads)
- Adversarial: empty agent_id, empty namespace, None values, non-memory calls
- deny_count observable counter
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryOperation,
)
from veronica_core.shield.memory_boundary import (
    MemoryAccessRule,
    MemoryBoundaryConfig,
    MemoryBoundaryHook,
)
from veronica_core.shield.types import ToolCallContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    kind: str | None = None,
    agent_id: str | None = None,
    namespace: str | None = None,
    request_id: str = "req-1",
) -> ToolCallContext:
    meta: dict[str, Any] = {}
    if kind is not None:
        meta["kind"] = kind
    if agent_id is not None:
        meta["agent_id"] = agent_id
    if namespace is not None:
        meta["namespace"] = namespace
    return ToolCallContext(request_id=request_id, metadata=meta)


def _read_op(agent_id: str = "agent-1", namespace: str = "ns-a") -> MemoryOperation:
    return MemoryOperation(
        action=MemoryAction.READ, agent_id=agent_id, namespace=namespace
    )


def _write_op(agent_id: str = "agent-1", namespace: str = "ns-a") -> MemoryOperation:
    return MemoryOperation(
        action=MemoryAction.WRITE, agent_id=agent_id, namespace=namespace
    )


# ---------------------------------------------------------------------------
# Default (no config) -- allow all
# ---------------------------------------------------------------------------


class TestDefaultDenyAll:
    """Default is deny-all (fail-closed) when no rules configured."""

    def test_no_config_denies_memory_read(self) -> None:
        hook = MemoryBoundaryHook()
        decision = hook.before_op(_read_op(), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_no_config_denies_memory_write(self) -> None:
        hook = MemoryBoundaryHook()
        decision = hook.before_op(_write_op(), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_explicit_allow_all_with_flag(self) -> None:
        config = MemoryBoundaryConfig(rules=[], default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        decision = hook.before_op(_read_op(agent_id="unknown-agent"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_default_deny_no_rules_denies(self) -> None:
        config = MemoryBoundaryConfig(rules=[], default_allow=False)
        hook = MemoryBoundaryHook(config=config)
        decision = hook.before_op(_read_op(), None)
        assert decision.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# Explicit deny rules
# ---------------------------------------------------------------------------


class TestExplicitRules:
    def test_deny_write_for_specific_agent_and_namespace(self) -> None:
        rule = MemoryAccessRule(
            agent_id="bad-agent", namespace="secret", allow_write=False, allow_read=True
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)

        decision = hook.before_op(
            _write_op(agent_id="bad-agent", namespace="secret"), None
        )
        assert decision.verdict is GovernanceVerdict.DENY

    def test_allow_read_when_write_is_denied(self) -> None:
        rule = MemoryAccessRule(
            agent_id="bad-agent", namespace="secret", allow_write=False, allow_read=True
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)

        decision = hook.before_op(
            _read_op(agent_id="bad-agent", namespace="secret"), None
        )
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_deny_read_for_specific_agent_and_namespace(self) -> None:
        rule = MemoryAccessRule(
            agent_id="read-denied", namespace="classified", allow_read=False
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)

        decision = hook.before_op(
            _read_op(agent_id="read-denied", namespace="classified"), None
        )
        assert decision.verdict is GovernanceVerdict.DENY

    def test_other_agent_is_allowed_by_default(self) -> None:
        rule = MemoryAccessRule(
            agent_id="restricted", namespace="vault", allow_write=False
        )
        config = MemoryBoundaryConfig(rules=[rule], default_allow=True)
        hook = MemoryBoundaryHook(config=config)

        # A different agent should still be allowed.
        decision = hook.before_op(
            _write_op(agent_id="other-agent", namespace="vault"), None
        )
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_wildcard_agent_denies_all_agents_write(self) -> None:
        rule = MemoryAccessRule(
            agent_id="*", namespace="locked", allow_write=False, allow_read=True
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)

        for agent in ("agent-1", "agent-2", "any-agent"):
            dec = hook.before_op(_write_op(agent_id=agent, namespace="locked"), None)
            assert dec.verdict is GovernanceVerdict.DENY, (
                f"expected DENY for agent {agent}"
            )

    def test_wildcard_namespace_denies_all_namespaces(self) -> None:
        rule = MemoryAccessRule(
            agent_id="rogue", namespace="*", allow_read=False, allow_write=False
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)

        for ns in ("ns-a", "ns-b", "admin"):
            dec = hook.before_op(_read_op(agent_id="rogue", namespace=ns), None)
            assert dec.verdict is GovernanceVerdict.DENY

    def test_exact_rule_beats_wildcard(self) -> None:
        """Exact agent+namespace rule overrides wildcard rule."""
        wildcard = MemoryAccessRule(
            agent_id="*", namespace="*", allow_write=False, allow_read=True
        )
        exact = MemoryAccessRule(
            agent_id="admin", namespace="ns-a", allow_write=True, allow_read=True
        )
        config = MemoryBoundaryConfig(rules=[wildcard, exact], default_allow=True)
        hook = MemoryBoundaryHook(config=config)

        # admin writing to ns-a: exact rule wins -> ALLOW
        dec_admin = hook.before_op(_write_op(agent_id="admin", namespace="ns-a"), None)
        assert dec_admin.verdict is GovernanceVerdict.ALLOW

        # other agents: wildcard rule applies -> DENY write
        dec_other = hook.before_op(_write_op(agent_id="user", namespace="ns-a"), None)
        assert dec_other.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# Non-read/write actions pass through
# ---------------------------------------------------------------------------


class TestNonReadWritePassThrough:
    def test_archive_action_passes_through(self) -> None:
        rule = MemoryAccessRule(
            agent_id="*", namespace="*", allow_read=False, allow_write=False
        )
        config = MemoryBoundaryConfig(rules=[rule], default_allow=False)
        hook = MemoryBoundaryHook(config=config)

        op = MemoryOperation(
            action=MemoryAction.ARCHIVE, agent_id="agent-1", namespace="ns"
        )
        decision = hook.before_op(op, None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_delete_action_passes_through(self) -> None:
        config = MemoryBoundaryConfig(rules=[], default_allow=False)
        hook = MemoryBoundaryHook(config=config)
        op = MemoryOperation(action=MemoryAction.DELETE, agent_id="a", namespace="n")
        assert hook.before_op(op, None).verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# PostDispatchHook (after_llm_call) path
# ---------------------------------------------------------------------------


class TestPostDispatchHook:
    def test_non_memory_kind_is_ignored(self) -> None:
        hook = MemoryBoundaryHook()
        ctx = _ctx(kind="tool_call", agent_id="agent-1", namespace="ns-a")
        hook.after_llm_call(ctx, response="irrelevant")  # must not raise

    def test_no_kind_in_metadata_is_ignored(self) -> None:
        hook = MemoryBoundaryHook()
        ctx = ToolCallContext(request_id="r1", metadata={})
        hook.after_llm_call(ctx, response=None)  # must not raise

    def test_memory_write_denied_raises_permission_error(self) -> None:
        rule = MemoryAccessRule(agent_id="bad", namespace="vault", allow_write=False)
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)
        ctx = _ctx(kind="memory_write", agent_id="bad", namespace="vault")
        with pytest.raises(PermissionError):
            hook.after_llm_call(ctx, response=None)

    def test_memory_read_denied_raises_permission_error(self) -> None:
        rule = MemoryAccessRule(
            agent_id="spy", namespace="classified", allow_read=False
        )
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)
        ctx = _ctx(kind="memory_read", agent_id="spy", namespace="classified")
        with pytest.raises(PermissionError):
            hook.after_llm_call(ctx, response=None)

    def test_memory_read_allowed_does_not_raise(self) -> None:
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        ctx = _ctx(kind="memory_read", agent_id="legit-agent", namespace="public")
        hook.after_llm_call(ctx, response="data")  # must not raise


# ---------------------------------------------------------------------------
# MemoryGovernor integration
# ---------------------------------------------------------------------------


class TestMemoryGovernorIntegration:
    def test_hook_registers_in_governor(self) -> None:
        hook = MemoryBoundaryHook()
        governor = MemoryGovernor(hooks=[hook], fail_closed=False)
        assert governor.hook_count == 1

    def test_governor_allows_when_hook_allows(self) -> None:
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        governor = MemoryGovernor(hooks=[hook], fail_closed=False)
        decision = governor.evaluate(_read_op())
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_governor_denies_when_hook_denies(self) -> None:
        rule = MemoryAccessRule(agent_id="blocked", namespace="ns", allow_read=False)
        config = MemoryBoundaryConfig(rules=[rule])
        hook = MemoryBoundaryHook(config=config)
        governor = MemoryGovernor(hooks=[hook], fail_closed=False)

        op = MemoryOperation(
            action=MemoryAction.READ, agent_id="blocked", namespace="ns"
        )
        decision = governor.evaluate(op)
        assert decision.verdict is GovernanceVerdict.DENY
        assert "memory_boundary" in decision.policy_id


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestDenyCountObservability:
    def test_deny_count_increments_on_before_op_deny(self) -> None:
        rule = MemoryAccessRule(agent_id="a", namespace="n", allow_write=False)
        hook = MemoryBoundaryHook(config=MemoryBoundaryConfig(rules=[rule]))
        assert hook.deny_count == 0
        hook.before_op(_write_op(agent_id="a", namespace="n"), None)
        assert hook.deny_count == 1

    def test_deny_count_increments_on_after_llm_call_deny(self) -> None:
        rule = MemoryAccessRule(agent_id="spy", namespace="vault", allow_read=False)
        hook = MemoryBoundaryHook(config=MemoryBoundaryConfig(rules=[rule]))
        ctx = _ctx(kind="memory_read", agent_id="spy", namespace="vault")
        with pytest.raises(PermissionError):
            hook.after_llm_call(ctx, response=None)
        assert hook.deny_count == 1


# ---------------------------------------------------------------------------
# Concurrent access (5 threads)
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_concurrent_reads_all_allowed(self) -> None:
        """5 threads reading simultaneously must all succeed without race."""
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        results: list[GovernanceVerdict] = []
        lock = threading.Lock()

        def read_task() -> None:
            dec = hook.before_op(_read_op(agent_id="concurrent-agent"), None)
            with lock:
                results.append(dec.verdict)

        threads = [threading.Thread(target=read_task) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(v is GovernanceVerdict.ALLOW for v in results)
        assert len(results) == 5

    def test_concurrent_deny_count_accuracy(self) -> None:
        """5 threads each triggering 1 deny must yield deny_count == 5."""
        rule = MemoryAccessRule(agent_id="*", namespace="locked", allow_write=False)
        hook = MemoryBoundaryHook(config=MemoryBoundaryConfig(rules=[rule]))

        def write_task() -> None:
            hook.before_op(_write_op(namespace="locked"), None)

        threads = [threading.Thread(target=write_task) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert hook.deny_count == 5


# ---------------------------------------------------------------------------
# Adversarial: empty / None inputs
# ---------------------------------------------------------------------------


class TestAdversarialInputs:
    def test_empty_agent_id_uses_default_policy(self) -> None:
        """Empty agent_id falls through to default_allow."""
        hook = MemoryBoundaryHook(config=MemoryBoundaryConfig(default_allow=True))
        decision = hook.before_op(
            MemoryOperation(action=MemoryAction.READ, agent_id="", namespace="ns"),
            None,
        )
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_empty_namespace_uses_default_policy(self) -> None:
        hook = MemoryBoundaryHook(config=MemoryBoundaryConfig(default_allow=True))
        decision = hook.before_op(
            MemoryOperation(
                action=MemoryAction.WRITE, agent_id="agent-1", namespace=""
            ),
            None,
        )
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_after_llm_call_none_agent_id_in_metadata(self) -> None:
        """None agent_id in metadata should not crash -- converts to empty str."""
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        ctx = ToolCallContext(
            request_id="r1",
            metadata={"kind": "memory_read", "agent_id": None, "namespace": "ns"},
        )
        hook.after_llm_call(ctx, response=None)  # must not raise

    def test_after_llm_call_none_namespace_in_metadata(self) -> None:
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        ctx = ToolCallContext(
            request_id="r1",
            metadata={"kind": "memory_write", "agent_id": "a", "namespace": None},
        )
        hook.after_llm_call(ctx, response=None)  # must not raise (default allow)

    def test_after_op_with_error_does_not_raise(self) -> None:
        hook = MemoryBoundaryHook()
        op = _read_op()
        from veronica_core.memory.types import MemoryGovernanceDecision

        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW, policy_id="test", operation=op
        )
        # after_op must never raise.
        hook.after_op(op, decision, result=None, error=RuntimeError("oops"))


# ---------------------------------------------------------------------------
# Adversarial: boundary abuse and resource exhaustion
# ---------------------------------------------------------------------------


class TestAdversarialBoundaryAbuse:
    def test_same_specificity_first_rule_wins(self) -> None:
        """When two rules have equal specificity, the first match in list order wins."""
        rule_deny = MemoryAccessRule(
            agent_id="agent-x", namespace="ns-y", allow_write=False
        )
        rule_allow = MemoryAccessRule(
            agent_id="agent-x", namespace="ns-y", allow_write=True
        )
        config = MemoryBoundaryConfig(rules=[rule_deny, rule_allow])
        hook = MemoryBoundaryHook(config=config)
        # Both rules have max specificity (3) and match. First match (deny) should win
        # because _evaluate_rules picks the highest score, and on tie, keeps the first.
        decision = hook.before_op(_write_op(agent_id="agent-x", namespace="ns-y"), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_many_rules_performance(self) -> None:
        """100 rules must not crash or produce incorrect results."""
        rules = [
            MemoryAccessRule(agent_id=f"agent-{i}", namespace="ns", allow_write=False)
            for i in range(100)
        ]
        # Add one wildcard allow at the end.
        rules.append(MemoryAccessRule(agent_id="*", namespace="ns", allow_write=True))
        config = MemoryBoundaryConfig(rules=rules)
        hook = MemoryBoundaryHook(config=config)

        # Exact match for agent-50 should deny (specificity 3 > wildcard specificity 1).
        dec_exact = hook.before_op(_write_op(agent_id="agent-50", namespace="ns"), None)
        assert dec_exact.verdict is GovernanceVerdict.DENY

        # Unknown agent should match wildcard allow.
        dec_wild = hook.before_op(_write_op(agent_id="outsider", namespace="ns"), None)
        assert dec_wild.verdict is GovernanceVerdict.ALLOW

    def test_wildcard_agent_exact_ns_vs_exact_agent_wildcard_ns(self) -> None:
        """Exact namespace (specificity +1) vs exact agent (specificity +2): agent wins."""
        rule_ns = MemoryAccessRule(agent_id="*", namespace="vault", allow_write=False)
        rule_agent = MemoryAccessRule(agent_id="admin", namespace="*", allow_write=True)
        config = MemoryBoundaryConfig(rules=[rule_ns, rule_agent])
        hook = MemoryBoundaryHook(config=config)

        # admin + vault: rule_agent has specificity 2, rule_ns has specificity 1.
        # Agent-specific rule wins -> allow.
        dec = hook.before_op(_write_op(agent_id="admin", namespace="vault"), None)
        assert dec.verdict is GovernanceVerdict.ALLOW

    def test_deny_count_never_negative(self) -> None:
        """deny_count must never go below 0 even after many allows."""
        config = MemoryBoundaryConfig(default_allow=True)
        hook = MemoryBoundaryHook(config=config)
        for _ in range(20):
            hook.before_op(_read_op(), None)
        assert hook.deny_count == 0

    def test_concurrent_mixed_allow_deny(self) -> None:
        """10 threads: 5 denied + 5 allowed. deny_count must be exactly 5."""
        deny_rule = MemoryAccessRule(
            agent_id="deny-agent", namespace="ns", allow_read=False
        )
        config = MemoryBoundaryConfig(rules=[deny_rule], default_allow=True)
        hook = MemoryBoundaryHook(config=config)

        def denied_task() -> None:
            hook.before_op(_read_op(agent_id="deny-agent", namespace="ns"), None)

        def allowed_task() -> None:
            hook.before_op(_read_op(agent_id="ok-agent", namespace="ns"), None)

        threads = [threading.Thread(target=denied_task) for _ in range(5)]
        threads += [threading.Thread(target=allowed_task) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert hook.deny_count == 5
