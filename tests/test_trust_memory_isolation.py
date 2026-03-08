"""Tests for trust-based memory isolation (Issue #73).

Covers:
- UNTRUSTED agents denied access to TRUSTED namespaces (read and write)
- PROVISIONAL agents denied write, allowed read on TRUSTED namespaces
- TRUSTED / PRIVILEGED agents get full access to TRUSTED namespaces
- Trust-level transitions (promotion/demotion via TrustEscalationTracker)
- Non-trusted namespaces bypass trust check
- Concurrent trust checks (5 threads)
- Adversarial: missing trust level (None), unknown agent, no tracker configured
"""

from __future__ import annotations

import threading


from veronica_core.a2a import (
    TrustEscalationTracker,
    TrustBasedPolicyRouter,
    TrustLevel,
    TrustPolicy,
)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRUSTED_NS = frozenset({"trusted-vault", "privileged-data"})
_OPEN_NS = "public-cache"


def _policy() -> TrustPolicy:
    return TrustPolicy(
        default_trust=TrustLevel.UNTRUSTED,
        promotion_threshold=3,
        allow_promotion_to=TrustLevel.TRUSTED,
    )


def _make_hook(
    tracker: TrustEscalationTracker | None = None,
    extra_rules: list[MemoryAccessRule] | None = None,
) -> MemoryBoundaryHook:
    router = TrustBasedPolicyRouter()
    config = MemoryBoundaryConfig(rules=extra_rules or [], default_allow=True)
    return MemoryBoundaryHook(
        config=config,
        trust_router=router,
        trust_tracker=tracker,
        trusted_namespaces=_TRUSTED_NS,
    )


def _op(
    action: MemoryAction,
    agent_id: str = "agent-1",
    namespace: str = "trusted-vault",
) -> MemoryOperation:
    return MemoryOperation(action=action, agent_id=agent_id, namespace=namespace)


# ---------------------------------------------------------------------------
# Trust level enforcement
# ---------------------------------------------------------------------------


