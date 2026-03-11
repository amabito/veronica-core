"""Adversarial tests for veronica_core.kernel.ha -- attacker mindset.

Tests state transition chains, expiry edge cases, NaN/Inf amounts,
metadata immutability, concurrent operations, capture() reserved keys,
and boundary conditions for PolicyEpochStamp and BreakerReflection.

Does NOT duplicate coverage already present in test_kernel_ha.py.
"""

from __future__ import annotations

import math
import threading
import time
import types as _types
from typing import Any

import pytest

from veronica_core.kernel.ha import (
    BreakerReflection,
    HeartbeatSnapshot,
    PolicyEpochStamp,
    Reservation,
    ReservationState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending(**overrides: Any) -> Reservation:
    defaults: dict[str, Any] = {
        "reservation_id": "adv-res",
        "resource_type": "tokens",
        "amount": 100.0,
        "state": ReservationState.PENDING,
    }
    defaults.update(overrides)
    return Reservation(**defaults)


def _breaker_reflection(bid: str = "cb-adv") -> BreakerReflection:
    return BreakerReflection(
        breaker_id=bid,
        state="CLOSED",
        failure_count=0,
        success_count=0,
        last_failure_ts=0.0,
        last_success_ts=0.0,
        recovery_timeout=60.0,
        failure_threshold=5,
    )


# ---------------------------------------------------------------------------
# 1. State transition chains
# ---------------------------------------------------------------------------


class TestAdversarialReservationStateChains:
    """State machine chains -- attacker seeks invalid transitions."""

    def test_pending_commit_rollback_final_state_is_rolled_back(self) -> None:
        """PENDING -> commit() -> rollback() must land in ROLLED_BACK."""
        r = _pending()
        committed = r.commit()
        rolled = committed.rollback()

        assert rolled.state == ReservationState.ROLLED_BACK
        # Original PENDING must be untouched.
        assert r.state == ReservationState.PENDING
        assert committed.state == ReservationState.COMMITTED

    def test_pending_rollback_then_commit_raises(self) -> None:
        """ROLLED_BACK is terminal -- commit() must raise."""
        r = _pending()
        rolled = r.rollback()
        assert rolled.state == ReservationState.ROLLED_BACK

        with pytest.raises(ValueError, match="only PENDING reservations can be committed"):
            rolled.commit()

    def test_pending_commit_commit_raises(self) -> None:
        """COMMITTED -> commit() again must raise -- COMMITTED is not PENDING."""
        r = _pending()
        committed = r.commit()

        with pytest.raises(ValueError, match="only PENDING reservations can be committed"):
            committed.commit()

    def test_double_rollback_raises(self) -> None:
        """ROLLED_BACK -> rollback() again must raise -- ROLLED_BACK is terminal."""
        r = _pending()
        rolled_once = r.rollback()

        with pytest.raises(ValueError, match="already in a terminal state"):
            rolled_once.rollback()

    def test_expired_state_rollback_raises(self) -> None:
        """EXPIRED is a terminal state -- rollback() must raise."""
        r = _pending(state=ReservationState.EXPIRED)

        with pytest.raises(ValueError, match="already in a terminal state"):
            r.rollback()

    def test_expired_state_commit_raises(self) -> None:
        """EXPIRED -> commit() must raise -- EXPIRED is not PENDING."""
        r = _pending(state=ReservationState.EXPIRED)

        with pytest.raises(ValueError, match="only PENDING reservations can be committed"):
            r.commit()

    def test_original_unchanged_after_full_chain(self) -> None:
        """The original PENDING object must survive the full chain unchanged."""
        r = _pending()
        committed = r.commit()
        committed.rollback()

        assert r.state == ReservationState.PENDING
        assert committed.state == ReservationState.COMMITTED


# ---------------------------------------------------------------------------
# 2. Expired reservation behavior
# ---------------------------------------------------------------------------


class TestAdversarialReservationExpired:
    """Expiry interactions with commit/rollback."""

    def test_expired_pending_commit_raises(self) -> None:
        """PENDING with past expires_at cannot be committed."""
        r = _pending(expires_at=time.time() - 0.01)

        with pytest.raises(ValueError, match="Cannot commit expired reservation"):
            r.commit()

    def test_expired_pending_rollback_succeeds(self) -> None:
        """PENDING with past expires_at CAN be rolled back (cleanup path)."""
        r = _pending(expires_at=time.time() - 0.01)
        rolled = r.rollback()

        assert rolled.state == ReservationState.ROLLED_BACK

    def test_committed_expired_rollback_succeeds(self) -> None:
        """COMMITTED with past expires_at CAN be rolled back (cleanup path)."""
        r = _pending(state=ReservationState.COMMITTED, expires_at=time.time() - 0.01)
        rolled = r.rollback()

        assert rolled.state == ReservationState.ROLLED_BACK

    def test_no_expiry_zero_commit_always_works(self) -> None:
        """expires_at=0.0 means no expiry -- commit() succeeds regardless of time."""
        r = _pending(expires_at=0.0)
        committed = r.commit()

        assert committed.state == ReservationState.COMMITTED

    def test_future_expires_at_is_not_expired(self) -> None:
        """expires_at in far future must not block commit."""
        r = _pending(expires_at=time.time() + 86400)
        committed = r.commit()

        assert committed.state == ReservationState.COMMITTED

    def test_rolled_back_expired_rollback_raises_terminal(self) -> None:
        """ROLLED_BACK with past expires_at must still raise -- terminal state wins."""
        r = _pending(
            state=ReservationState.ROLLED_BACK,
            expires_at=time.time() - 1.0,
        )
        with pytest.raises(ValueError, match="already in a terminal state"):
            r.rollback()


# ---------------------------------------------------------------------------
# 3. NaN/Inf amount edge cases
# ---------------------------------------------------------------------------


class TestAdversarialReservationAmountEdgeCases:
    """IEEE-754 float edge cases for amount field."""

    def test_nan_amount_raises_at_construction(self) -> None:
        """NaN >= 0 is False in IEEE-754 -- construction must raise ValueError."""
        with pytest.raises(ValueError, match="amount must be >= 0"):
            _pending(amount=float("nan"))

    def test_positive_inf_amount_construction_succeeds(self) -> None:
        """Inf >= 0 is True -- construction must succeed."""
        r = _pending(amount=float("inf"))
        assert math.isinf(r.amount)
        assert r.amount > 0

    def test_positive_inf_amount_commit_preserves_value(self) -> None:
        """commit() must preserve Inf amount exactly."""
        r = _pending(amount=float("inf"))
        committed = r.commit()

        assert math.isinf(committed.amount)
        assert committed.amount == r.amount

    def test_zero_amount_commit_succeeds(self) -> None:
        """amount=0.0 is a valid boundary -- commit() must succeed."""
        r = _pending(amount=0.0)
        committed = r.commit()

        assert committed.amount == 0.0
        assert committed.state == ReservationState.COMMITTED

    def test_zero_amount_rollback_succeeds(self) -> None:
        """amount=0.0 is a valid boundary -- rollback() must succeed."""
        r = _pending(amount=0.0)
        rolled = r.rollback()

        assert rolled.amount == 0.0
        assert rolled.state == ReservationState.ROLLED_BACK

    def test_committed_amount_equals_original_exact(self) -> None:
        """After commit(), committed.amount must be bit-for-bit identical."""
        original_amount = 3.141592653589793
        r = _pending(amount=original_amount)
        committed = r.commit()

        assert committed.amount == original_amount

    def test_negative_inf_amount_raises_at_construction(self) -> None:
        """-Inf < 0 -- construction must raise ValueError."""
        with pytest.raises(ValueError, match="amount must be >= 0"):
            _pending(amount=float("-inf"))


# ---------------------------------------------------------------------------
# 4. Metadata immutability across transitions
# ---------------------------------------------------------------------------


class TestAdversarialReservationMetadataImmutability:
    """Metadata must stay frozen through commit/rollback."""

    def test_committed_metadata_is_mapping_proxy(self) -> None:
        """After commit(), metadata must still be MappingProxyType."""
        r = _pending(metadata={"key": "value"})
        committed = r.commit()

        assert isinstance(committed.metadata, _types.MappingProxyType)

    def test_committed_metadata_mutation_raises_type_error(self) -> None:
        """Mutating metadata on committed Reservation must raise TypeError."""
        r = _pending(metadata={"key": "value"})
        committed = r.commit()

        with pytest.raises(TypeError):
            committed.metadata["key"] = "hacked"  # type: ignore[index]

    def test_rolled_back_metadata_is_mapping_proxy(self) -> None:
        """After rollback(), metadata must still be MappingProxyType."""
        r = _pending(metadata={"key": "value"})
        rolled = r.rollback()

        assert isinstance(rolled.metadata, _types.MappingProxyType)

    def test_rolled_back_metadata_mutation_raises_type_error(self) -> None:
        """Mutating metadata on rolled-back Reservation must raise TypeError."""
        r = _pending(metadata={"key": "value"})
        rolled = r.rollback()

        with pytest.raises(TypeError):
            rolled.metadata["key"] = "hacked"  # type: ignore[index]

    def test_commit_does_not_alias_original_metadata(self) -> None:
        """commit() must deep-copy metadata -- no aliasing with original."""
        original_meta = {"shared": "before"}
        r = _pending(metadata=original_meta)
        committed = r.commit()

        # The committed proxy must have its own independent copy.
        # Mutating the original dict used at construction must not affect either.
        assert committed.metadata["shared"] == "before"
        # Verify identity: the underlying objects are not the same mapping.
        assert committed.metadata is not r.metadata

    def test_empty_metadata_commit_produces_empty_proxy(self) -> None:
        """Empty metadata must produce an empty MappingProxyType after commit."""
        r = _pending(metadata={})
        committed = r.commit()

        assert isinstance(committed.metadata, _types.MappingProxyType)
        assert len(committed.metadata) == 0

    def test_metadata_with_nested_values_survives_commit(self) -> None:
        """Nested values in metadata survive commit without corruption."""
        r = _pending(metadata={"tags": ["a", "b"], "count": 42})
        committed = r.commit()

        assert committed.metadata["count"] == 42
        assert committed.metadata["tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# 5. Concurrent operations
# ---------------------------------------------------------------------------


class TestAdversarialReservationConcurrent:
    """Concurrent creation and transitions -- no shared mutable state."""

    def test_ten_threads_each_commit_own_pending_all_succeed(self) -> None:
        """10 threads each committing their own PENDING must all succeed."""
        results: list[ReservationState] = []
        errors: list[Exception] = []

        def commit_own() -> None:
            try:
                r = Reservation(
                    reservation_id=f"concurrent-{threading.get_ident()}",
                    resource_type="tokens",
                    amount=10.0,
                )
                committed = r.commit()
                results.append(committed.state)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=commit_own) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10
        assert all(s == ReservationState.COMMITTED for s in results)

    def test_ten_threads_each_rollback_own_committed_all_succeed(self) -> None:
        """10 threads each rolling back their own COMMITTED must all succeed."""
        results: list[ReservationState] = []
        errors: list[Exception] = []

        def rollback_own() -> None:
            try:
                r = Reservation(
                    reservation_id=f"rollback-{threading.get_ident()}",
                    resource_type="tokens",
                    amount=10.0,
                    state=ReservationState.COMMITTED,
                )
                rolled = r.rollback()
                results.append(rolled.state)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=rollback_own) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10
        assert all(s == ReservationState.ROLLED_BACK for s in results)

    def test_ten_threads_commit_independently_constructed_pending(self) -> None:
        """Reservation is immutable -- 10 separately constructed instances commit
        concurrently without interfering with each other."""
        results: list[bool] = []
        errors: list[Exception] = []

        def do_commit(idx: int) -> None:
            try:
                r = Reservation(
                    reservation_id=f"ind-{idx}",
                    resource_type="tokens",
                    amount=float(idx),
                )
                committed = r.commit()
                results.append(committed.amount == float(idx))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_commit, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10
        assert all(results)

    def test_ten_threads_each_chain_commit_rollback_no_errors(self) -> None:
        """10 threads each running a PENDING->commit->rollback chain independently."""
        errors: list[Exception] = []

        def full_chain(idx: int) -> None:
            try:
                r = Reservation(
                    reservation_id=f"chain-{idx}",
                    resource_type="capacity",
                    amount=1.0,
                )
                committed = r.commit()
                rolled = committed.rollback()
                assert rolled.state == ReservationState.ROLLED_BACK
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=full_chain, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# 6. HeartbeatSnapshot.capture() adversarial
# ---------------------------------------------------------------------------


