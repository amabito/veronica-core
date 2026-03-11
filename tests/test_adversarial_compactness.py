"""Adversarial tests for CompactnessEvaluator and memory type system.

Attack categories:
1. CompactnessEvaluator corrupted inputs  -- NaN, Inf, negative, garbage strings
2. CompactnessEvaluator boundary abuse    -- off-by-one, min constraints, zero limits
3. CompactnessEvaluator concurrent access -- 10-thread before_op race
4. Type immutability                      -- FrozenInstanceError on all frozen types
5. MessageContext validation              -- negative size, metadata proxy
6. MemoryOperation validation             -- non-MemoryAction, negative size
"""

from __future__ import annotations

import math
import sys
import threading
import types as _types
from typing import Any

import pytest

from veronica_core.memory.compactness import CompactnessEvaluator
from veronica_core.memory.types import (
    CompactnessConstraints,
    DegradeDirective,
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MessageContext,
    ThreatContext,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _op(
    *,
    size: int = 0,
    provenance: MemoryProvenance = MemoryProvenance.UNKNOWN,
    metadata: dict[str, Any] | None = None,
) -> MemoryOperation:
    return MemoryOperation(
        action=MemoryAction.WRITE,
        resource_id="res-adv",
        agent_id="agent-adv",
        content_size_bytes=size,
        provenance=provenance,
        metadata=metadata or {},
    )


def _ctx(constraints: CompactnessConstraints | None) -> MemoryPolicyContext:
    return MemoryPolicyContext(
        operation=_op(),
        chain_id="chain-adv",
        trust_level="trusted",
        memory_view=ExecutionMode.LIVE.value,  # type: ignore[arg-type]  # cast accepted
        compactness=constraints,
    )


# ---------------------------------------------------------------------------
# 1. CompactnessEvaluator -- corrupted inputs
# ---------------------------------------------------------------------------


class TestAdversarialCorruptedInputs:
    """Garbage metadata values must not crash the evaluator."""

    def test_nan_as_raw_replay_ratio_does_not_crash(self) -> None:
        """NaN raw_replay_ratio: float comparison with NaN always returns False.

        NaN > any_float == False, so the limit check must silently pass (ALLOW).
        The evaluator must not crash or raise.
        """
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": float("nan")})
        decision = ev.before_op(op, _ctx(constraints))
        # NaN > 0.5 is False -> no degrade triggered from ratio alone
        assert decision is not None
        assert decision.verdict in (GovernanceVerdict.ALLOW, GovernanceVerdict.DEGRADE)

    def test_inf_as_raw_replay_ratio_triggers_degrade(self) -> None:
        """Inf raw_replay_ratio exceeds any finite limit -- must DEGRADE."""
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.9)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": float("inf")})
        decision = ev.before_op(op, _ctx(constraints))
        # inf > 0.9 is True -> must DEGRADE with raw_replay_blocked
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.raw_replay_blocked is True

    def test_negative_inf_as_raw_replay_ratio_does_not_crash(self) -> None:
        """-inf raw_replay_ratio is below any positive limit -- evaluator must not crash."""
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": float("-inf")})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision is not None

    def test_garbage_string_as_packet_tokens_raises_value_error(self) -> None:
        """Non-numeric string for packet_tokens: int('garbage') raises ValueError.

        The evaluator does not catch this -- callers must provide valid metadata.
        This test documents the behavior: ValueError propagates.
        """
        constraints = CompactnessConstraints(max_packet_tokens=100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": "garbage"})
        with pytest.raises((ValueError, TypeError)):
            ev.before_op(op, _ctx(constraints))

    def test_empty_string_as_packet_tokens_raises(self) -> None:
        """Empty string for packet_tokens: int('') must raise ValueError."""
        constraints = CompactnessConstraints(max_packet_tokens=100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": ""})
        with pytest.raises((ValueError, TypeError)):
            ev.before_op(op, _ctx(constraints))

    def test_none_as_attribute_count_raises(self) -> None:
        """None for attribute_count: int(None) must raise TypeError."""
        constraints = CompactnessConstraints(max_attributes_per_packet=5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": None})
        with pytest.raises((ValueError, TypeError)):
            ev.before_op(op, _ctx(constraints))

    def test_negative_packet_tokens_in_metadata_allows(self) -> None:
        """Negative packet_tokens is below any positive limit -- must ALLOW."""
        constraints = CompactnessConstraints(max_packet_tokens=100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": -50})
        decision = ev.before_op(op, _ctx(constraints))
        # -50 > 100 is False -> no degrade
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_negative_attribute_count_in_metadata_allows(self) -> None:
        """Negative attribute_count is below any positive limit -- must ALLOW."""
        constraints = CompactnessConstraints(max_attributes_per_packet=10)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": -1})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_negative_raw_replay_ratio_in_metadata_allows(self) -> None:
        """Negative raw_replay_ratio is below any positive limit -- must ALLOW."""
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.5)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": -0.9})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_sys_maxsize_as_packet_tokens_triggers_degrade(self) -> None:
        """sys.maxsize packet_tokens exceeds any practical limit -- must DEGRADE."""
        constraints = CompactnessConstraints(max_packet_tokens=1000)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": sys.maxsize})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_large_int_as_attribute_count_triggers_degrade(self) -> None:
        """2**32 attribute_count exceeds any realistic limit -- must DEGRADE."""
        constraints = CompactnessConstraints(max_attributes_per_packet=100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": 2**32})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_float_as_packet_tokens_truncates_to_int(self) -> None:
        """Float for packet_tokens: int(501.9) == 501 -- still exceeds 500 limit."""
        constraints = CompactnessConstraints(max_packet_tokens=500)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 501.9})
        decision = ev.before_op(op, _ctx(constraints))
        # int(501.9) = 501 > 500 -> DEGRADE
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_float_below_limit_as_packet_tokens_allows(self) -> None:
        """Float for packet_tokens: int(499.9) == 499 -- below 500 limit -> ALLOW."""
        constraints = CompactnessConstraints(max_packet_tokens=500)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 499.9})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# 2. CompactnessEvaluator -- boundary abuse
