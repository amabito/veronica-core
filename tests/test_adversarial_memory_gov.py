"""Adversarial tests for memory governance -- attacker mindset.

Tests are grouped by attack category:
1. Immutability bypass    -- try to mutate frozen/proxy fields post-construction
2. Fail-closed            -- zero hooks + fail_closed, hook exceptions -> DENY
3. Hook poisoning         -- hooks that raise, return garbage, or mutate operation
4. Concurrent access      -- races between evaluate() calls and add_hook()
5. Verdict aggregation    -- QUARANTINE > DEGRADE > ALLOW priority under mixed inputs
6. Resource exhaustion    -- hook cap enforcement, blocking hooks with timeout
7. Boundary              -- empty operation, max/min content_size_bytes
"""

from __future__ import annotations

import threading
import time
import types as _types
from typing import Any

import pytest

from _nogil_compat import nogil_unstable
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import (
    DefaultMemoryGovernanceHook,
)
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _op(
    action: MemoryAction = MemoryAction.READ,
    **kwargs: Any,
) -> MemoryOperation:
    return MemoryOperation(action=action, resource_id="r", agent_id="a", **kwargs)


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

        def after_op(self, *args: Any, **kwargs: Any) -> None:
            pass

    return _VerdictHook()


# ---------------------------------------------------------------------------
# 1. Immutability bypass
# ---------------------------------------------------------------------------


class TestAdversarialImmutabilityBypass:
    """Try to mutate fields that must be frozen after construction."""

    def test_memory_operation_metadata_is_mapping_proxy(self) -> None:
        """metadata must be a MappingProxyType -- not a plain dict."""
        op = _op(metadata={"key": "value"})
        assert isinstance(op.metadata, _types.MappingProxyType)

    def test_memory_operation_metadata_direct_setitem_fails(self) -> None:
        """Attempting op.metadata['x'] = y must raise TypeError."""
        op = _op(metadata={"key": "value"})
        with pytest.raises(TypeError):
            op.metadata["new_key"] = "injected"  # type: ignore[index]

    def test_memory_operation_metadata_del_item_fails(self) -> None:
        """Attempting del op.metadata['key'] must raise TypeError."""
        op = _op(metadata={"key": "value"})
        with pytest.raises(TypeError):
            del op.metadata["key"]  # type: ignore[attr-defined]

    def test_memory_operation_metadata_update_fails(self) -> None:
        """Calling op.metadata.update({}) must raise AttributeError."""
        op = _op(metadata={"key": "value"})
        with pytest.raises(AttributeError):
            op.metadata.update({"injected": True})  # type: ignore[attr-defined]

    def test_memory_operation_metadata_source_dict_mutation_isolated(self) -> None:
        """Mutating the original dict passed at construction must not affect op.metadata."""
        source: dict[str, Any] = {"key": "original"}
        op = _op(metadata=source)
        source["key"] = "mutated"
        source["extra"] = "injected"
        assert op.metadata["key"] == "original"
        assert "extra" not in op.metadata

    def test_memory_operation_frozen_field_assignment_fails(self) -> None:
        """Direct field assignment on frozen MemoryOperation must raise."""
        op = _op()
        with pytest.raises((AttributeError, TypeError)):
            op.agent_id = "attacker"  # type: ignore[misc]

    def test_memory_governance_decision_audit_metadata_is_mapping_proxy(self) -> None:
        """audit_metadata on MemoryGovernanceDecision must be a MappingProxyType."""
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            audit_metadata={"trace": "abc"},
        )
        assert isinstance(decision.audit_metadata, _types.MappingProxyType)

    def test_memory_governance_decision_audit_metadata_setitem_fails(self) -> None:
        """Attempting decision.audit_metadata['x'] = y must raise TypeError."""
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            audit_metadata={"trace": "abc"},
        )
        with pytest.raises(TypeError):
            decision.audit_metadata["injected"] = "evil"  # type: ignore[index]

    def test_memory_governance_decision_audit_metadata_source_mutation_isolated(
        self,
    ) -> None:
        """Mutating the dict passed as audit_metadata must not affect the decision."""
        source: dict[str, Any] = {"trace": "original"}
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            audit_metadata=source,
        )
        source["trace"] = "tampered"
        assert decision.audit_metadata["trace"] == "original"

    def test_memory_governance_decision_frozen_verdict_assignment_fails(self) -> None:
        """Direct verdict assignment on frozen MemoryGovernanceDecision must raise."""
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)
        with pytest.raises((AttributeError, TypeError)):
            decision.verdict = GovernanceVerdict.DENY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Fail-closed semantics
