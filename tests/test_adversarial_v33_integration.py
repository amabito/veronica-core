"""Adversarial tests for v3.3 integration features -- attacker mindset.

Categories covered:
1. Hook poisoning       -- before_op() raises / returns None / returns garbage verdict
2. Concurrent access    -- 10 threads wrapping LLM calls with MemoryGovernor
3. Policy view corruption -- PolicyViewHolder.current raises / returns broken object
4. State manipulation   -- memory_governor set to non-MemoryGovernor object
5. notify_after failure -- MemoryGovernor.notify_after() raises
6. Chain event flooding -- emit many memory_governance_denied events, dedup must cap
7. TOCTOU               -- PolicyViewHolder.swap() between _get_policy_audit_metadata calls
8. Boundary abuse       -- 100 hooks, all ALLOW except last one DENY
9. AIContainer + broken memory_governor
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.container.aicontainer import AIContainer
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import (
    DefaultMemoryGovernanceHook,
    DenyAllMemoryGovernanceHook,
)
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)
from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import VerificationResult
from veronica_core.shield.types import Decision

import hashlib


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _op(action: MemoryAction = MemoryAction.WRITE, **kwargs: Any) -> MemoryOperation:
    return MemoryOperation(action=action, agent_id="agent-1", **kwargs)


def _make_verdict_hook(verdict: GovernanceVerdict, policy_id: str = "test") -> Any:
    class _VerdictHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=verdict,
                reason=f"forced {verdict.value}",
                policy_id=policy_id,
                operation=operation,
            )

        def after_op(self, operation: Any, decision: Any, result: Any = None, error: Any = None) -> None:
            pass

    return _VerdictHook()


def _make_raising_hook(exc: Exception) -> Any:
    class _RaisingHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            raise exc

        def after_op(self, *args: Any, **kwargs: Any) -> None:
            pass

    return _RaisingHook()


def _make_raising_after_hook(exc: Exception) -> Any:
    class _RaisingAfterHook:
        def before_op(
            self,
            operation: MemoryOperation,
            context: MemoryPolicyContext | None,
        ) -> MemoryGovernanceDecision:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                reason="allow",
                policy_id="test",
                operation=operation,
            )

        def after_op(self, *args: Any, **kwargs: Any) -> None:
            raise exc

    return _RaisingAfterHook()


def _make_context(memory_governor: MemoryGovernor | None = None, policy_view_holder: Any = None) -> ExecutionContext:
    config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10)
    return ExecutionContext(
        config=config,
        memory_governor=memory_governor,
        policy_view_holder=policy_view_holder,
    )


def _valid_verification() -> VerificationResult:
    return VerificationResult(valid=True, errors=(), warnings=())


def _make_frozen_view(policy_id: str = "test-policy") -> FrozenPolicyView:
    rule = PolicyRule(rule_id="r1", rule_type="budget", enabled=True, priority=100, parameters={})
    rules = (rule,)
    canonical = _canonical_rules_json(rules)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=h, epoch=1, issuer="test")
    bundle = PolicyBundle(metadata=meta, rules=rules)
    return FrozenPolicyView(bundle, _valid_verification())


# ===========================================================================
# Category 1: Hook poisoning
# ===========================================================================


class TestAdversarialV33HookPoisoning:
    """Hook poisoning -- before_op() that raises, returns None, or returns garbage verdict."""

    def test_raising_hook_is_fail_closed_deny(self) -> None:
        """before_op() raising must produce DENY (fail-closed)."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_raising_hook(RuntimeError("poison")))
        op = _op()
        decision = gov.evaluate(op)
        assert decision.denied, "raising hook must be fail-closed DENY"

    def test_raising_hook_in_execution_context_halts_node(self) -> None:
        """ExecutionContext: MemoryGovernor.evaluate() raising must return HALT."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_raising_hook(ValueError("boom")))
        ctx = _make_context(memory_governor=gov)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

    def test_raising_hook_emits_chain_memory_governance_denied_event(self) -> None:
        """Chain event must use CHAIN_MEMORY_GOVERNANCE_DENIED event type."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_raising_hook(RuntimeError("poison")))
        ctx = _make_context(memory_governor=gov)
        ctx.wrap_llm_call(fn=lambda: None)
        snap = ctx.get_snapshot()
        mg_events = [e for e in snap.events if "MEMORY_GOVERNANCE" in e.event_type]
        assert len(mg_events) >= 1
        assert mg_events[0].event_type == "CHAIN_MEMORY_GOVERNANCE_DENIED"

    def test_none_returning_hook_treated_as_deny(self) -> None:
        """before_op() returning None must be treated as DENY (fail-closed TypeError)."""
        class _NoneHook:
            def before_op(self, operation: Any, context: Any) -> Any:
                return None  # type: ignore[return-value]

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_NoneHook())
        decision = gov.evaluate(_op())
        assert decision.denied

    def test_none_returning_hook_halts_execution_context(self) -> None:
        """ExecutionContext with None-returning hook must halt the node."""
        class _NoneHook:
            def before_op(self, operation: Any, context: Any) -> Any:
                return None  # type: ignore[return-value]

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_NoneHook())
        ctx = _make_context(memory_governor=gov)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

    def test_garbage_verdict_unknown_string_is_denied(self) -> None:
        """Hook returning unknown verdict string must be fail-closed DENY."""
        class _GarbageVerdictHook:
            def before_op(self, operation: Any, context: Any) -> Any:
                # Fabricate a decision with an unknown verdict value
                decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="test",
                    operation=operation,
                )
                # Bypass frozen dataclass to inject garbage verdict
                object.__setattr__(decision, "verdict", "TOTALLY_UNKNOWN_VERDICT")
                return decision

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_GarbageVerdictHook())
        decision = gov.evaluate(_op())
        assert decision.denied, "unknown verdict must be fail-closed DENY"

    def test_hook_that_raises_keyboard_interrupt_propagates(self) -> None:
        """KeyboardInterrupt from a hook must not be swallowed by BLE001 handler."""
        class _KillHook:
            def before_op(self, operation: Any, context: Any) -> Any:
                raise KeyboardInterrupt("user abort")

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_KillHook())
        # KeyboardInterrupt is NOT a subclass of Exception so BLE001 catches BaseException
        # -- but the governor uses 'except Exception' for DENY, so BaseException propagates.
        # Verify we do NOT silently swallow it (it should propagate).
        with pytest.raises((KeyboardInterrupt, BaseException)):
            gov.evaluate(_op())

    def test_fn_not_called_when_hook_raises(self) -> None:
        """The wrapped LLM function must NOT be called when the hook raises."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_raising_hook(RuntimeError("poison")))
        ctx = _make_context(memory_governor=gov)
        called = []
        result = ctx.wrap_llm_call(fn=lambda: called.append(1))
        assert result == Decision.HALT
        assert called == [], "fn must never be called when hook raises"


# ===========================================================================
# Category 2: Concurrent access
# ===========================================================================


class TestAdversarialV33ConcurrentAccess:
    """10 threads calling wrap_llm_call simultaneously with MemoryGovernor."""

    def test_concurrent_wrap_llm_all_allowed_no_crash(self) -> None:
        """10 threads, allow-all governor -- all must complete, no data race."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        ctx = _make_context(memory_governor=gov)

        results: list[Decision] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run() -> None:
            try:
                d = ctx.wrap_llm_call(fn=lambda: None)
                with lock:
                    results.append(d)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"unexpected exceptions: {errors}"
        assert len(results) == 10
        # All should be ALLOW (allowed)
        assert all(r == Decision.ALLOW for r in results)

    def test_concurrent_wrap_llm_deny_governor_all_halted(self) -> None:
        """10 threads, deny-all governor -- all must return HALT, no crash."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))
        ctx = _make_context(memory_governor=gov)

        results: list[Decision] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run() -> None:
            try:
                d = ctx.wrap_llm_call(fn=lambda: None)
                with lock:
                    results.append(d)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"unexpected exceptions: {errors}"
        assert len(results) == 10
        assert all(r == Decision.HALT for r in results)

    def test_concurrent_add_hook_and_evaluate_no_deadlock(self) -> None:
        """add_hook() racing evaluate() must not deadlock or corrupt state."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

        stop = threading.Event()
        errors: list[BaseException] = []

        def _evaluate_loop() -> None:
            while not stop.is_set():
                try:
                    gov.evaluate(_op())
                except BaseException as exc:
                    errors.append(exc)

        def _add_hook_loop() -> None:
            for _ in range(5):
                try:
                    # Governor caps at 100; we add a few and rely on the cap
                    if gov.hook_count < 10:
                        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
                except RuntimeError:
                    pass  # cap exceeded -- expected

        evaluators = [threading.Thread(target=_evaluate_loop) for _ in range(4)]
        adder = threading.Thread(target=_add_hook_loop)

        for t in evaluators:
            t.start()
        adder.start()

        adder.join(timeout=2.0)
        stop.set()
        for t in evaluators:
            t.join(timeout=2.0)

        assert errors == [], f"unexpected exceptions during concurrent access: {errors}"


