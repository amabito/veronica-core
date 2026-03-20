"""Adversarial tests for message governance hooks and MemoryGovernor directive merging.

Coverage:
- DenyOversizedMessageHook: boundary abuse, degrade_threshold extremes
- MessageBridgeHook: empty types, provenance variants, trust casing
- MemoryGovernor: DegradeDirective merging, error handling, thread safety
- MessageContext: validation edge cases
- BridgePolicy: evaluation order (short-circuit semantics)
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from veronica_core.memory.governor import MemoryGovernor, _merge_directives
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook
from veronica_core.memory.message_governance import (
    DenyOversizedMessageHook,
    MessageBridgeHook,
)
from veronica_core.memory.types import (
    BridgePolicy,
    DegradeDirective,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MessageContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(size: int = 0, **kwargs: Any) -> MessageContext:
    """Build a minimal MessageContext."""
    return MessageContext(content_size_bytes=size, **kwargs)


def _op(size: int = 0) -> MemoryOperation:
    """Build a minimal MemoryOperation for governor tests."""
    return MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=size)


class _StubHook:
    """Stub MemoryGovernanceHook returning a fixed decision."""

    def __init__(self, decision: MemoryGovernanceDecision) -> None:
        self._decision = decision

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        return self._decision

    def after_op(self, *args: Any, **kwargs: Any) -> None:
        pass


class _RaisingHook:
    """Hook that raises RuntimeError from before_op."""

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        raise RuntimeError("simulated hook failure")

    def after_op(self, *args: Any, **kwargs: Any) -> None:
        pass


class _NoneHook:
    """Hook that returns None from before_op (violates protocol)."""

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        return None  # type: ignore[return-value]

    def after_op(self, *args: Any, **kwargs: Any) -> None:
        pass


def _allow_decision(policy_id: str = "stub") -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.ALLOW, policy_id=policy_id
    )


def _degrade_decision(
    directive: DegradeDirective | None = None,
    policy_id: str = "stub",
) -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.DEGRADE,
        policy_id=policy_id,
        degrade_directive=directive,
    )


def _quarantine_decision(policy_id: str = "stub") -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.QUARANTINE, policy_id=policy_id
    )


def _deny_decision(policy_id: str = "stub") -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(verdict=GovernanceVerdict.DENY, policy_id=policy_id)


# ---------------------------------------------------------------------------
# 1. DenyOversizedMessageHook -- Boundary Abuse
# ---------------------------------------------------------------------------


class TestAdversarialDenyOversizedMessageHook:
    """Boundary abuse tests for DenyOversizedMessageHook."""

    def test_size_zero_is_allowed(self) -> None:
        """content_size_bytes = 0 must be ALLOW."""
        hook = DenyOversizedMessageHook(max_bytes=1000)
        ctx = _ctx(size=0)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_size_exactly_max_bytes_is_denied(self) -> None:
        """size == max_bytes must be DENY (boundary-inclusive)."""
        max_bytes = 1000
        hook = DenyOversizedMessageHook(max_bytes=max_bytes)
        ctx = _ctx(size=max_bytes)

        decision = hook.before_message(ctx)

        # Boundary fix: size >= max_bytes is DENY, not just strictly greater.
        assert decision.verdict is GovernanceVerdict.DENY

    def test_size_max_bytes_plus_one_is_denied(self) -> None:
        """size == max_bytes + 1 must be DENY."""
        max_bytes = 1000
        hook = DenyOversizedMessageHook(max_bytes=max_bytes)
        ctx = _ctx(size=max_bytes + 1)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_size_sys_maxsize_is_denied(self) -> None:
        """Enormous size (sys.maxsize) must be DENY without overflow."""
        hook = DenyOversizedMessageHook(max_bytes=1_000_000)
        ctx = _ctx(size=sys.maxsize)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_max_bytes_one_size_one_is_denied(self) -> None:
        """max_bytes=1, size=1 -- exactly at limit, must be DENY (boundary-inclusive)."""
        hook = DenyOversizedMessageHook(max_bytes=1)
        ctx = _ctx(size=1)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_max_bytes_one_size_two_is_denied(self) -> None:
        """max_bytes=1, size=2 -- one byte over, must be DENY."""
        hook = DenyOversizedMessageHook(max_bytes=1)
        ctx = _ctx(size=2)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_degrade_threshold_one_rejected(self) -> None:
        """degrade_threshold=1.0 -- rejected by validation.

        Bug #3 fix: degrade_threshold=1.0 creates an empty DEGRADE zone
        (degrade_at == max_bytes). This is now invalid: must be < 1.0.
        """
        max_bytes = 1000
        with pytest.raises(ValueError, match="degrade_threshold must be in"):
            DenyOversizedMessageHook(max_bytes=max_bytes, degrade_threshold=1.0)

    def test_degrade_threshold_very_small_almost_all_degrade(self) -> None:
        """degrade_threshold=0.001 -- degrade_at is nearly 0.

        Almost all non-zero sizes land in DEGRADE range.
        """
        max_bytes = 1_000_000
        hook = DenyOversizedMessageHook(max_bytes=max_bytes, degrade_threshold=0.001)
        # degrade_at = int(1_000_000 * 0.001) = 1000

        # Size 1 -- below degrade_at (1000) -> ALLOW.
        assert hook.before_message(_ctx(size=1)).verdict is GovernanceVerdict.ALLOW

        # Size exactly degrade_at -- not strictly greater, so ALLOW.
        assert hook.before_message(_ctx(size=1000)).verdict is GovernanceVerdict.ALLOW

        # Size degrade_at + 1 -- in degrade zone -> DEGRADE.
        decision = hook.before_message(_ctx(size=1001))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None

        # Size max_bytes -- DENY (boundary-inclusive).
        assert (
            hook.before_message(_ctx(size=max_bytes)).verdict is GovernanceVerdict.DENY
        )

    def test_invalid_max_bytes_zero_raises(self) -> None:
        """max_bytes=0 must raise ValueError at construction."""
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            DenyOversizedMessageHook(max_bytes=0)

    def test_invalid_max_bytes_negative_raises(self) -> None:
        """Negative max_bytes must raise ValueError at construction."""
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            DenyOversizedMessageHook(max_bytes=-1)

    def test_invalid_degrade_threshold_zero_raises(self) -> None:
        """degrade_threshold=0.0 is outside (0, 1] and must raise ValueError."""
        with pytest.raises(ValueError, match="degrade_threshold"):
            DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.0)

    def test_invalid_degrade_threshold_above_one_raises(self) -> None:
        """degrade_threshold=1.001 must raise ValueError."""
        with pytest.raises(ValueError, match="degrade_threshold"):
            DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=1.001)

    def test_degrade_verdict_carries_directive(self) -> None:
        """DEGRADE verdict must include a non-None degrade_directive."""
        hook = DenyOversizedMessageHook(max_bytes=1000, degrade_threshold=0.5)
        # degrade_at = 500; size 600 is in the degrade zone.
        ctx = _ctx(size=600)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.summary_required is True


# ---------------------------------------------------------------------------
# 2. MessageBridgeHook -- Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialMessageBridgeHook:
    """Adversarial tests for MessageBridgeHook."""

    def test_empty_message_type_denied_when_allowed_types_set(self) -> None:
        """Empty string message_type must be DENY if not in allowed_types."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True),
            allowed_message_types=frozenset({"agent_to_agent", "tool_result"}),
        )
        ctx = _ctx(message_type="")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_empty_allowed_types_frozenset_denies_all(self) -> None:
        """frozenset() means 'no types allowed' -- must DENY all messages.

        Fix: `if self._allowed_types is not None` distinguishes None (skip
        filter) from empty frozenset (deny all).
        """
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, quarantine_untrusted=False),
            allowed_message_types=frozenset(),
        )
        ctx = _ctx(message_type="any_type", trust_level="trusted")

        decision = hook.before_message(ctx)

        # Empty frozenset means no types are allowed -- DENY.
        assert decision.verdict is GovernanceVerdict.DENY

    def test_deny_archive_takes_priority_over_allowed_types(self) -> None:
        """allow_archive=False must DENY before allowed_types is even checked."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=False),
            allowed_message_types=frozenset({"tool_result"}),
        )
        ctx = _ctx(message_type="tool_result")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY
        assert "archive not permitted" in decision.reason

    def test_require_signature_with_verified_provenance_allows(self) -> None:
        """require_signature=True + VERIFIED provenance must pass signature check."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, require_signature=True),
        )
        ctx = _ctx(provenance=MemoryProvenance.VERIFIED, trust_level="trusted")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_require_signature_with_unverified_provenance_denied(self) -> None:
        """require_signature=True + UNVERIFIED provenance must DENY."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, require_signature=True),
        )
        ctx = _ctx(provenance=MemoryProvenance.UNVERIFIED)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY
        assert "signature required" in decision.reason

    def test_require_signature_with_quarantined_provenance_denied(self) -> None:
        """QUARANTINED provenance is not VERIFIED; require_signature must DENY."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, require_signature=True),
        )
        ctx = _ctx(provenance=MemoryProvenance.QUARANTINED)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_require_signature_with_unknown_provenance_denied(self) -> None:
        """UNKNOWN provenance is not VERIFIED; require_signature must DENY."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, require_signature=True),
        )
        ctx = _ctx(provenance=MemoryProvenance.UNKNOWN)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY

    def test_quarantine_untrusted_exact_case_match(self) -> None:
        """quarantine_untrusted checks trust_level in ('untrusted', '').

        The check is case-sensitive. 'Untrusted' (capital U) must NOT trigger
        quarantine; 'untrusted' (all lower) must.
        """
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=False,
                quarantine_untrusted=True,
            ),
        )
        # Lowercase 'untrusted' must trigger QUARANTINE.
        ctx_lower = _ctx(trust_level="untrusted")
        decision_lower = hook.before_message(ctx_lower)
        assert decision_lower.verdict is GovernanceVerdict.QUARANTINE

        # Wrong case ('Untrusted') must NOT trigger quarantine -- falls through to ALLOW.
        ctx_upper = _ctx(trust_level="Untrusted")
        decision_upper = hook.before_message(ctx_upper)
        assert decision_upper.verdict is GovernanceVerdict.ALLOW

    def test_quarantine_untrusted_all_caps_not_quarantined(self) -> None:
        """'UNTRUSTED' (all caps) is not in ('untrusted', ''); must not quarantine."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, quarantine_untrusted=True),
        )
        ctx = _ctx(trust_level="UNTRUSTED")

        decision = hook.before_message(ctx)

        # Not in the quarantine set -- falls through to ALLOW.
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_empty_trust_level_quarantined(self) -> None:
        """Empty string trust_level is treated as untrusted and must be QUARANTINED."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(allow_archive=True, quarantine_untrusted=True),
        )
        ctx = _ctx(trust_level="")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.QUARANTINE

    def test_allowed_types_check_before_quarantine(self) -> None:
        """allowed_types check runs BEFORE quarantine_untrusted check.

        If message_type is not in allowed_types, DENY should win even if the
        message would otherwise be quarantined.
        """
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                quarantine_untrusted=True,
            ),
            allowed_message_types=frozenset({"agent_to_agent"}),
        )
        # trust_level="untrusted" would quarantine, but message_type is wrong.
        ctx = _ctx(message_type="tool_result", trust_level="untrusted")

        decision = hook.before_message(ctx)

        # DENY from type check wins (type check runs first).
        assert decision.verdict is GovernanceVerdict.DENY

    def test_all_provenances_without_require_signature(self) -> None:
        """When require_signature=False, all provenance values must reach later checks."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=False,
                quarantine_untrusted=False,
            ),
        )
        for prov in MemoryProvenance:
            ctx = _ctx(provenance=prov, trust_level="trusted")
            decision = hook.before_message(ctx)
            assert decision.verdict is GovernanceVerdict.ALLOW, (
                f"Provenance {prov} should ALLOW when require_signature=False"
            )