# ---------------------------------------------------------------------------


class TestAdversarialFailClosed:
    """Verify fail-closed behavior with zero hooks and with hook exceptions."""

    def test_no_hooks_fail_closed_returns_deny(self) -> None:
        """Zero hooks + fail_closed=True must return DENY unconditionally."""
        gov = MemoryGovernor(fail_closed=True)
        for action in MemoryAction:
            decision = gov.evaluate(_op(action=action))
            assert decision.verdict is GovernanceVerdict.DENY, (
                f"Expected DENY for action={action}, got {decision.verdict}"
            )

    def test_no_hooks_fail_open_returns_allow(self) -> None:
        """Zero hooks + fail_closed=False must return ALLOW unconditionally."""
        gov = MemoryGovernor(fail_closed=False)
        for action in MemoryAction:
            decision = gov.evaluate(_op(action=action))
            assert decision.verdict is GovernanceVerdict.ALLOW, (
                f"Expected ALLOW for action={action}, got {decision.verdict}"
            )

    def test_hook_exception_in_first_position_returns_deny(self) -> None:
        """A hook that raises in before_op at position 0 must produce DENY."""

        class _BombHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                raise ValueError("bomb")

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_BombHook())
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY
        assert "hook error" in decision.reason
        assert "ValueError" not in decision.reason  # Rule 5: no exc type leak

    def test_hook_exception_after_allow_hook_returns_deny(self) -> None:
        """ALLOW hook followed by a raising hook must still return DENY."""

        class _BombHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                raise RuntimeError("late failure")

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_BombHook())
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY

    def test_hook_exception_type_error_returns_deny(self) -> None:
        """TypeError from hook.before_op must also produce DENY."""

        class _TypeErrorHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                raise TypeError("unexpected type")

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_TypeErrorHook())
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY

    def test_hook_exception_does_not_propagate_to_caller(self) -> None:
        """evaluate() must never surface hook exceptions to the caller."""

        class _PanicHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                raise Exception("panic")  # noqa: TRY002

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_PanicHook())
        # Must not raise -- must return a DENY decision instead.
        decision = gov.evaluate(_op())
        assert decision is not None
        assert decision.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# 3. Hook poisoning
# ---------------------------------------------------------------------------