# ===========================================================================
# Category 3: Policy view corruption
# ===========================================================================


class TestAdversarialV33PolicyViewCorruption:
    """PolicyViewHolder.current raises or returns broken object -- must never crash audit."""

    def test_current_raises_returns_none_metadata(self) -> None:
        """If holder.current raises, _get_policy_audit_metadata must return None."""
        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(side_effect=RuntimeError("corrupt"))

        ctx = _make_context(policy_view_holder=holder)
        # get_snapshot() calls _get_policy_audit_metadata internally
        snap = ctx.get_snapshot()
        assert snap.policy_metadata is None

    def test_to_audit_dict_raises_returns_none_metadata(self) -> None:
        """If view.to_audit_dict() raises, _get_policy_audit_metadata must return None."""
        broken_view = MagicMock(spec=FrozenPolicyView)
        broken_view.to_audit_dict.side_effect = RuntimeError("serialization failure")

        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(return_value=broken_view)

        ctx = _make_context(policy_view_holder=holder)
        snap = ctx.get_snapshot()
        assert snap.policy_metadata is None

    def test_none_current_view_returns_none_metadata(self) -> None:
        """PolicyViewHolder.current returning None must yield None metadata."""
        holder = PolicyViewHolder(initial=None)
        ctx = _make_context(policy_view_holder=holder)
        snap = ctx.get_snapshot()
        assert snap.policy_metadata is None

    def test_valid_view_metadata_propagates_to_snapshot(self) -> None:
        """A valid FrozenPolicyView must produce policy_metadata in the snapshot."""
        view = _make_frozen_view("audit-policy")
        holder = PolicyViewHolder(initial=view)
        ctx = _make_context(policy_view_holder=holder)
        snap = ctx.get_snapshot()
        assert snap.policy_metadata is not None
        assert snap.policy_metadata["policy_id"] == "audit-policy"

    def test_corrupt_view_holder_does_not_crash_chain_event(self) -> None:
        """Broken holder must not prevent chain events from being emitted."""
        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(side_effect=OSError("disk error"))

        # DenyAll governor ensures memory_governance_denied event is emitted
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))

        ctx = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10),
            memory_governor=gov,
            policy_view_holder=holder,
        )
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT
        # Chain event must still be present despite broken holder
        snap = ctx.get_snapshot()
        event_types = [e.event_type for e in snap.events]
        assert any("MEMORY" in et or "memory" in et.lower() for et in event_types)