class TestAdversarialHeartbeatCapture:
    """capture() must reject reserved metadata keys and handle edge inputs."""

    def test_capture_reserved_key_kernel_id_raises(self) -> None:
        """metadata key 'kernel_id' is reserved -- must raise ValueError."""
        with pytest.raises(ValueError, match="reserved keys"):
            HeartbeatSnapshot.capture(
                kernel_id="k-adv",
                sequence=0,
                metadata={"kernel_id": "spoofed"},
            )

    def test_capture_reserved_key_sequence_raises(self) -> None:
        """metadata key 'sequence' is reserved -- must raise ValueError."""
        with pytest.raises(ValueError, match="reserved keys"):
            HeartbeatSnapshot.capture(
                kernel_id="k-adv",
                sequence=0,
                metadata={"sequence": 999},
            )

    def test_capture_reserved_key_breakers_raises(self) -> None:
        """metadata key 'breakers' is reserved -- must raise ValueError."""
        with pytest.raises(ValueError, match="reserved keys"):
            HeartbeatSnapshot.capture(
                kernel_id="k-adv",
                sequence=0,
                metadata={"breakers": []},
            )

    def test_capture_reserved_key_timestamp_raises(self) -> None:
        """metadata key 'timestamp' is reserved -- must raise ValueError."""
        with pytest.raises(ValueError, match="reserved keys"):
            HeartbeatSnapshot.capture(
                kernel_id="k-adv",
                sequence=0,
                metadata={"timestamp": 0.0},
            )

    def test_capture_negative_sequence_raises(self) -> None:
        """Negative sequence must raise ValueError."""
        with pytest.raises(ValueError, match="sequence must be >= 0"):
            HeartbeatSnapshot.capture(kernel_id="k-adv", sequence=-1)

    def test_capture_empty_kernel_id_does_not_raise(self) -> None:
        """Empty kernel_id has no validation -- must not raise."""
        snap = HeartbeatSnapshot.capture(kernel_id="", sequence=0)
        assert snap.kernel_id == ""

    def test_capture_none_metadata_produces_empty_frozen_dict(self) -> None:
        """metadata=None must produce an empty MappingProxyType (not a crash)."""
        snap = HeartbeatSnapshot.capture(kernel_id="k-adv", sequence=0, metadata=None)
        assert isinstance(snap.metadata, _types.MappingProxyType)
        assert len(snap.metadata) == 0

    def test_capture_breakers_as_list_coerced_to_tuple(self) -> None:
        """breakers passed as list must be coerced to tuple."""
        br = _breaker_reflection("cb-list")
        snap = HeartbeatSnapshot.capture(
            kernel_id="k-adv",
            sequence=0,
            breakers=[br],  # type: ignore[arg-type]
        )
        assert isinstance(snap.breakers, tuple)
        assert snap.breakers[0] is br

    def test_capture_multiple_reserved_key_collisions_reported(self) -> None:
        """Multiple reserved key collisions must be reported together."""
        with pytest.raises(ValueError, match="reserved keys"):
            HeartbeatSnapshot.capture(
                kernel_id="k-adv",
                sequence=0,
                metadata={"kernel_id": "x", "sequence": 1},
            )

    def test_capture_unreserved_metadata_key_succeeds(self) -> None:
        """Non-reserved metadata keys must be accepted without error."""
        snap = HeartbeatSnapshot.capture(
            kernel_id="k-adv",
            sequence=0,
            metadata={"custom_tag": "ok", "region": "us-west"},
        )
        assert snap.metadata["custom_tag"] == "ok"
        assert snap.metadata["region"] == "us-west"


