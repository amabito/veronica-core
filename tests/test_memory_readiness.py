"""Tests for memory governance readiness diagnostics.

Covers: no hooks, partial hooks, full stack, deterministic snapshot,
        serialization, and side-effect-free behavior.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from veronica_core.diagnostics.readiness import MemoryGovernanceReadiness
from veronica_core.memory.compactness import CompactnessEvaluator
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)
from veronica_core.memory.view_policy import ViewPolicyEvaluator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubHook:
    """Minimal hook for testing."""

    def before_op(
        self, operation: MemoryOperation, context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW, policy_id="stub")

    def after_op(
        self, operation: MemoryOperation, decision: MemoryGovernanceDecision,
        result: Any = None, error: BaseException | None = None,
    ) -> None:
        pass


# ---------------------------------------------------------------------------
# No governor
# ---------------------------------------------------------------------------


class TestNoGovernor:
    """Readiness when no governor is provided."""

    def test_none_governor(self) -> None:
        readiness = MemoryGovernanceReadiness()
        snapshot = readiness.check(None)
        assert not snapshot.governance_enabled
        assert snapshot.fail_closed is True
        assert snapshot.hook_count == 0
        assert snapshot.registered_hooks == ()
        assert not snapshot.compactness_evaluator_present
        assert not snapshot.view_policy_evaluator_present
        assert not snapshot.boundary_hook_present
        assert len(snapshot.supported_views) > 0
        assert len(snapshot.supported_modes) > 0


# ---------------------------------------------------------------------------
# No hooks
# ---------------------------------------------------------------------------


class TestNoHooks:
    """Governor exists but has no hooks."""

    def test_empty_governor_fail_closed(self) -> None:
        governor = MemoryGovernor(fail_closed=True)
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert not snapshot.governance_enabled
        assert snapshot.fail_closed is True
        assert snapshot.hook_count == 0

    def test_empty_governor_fail_open(self) -> None:
        governor = MemoryGovernor(fail_closed=False)
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert not snapshot.governance_enabled
        assert snapshot.fail_closed is False


# ---------------------------------------------------------------------------
# Partial hooks
# ---------------------------------------------------------------------------


class TestPartialHooks:
    """Governor with some hooks registered."""

    def test_only_default_hook(self) -> None:
        governor = MemoryGovernor(hooks=[DefaultMemoryGovernanceHook()])
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert snapshot.governance_enabled
        assert snapshot.hook_count == 1
        assert "DefaultMemoryGovernanceHook" in snapshot.registered_hooks
        assert not snapshot.compactness_evaluator_present
        assert not snapshot.degrade_support

    def test_compactness_only(self) -> None:
        governor = MemoryGovernor(hooks=[CompactnessEvaluator()])
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert snapshot.compactness_evaluator_present
        assert not snapshot.view_policy_evaluator_present
        assert snapshot.degrade_support

    def test_view_policy_only(self) -> None:
        governor = MemoryGovernor(hooks=[ViewPolicyEvaluator()])
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert snapshot.view_policy_evaluator_present
        assert not snapshot.compactness_evaluator_present
        assert snapshot.degrade_support


# ---------------------------------------------------------------------------
# Full stack
# ---------------------------------------------------------------------------


class TestFullStack:
    """Governor with full memory governance stack."""

    def test_full_stack(self) -> None:
        governor = MemoryGovernor(hooks=[
            CompactnessEvaluator(),
            ViewPolicyEvaluator(),
        ])
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert snapshot.governance_enabled
        assert snapshot.hook_count == 2
        assert snapshot.compactness_evaluator_present
        assert snapshot.view_policy_evaluator_present
        assert snapshot.degrade_support
        assert snapshot.lifecycle_support

    def test_full_stack_with_rule_evaluator(self) -> None:
        from veronica_core.policy.memory_rules import (
            MemoryRuleCompiler,
            MemoryRuleEvaluator,
        )
        from veronica_core.policy.bundle import PolicyRule

        rule = PolicyRule(
            rule_id="test",
            rule_type="memory",
            parameters={"verdict": "allow"},
        )
        compiled = MemoryRuleCompiler().compile(rule)
        evaluator = MemoryRuleEvaluator((compiled,))

        governor = MemoryGovernor(hooks=[
            CompactnessEvaluator(),
            ViewPolicyEvaluator(),
            evaluator,
        ])
        snapshot = MemoryGovernanceReadiness().check(governor)
        assert snapshot.memory_rule_evaluator_present
        assert snapshot.hook_count == 3


# ---------------------------------------------------------------------------
# Deterministic snapshot
# ---------------------------------------------------------------------------


class TestDeterministicSnapshot:
    """Repeated calls produce identical results."""

    def test_repeated_check(self) -> None:
        governor = MemoryGovernor(hooks=[CompactnessEvaluator()])
        readiness = MemoryGovernanceReadiness()
        snap1 = readiness.check(governor)
        snap2 = readiness.check(governor)
        assert snap1.to_dict() == snap2.to_dict()

    def test_supported_views_are_sorted(self) -> None:
        snapshot = MemoryGovernanceReadiness().check(None)
        views = list(snapshot.supported_views)
        assert views == sorted(views)

    def test_supported_modes_are_sorted(self) -> None:
        snapshot = MemoryGovernanceReadiness().check(None)
        modes = list(snapshot.supported_modes)
        assert modes == sorted(modes)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict() produces JSON-compatible output."""

    def test_to_dict_keys(self) -> None:
        snapshot = MemoryGovernanceReadiness().check(None)
        d = snapshot.to_dict()
        expected_keys = {
            "governance_enabled", "fail_closed", "hook_count",
            "registered_hooks", "compactness_evaluator_present",
            "view_policy_evaluator_present", "boundary_hook_present",
            "memory_rule_evaluator_present", "supported_views",
            "supported_modes", "degrade_support", "lifecycle_support",
            "audit_schema_version",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_types(self) -> None:
        governor = MemoryGovernor(hooks=[CompactnessEvaluator()])
        d = MemoryGovernanceReadiness().check(governor).to_dict()
        assert isinstance(d["governance_enabled"], bool)
        assert isinstance(d["hook_count"], int)
        assert isinstance(d["registered_hooks"], list)
        assert isinstance(d["supported_views"], list)
        assert isinstance(d["audit_schema_version"], str)

    def test_snapshot_is_frozen(self) -> None:
        snapshot = MemoryGovernanceReadiness().check(None)
        with pytest.raises(AttributeError):
            snapshot.governance_enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Side-effect free
# ---------------------------------------------------------------------------


class TestSideEffectFree:
    """Readiness check does not modify the governor."""

    def test_hook_count_unchanged(self) -> None:
        governor = MemoryGovernor(hooks=[_StubHook()])
        before = governor.hook_count
        MemoryGovernanceReadiness().check(governor)
        after = governor.hook_count
        assert before == after

    def test_governor_still_functional(self) -> None:
        from veronica_core.memory.types import MemoryAction, MemoryOperation
        governor = MemoryGovernor(hooks=[DefaultMemoryGovernanceHook()])
        MemoryGovernanceReadiness().check(governor)
        op = MemoryOperation(action=MemoryAction.READ, resource_id="r")
        decision = governor.evaluate(op)
        assert decision.allowed


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialReadiness:
    """Adversarial tests for MemoryGovernanceReadiness -- attacker mindset."""

    # ------------------------------------------------------------------
    # Concurrent access
    # ------------------------------------------------------------------

    def test_concurrent_check_produces_identical_snapshots(self) -> None:
        """10 threads calling check() simultaneously must all see the same snapshot."""
        governor = MemoryGovernor(hooks=[CompactnessEvaluator(), ViewPolicyEvaluator()])
        readiness = MemoryGovernanceReadiness()
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(10)

        def worker() -> None:
            try:
                barrier.wait()  # all threads start together
                snap = readiness.check(governor)
                results.append(snap.to_dict())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        first = results[0]
        for snapshot_dict in results[1:]:
            assert snapshot_dict == first, "Concurrent snapshots diverged"

    def test_concurrent_check_does_not_mutate_governor_hooks(self) -> None:
        """Governor._hooks must not be modified by concurrent readiness checks."""
        stub1 = _StubHook()
        stub2 = _StubHook()
        governor = MemoryGovernor(hooks=[stub1, stub2])
        readiness = MemoryGovernanceReadiness()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            readiness.check(governor)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Hook list must be untouched: same objects, same order.
        with governor._lock:
            hooks_after = list(governor._hooks)
        assert hooks_after == [stub1, stub2]

    # ------------------------------------------------------------------
    # Hook spoofing -- name-based detection
    # ------------------------------------------------------------------

    def test_hook_spoofing_name_based_detection(self) -> None:
        """A stub class named 'CompactnessEvaluator' triggers compactness_evaluator_present.

        Detection is intentionally name-based (class.__name__), not type-based.
        This is by design: the readiness check inspects the governance stack
        as-configured, not as-imported.  A hook with the right name is treated
        as the right evaluator.  Callers that need type safety must use
        isinstance() checks at registration time.
        """

        class CompactnessEvaluator(_StubHook):  # noqa: N801 -- deliberate name spoof
            """Impersonates CompactnessEvaluator by class name."""

        spoof = CompactnessEvaluator()
        governor = MemoryGovernor(hooks=[spoof])
        snapshot = MemoryGovernanceReadiness().check(governor)

        # Name-match succeeds -- this is documented behavior, not a security bypass.
        assert snapshot.compactness_evaluator_present is True
        assert snapshot.hook_count == 1

    def test_hook_with_empty_class_name_does_not_crash(self) -> None:
        """A hook whose class __name__ is an empty string must not raise."""

        class _EmptyNameBase(_StubHook):
            pass

        # Forcibly rename the class to an empty string to simulate a
        # pathological metaclass or C-extension corner case.
        _EmptyNameBase.__name__ = ""

        governor = MemoryGovernor(hooks=[_EmptyNameBase()])
        readiness = MemoryGovernanceReadiness()

        # Must complete without raising.
        snapshot = readiness.check(governor)
        assert snapshot.hook_count == 1
        assert "" in snapshot.registered_hooks
        # Empty name matches none of the known evaluator sets.
        assert not snapshot.compactness_evaluator_present
        assert not snapshot.view_policy_evaluator_present
        assert not snapshot.boundary_hook_present
        assert not snapshot.memory_rule_evaluator_present

    # ------------------------------------------------------------------
    # Snapshot immutability
    # ------------------------------------------------------------------

    def test_to_dict_returns_new_dict_each_call(self) -> None:
        """to_dict() must return a distinct dict object on every call."""
        snapshot = MemoryGovernanceReadiness().check(None)
        d1 = snapshot.to_dict()
        d2 = snapshot.to_dict()
        assert d1 is not d2
        assert d1 == d2  # same content

    def test_modifying_to_dict_result_does_not_affect_snapshot(self) -> None:
        """Mutating the dict returned by to_dict() must not alter the snapshot."""
        governor = MemoryGovernor(hooks=[CompactnessEvaluator()])
        snapshot = MemoryGovernanceReadiness().check(governor)
        d = snapshot.to_dict()

        # Mutate every mutable field in the dict.
        d["governance_enabled"] = False
        d["hook_count"] = 999
        d["registered_hooks"].clear()
        d["supported_views"].clear()
        d["supported_modes"].clear()

        # Snapshot is frozen -- fields must be unchanged.
        assert snapshot.governance_enabled is True
        assert snapshot.hook_count == 1
        assert len(snapshot.registered_hooks) == 1
        assert len(snapshot.supported_views) > 0
        assert len(snapshot.supported_modes) > 0

    def test_registered_hooks_tuple_is_immutable(self) -> None:
        """registered_hooks is a tuple -- it must resist mutation attempts."""
        governor = MemoryGovernor(hooks=[CompactnessEvaluator()])
        snapshot = MemoryGovernanceReadiness().check(governor)

        with pytest.raises(TypeError):
            snapshot.registered_hooks[0] = "injected"  # type: ignore[index]

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_duplicate_hooks_of_same_type_reflected_in_hook_count(self) -> None:
        """Two instances of the same hook class both count toward hook_count."""
        hook_a = CompactnessEvaluator()
        hook_b = CompactnessEvaluator()
        governor = MemoryGovernor(hooks=[hook_a, hook_b])
        snapshot = MemoryGovernanceReadiness().check(governor)

        assert snapshot.hook_count == 2
        assert snapshot.registered_hooks.count("CompactnessEvaluator") == 2
        assert snapshot.compactness_evaluator_present is True

    def test_one_hundred_hooks_completes_without_error(self) -> None:
        """Governor with 100 hooks (the cap) must be inspected without crashing."""
        # MemoryGovernor caps at 100 hooks (_MAX_HOOKS).
        hooks = [_StubHook() for _ in range(100)]
        governor = MemoryGovernor(hooks=hooks)
        readiness = MemoryGovernanceReadiness()

        # Must not raise and must report all 100.
        snapshot = readiness.check(governor)
        assert snapshot.hook_count == 100
        assert len(snapshot.registered_hooks) == 100
        assert snapshot.governance_enabled is True

    def test_lifecycle_support_is_true(self) -> None:
        """lifecycle.py is present in the package so lifecycle_support must be True."""
        snapshot = MemoryGovernanceReadiness().check(None)
        assert snapshot.lifecycle_support is True