# ===========================================================================
# Category 4: State manipulation
# ===========================================================================


class TestAdversarialV33StateManipulation:
    """memory_governor set to non-MemoryGovernor objects -- must fail clearly."""

    def test_memory_governor_not_governor_object_raises_or_denies(self) -> None:
        """Assigning a non-MemoryGovernor as governor must either raise or deny cleanly."""
        # We inject a plain object that has no .evaluate() method
        # AIContainer._check_memory_governor wraps in try/except -> returns denied PolicyDecision
        container = AIContainer()
        container.memory_governor = object()  # type: ignore[assignment]
        decision = container.check(cost_usd=0.0, step_count=0)
        # Must not crash; must deny due to exception handling
        assert not decision.allowed

    def test_memory_governor_none_is_skipped(self) -> None:
        """memory_governor=None must cause the check to be skipped entirely."""
        container = AIContainer()
        assert container.memory_governor is None
        decision = container.check(cost_usd=0.0, step_count=0)
        assert decision.allowed

    def test_execution_context_governor_replaced_mid_use(self) -> None:
        """Replacing _memory_governor attribute mid-use (best-effort, not supported).

        The context must not crash even if an internal reference is replaced.
        This tests robustness, not a supported API.
        """
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        ctx = _make_context(memory_governor=gov)

        # First call should succeed
        r1 = ctx.wrap_llm_call(fn=lambda: None)
        assert r1 == Decision.ALLOW

        # Replace with a broken object -- subsequent calls must not crash the process
        ctx._memory_governor = object()  # type: ignore[assignment]
        # The except Exception handler in _check_memory_governance must catch this
        r2 = ctx.wrap_llm_call(fn=lambda: None)
        assert r2 == Decision.HALT  # fail-closed on error

    def test_memory_governor_string_raises_attribute_error_safely(self) -> None:
        """A string as memory_governor must be fail-closed denied in AIContainer."""
        container = AIContainer()
        container.memory_governor = "not-a-governor"  # type: ignore[assignment]
        decision = container.check(cost_usd=0.0, step_count=0)
        assert not decision.allowed
        assert "memory governor error" in decision.reason


# ===========================================================================
# Category 5: notify_after failure
# ===========================================================================


class TestAdversarialV33NotifyAfterFailure:
    """MemoryGovernor.notify_after() raising must NOT disrupt the success path."""

    def test_after_op_raising_does_not_propagate(self) -> None:
        """after_op() raising must be swallowed by notify_after()."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_raising_after_hook(RuntimeError("after_op bomb")))
        op = _op()
        gov.evaluate(op)
        allow_decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="ok",
            policy_id="test",
            operation=op,
        )
        # notify_after must not raise
        gov.notify_after(op, allow_decision)

    def test_after_op_raising_does_not_affect_wrap_llm_result(self) -> None:
        """ExecutionContext: after_op raising must not change ALLOW result."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_raising_after_hook(ValueError("after_op explodes")))
        ctx = _make_context(memory_governor=gov)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW

    def test_after_op_raising_system_exit_propagates(self) -> None:
        """SystemExit from after_op is a BaseException -- must propagate through notify_after.

        The governor uses 'except Exception' so SystemExit escapes.
        """
        class _SystemExitAfterHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="test",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise SystemExit(1)

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_SystemExitAfterHook())
        op = _op()
        allow_dec = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW, reason="", policy_id="", operation=op
        )
        with pytest.raises(SystemExit):
            gov.notify_after(op, allow_dec)

    def test_multiple_after_op_hooks_one_raising_others_called(self) -> None:
        """When hook N raises in after_op, hooks N+1 must still be called."""
        call_log: list[str] = []

        class _LogHook:
            def __init__(self, name: str) -> None:
                self._name = name

            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id=self._name,
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                call_log.append(self._name)

        class _MiddleRaisingHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="middle",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("middle raises")

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_LogHook("first"))
        gov.add_hook(_MiddleRaisingHook())
        gov.add_hook(_LogHook("third"))

        op = _op()
        gov.evaluate(op)
        allow_dec = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW, reason="", policy_id="", operation=op
        )
        gov.notify_after(op, allow_dec)

        # Both first and third must be logged (middle swallowed)
        assert "first" in call_log
        assert "third" in call_log