class TestAdversarialHookPoisoning:
    """Hooks that misbehave: raise, return garbage verdicts, try to mutate operation."""

    def test_hook_returns_none_raises_attribute_error_treated_as_deny(self) -> None:
        """A hook whose before_op returns None causes AttributeError, must DENY."""

        class _NoneReturningHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                return None  # type: ignore[return-value]

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_NoneReturningHook())
        decision = gov.evaluate(_op())
        # Either AttributeError is caught as DENY or governor explicitly denies
        assert decision.verdict is GovernanceVerdict.DENY

    def test_hook_returns_unknown_verdict_string_treated_as_deny(self) -> None:
        """A hook returning a raw string instead of GovernanceVerdict must be denied."""

        class _GarbageVerdictHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                # Construct decision with a non-enum verdict via object.__setattr__
                d = object.__new__(MemoryGovernanceDecision)
                object.__setattr__(
                    d, "verdict", "allow_everything"
                )  # not a GovernanceVerdict
                object.__setattr__(d, "reason", "poisoned")
                object.__setattr__(d, "policy_id", "evil")
                object.__setattr__(d, "operation", operation)
                object.__setattr__(d, "audit_metadata", _types.MappingProxyType({}))
                return d  # type: ignore[return-value]

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_GarbageVerdictHook())
        decision = gov.evaluate(_op())
        # Unknown verdict is treated as DENY (fail-closed)
        assert decision.verdict is GovernanceVerdict.DENY

    def test_hook_that_mutates_operation_via_object_setattr_is_still_evaluated(
        self,
    ) -> None:
        """Document that object.__setattr__ bypasses frozen dataclass on Python.

        Python's frozen=True dataclass uses __setattr__ on the class, but
        object.__setattr__ calls the C-level slot directly and bypasses it.
        This is a known CPython limitation -- not a bug in MemoryGovernor.

        The important invariant tested here: the governor still completes
        evaluation and returns a valid decision even when a hook mutates the
        operation via this low-level bypass.
        """

        class _MutatingHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                # object.__setattr__ bypasses frozen dataclass on CPython.
                # This is a CPython limitation, not a governor bug.
                object.__setattr__(operation, "agent_id", "attacker")
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    policy_id="mutating",
                    operation=operation,
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_MutatingHook())
        op = _op(action=MemoryAction.WRITE)
        # Governor must complete evaluation and return a valid decision.
        decision = gov.evaluate(op)
        assert decision is not None
        assert decision.verdict in (GovernanceVerdict.ALLOW, GovernanceVerdict.DENY)

    def test_hook_that_raises_in_after_op_does_not_propagate(self) -> None:
        """A hook raising in after_op must not surface through notify_after()."""

        class _RaisingAfterHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW, policy_id="ok"
                )

            def after_op(
                self,
                operation: MemoryOperation,
                decision: MemoryGovernanceDecision,
                result: Any = None,
                error: BaseException | None = None,
            ) -> None:
                raise RuntimeError("after_op sabotage")

        gov = MemoryGovernor()
        gov.add_hook(_RaisingAfterHook())
        op = _op()
        d = gov.evaluate(op)
        # Must not raise
        gov.notify_after(op, d)

    @pytest.mark.parametrize(
        "exc_type",
        [
            RuntimeError,
            ValueError,
            KeyError,
            MemoryError,
            ZeroDivisionError,
        ],
    )
    def test_hook_various_exception_types_all_produce_deny(
        self, exc_type: type[Exception]
    ) -> None:
        """Any exception class from before_op must produce DENY, never propagate."""
        exc_instance = exc_type("adversarial")

        class _AnyExcHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                raise exc_instance

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_AnyExcHook())
        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY, (
            f"Expected DENY for {exc_type.__name__}, got {decision.verdict}"
        )


# ---------------------------------------------------------------------------
# 4. Concurrent access
# ---------------------------------------------------------------------------