# ---------------------------------------------------------------------------
# 3. Governor DegradeDirective Merging
# ---------------------------------------------------------------------------


class TestAdversarialGovernorDegradeMerging:
    """Tests for DegradeDirective merging under multiple DEGRADE hooks."""

    def test_two_degrade_hooks_directives_merged(self) -> None:
        """Two DEGRADE hooks must produce a merged directive (union of both)."""
        directive_a = DegradeDirective(
            mode="compact",
            summary_required=True,
            redacted_fields=("field_a",),
        )
        directive_b = DegradeDirective(
            mode="redact",
            raw_replay_blocked=True,
            redacted_fields=("field_b",),
        )
        governor = MemoryGovernor(
            hooks=[
                _StubHook(_allow_decision("hook_allow")),
                _StubHook(_degrade_decision(directive_a, "hook_degrade_a")),
                _StubHook(_degrade_decision(directive_b, "hook_degrade_b")),
            ],
            fail_closed=False,
        )

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DEGRADE
        merged = decision.degrade_directive
        assert merged is not None
        # Both booleans OR'd: summary_required from A, raw_replay_blocked from B.
        assert merged.summary_required is True
        assert merged.raw_replay_blocked is True
        # Redacted fields union (sorted).
        assert "field_a" in merged.redacted_fields
        assert "field_b" in merged.redacted_fields

    def test_degrade_with_none_directive_does_not_crash(self) -> None:
        """DEGRADE verdict with None directive must not crash the merge."""
        governor = MemoryGovernor(
            hooks=[
                _StubHook(_degrade_decision(None, "hook_none_directive")),
            ],
            fail_closed=False,
        )

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DEGRADE
        # No crash; directive may be None.

    def test_quarantine_wins_over_degrade(self) -> None:
        """QUARANTINE verdict must override DEGRADE (higher severity)."""
        governor = MemoryGovernor(
            hooks=[
                _StubHook(_degrade_decision(DegradeDirective(mode="compact"))),
                _StubHook(_quarantine_decision()),
            ],
            fail_closed=False,
        )

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.QUARANTINE
        # Bug #9 fix: QUARANTINE preserves the degrade directive from earlier
        # hooks so enforcement can apply degradation even at QUARANTINE level.
        assert decision.degrade_directive is not None

    def test_degrade_directive_max_content_size_bytes_stricter_wins(self) -> None:
        """max_content_size_bytes merges with min() semantics (stricter limit wins)."""
        d1 = DegradeDirective(max_content_size_bytes=500)
        d2 = DegradeDirective(max_content_size_bytes=1000)

        merged = _merge_directives(d1, d2)

        assert merged is not None
        assert merged.max_content_size_bytes == 500

    def test_degrade_directive_mode_new_wins_when_nonempty(self) -> None:
        """mode field: new value wins if non-empty, else existing is kept."""
        d_existing = DegradeDirective(mode="compact")
        d_new = DegradeDirective(mode="redact")

        merged = _merge_directives(d_existing, d_new)

        assert merged is not None
        assert merged.mode == "redact"

    def test_degrade_directive_mode_existing_kept_when_new_empty(self) -> None:
        """If new.mode is empty, existing mode must be preserved."""
        d_existing = DegradeDirective(mode="compact")
        d_new = DegradeDirective(mode="")

        merged = _merge_directives(d_existing, d_new)

        assert merged is not None
        assert merged.mode == "compact"

    def test_merge_directives_both_none_returns_none(self) -> None:
        """Merging None with None must return None."""
        assert _merge_directives(None, None) is None

    def test_merge_directives_existing_none_returns_new(self) -> None:
        """Merging None existing with non-None new must return new."""
        new = DegradeDirective(mode="compact")
        result = _merge_directives(None, new)
        assert result is new

    def test_merge_directives_new_none_returns_existing(self) -> None:
        """Merging non-None existing with None new must return existing."""
        existing = DegradeDirective(mode="compact")
        result = _merge_directives(existing, None)
        assert result is existing

    def test_allowed_provenance_union_sorted(self) -> None:
        """allowed_provenance merges as union, sorted for determinism."""
        d1 = DegradeDirective(allowed_provenance=("verified", "unverified"))
        d2 = DegradeDirective(allowed_provenance=("quarantined",))

        merged = _merge_directives(d1, d2)

        assert merged is not None
        assert set(merged.allowed_provenance) == {
            "verified",
            "unverified",
            "quarantined",
        }
        # Check sorted determinism.
        assert merged.allowed_provenance == tuple(sorted(merged.allowed_provenance))