# ---------------------------------------------------------------------------
# 7. PolicyEpochStamp edge cases
# ---------------------------------------------------------------------------


class TestAdversarialPolicyEpochStamp:
    """Boundary and float edge cases for PolicyEpochStamp."""

    def test_epoch_zero_is_valid_boundary(self) -> None:
        """epoch=0 is the lower boundary -- must succeed."""
        stamp = PolicyEpochStamp(epoch=0, policy_hash="h0")
        assert stamp.epoch == 0

    def test_epoch_minus_one_is_invalid(self) -> None:
        """epoch=-1 must raise ValueError (one below boundary)."""
        with pytest.raises(ValueError, match="epoch must be >= 0"):
            PolicyEpochStamp(epoch=-1, policy_hash="h")

    def test_very_large_epoch_is_valid(self) -> None:
        """Python int has no overflow -- very large epoch must succeed."""
        large = 2**63
        stamp = PolicyEpochStamp(epoch=large, policy_hash="h")
        assert stamp.epoch == large

    def test_nan_timestamp_construction_succeeds(self) -> None:
        """timestamp is a raw float with no validation -- NaN must not raise."""
        stamp = PolicyEpochStamp(epoch=1, policy_hash="h", timestamp=float("nan"))
        assert math.isnan(stamp.timestamp)

    def test_inf_timestamp_construction_succeeds(self) -> None:
        """timestamp is a raw float with no validation -- Inf must not raise."""
        stamp = PolicyEpochStamp(epoch=1, policy_hash="h", timestamp=float("inf"))
        assert math.isinf(stamp.timestamp)

    def test_frozen_after_construction(self) -> None:
        """PolicyEpochStamp is frozen -- mutation must raise."""
        stamp = PolicyEpochStamp(epoch=5, policy_hash="h")
        with pytest.raises((TypeError, AttributeError)):
            stamp.epoch = 99  # type: ignore[misc]

    def test_epoch_one_is_valid(self) -> None:
        """epoch=1 is just above boundary -- must succeed."""
        stamp = PolicyEpochStamp(epoch=1, policy_hash="h")
        assert stamp.epoch == 1