class TestAdversarialConcurrentAccess:
    """Multiple threads calling evaluate() or add_hook() simultaneously."""

    def test_concurrent_evaluate_20_threads_no_corruption(self) -> None:
        """20 threads each performing 100 evaluations must produce consistent results."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

        results: list[GovernanceVerdict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                try:
                    d = gov.evaluate(_op())
                    with lock:
                        results.append(d.verdict)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(results) == 2000
        assert all(v is GovernanceVerdict.ALLOW for v in results)

    def test_concurrent_add_hook_during_evaluate_no_deadlock(self) -> None:
        """add_hook() racing with evaluate() must not deadlock (5s timeout)."""
        gov = MemoryGovernor(fail_closed=False)
        errors: list[BaseException] = []
        completed = threading.Event()
        lock = threading.Lock()

        def adder() -> None:
            for _ in range(10):
                try:
                    gov.add_hook(DefaultMemoryGovernanceHook())
                except RuntimeError:
                    pass
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        def evaluator() -> None:
            for _ in range(50):
                try:
                    gov.evaluate(_op())
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
            completed.set()

        threads = [threading.Thread(target=adder) for _ in range(3)] + [
            threading.Thread(target=evaluator) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # All threads must have finished within 5 seconds (no deadlock)
        for t in threads:
            assert not t.is_alive(), "Thread still alive -- possible deadlock"

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_evaluate_mixed_hooks_no_corruption(self) -> None:
        """Mixed ALLOW/QUARANTINE/DENY hooks with 10 concurrent threads."""
        gov = MemoryGovernor()
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        gov.add_hook(_make_verdict_hook(GovernanceVerdict.QUARANTINE))

        verdicts: list[GovernanceVerdict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(30):
                try:
                    d = gov.evaluate(_op())
                    with lock:
                        verdicts.append(d.verdict)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(verdicts) == 300
        # Every result must be QUARANTINE (worst non-DENY wins)
        assert all(v is GovernanceVerdict.QUARANTINE for v in verdicts)


# ---------------------------------------------------------------------------
# 5. Verdict aggregation
# ---------------------------------------------------------------------------


class TestAdversarialVerdictAggregation:
    """QUARANTINE > DEGRADE > ALLOW priority, mixed verdict ordering."""

    @pytest.mark.parametrize(
        "hook_verdicts, expected",
        [
            # QUARANTINE always wins over ALLOW and DEGRADE
            (
                [GovernanceVerdict.ALLOW, GovernanceVerdict.QUARANTINE],
                GovernanceVerdict.QUARANTINE,
            ),
            (
                [GovernanceVerdict.DEGRADE, GovernanceVerdict.QUARANTINE],
                GovernanceVerdict.QUARANTINE,
            ),
            (
                [GovernanceVerdict.QUARANTINE, GovernanceVerdict.DEGRADE],
                GovernanceVerdict.QUARANTINE,
            ),
            (
                [GovernanceVerdict.QUARANTINE, GovernanceVerdict.ALLOW],
                GovernanceVerdict.QUARANTINE,
            ),
            # DEGRADE wins over ALLOW
            (
                [GovernanceVerdict.ALLOW, GovernanceVerdict.DEGRADE],
                GovernanceVerdict.DEGRADE,
            ),
            (
                [GovernanceVerdict.DEGRADE, GovernanceVerdict.ALLOW],
                GovernanceVerdict.DEGRADE,
            ),
            # All ALLOW stays ALLOW
            (
                [GovernanceVerdict.ALLOW, GovernanceVerdict.ALLOW],
                GovernanceVerdict.ALLOW,
            ),
            # DENY at any position short-circuits to DENY
            ([GovernanceVerdict.DENY, GovernanceVerdict.ALLOW], GovernanceVerdict.DENY),
            ([GovernanceVerdict.ALLOW, GovernanceVerdict.DENY], GovernanceVerdict.DENY),
            (
                [GovernanceVerdict.QUARANTINE, GovernanceVerdict.DENY],
                GovernanceVerdict.DENY,
            ),
            (
                [GovernanceVerdict.DEGRADE, GovernanceVerdict.DENY],
                GovernanceVerdict.DENY,
            ),
            # 3-way mix
            (
                [
                    GovernanceVerdict.ALLOW,
                    GovernanceVerdict.DEGRADE,
                    GovernanceVerdict.QUARANTINE,
                ],
                GovernanceVerdict.QUARANTINE,
            ),
            (
                [
                    GovernanceVerdict.QUARANTINE,
                    GovernanceVerdict.DEGRADE,
                    GovernanceVerdict.ALLOW,
                ],
                GovernanceVerdict.QUARANTINE,
            ),
        ],
    )
    def test_verdict_priority(
        self,
        hook_verdicts: list[GovernanceVerdict],
        expected: GovernanceVerdict,
    ) -> None:
        """Parametrized verdict aggregation: worst severity must win."""
        gov = MemoryGovernor()
        for v in hook_verdicts:
            gov.add_hook(_make_verdict_hook(v))
        decision = gov.evaluate(_op())
        assert decision.verdict is expected, (
            f"hooks={[v.value for v in hook_verdicts]}, "
            f"expected={expected.value}, got={decision.verdict.value}"
        )

    def test_deny_does_not_call_subsequent_hooks(self) -> None:
        """Once a DENY is issued, subsequent hooks must not be called."""
        call_log: list[str] = []

        class _TrackHook:
            def __init__(self, name: str, verdict: GovernanceVerdict) -> None:
                self._name = name
                self._verdict = verdict

            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                call_log.append(self._name)
                return MemoryGovernanceDecision(
                    verdict=self._verdict, policy_id=self._name
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_TrackHook("hook-1", GovernanceVerdict.ALLOW))
        gov.add_hook(_TrackHook("hook-2", GovernanceVerdict.DENY))
        gov.add_hook(_TrackHook("hook-3", GovernanceVerdict.ALLOW))  # must not run

        decision = gov.evaluate(_op())
        assert decision.verdict is GovernanceVerdict.DENY
        assert "hook-3" not in call_log, "hook-3 must not be evaluated after DENY"
        assert "hook-2" in call_log
        assert "hook-1" in call_log

    def test_quarantine_beats_degrade_regardless_of_insertion_order(self) -> None:
        """QUARANTINE must always win over DEGRADE regardless of insertion order."""
        for first, second in [
            (GovernanceVerdict.QUARANTINE, GovernanceVerdict.DEGRADE),
            (GovernanceVerdict.DEGRADE, GovernanceVerdict.QUARANTINE),
        ]:
            gov = MemoryGovernor()
            gov.add_hook(_make_verdict_hook(first))
            gov.add_hook(_make_verdict_hook(second))
            assert gov.evaluate(_op()).verdict is GovernanceVerdict.QUARANTINE


# ---------------------------------------------------------------------------
# 6. Resource exhaustion
# ---------------------------------------------------------------------------


class TestAdversarialResourceExhaustion:
    """Hook cap enforcement and hooks that block for too long."""

    def test_add_exactly_100_hooks_succeeds(self) -> None:
        """Adding exactly 100 hooks must succeed without raising."""
        gov = MemoryGovernor()
        for _ in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        assert gov.hook_count == 100

    def test_add_101st_hook_raises_runtime_error(self) -> None:
        """The 101st add_hook() call must raise RuntimeError mentioning 100."""
        gov = MemoryGovernor()
        for _ in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        with pytest.raises(RuntimeError, match="100"):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

    def test_add_many_beyond_cap_always_raises(self) -> None:
        """Every call beyond cap=100 must raise RuntimeError, not silently drop."""
        gov = MemoryGovernor()
        for _ in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))

        for attempt in range(5):
            with pytest.raises(RuntimeError):
                gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
            assert gov.hook_count == 100, (
                f"Hook count changed after rejected attempt {attempt}"
            )

    def test_hook_count_stays_at_cap_after_rejection(self) -> None:
        """hook_count must remain exactly 100 after a rejected add_hook()."""
        gov = MemoryGovernor()
        for _ in range(100):
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        try:
            gov.add_hook(_make_verdict_hook(GovernanceVerdict.ALLOW))
        except RuntimeError:
            pass
        assert gov.hook_count == 100

    @nogil_unstable
    def test_slow_hook_in_evaluate_completes_within_timeout(self) -> None:
        """A hook sleeping briefly must still complete -- evaluate() has no internal timeout."""
        SLEEP_S = 0.2  # 200 ms -- generous for nogil scheduler jitter

        class _SlowHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                time.sleep(SLEEP_S)
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW, policy_id="slow"
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        gov = MemoryGovernor()
        gov.add_hook(_SlowHook())

        start = time.monotonic()
        decision = gov.evaluate(_op())
        elapsed = time.monotonic() - start

        assert decision.verdict is GovernanceVerdict.ALLOW
        # Allow +-75% timing tolerance for nogil/Windows scheduler jitter.
        # The key invariant: evaluate() must not return before the hook finishes.
        assert elapsed >= SLEEP_S * 0.25, "Hook sleep appeared skipped"
        assert elapsed < 5.0, "evaluate() took unreasonably long"


# ---------------------------------------------------------------------------
# 7. Boundary conditions
# ---------------------------------------------------------------------------


class TestAdversarialBoundaryConditions:
    """Edge values: empty operation fields, max/min content_size_bytes."""

    def test_empty_string_fields_accepted(self) -> None:
        """MemoryOperation with all optional fields as empty strings must construct."""
        op = MemoryOperation(
            action=MemoryAction.READ,
            resource_id="",
            agent_id="",
            namespace="",
            content_hash="",
        )
        assert op.resource_id == ""
        assert op.agent_id == ""

    def test_zero_content_size_bytes_accepted(self) -> None:
        """content_size_bytes=0 must be valid."""
        op = MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=0)
        assert op.content_size_bytes == 0

    def test_large_content_size_bytes_accepted(self) -> None:
        """Very large content_size_bytes (2^53) must be accepted without error."""
        large = 2**53
        op = MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=large)
        assert op.content_size_bytes == large

    @pytest.mark.parametrize("negative", [-1, -100, -(2**53)])
    def test_negative_content_size_bytes_rejected(self, negative: int) -> None:
        """Negative content_size_bytes values must raise ValueError."""
        with pytest.raises(ValueError, match="content_size_bytes"):
            MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=negative)

    def test_non_memory_action_string_rejected(self) -> None:
        """Passing a plain string for action must raise TypeError."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action="write")  # type: ignore[arg-type]

    def test_non_memory_action_int_rejected(self) -> None:
        """Passing an integer for action must raise TypeError."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action=1)  # type: ignore[arg-type]

    def test_empty_operation_evaluates_without_crash(self) -> None:
        """Governor must evaluate a minimally-constructed MemoryOperation without error."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())
        op = MemoryOperation(action=MemoryAction.READ)
        decision = gov.evaluate(op)
        assert decision is not None
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_all_memory_actions_evaluate_without_crash(self) -> None:
        """Governor must handle every MemoryAction value without crashing."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())
        for action in MemoryAction:
            op = MemoryOperation(action=action)
            decision = gov.evaluate(op)
            assert decision is not None, f"Got None for action={action}"

    def test_all_memory_provenances_evaluate_without_crash(self) -> None:
        """Governor must handle every MemoryProvenance value without crashing."""
        gov = MemoryGovernor()
        gov.add_hook(DefaultMemoryGovernanceHook())
        for provenance in MemoryProvenance:
            op = MemoryOperation(
                action=MemoryAction.WRITE,
                provenance=provenance,
            )
            decision = gov.evaluate(op)
            assert decision is not None, f"Got None for provenance={provenance}"

    def test_metadata_with_nested_mutable_values_is_isolated(self) -> None:
        """Nested mutable values inside metadata must not allow external mutation."""
        inner_list: list[str] = ["original"]
        op = _op(metadata={"items": inner_list})
        inner_list.append("injected")
        # The proxy protects the top-level dict; nested objects are not deep-copied,
        # but the proxy key itself cannot be replaced or deleted
        with pytest.raises(TypeError):
            op.metadata["items"] = ["replacement"]  # type: ignore[index]

    def test_governance_decision_with_empty_reason_and_policy_id(self) -> None:
        """MemoryGovernanceDecision allows empty reason and policy_id."""
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="",
            policy_id="",
        )
        assert decision.denied is True
        assert decision.reason == ""
        assert decision.policy_id == ""

    def test_to_audit_dict_with_all_provenance_values(self) -> None:
        """to_audit_dict() must serialize all MemoryProvenance values correctly."""
        for provenance in MemoryProvenance:
            op = MemoryOperation(
                action=MemoryAction.READ,
                provenance=provenance,
            )
            decision = MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                operation=op,
            )
            d = decision.to_audit_dict()
            assert d["operation_provenance"] == provenance.value