# ---------------------------------------------------------------------------
# 4. Governor Error Handling
# ---------------------------------------------------------------------------


class TestAdversarialGovernorErrorHandling:
    """Fail-closed error handling in MemoryGovernor."""

    def test_hook_raises_runtime_error_results_in_deny(self) -> None:
        """RuntimeError from before_op must produce DENY (fail-closed)."""
        governor = MemoryGovernor(hooks=[_RaisingHook()], fail_closed=True)

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DENY
        assert "hook error" in decision.reason
        assert "RuntimeError" not in decision.reason  # Rule 5: no exc type leak

    def test_hook_returns_none_results_in_deny(self) -> None:
        """before_op returning None raises TypeError internally; must DENY."""
        governor = MemoryGovernor(hooks=[_NoneHook()], fail_closed=True)

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DENY
        assert "hook error" in decision.reason
        assert "TypeError" not in decision.reason  # Rule 5: no exc type leak

    def test_unknown_verdict_enum_value_results_in_deny(self) -> None:
        """An unrecognized verdict not in _VERDICT_RANK must DENY (fail-closed)."""

        class _UnknownVerdictHook:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                # Bypass the enum by constructing with a known verdict then
                # checking that an unknown GovernanceVerdict causes the governor
                # to fail closed. We test this by patching _VERDICT_RANK lookup.
                # Since GovernanceVerdict is a closed enum, we can only simulate
                # this by returning a verdict value that is NOT in _VERDICT_RANK.
                # _VERDICT_RANK contains ALLOW, DEGRADE, QUARANTINE (not DENY).
                # DENY is handled via short-circuit, so we need a sentinel.
                # The cleanest approach: a fake GovernanceVerdict-like object.
                class _FakeVerdict(str):
                    pass

                fake_verdict = _FakeVerdict("totally_unknown")

                # Build the decision with a real GovernanceVerdict to satisfy
                # the dataclass, then swap the verdict field post-construction
                # using object.__setattr__ (frozen dataclass).
                decision = MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.ALLOW,
                    policy_id="fake",
                )
                object.__setattr__(decision, "verdict", fake_verdict)
                return decision  # type: ignore[return-value]

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        governor = MemoryGovernor(hooks=[_UnknownVerdictHook()], fail_closed=True)

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DENY
        assert "unknown verdict" in decision.reason

    def test_fail_closed_with_no_hooks_denies(self) -> None:
        """Governor with no hooks and fail_closed=True must DENY."""
        governor = MemoryGovernor(fail_closed=True)

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.DENY
        assert "no hooks" in decision.reason

    def test_fail_open_with_no_hooks_allows(self) -> None:
        """Governor with no hooks and fail_closed=False must ALLOW."""
        governor = MemoryGovernor(fail_closed=False)

        decision = governor.evaluate(_op())

        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_error_in_after_op_does_not_raise(self) -> None:
        """notify_after must never raise even if a hook's after_op raises."""

        class _AfterOpRaiser:
            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                return _allow_decision()

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("after_op failure")

        governor = MemoryGovernor(hooks=[_AfterOpRaiser()])
        op = _op()
        decision = governor.evaluate(op)

        # Must not raise.
        governor.notify_after(op, decision)

    def test_first_deny_short_circuits_remaining_hooks(self) -> None:
        """DENY from hook N must stop evaluation; hooks N+1... are not called."""
        called: list[str] = []

        class _TrackingHook:
            def __init__(self, name: str, verdict: GovernanceVerdict) -> None:
                self._name = name
                self._verdict = verdict

            def before_op(
                self,
                operation: MemoryOperation,
                context: MemoryPolicyContext | None,
            ) -> MemoryGovernanceDecision:
                called.append(self._name)
                return MemoryGovernanceDecision(
                    verdict=self._verdict, policy_id=self._name
                )

            def after_op(self, *args: Any, **kwargs: Any) -> None:
                pass

        governor = MemoryGovernor(
            hooks=[
                _TrackingHook("allow_1", GovernanceVerdict.ALLOW),
                _TrackingHook("deny_2", GovernanceVerdict.DENY),
                _TrackingHook("allow_3", GovernanceVerdict.ALLOW),
            ],
        )

        governor.evaluate(_op())

        assert "allow_1" in called
        assert "deny_2" in called
        assert "allow_3" not in called, "hook after DENY must not be called"