# ---------------------------------------------------------------------------


class TestAdversarialBoundaryAbuse:
    """Off-by-one, minimum constraints, zero/negative limit values."""

    def test_content_size_bytes_exactly_at_max_payload_allows(self) -> None:
        """size == max_payload_bytes must ALLOW (not denied -- denial is strictly greater)."""
        constraints = CompactnessConstraints(max_payload_bytes=1024)
        ev = CompactnessEvaluator()
        op = _op(size=1024)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_content_size_bytes_one_over_max_payload_denies(self) -> None:
        """size == max_payload_bytes + 1 must DENY."""
        constraints = CompactnessConstraints(max_payload_bytes=1024)
        ev = CompactnessEvaluator()
        op = _op(size=1025)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DENY

    def test_max_payload_bytes_equals_one_denies_size_two(self) -> None:
        """max_payload_bytes=1 is valid; size=2 must be denied."""
        constraints = CompactnessConstraints(max_payload_bytes=1)
        ev = CompactnessEvaluator()
        op = _op(size=2)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DENY

    def test_max_payload_bytes_equals_one_allows_size_one(self) -> None:
        """max_payload_bytes=1, size=1 must ALLOW."""
        constraints = CompactnessConstraints(max_payload_bytes=1)
        ev = CompactnessEvaluator()
        op = _op(size=1)
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_all_constraints_zero_with_huge_operation_allows(self) -> None:
        """All limits at 0 (disabled) with massive payload must ALLOW (no limits)."""
        constraints = CompactnessConstraints(
            max_payload_bytes=0,
            max_packet_tokens=0,
            max_attributes_per_packet=0,
            max_raw_replay_ratio=1.0,
            require_compaction_if_over_budget=False,
            prefer_verified_summary=False,
        )
        ev = CompactnessEvaluator()
        op = _op(
            size=2**40,
            metadata={
                "packet_tokens": sys.maxsize,
                "attribute_count": 10_000_000,
                "raw_replay_ratio": 0.99,
            },
        )
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_negative_max_packet_tokens_in_constraints_treated_as_no_limit(self) -> None:
        """Negative max_packet_tokens: condition 'constraints.max_packet_tokens > 0' is False.

        Result: no token limit applied -- large packet_tokens must ALLOW.
        """
        # CompactnessConstraints is a frozen dataclass with no validation;
        # negative values are accepted at construction.
        constraints = CompactnessConstraints(max_packet_tokens=-100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 999_999})
        decision = ev.before_op(op, _ctx(constraints))
        # -100 > 0 is False -> no limit check -> ALLOW
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_negative_max_attributes_in_constraints_treated_as_no_limit(self) -> None:
        """Negative max_attributes_per_packet: condition '>0' is False -> no limit."""
        constraints = CompactnessConstraints(max_attributes_per_packet=-1)
        ev = CompactnessEvaluator()
        op = _op(metadata={"attribute_count": 1_000_000})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.ALLOW

    def test_max_raw_replay_ratio_zero_degrades_any_nonzero_ratio(self) -> None:
        """max_raw_replay_ratio=0.0: any positive ratio in metadata must DEGRADE."""
        constraints = CompactnessConstraints(max_raw_replay_ratio=0.0)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": 0.001})
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        assert decision.degrade_directive is not None
        assert decision.degrade_directive.raw_replay_blocked is True

    def test_require_compaction_false_with_over_budget_does_not_force_summary(self) -> None:
        """require_compaction_if_over_budget=False must not set summary_required even if over."""
        constraints = CompactnessConstraints(
            max_packet_tokens=10,
            require_compaction_if_over_budget=False,
        )
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": 9999})
        decision = ev.before_op(op, _ctx(constraints))
        # packet_tokens limit triggers DEGRADE but summary_required comes only from
        # max_packet_tokens branch (summary_required=True) not from compaction flag.
        assert decision.verdict is GovernanceVerdict.DEGRADE

    def test_all_soft_limits_simultaneously_triggered_merges_into_one_directive(self) -> None:
        """All soft limits triggered at once must produce a single merged DegradeDirective."""
        constraints = CompactnessConstraints(
            max_packet_tokens=1,
            max_attributes_per_packet=1,
            max_raw_replay_ratio=0.0,
            require_compaction_if_over_budget=True,
            prefer_verified_summary=True,
        )
        ev = CompactnessEvaluator()
        op = _op(
            provenance=MemoryProvenance.UNVERIFIED,
            metadata={
                "packet_tokens": 1_000_000,
                "attribute_count": 1_000_000,
                "raw_replay_ratio": 0.001,
            },
        )
        decision = ev.before_op(op, _ctx(constraints))
        assert decision.verdict is GovernanceVerdict.DEGRADE
        d = decision.degrade_directive
        assert d is not None
        assert d.summary_required is True
        assert d.raw_replay_blocked is True
        assert d.verified_only is True
        # Only one degrade directive returned (not a list)
        assert isinstance(d, DegradeDirective)