class TestTrustLevelEnforcement:
    def test_untrusted_cannot_read_trusted_namespace(self) -> None:
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        # New agent starts UNTRUSTED by default.
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="untrusted-agent"), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_untrusted_cannot_write_trusted_namespace(self) -> None:
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(_op(MemoryAction.WRITE, agent_id="untrusted-agent"), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_trusted_agent_can_read_trusted_namespace(self) -> None:
        policy = TrustPolicy(
            default_trust=TrustLevel.TRUSTED,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="trusted-agent"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_trusted_agent_can_write_trusted_namespace(self) -> None:
        policy = TrustPolicy(
            default_trust=TrustLevel.TRUSTED,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(_op(MemoryAction.WRITE, agent_id="trusted-agent"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_provisional_agent_can_read_trusted_namespace(self) -> None:
        """PROVISIONAL is intermediate trust -- read is allowed on trusted namespaces."""
        policy = TrustPolicy(
            default_trust=TrustLevel.PROVISIONAL,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="provisional-agent"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_provisional_agent_cannot_write_trusted_namespace(self) -> None:
        """PROVISIONAL cannot write to trusted namespaces."""
        policy = TrustPolicy(
            default_trust=TrustLevel.PROVISIONAL,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(_op(MemoryAction.WRITE, agent_id="provisional-agent"), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_non_trusted_namespace_bypasses_trust_check(self) -> None:
        """UNTRUSTED agents should freely access non-trusted namespaces."""
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        decision = hook.before_op(
            _op(MemoryAction.READ, agent_id="untrusted-agent", namespace=_OPEN_NS),
            None,
        )
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# Trust level transitions
# ---------------------------------------------------------------------------


class TestTrustLevelTransitions:
    def test_promotion_grants_access(self) -> None:
        """After enough successes, agent is promoted and gains read/write access."""
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        agent_id = "promotable-agent"

        # Before promotion: denied.
        dec_before = hook.before_op(_op(MemoryAction.WRITE, agent_id=agent_id), None)
        assert dec_before.verdict is GovernanceVerdict.DENY

        # Record 3 successes to trigger promotion (threshold=3, to PROVISIONAL first).
        for _ in range(3):
            tracker.record_success(agent_id)

        # After first promotion: PROVISIONAL -- write still denied.
        assert tracker.get_trust_level(agent_id) == TrustLevel.PROVISIONAL
        dec_provisional_write = hook.before_op(_op(MemoryAction.WRITE, agent_id=agent_id), None)
        assert dec_provisional_write.verdict is GovernanceVerdict.DENY

        # PROVISIONAL read: allowed.
        dec_provisional_read = hook.before_op(_op(MemoryAction.READ, agent_id=agent_id), None)
        assert dec_provisional_read.verdict is GovernanceVerdict.ALLOW

        # 3 more successes: promote to TRUSTED.
        for _ in range(3):
            tracker.record_success(agent_id)
        assert tracker.get_trust_level(agent_id) == TrustLevel.TRUSTED

        # TRUSTED: both read and write allowed.
        dec_trusted_write = hook.before_op(_op(MemoryAction.WRITE, agent_id=agent_id), None)
        assert dec_trusted_write.verdict is GovernanceVerdict.ALLOW

    def test_demotion_revokes_access(self) -> None:
        """Failure demotes to UNTRUSTED and revokes access."""
        policy = TrustPolicy(
            default_trust=TrustLevel.TRUSTED,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        agent_id = "demotable-agent"

        # Start TRUSTED: can write.
        dec_before = hook.before_op(_op(MemoryAction.WRITE, agent_id=agent_id), None)
        assert dec_before.verdict is GovernanceVerdict.ALLOW

        # Record failure: demoted to UNTRUSTED.
        tracker.record_failure(agent_id)
        assert tracker.get_trust_level(agent_id) == TrustLevel.UNTRUSTED

        # After demotion: denied.
        dec_after = hook.before_op(_op(MemoryAction.WRITE, agent_id=agent_id), None)
        assert dec_after.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# Concurrent trust checks
# ---------------------------------------------------------------------------


class TestConcurrentTrustChecks:
    def test_concurrent_untrusted_all_denied(self) -> None:
        """5 UNTRUSTED threads requesting trusted namespace access: all denied."""
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        results: list[GovernanceVerdict] = []
        lock = threading.Lock()

        def check_task(agent_num: int) -> None:
            dec = hook.before_op(
                _op(MemoryAction.READ, agent_id=f"concurrent-untrusted-{agent_num}"),
                None,
            )
            with lock:
                results.append(dec.verdict)

        threads = [threading.Thread(target=check_task, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(v is GovernanceVerdict.DENY for v in results)
        assert len(results) == 5

    def test_concurrent_trusted_all_allowed(self) -> None:
        """5 TRUSTED threads all get access concurrently."""
        policy = TrustPolicy(
            default_trust=TrustLevel.TRUSTED,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        hook = _make_hook(tracker=tracker)
        results: list[GovernanceVerdict] = []
        lock = threading.Lock()

        def check_task(agent_num: int) -> None:
            dec = hook.before_op(
                _op(MemoryAction.WRITE, agent_id=f"concurrent-trusted-{agent_num}"),
                None,
            )
            with lock:
                results.append(dec.verdict)

        threads = [threading.Thread(target=check_task, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(v is GovernanceVerdict.ALLOW for v in results)


# ---------------------------------------------------------------------------
# Adversarial: missing / unknown trust info
# ---------------------------------------------------------------------------


class TestAdversarialTrustInputs:
    def test_no_tracker_configured_skips_trust_check(self) -> None:
        """Without a tracker, trust check is skipped -- falls through to rules."""
        router = TrustBasedPolicyRouter()
        hook = MemoryBoundaryHook(
            config=MemoryBoundaryConfig(default_allow=True),
            trust_router=router,
            trust_tracker=None,  # No tracker -- trust check skipped.
            trusted_namespaces=_TRUSTED_NS,
        )
        # Trust check requires tracker; without it, falls through to default_allow=True.
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="mystery-agent"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_unknown_agent_not_in_tracker_denied_on_trusted_ns(self) -> None:
        """Agent not seen by tracker gets default_trust (UNTRUSTED) -- denied."""
        tracker = TrustEscalationTracker(policy=_policy())
        hook = _make_hook(tracker=tracker)
        # "ghost-agent" was never recorded -- gets default_trust = UNTRUSTED.
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="ghost-agent"), None)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_no_tracker_no_router_skips_trust_check(self) -> None:
        """No trust_router and no trust_tracker: trust check is skipped entirely."""
        hook = MemoryBoundaryHook(
            config=MemoryBoundaryConfig(default_allow=True),
            trust_router=None,
            trust_tracker=None,
            trusted_namespaces=_TRUSTED_NS,
        )
        # Should fall through to rule check (default_allow=True).
        decision = hook.before_op(_op(MemoryAction.READ, agent_id="any"), None)
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_trust_check_and_explicit_deny_rule_both_active(self) -> None:
        """When both trust and rules apply, most restrictive wins."""
        policy = TrustPolicy(
            default_trust=TrustLevel.TRUSTED,
            promotion_threshold=10,
            allow_promotion_to=TrustLevel.TRUSTED,
        )
        tracker = TrustEscalationTracker(policy=policy)
        # Even though agent is TRUSTED, an explicit deny rule blocks write.
        rule = MemoryAccessRule(agent_id="constrained-trusted", namespace="trusted-vault", allow_write=False)
        hook = MemoryBoundaryHook(
            config=MemoryBoundaryConfig(rules=[rule], default_allow=True),
            trust_router=TrustBasedPolicyRouter(),
            trust_tracker=tracker,
            trusted_namespaces=_TRUSTED_NS,
        )
        decision = hook.before_op(
            _op(MemoryAction.WRITE, agent_id="constrained-trusted"), None
        )
        assert decision.verdict is GovernanceVerdict.DENY