# ---------------------------------------------------------------------------
# 5. Governor Thread Safety
# ---------------------------------------------------------------------------


class TestAdversarialGovernorThreadSafety:
    """Thread safety of MemoryGovernor.evaluate()."""

    def test_concurrent_evaluate_10_threads_no_corruption(self) -> None:
        """10 threads calling evaluate() simultaneously must all get valid decisions."""
        governor = MemoryGovernor(
            hooks=[DefaultMemoryGovernanceHook()],
            fail_closed=False,
        )
        results: list[MemoryGovernanceDecision] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                decision = governor.evaluate(_op())
                with lock:
                    results.append(decision)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(results) == 10
        for result in results:
            assert result.verdict in GovernanceVerdict

    def test_concurrent_add_hook_and_evaluate(self) -> None:
        """add_hook() and evaluate() from different threads must not deadlock or corrupt."""
        governor = MemoryGovernor(fail_closed=False)
        errors: list[Exception] = []
        lock = threading.Lock()

        def adder() -> None:
            try:
                for _ in range(5):
                    governor.add_hook(DefaultMemoryGovernanceHook())
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def evaluator() -> None:
            try:
                for _ in range(5):
                    governor.evaluate(_op())
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=adder) for _ in range(3)] + [
            threading.Thread(target=evaluator) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# 6. MessageContext Adversarial Validation
# ---------------------------------------------------------------------------


class TestAdversarialMessageContext:
    """Validation edge cases for MessageContext construction."""

    def test_negative_content_size_bytes_raises(self) -> None:
        """Negative content_size_bytes must raise ValueError."""
        with pytest.raises(ValueError, match="content_size_bytes must be >= 0"):
            MessageContext(content_size_bytes=-1)

    def test_very_long_sender_id_does_not_crash(self) -> None:
        """sender_id with 10000+ characters must not raise."""
        long_id = "x" * 10_001
        ctx = MessageContext(sender_id=long_id, content_size_bytes=0)
        assert len(ctx.sender_id) == 10_001

    def test_very_long_recipient_id_does_not_crash(self) -> None:
        """recipient_id with 10000+ characters must not raise."""
        long_id = "y" * 10_001
        ctx = MessageContext(recipient_id=long_id, content_size_bytes=0)
        assert len(ctx.recipient_id) == 10_001

    def test_metadata_with_nested_dict_does_not_crash(self) -> None:
        """Nested dict metadata must be accepted without raising."""
        ctx = MessageContext(
            content_size_bytes=0,
            metadata={"outer": {"inner": [1, 2, None]}},
        )
        assert ctx.metadata["outer"]["inner"][2] is None

    def test_metadata_with_none_values_does_not_crash(self) -> None:
        """Metadata containing None values must be accepted."""
        ctx = MessageContext(
            content_size_bytes=0,
            metadata={"key": None},
        )
        assert ctx.metadata["key"] is None

    def test_metadata_is_immutable_after_construction(self) -> None:
        """MessageContext.metadata must be read-only (MappingProxyType)."""
        ctx = MessageContext(content_size_bytes=0, metadata={"k": "v"})
        with pytest.raises(TypeError):
            ctx.metadata["k"] = "mutated"  # type: ignore[index]

    def test_zero_content_size_bytes_is_valid(self) -> None:
        """content_size_bytes=0 must be valid."""
        ctx = MessageContext(content_size_bytes=0)
        assert ctx.content_size_bytes == 0


# ---------------------------------------------------------------------------
# 7. BridgePolicy Evaluation Order
# ---------------------------------------------------------------------------


class TestAdversarialBridgePolicyEvaluationOrder:
    """Verify strict evaluation order and short-circuit semantics."""

    def test_allow_archive_false_denies_before_signature_check(self) -> None:
        """allow_archive=False must DENY without reaching require_signature."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=False,
                require_signature=True,  # would also deny, but archive check is first
            ),
        )
        ctx = _ctx(provenance=MemoryProvenance.UNVERIFIED)

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY
        assert "archive not permitted" in decision.reason

    def test_signature_check_before_allowed_types(self) -> None:
        """require_signature check must DENY before allowed_types is evaluated."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=True,
            ),
            allowed_message_types=frozenset({"tool_result"}),
        )
        # Correct type, wrong provenance.
        ctx = _ctx(message_type="tool_result", provenance=MemoryProvenance.UNVERIFIED)

        decision = hook.before_message(ctx)

        # Signature check fires first -> DENY with 'signature required'.
        assert decision.verdict is GovernanceVerdict.DENY
        assert "signature required" in decision.reason

    def test_allowed_types_before_quarantine(self) -> None:
        """allowed_types DENY must fire before quarantine_untrusted check."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=False,
                quarantine_untrusted=True,
            ),
            allowed_message_types=frozenset({"agent_to_agent"}),
        )
        # Wrong type + untrusted -- type check must win.
        ctx = _ctx(message_type="tool_result", trust_level="untrusted")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.DENY
        assert "not in allowed types" in decision.reason

    def test_quarantine_is_last_non_allow_check(self) -> None:
        """quarantine_untrusted is the final gate before ALLOW."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=False,
                quarantine_untrusted=True,
            ),
            # No allowed_types filter.
        )
        ctx = _ctx(trust_level="untrusted")

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.QUARANTINE

    def test_all_checks_pass_results_in_allow(self) -> None:
        """When all checks pass, verdict must be ALLOW."""
        hook = MessageBridgeHook(
            policy=BridgePolicy(
                allow_archive=True,
                require_signature=True,
                quarantine_untrusted=True,
            ),
            allowed_message_types=frozenset({"agent_to_agent"}),
        )
        ctx = _ctx(
            message_type="agent_to_agent",
            provenance=MemoryProvenance.VERIFIED,
            trust_level="trusted",
        )

        decision = hook.before_message(ctx)

        assert decision.verdict is GovernanceVerdict.ALLOW