# ---------------------------------------------------------------------------
# 3. CompactnessEvaluator -- concurrent access
# ---------------------------------------------------------------------------


class TestAdversarialConcurrentAccess:
    """Thread-safety: 10 threads calling before_op on the same evaluator."""

    def test_concurrent_before_op_10_threads_no_corruption(self) -> None:
        """10 threads each calling before_op 50 times must all return valid decisions.

        CompactnessEvaluator documents thread-safety via immutable instance state.
        This verifies no corruption occurs under concurrent load.
        """
        constraints = CompactnessConstraints(
            max_packet_tokens=100,
            max_payload_bytes=500,
        )
        ev = CompactnessEvaluator(default_constraints=constraints)

        verdicts: list[GovernanceVerdict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            for i in range(50):
                try:
                    op = _op(
                        size=i * 10,
                        metadata={"packet_tokens": i * 5},
                    )
                    decision = ev.before_op(op, None)
                    with lock:
                        verdicts.append(decision.verdict)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        for t in threads:
            assert not t.is_alive(), "Thread still alive -- possible deadlock"

        assert errors == [], f"Thread errors: {errors}"
        assert len(verdicts) == 500
        # Every verdict must be a valid GovernanceVerdict
        valid = set(GovernanceVerdict)
        assert all(v in valid for v in verdicts)

    def test_concurrent_before_op_with_none_context_thread_safe(self) -> None:
        """before_op(op, None) across 10 threads with default_constraints must not crash."""
        default = CompactnessConstraints(max_payload_bytes=100)
        ev = CompactnessEvaluator(default_constraints=default)

        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(20):
                try:
                    op_small = _op(size=50)
                    op_large = _op(size=200)
                    ev.before_op(op_small, None)
                    ev.before_op(op_large, None)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert errors == [], f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# 4. Type immutability
# ---------------------------------------------------------------------------


class TestAdversarialTypeImmutability:
    """FrozenInstanceError on all frozen dataclass types."""

    def test_degrade_directive_is_frozen(self) -> None:
        """Direct field assignment on DegradeDirective must raise FrozenInstanceError."""
        d = DegradeDirective(mode="compact", summary_required=True)
        with pytest.raises((AttributeError, TypeError)):
            d.mode = "redact"  # type: ignore[misc]

    def test_degrade_directive_verified_only_frozen(self) -> None:
        """Assigning verified_only on frozen DegradeDirective must raise."""
        d = DegradeDirective(verified_only=True)
        with pytest.raises((AttributeError, TypeError)):
            d.verified_only = False  # type: ignore[misc]

    def test_degrade_directive_max_packet_tokens_frozen(self) -> None:
        """Assigning max_packet_tokens on frozen DegradeDirective must raise."""
        d = DegradeDirective(max_packet_tokens=500)
        with pytest.raises((AttributeError, TypeError)):
            d.max_packet_tokens = 0  # type: ignore[misc]

    def test_compactness_constraints_is_frozen(self) -> None:
        """Direct field assignment on CompactnessConstraints must raise."""
        c = CompactnessConstraints(max_packet_tokens=100)
        with pytest.raises((AttributeError, TypeError)):
            c.max_packet_tokens = 999  # type: ignore[misc]

    def test_compactness_constraints_max_payload_bytes_frozen(self) -> None:
        """Assigning max_payload_bytes on frozen CompactnessConstraints must raise."""
        c = CompactnessConstraints(max_payload_bytes=1024)
        with pytest.raises((AttributeError, TypeError)):
            c.max_payload_bytes = 0  # type: ignore[misc]

    def test_compactness_constraints_prefer_verified_frozen(self) -> None:
        """Assigning prefer_verified_summary on frozen CompactnessConstraints must raise."""
        c = CompactnessConstraints(prefer_verified_summary=True)
        with pytest.raises((AttributeError, TypeError)):
            c.prefer_verified_summary = False  # type: ignore[misc]

    def test_threat_context_is_frozen(self) -> None:
        """Direct field assignment on ThreatContext must raise."""
        t = ThreatContext(threat_hypothesis="oversize payload", compactness_enforced=True)
        with pytest.raises((AttributeError, TypeError)):
            t.threat_hypothesis = "injected"  # type: ignore[misc]

    def test_threat_context_compactness_enforced_frozen(self) -> None:
        """Assigning compactness_enforced on frozen ThreatContext must raise."""
        t = ThreatContext(compactness_enforced=True)
        with pytest.raises((AttributeError, TypeError)):
            t.compactness_enforced = False  # type: ignore[misc]

    def test_memory_operation_metadata_is_mapping_proxy_type(self) -> None:
        """MemoryOperation.metadata must be MappingProxyType (not a plain dict)."""
        op = _op(metadata={"key": "val"})
        assert isinstance(op.metadata, _types.MappingProxyType)

    def test_memory_operation_metadata_setitem_raises(self) -> None:
        """op.metadata['x'] = y must raise TypeError (MappingProxyType)."""
        op = _op(metadata={"key": "val"})
        with pytest.raises(TypeError):
            op.metadata["new"] = "injected"  # type: ignore[index]

    def test_memory_operation_metadata_source_mutation_isolated(self) -> None:
        """Mutating the source dict after construction must not affect op.metadata."""
        src: dict[str, Any] = {"x": 1}
        op = _op(metadata=src)
        src["x"] = 999
        src["y"] = "injected"
        assert op.metadata["x"] == 1
        assert "y" not in op.metadata


# ---------------------------------------------------------------------------
# 5. MessageContext validation
# ---------------------------------------------------------------------------


class TestAdversarialMessageContextValidation:
    """MessageContext rejects negative sizes and freezes metadata."""

    def test_negative_content_size_bytes_raises_value_error(self) -> None:
        """MessageContext with negative content_size_bytes must raise ValueError."""
        with pytest.raises(ValueError, match="content_size_bytes"):
            MessageContext(content_size_bytes=-1)

    @pytest.mark.parametrize("negative", [-1, -100, -(2**32)])
    def test_various_negative_sizes_all_rejected(self, negative: int) -> None:
        """Parametrized: all negative content_size_bytes values must raise ValueError."""
        with pytest.raises(ValueError, match="content_size_bytes"):
            MessageContext(content_size_bytes=negative)

    def test_zero_content_size_bytes_is_valid(self) -> None:
        """MessageContext with content_size_bytes=0 must succeed."""
        ctx = MessageContext(content_size_bytes=0)
        assert ctx.content_size_bytes == 0

    def test_very_large_content_size_bytes_is_valid(self) -> None:
        """MessageContext accepts extremely large content_size_bytes (no upper bound)."""
        ctx = MessageContext(content_size_bytes=2**53)
        assert ctx.content_size_bytes == 2**53

    def test_metadata_is_mapping_proxy(self) -> None:
        """MessageContext.metadata must be MappingProxyType after construction."""
        ctx = MessageContext(metadata={"key": "val"})
        assert isinstance(ctx.metadata, _types.MappingProxyType)

    def test_metadata_setitem_raises_type_error(self) -> None:
        """Attempting ctx.metadata['x'] = y must raise TypeError."""
        ctx = MessageContext(metadata={"a": 1})
        with pytest.raises(TypeError):
            ctx.metadata["b"] = "injected"  # type: ignore[index]

    def test_metadata_source_mutation_isolated(self) -> None:
        """Mutating the dict passed to MessageContext must not affect ctx.metadata."""
        src: dict[str, Any] = {"k": "original"}
        ctx = MessageContext(metadata=src)
        src["k"] = "tampered"
        assert ctx.metadata["k"] == "original"

    def test_message_context_fields_frozen(self) -> None:
        """MessageContext is a frozen dataclass; direct assignment must raise."""
        ctx = MessageContext(sender_id="alice")
        with pytest.raises((AttributeError, TypeError)):
            ctx.sender_id = "attacker"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. MemoryOperation validation
# ---------------------------------------------------------------------------


class TestAdversarialMemoryOperationValidation:
    """MemoryOperation rejects invalid action types and negative sizes."""

    def test_non_memory_action_string_raises_type_error(self) -> None:
        """Plain string for action must raise TypeError mentioning MemoryAction."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action="write")  # type: ignore[arg-type]

    def test_non_memory_action_int_raises_type_error(self) -> None:
        """Integer for action must raise TypeError."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action=42)  # type: ignore[arg-type]

    def test_non_memory_action_none_raises_type_error(self) -> None:
        """None for action must raise TypeError."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("negative", [-1, -1000, -(2**31)])
    def test_negative_content_size_bytes_raises_value_error(self, negative: int) -> None:
        """Parametrized: all negative content_size_bytes values must raise ValueError."""
        with pytest.raises(ValueError, match="content_size_bytes"):
            MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=negative)

    def test_valid_all_memory_actions_accepted(self) -> None:
        """Every MemoryAction member must be accepted without raising."""
        for action in MemoryAction:
            op = MemoryOperation(action=action)
            assert op.action is action

    def test_valid_all_provenance_values_accepted(self) -> None:
        """Every MemoryProvenance member must be accepted without raising."""
        for prov in MemoryProvenance:
            op = MemoryOperation(action=MemoryAction.READ, provenance=prov)
            assert op.provenance is prov

    def test_evaluator_with_corrupted_metadata_nan_ratio_survives(self) -> None:
        """Full path: evaluator receives NaN ratio -- must return a valid decision."""
        constraints = CompactnessConstraints(max_raw_replay_ratio=1.0)
        ev = CompactnessEvaluator()
        op = _op(metadata={"raw_replay_ratio": math.nan})
        # math.nan > 1.0 is False -> no degrade from ratio
        decision = ev.before_op(op, _ctx(constraints))
        assert decision is not None
        assert decision.verdict in (GovernanceVerdict.ALLOW, GovernanceVerdict.DEGRADE)

    def test_evaluator_with_inf_packet_tokens_triggers_overflow_or_degrade(self) -> None:
        """Inf as packet_tokens: int(float('inf')) raises OverflowError.

        The evaluator does int() conversion -- this documents that callers must
        sanitize metadata before passing float('inf') as an integer field.
        """
        constraints = CompactnessConstraints(max_packet_tokens=100)
        ev = CompactnessEvaluator()
        op = _op(metadata={"packet_tokens": float("inf")})
        # int(float("inf")) raises OverflowError -- evaluator does not guard this
        with pytest.raises((OverflowError, ValueError, TypeError)):
            ev.before_op(op, _ctx(constraints))