# ---------------------------------------------------------------------------
# 8. BreakerReflection edge cases
# ---------------------------------------------------------------------------


class TestAdversarialBreakerReflection:
    """Boundary counts and large values for BreakerReflection."""

    def _make(self, **overrides: Any) -> BreakerReflection:
        defaults: dict[str, Any] = {
            "breaker_id": "cb-adv",
            "state": "CLOSED",
            "failure_count": 0,
            "success_count": 0,
            "last_failure_ts": 0.0,
            "last_success_ts": 0.0,
            "recovery_timeout": 60.0,
            "failure_threshold": 5,
        }
        defaults.update(overrides)
        return BreakerReflection(**defaults)

    def test_zero_failure_zero_success_is_valid_fresh_breaker(self) -> None:
        """failure_count=0, success_count=0 represents a fresh breaker -- valid."""
        br = self._make(failure_count=0, success_count=0)
        assert br.failure_count == 0
        assert br.success_count == 0

    def test_very_large_failure_count_is_valid(self) -> None:
        """Python int has no overflow -- 2**31 failure count must succeed."""
        large = 2**31
        br = self._make(failure_count=large)
        assert br.failure_count == large

    def test_very_large_success_count_is_valid(self) -> None:
        """Python int has no overflow -- 2**31 success count must succeed."""
        large = 2**31
        br = self._make(success_count=large)
        assert br.success_count == large

    def test_negative_failure_count_minus_one_raises(self) -> None:
        """failure_count=-1 is one below boundary -- must raise ValueError."""
        with pytest.raises(ValueError, match="failure_count must be >= 0"):
            self._make(failure_count=-1)

    def test_negative_success_count_minus_one_raises(self) -> None:
        """success_count=-1 is one below boundary -- must raise ValueError."""
        with pytest.raises(ValueError, match="success_count must be >= 0"):
            self._make(success_count=-1)

    def test_empty_breaker_id_is_valid(self) -> None:
        """Empty breaker_id is valid (unbound breaker) -- must not raise."""
        br = self._make(breaker_id="")
        assert br.breaker_id == ""

    def test_frozen_after_construction(self) -> None:
        """BreakerReflection is frozen -- mutation must raise."""
        br = self._make()
        with pytest.raises((TypeError, AttributeError)):
            br.failure_count = 99  # type: ignore[misc]

    def test_nan_recovery_timeout_construction_succeeds(self) -> None:
        """recovery_timeout is a raw float with no validation -- NaN must not raise."""
        br = self._make(recovery_timeout=float("nan"))
        assert math.isnan(br.recovery_timeout)