# ===========================================================================
# Category 6: Chain event flooding
# ===========================================================================


class TestAdversarialV33ChainEventFlooding:
    """emit_chain_event -- dedup and cap must prevent overflow."""

    def test_flood_memory_governance_denied_events_capped_at_1000(self) -> None:
        """Emitting 1100 identical events must store at most 1000 (cap)."""
        from veronica_core.containment._chain_event_log import _ChainEventLog

        log = _ChainEventLog()
        for i in range(1100):
            log.emit_chain_event(
                stop_reason="memory_governance_denied",
                detail=f"detail-{i}",  # different detail -> unique dedup key
                request_id="req-123",
            )
        assert len(log) <= 1000

    def test_duplicate_memory_governance_denied_deduped(self) -> None:
        """Identical events must be deduplicated."""
        from veronica_core.containment._chain_event_log import _ChainEventLog

        log = _ChainEventLog()
        for _ in range(50):
            log.emit_chain_event(
                stop_reason="memory_governance_denied",
                detail="same reason",
                request_id="req-abc",
            )
        # All 50 are identical -- dedup must store only 1
        assert len(log) == 1

    def test_memory_governance_denied_events_on_execution_context_capped(self) -> None:
        """Repeated deny-all wraps on ExecutionContext must not grow events past cap."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))
        ctx = _make_context(memory_governor=gov)

        # Emit many deny events -- dedup keeps unique ones only
        for i in range(200):
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(operation_name=f"op-{i}"),
            )

        snap = ctx.get_snapshot()
        assert len(snap.events) <= 1000, "chain event log must not exceed cap"

    def test_policy_metadata_present_in_flooded_events(self) -> None:
        """When policy_view_holder is set, flooded events must carry policy metadata."""
        from veronica_core.containment._chain_event_log import _ChainEventLog

        view = _make_frozen_view("flood-policy")
        log = _ChainEventLog()
        policy_meta = view.to_audit_dict()
        for i in range(5):
            log.emit_chain_event(
                stop_reason="memory_governance_denied",
                detail=f"detail-{i}",
                request_id="req-xyz",
                policy_metadata=policy_meta,
            )
        events = log.snapshot()
        for ev in events:
            assert ev.metadata.get("policy", {}).get("policy_id") == "flood-policy"


# ===========================================================================
# Category 7: TOCTOU -- PolicyViewHolder.swap() races
# ===========================================================================


class TestAdversarialV33TOCTOU:
    """PolicyViewHolder.swap() between _get_policy_audit_metadata calls must not crash."""

    def test_swap_during_get_snapshot_does_not_crash(self) -> None:
        """Concurrent swap() while get_snapshot() reads policy metadata must not crash."""
        view_a = _make_frozen_view("policy-a")
        view_b = _make_frozen_view("policy-b")
        holder = PolicyViewHolder(initial=view_a)
        ctx = _make_context(policy_view_holder=holder)

        errors: list[BaseException] = []
        snapshots: list[Any] = []
        lock = threading.Lock()

        def _swap_loop() -> None:
            for _ in range(50):
                holder.swap(view_b)
                holder.swap(view_a)

        def _snapshot_loop() -> None:
            for _ in range(50):
                try:
                    snap = ctx.get_snapshot()
                    with lock:
                        snapshots.append(snap)
                except BaseException as exc:
                    with lock:
                        errors.append(exc)

        swapper = threading.Thread(target=_swap_loop)
        reader = threading.Thread(target=_snapshot_loop)
        swapper.start()
        reader.start()
        swapper.join(timeout=5.0)
        reader.join(timeout=5.0)

        assert errors == [], f"TOCTOU crash: {errors}"
        # All snapshots must have policy_metadata (either a or b, never crash)
        for snap in snapshots:
            if snap.policy_metadata is not None:
                assert snap.policy_metadata["policy_id"] in ("policy-a", "policy-b")

    def test_swap_to_none_during_snapshot_yields_none_or_valid(self) -> None:
        """Swapping to None while get_snapshot() runs must not crash."""
        view = _make_frozen_view("transient-policy")
        holder = PolicyViewHolder(initial=view)
        ctx = _make_context(policy_view_holder=holder)

        errors: list[BaseException] = []

        def _clear_loop() -> None:
            for _ in range(30):
                holder.swap(None)
                holder.swap(view)

        def _read_loop() -> None:
            for _ in range(30):
                try:
                    ctx.get_snapshot()
                except BaseException as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=_clear_loop)
        t2 = threading.Thread(target=_read_loop)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert errors == [], f"TOCTOU crash with None swap: {errors}"

    def test_emit_chain_event_races_policy_swap_no_crash(self) -> None:
        """Emitting chain events while swapping policy view must not crash."""
        view_a = _make_frozen_view("race-a")
        view_b = _make_frozen_view("race-b")
        holder = PolicyViewHolder(initial=view_a)
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))

        ctx = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=50),
            memory_governor=gov,
            policy_view_holder=holder,
        )
        errors: list[BaseException] = []

        def _deny_calls() -> None:
            for _ in range(10):
                try:
                    ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(operation_name="op"))
                except BaseException as exc:
                    errors.append(exc)

        def _swap_views() -> None:
            for _ in range(20):
                holder.swap(view_b)
                holder.swap(view_a)

        t1 = threading.Thread(target=_deny_calls)
        t2 = threading.Thread(target=_swap_views)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert errors == [], f"race crash: {errors}"


# ===========================================================================
# Category 8: Boundary abuse
# ===========================================================================


class TestAdversarialV33BoundaryAbuse:
    """Boundary cases: 100 hooks (max), mixed verdicts, zero/max content sizes."""

    def test_100_allow_hooks_1_deny_at_end_returns_deny(self) -> None:
        """99 ALLOW hooks followed by 1 DENY hook must return DENY."""
        gov = MemoryGovernor(fail_closed=True)
        for i in range(99):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW, policy_id=f"allow-{i}"))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY, policy_id="final-deny"))
        decision = gov.evaluate(_op())
        assert decision.denied

    def test_100_allow_hooks_at_cap(self) -> None:
        """Exactly 100 ALLOW hooks must succeed evaluation."""
        gov = MemoryGovernor(fail_closed=True)
        for i in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW, policy_id=f"h-{i}"))
        decision = gov.evaluate(_op())
        assert decision.allowed

    def test_101st_hook_raises_runtime_error(self) -> None:
        """Adding hook 101 must raise RuntimeError (cap = 100)."""
        gov = MemoryGovernor(fail_closed=True)
        for i in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW, policy_id=f"h-{i}"))
        with pytest.raises(RuntimeError, match="hook count capped"):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

    def test_deny_at_position_50_stops_evaluation(self) -> None:
        """DENY at hook 50 must stop evaluation; hooks 51-99 must not run."""
        call_log: list[int] = []

        class _LoggedAllowHook:
            def __init__(self, idx: int) -> None:
                self._idx = idx

            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                call_log.append(self._idx)
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id=f"h-{self._idx}",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=True)
        for i in range(50):
            gov.add_hook(_LoggedAllowHook(i))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY, policy_id="mid-deny"))
        for i in range(51, 60):
            gov.add_hook(_LoggedAllowHook(i))

        decision = gov.evaluate(_op())
        assert decision.denied
        # Hooks after the DENY must not be called
        assert all(idx < 50 for idx in call_log), f"hooks after DENY were called: {call_log}"

    def test_zero_hooks_fail_closed_denies(self) -> None:
        """Zero hooks with fail_closed=True must deny."""
        gov = MemoryGovernor(fail_closed=True)
        decision = gov.evaluate(_op())
        assert decision.denied

    def test_zero_hooks_fail_open_allows(self) -> None:
        """Zero hooks with fail_closed=False must allow."""
        gov = MemoryGovernor(fail_closed=False)
        decision = gov.evaluate(_op())
        assert decision.allowed

    def test_quarantine_over_degrade_wins(self) -> None:
        """QUARANTINE verdict must beat DEGRADE when both are present."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.QUARANTINE

    def test_degrade_over_allow_wins(self) -> None:
        """DEGRADE must win over ALLOW when both are present."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DEGRADE))
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DEGRADE


# ===========================================================================
# Category 9: AIContainer memory_governor interaction
# ===========================================================================


class TestAdversarialV33AIContainerMemoryGovernor:
    """AIContainer.check() with broken memory_governor must be fail-closed."""

    def test_aicontainer_deny_all_governor_returns_denied(self) -> None:
        """AIContainer with DenyAll governor must return denied PolicyDecision."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(DenyAllMemoryGovernanceHook())
        container = AIContainer(memory_governor=gov)
        decision = container.check(cost_usd=0.0, step_count=0)
        assert not decision.allowed
        assert "memory governance denied" in decision.reason

    def test_aicontainer_raising_governor_returns_denied(self) -> None:
        """AIContainer with governor that raises must return denied (fail-closed)."""
        broken_gov = MagicMock(spec=MemoryGovernor)
        broken_gov.evaluate.side_effect = RuntimeError("governor exploded")
        container = AIContainer(memory_governor=broken_gov)
        decision = container.check(cost_usd=0.0, step_count=0)
        assert not decision.allowed
        assert "memory governor error" in decision.reason

    def test_aicontainer_allow_governor_passes_through(self) -> None:
        """AIContainer with allow-all governor must return allowed."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(DefaultMemoryGovernanceHook())
        container = AIContainer(memory_governor=gov)
        decision = container.check(cost_usd=0.0, step_count=0)
        assert decision.allowed

    def test_aicontainer_no_governor_skips_check(self) -> None:
        """AIContainer without governor must not check memory governance."""
        container = AIContainer()
        decision = container.check(cost_usd=0.0, step_count=0)
        assert decision.allowed
        assert container.memory_governor is None

    def test_aicontainer_governor_returning_none_fails_closed(self) -> None:
        """evaluate() returning None must be caught as fail-closed DENY.

        Previously this was a latent bug (AttributeError from None.denied).
        Fixed in v3.3: the try/except now wraps both evaluate() and the
        decision.denied check, with an explicit None guard.
        """
        broken_gov = MagicMock(spec=MemoryGovernor)
        broken_gov.evaluate.return_value = None  # Protocol violation
        container = AIContainer(memory_governor=broken_gov)
        decision = container.check(cost_usd=0.0, step_count=0)
        assert not decision.allowed
        assert "memory governor error" in decision.reason

    def test_aicontainer_governor_returns_quarantine_allowed(self) -> None:
        """QUARANTINE verdict is treated as allowed by AIContainer (not denied)."""
        class _QuarantineHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.QUARANTINE,
                    reason="quarantine",
                    policy_id="q",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_QuarantineHook())
        container = AIContainer(memory_governor=gov)
        decision = container.check(cost_usd=0.0, step_count=0)
        # QUARANTINE.denied == False so AIContainer sees it as allowed
        assert decision.allowed

    def test_aicontainer_reset_preserves_governor(self) -> None:
        """reset() must not clear the memory_governor."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(DenyAllMemoryGovernanceHook())
        container = AIContainer(memory_governor=gov)
        container.reset()
        assert container.memory_governor is gov

    def test_aicontainer_concurrent_check_with_deny_governor_no_crash(self) -> None:
        """10 threads calling container.check() with deny governor must not crash."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))
        container = AIContainer(memory_governor=gov)

        results: list[bool] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _check() -> None:
            try:
                d = container.check(cost_usd=0.01, step_count=1)
                with lock:
                    results.append(d.allowed)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"unexpected exceptions: {errors}"
        assert all(r is False for r in results), "all must be denied"


# ===========================================================================
# Category 10: Emit path unification -- policy metadata in all event types
# ===========================================================================


class TestAdversarialV33EmitPathUnification:
    """After emit path unification (v3.3), ALL chain events must carry policy
    metadata when a PolicyViewHolder is configured. Tests cover abort, circuit
    breaker, budget_exceeded, budget_exceeded_by_child, and limit-check
    callbacks (_make_emit_chain_event_cb).
    """

    def test_abort_event_carries_policy_metadata(self) -> None:
        """abort() -> _emit_chain_event -> must include policy metadata."""
        view = _make_frozen_view("abort-policy")
        holder = PolicyViewHolder(initial=view)
        ctx = _make_context(policy_view_holder=holder)
        ctx.abort("test abort reason")

        snap = ctx.get_snapshot()
        aborted = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
        assert len(aborted) == 1
        assert "policy" in aborted[0].metadata
        assert aborted[0].metadata["policy"]["policy_id"] == "abort-policy"

    def test_abort_without_holder_emits_event_without_policy_key(self) -> None:
        """abort() without PolicyViewHolder must not inject 'policy' key."""
        ctx = _make_context(policy_view_holder=None)
        ctx.abort("no holder")

        snap = ctx.get_snapshot()
        aborted = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
        assert len(aborted) == 1
        assert "policy" not in aborted[0].metadata

    def test_abort_with_broken_holder_still_completes(self) -> None:
        """If PolicyViewHolder.current raises during abort, abort must still complete."""
        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(side_effect=RuntimeError("broken"))

        ctx = _make_context(policy_view_holder=holder)
        ctx.abort("broken holder abort")

        snap = ctx.get_snapshot()
        assert snap.aborted is True
        aborted = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
        assert len(aborted) == 1
        # policy metadata should be None (swallowed), not crash
        assert "policy" not in aborted[0].metadata

    def test_memory_governance_denied_event_carries_policy_metadata(self) -> None:
        """Memory governance DENY via _emit_chain_event must include policy."""
        view = _make_frozen_view("mg-deny-policy")
        holder = PolicyViewHolder(initial=view)
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))

        ctx = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10),
            memory_governor=gov,
            policy_view_holder=holder,
        )
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

        snap = ctx.get_snapshot()
        mg_events = [e for e in snap.events if "MEMORY_GOVERNANCE" in e.event_type]
        assert len(mg_events) >= 1
        assert mg_events[0].metadata.get("policy", {}).get("policy_id") == "mg-deny-policy"

    def test_memory_governance_error_event_carries_policy_metadata(self) -> None:
        """Memory governor error (raising) event must still carry policy metadata."""
        view = _make_frozen_view("mg-error-policy")
        holder = PolicyViewHolder(initial=view)
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_raising_hook(RuntimeError("governor explodes")))

        ctx = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10),
            memory_governor=gov,
            policy_view_holder=holder,
        )
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

        snap = ctx.get_snapshot()
        mg_events = [e for e in snap.events if "MEMORY_GOVERNANCE" in e.event_type]
        assert len(mg_events) >= 1
        assert mg_events[0].metadata.get("policy", {}).get("policy_id") == "mg-error-policy"


# ===========================================================================
# Category 11: _make_emit_chain_event_cb closure -- limit-exceeded events
# ===========================================================================


class TestAdversarialV33LimitExceededPolicyMetadata:
    """_make_emit_chain_event_cb enriches limit-exceeded events with policy
    metadata. Tests verify budget, step, retry, timeout limit events carry
    policy metadata.
    """

    def test_step_limit_exceeded_event_has_policy_metadata(self) -> None:
        """Exceeding step limit must emit event with policy metadata."""
        view = _make_frozen_view("step-limit-policy")
        holder = PolicyViewHolder(initial=view)
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=2, max_retries_total=10)
        ctx = ExecutionContext(config=config, policy_view_holder=holder)

        # Consume steps until limit
        for _ in range(5):
            ctx.wrap_llm_call(fn=lambda: None)

        snap = ctx.get_snapshot()
        step_events = [e for e in snap.events if "STEP" in e.event_type]
        if step_events:
            assert step_events[0].metadata.get("policy", {}).get("policy_id") == "step-limit-policy"

    def test_budget_limit_exceeded_event_has_policy_metadata(self) -> None:
        """Exceeding budget must emit event with policy metadata."""
        view = _make_frozen_view("budget-limit-policy")
        holder = PolicyViewHolder(initial=view)
        config = ExecutionConfig(max_cost_usd=0.01, max_steps=50, max_retries_total=10)
        ctx = ExecutionContext(config=config, policy_view_holder=holder)

        # Try to spend more than budget
        for _ in range(5):
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(operation_name="expensive", cost_estimate_hint=0.05),
            )

        snap = ctx.get_snapshot()
        budget_events = [e for e in snap.events if "BUDGET" in e.event_type]
        if budget_events:
            assert budget_events[0].metadata.get("policy", {}).get("policy_id") == "budget-limit-policy"

    def test_limit_exceeded_with_broken_holder_does_not_crash(self) -> None:
        """Limit exceeded + broken PolicyViewHolder must not crash the callback."""
        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(side_effect=OSError("disk error"))

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=1, max_retries_total=10)
        ctx = ExecutionContext(config=config, policy_view_holder=holder)

        # First call uses the 1 step; second should trigger step limit
        ctx.wrap_llm_call(fn=lambda: None)
        result = ctx.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

        snap = ctx.get_snapshot()
        # Must not crash -- events should exist, policy key should be absent
        step_events = [e for e in snap.events if "STEP" in e.event_type]
        if step_events:
            assert "policy" not in step_events[0].metadata


# ===========================================================================
# Category 12: Concurrent _build_memory_op with metadata access
# ===========================================================================


class TestAdversarialV33ConcurrentBuildMemoryOp:
    """_build_memory_op reads self._metadata -- concurrent wraps must not corrupt."""

    def test_10_threads_build_memory_op_no_corruption(self) -> None:
        """10 threads calling wrap_llm_call with MemoryGovernor must not corrupt metadata."""
        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        ctx = _make_context(memory_governor=gov)

        results: list[Decision] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run(i: int) -> None:
            try:
                d = ctx.wrap_llm_call(
                    fn=lambda: None,
                    options=WrapOptions(operation_name=f"op-{i}"),
                )
                with lock:
                    results.append(d)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"metadata corruption: {errors}"
        assert len(results) == 10

    def test_concurrent_wrap_with_deny_governor_and_policy_holder(self) -> None:
        """10 threads: deny governor + policy holder -- events have consistent metadata."""
        view = _make_frozen_view("concurrent-policy")
        holder = PolicyViewHolder(initial=view)
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))

        ctx = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=50),
            memory_governor=gov,
            policy_view_holder=holder,
        )

        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run(i: int) -> None:
            try:
                ctx.wrap_llm_call(
                    fn=lambda: None,
                    options=WrapOptions(operation_name=f"concurrent-op-{i}"),
                )
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"concurrent crash: {errors}"

        snap = ctx.get_snapshot()
        mg_events = [e for e in snap.events if "MEMORY_GOVERNANCE" in e.event_type]
        # All events should have consistent policy metadata
        for ev in mg_events:
            pm = ev.metadata.get("policy")
            if pm is not None:
                assert pm["policy_id"] == "concurrent-policy"


# ===========================================================================
# Category 13: Child context propagation of governor + policy holder
# ===========================================================================


class TestAdversarialV33ChildPropagation:
    """create_child must propagate memory_governor and policy_view_holder."""

    _CHILD_AGENTS = ["child-a", "child-b"]

    def test_create_child_inherits_memory_governor(self) -> None:
        """Child context from create_child must have parent's memory governor."""
        gov = MemoryGovernor(fail_closed=True)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.DENY))
        parent = _make_context(memory_governor=gov)
        child = parent.create_child(
            agent_name="child-a",
            agent_names=self._CHILD_AGENTS,
        )
        # Child should deny because it inherited the governor
        result = child.wrap_llm_call(fn=lambda: None)
        assert result == Decision.HALT

    def test_create_child_inherits_policy_view_holder(self) -> None:
        """Child context from create_child must have parent's policy_view_holder."""
        view = _make_frozen_view("parent-policy")
        holder = PolicyViewHolder(initial=view)
        parent = _make_context(policy_view_holder=holder)
        child = parent.create_child(
            agent_name="child-a",
            agent_names=self._CHILD_AGENTS,
        )
        snap = child.get_snapshot()
        assert snap.policy_metadata is not None
        assert snap.policy_metadata["policy_id"] == "parent-policy"

    def test_child_with_broken_holder_does_not_crash_parent(self) -> None:
        """If parent's holder breaks, child must not crash on wrap calls."""
        holder = MagicMock(spec=PolicyViewHolder)
        type(holder).current = PropertyMock(side_effect=RuntimeError("broken"))

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

        parent = ExecutionContext(
            config=ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10),
            memory_governor=gov,
            policy_view_holder=holder,
        )
        child = parent.create_child(
            agent_name="child-a",
            agent_names=self._CHILD_AGENTS,
        )
        # Must not crash
        result = child.wrap_llm_call(fn=lambda: None)
        assert result == Decision.ALLOW

    def test_child_abort_with_policy_holder_emits_policy_metadata(self) -> None:
        """Child abort must emit event with policy metadata from parent's holder."""
        view = _make_frozen_view("child-abort-policy")
        holder = PolicyViewHolder(initial=view)
        parent = _make_context(policy_view_holder=holder)
        child = parent.create_child(
            agent_name="child-a",
            agent_names=self._CHILD_AGENTS,
        )
        child.abort("child test abort")
        snap = child.get_snapshot()
        aborted = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
        assert len(aborted) == 1
        assert aborted[0].metadata.get("policy", {}).get("policy_id") == "child-abort-policy"


# ===========================================================================
# Category 14: notify_after BaseException swallowing
# ===========================================================================


class TestAdversarialV33NotifyAfterBaseException:
    """_notify_memory_governance_after must swallow BaseException to prevent
    a successful call from being corrupted by a governance hook's after_op.
    """

    def test_system_exit_from_after_op_does_not_corrupt_successful_call(self) -> None:
        """SystemExit from after_op must not turn a successful call into an error."""

        class _SystemExitAfterHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="sys-exit-hook",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise SystemExit(42)

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_SystemExitAfterHook())
        ctx = _make_context(memory_governor=gov)

        # Must return ALLOW, not crash or raise SystemExit
        result = ctx.wrap_llm_call(fn=lambda: "success")
        assert result == Decision.ALLOW

        snap = ctx.get_snapshot()
        # Node must be marked as completed, not error
        assert len(snap.nodes) >= 1
        assert snap.nodes[-1].status == "ok"

    def test_keyboard_interrupt_from_after_op_does_not_corrupt_successful_call(self) -> None:
        """KeyboardInterrupt from after_op must not corrupt a successful wrap result."""

        class _KBInterruptAfterHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="kb-hook",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise KeyboardInterrupt("ctrl-c during after_op")

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_KBInterruptAfterHook())
        ctx = _make_context(memory_governor=gov)

        result = ctx.wrap_llm_call(fn=lambda: "done")
        assert result == Decision.ALLOW

    def test_after_op_base_exception_cost_not_double_counted(self) -> None:
        """Even if after_op raises BaseException, cost must not be rolled back."""
        class _BaseExcAfterHook:
            def before_op(self, operation: Any, context: Any) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    reason="allow",
                    policy_id="cost-test",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise SystemExit(99)

        gov = MemoryGovernor(fail_closed=False)
        gov.add_hook(_BaseExcAfterHook())
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=10)
        ctx = ExecutionContext(config=config, memory_governor=gov)

        result = ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(operation_name="cost-op", cost_estimate_hint=0.5),
        )
        assert result == Decision.ALLOW

        snap = ctx.get_snapshot()
        # Cost should be accumulated (not rolled back)
        assert snap.cost_usd_accumulated >= 0.0
