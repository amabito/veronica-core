"""Tests for veronica_core.kernel.ha -- HA minimal ABI types.

Covers:
- PolicyEpochStamp: creation, validation, immutability
- BreakerReflection: creation, validation, immutability
- Reservation: creation, validation, expiry, activity, metadata freezing
- ReservationState: enum values
- HeartbeatSnapshot: creation, to_audit_dict, validation, breaker coercion, metadata
- CircuitBreaker.reflect(): snapshot accuracy, decoupling, thread safety
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.kernel.ha import (
    BreakerReflection,
    HeartbeatSnapshot,
    PolicyEpochStamp,
    Reservation,
    ReservationState,
)


# ---------------------------------------------------------------------------
# ReservationState
# ---------------------------------------------------------------------------


class TestReservationState:
    def test_all_values_accessible(self) -> None:
        assert ReservationState.PENDING.value == "PENDING"
        assert ReservationState.COMMITTED.value == "COMMITTED"
        assert ReservationState.ROLLED_BACK.value == "ROLLED_BACK"
        assert ReservationState.EXPIRED.value == "EXPIRED"

    def test_is_str_enum(self) -> None:
        assert isinstance(ReservationState.PENDING, str)


# ---------------------------------------------------------------------------
# PolicyEpochStamp
# ---------------------------------------------------------------------------


class TestPolicyEpochStamp:
    def test_creation_with_valid_fields(self) -> None:
        stamp = PolicyEpochStamp(epoch=1, policy_hash="abc123", issuer="test")
        assert stamp.epoch == 1
        assert stamp.policy_hash == "abc123"
        assert stamp.issuer == "test"
        assert stamp.timestamp > 0.0

    def test_creation_with_defaults(self) -> None:
        stamp = PolicyEpochStamp(epoch=0, policy_hash="")
        assert stamp.issuer == ""
        assert stamp.timestamp > 0.0

    def test_zero_epoch_is_valid(self) -> None:
        stamp = PolicyEpochStamp(epoch=0, policy_hash="x")
        assert stamp.epoch == 0

    def test_negative_epoch_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="epoch must be >= 0"):
            PolicyEpochStamp(epoch=-1, policy_hash="x")

    def test_frozen_immutability(self) -> None:
        stamp = PolicyEpochStamp(epoch=1, policy_hash="x")
        with pytest.raises((TypeError, AttributeError)):
            stamp.epoch = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BreakerReflection
# ---------------------------------------------------------------------------


class TestBreakerReflection:
    def _make(self, **overrides: object) -> BreakerReflection:
        defaults: dict = {
            "breaker_id": "cb-1",
            "state": "CLOSED",
            "failure_count": 0,
            "success_count": 0,
            "last_failure_ts": 0.0,
            "last_success_ts": 0.0,
            "recovery_timeout": 60.0,
            "failure_threshold": 5,
        }
        defaults.update(overrides)
        return BreakerReflection(**defaults)  # type: ignore[arg-type]

    def test_creation_with_valid_fields(self) -> None:
        r = self._make(failure_count=2, success_count=10)
        assert r.breaker_id == "cb-1"
        assert r.state == "CLOSED"
        assert r.failure_count == 2
        assert r.success_count == 10
        assert r.last_failure_ts == 0.0
        assert r.recovery_timeout == 60.0
        assert r.failure_threshold == 5
        assert r.timestamp > 0.0

    def test_negative_failure_count_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="failure_count must be >= 0"):
            self._make(failure_count=-1)

    def test_negative_success_count_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="success_count must be >= 0"):
            self._make(success_count=-1)

    def test_frozen_immutability(self) -> None:
        r = self._make()
        with pytest.raises((TypeError, AttributeError)):
            r.failure_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------


class TestReservation:
    def _make(self, **overrides: object) -> Reservation:
        defaults: dict = {
            "reservation_id": "res-1",
            "resource_type": "tokens",
            "amount": 100.0,
        }
        defaults.update(overrides)
        return Reservation(**defaults)  # type: ignore[arg-type]

    def test_creation_with_valid_fields(self) -> None:
        r = self._make()
        assert r.reservation_id == "res-1"
        assert r.resource_type == "tokens"
        assert r.amount == 100.0
        assert r.state == ReservationState.PENDING
        assert r.epoch_stamp is None
        assert r.expires_at == 0.0

    def test_empty_reservation_id_raises(self) -> None:
        with pytest.raises(ValueError, match="reservation_id must be non-empty"):
            self._make(reservation_id="")

    def test_empty_resource_type_raises(self) -> None:
        with pytest.raises(ValueError, match="resource_type must be non-empty"):
            self._make(resource_type="")

    def test_negative_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="amount must be >= 0"):
            self._make(amount=-0.01)

    def test_zero_amount_is_valid(self) -> None:
        r = self._make(amount=0.0)
        assert r.amount == 0.0

    def test_is_expired_future_expires_at_is_false(self) -> None:
        r = self._make(expires_at=time.time() + 3600)
        assert r.is_expired is False

    def test_is_expired_past_expires_at_is_true(self) -> None:
        r = self._make(expires_at=time.time() - 1.0)
        assert r.is_expired is True

    def test_is_expired_zero_never_expires(self) -> None:
        r = self._make(expires_at=0.0)
        assert r.is_expired is False

    def test_is_active_pending_not_expired(self) -> None:
        r = self._make(state=ReservationState.PENDING, expires_at=time.time() + 3600)
        assert r.is_active is True

    def test_is_active_committed_not_expired(self) -> None:
        r = self._make(state=ReservationState.COMMITTED, expires_at=time.time() + 3600)
        assert r.is_active is True

    def test_is_active_pending_no_expiry(self) -> None:
        r = self._make(state=ReservationState.PENDING, expires_at=0.0)
        assert r.is_active is True

    def test_is_active_rolled_back_is_false(self) -> None:
        r = self._make(state=ReservationState.ROLLED_BACK)
        assert r.is_active is False

    def test_is_active_expired_state_is_false(self) -> None:
        r = self._make(state=ReservationState.EXPIRED)
        assert r.is_active is False

    def test_is_active_committed_but_time_expired(self) -> None:
        r = self._make(
            state=ReservationState.COMMITTED,
            expires_at=time.time() - 1.0,
        )
        assert r.is_active is False

    def test_metadata_frozen_via_mapping_proxy(self) -> None:
        r = self._make(metadata={"key": "value"})
        assert isinstance(r.metadata, types.MappingProxyType)
        with pytest.raises(TypeError):
            r.metadata["new_key"] = "blocked"  # type: ignore[index]

    def test_metadata_original_dict_mutation_does_not_affect_reservation(self) -> None:
        original: dict = {"x": 1}
        r = self._make(metadata=original)
        original["x"] = 999
        assert r.metadata["x"] == 1

    def test_frozen_immutability(self) -> None:
        r = self._make()
        with pytest.raises((TypeError, AttributeError)):
            r.amount = 0.0  # type: ignore[misc]

    def test_with_epoch_stamp(self) -> None:
        stamp = PolicyEpochStamp(epoch=5, policy_hash="abc")
        r = self._make(epoch_stamp=stamp)
        assert r.epoch_stamp is stamp


# ---------------------------------------------------------------------------
# HeartbeatSnapshot
# ---------------------------------------------------------------------------


class TestHeartbeatSnapshot:
    def _breaker_reflection(self, breaker_id: str = "cb-1") -> BreakerReflection:
        return BreakerReflection(
            breaker_id=breaker_id,
            state="CLOSED",
            failure_count=0,
            success_count=3,
            last_failure_ts=0.0,
            last_success_ts=0.0,
            recovery_timeout=60.0,
            failure_threshold=5,
        )

    def test_creation_with_all_fields(self) -> None:
        br = self._breaker_reflection()
        snap = HeartbeatSnapshot(
            kernel_id="k-1",
            sequence=42,
            breakers=(br,),
            active_reservations=2,
            active_chains=1,
            total_decisions=100,
            uptime_seconds=3600.0,
            metadata={"env": "prod"},
        )
        assert snap.kernel_id == "k-1"
        assert snap.sequence == 42
        assert len(snap.breakers) == 1
        assert snap.active_reservations == 2
        assert snap.active_chains == 1
        assert snap.total_decisions == 100
        assert snap.uptime_seconds == 3600.0
        assert snap.metadata["env"] == "prod"
        assert snap.timestamp > 0.0

    def test_to_audit_dict_includes_all_key_fields(self) -> None:
        br = self._breaker_reflection("cb-audit")
        stamp = PolicyEpochStamp(epoch=3, policy_hash="h3", issuer="issuer-x")
        snap = HeartbeatSnapshot(
            kernel_id="k-audit",
            sequence=7,
            epoch_stamp=stamp,
            breakers=(br,),
            active_reservations=5,
            active_chains=2,
            total_decisions=50,
            uptime_seconds=120.0,
            metadata={"region": "us-east"},
        )
        d = snap.to_audit_dict()
        assert d["kernel_id"] == "k-audit"
        assert d["sequence"] == 7
        assert d["active_reservations"] == 5
        assert d["active_chains"] == 2
        assert d["total_decisions"] == 50
        assert d["uptime_seconds"] == 120.0
        assert d["breaker_count"] == 1
        assert d["breakers"][0]["breaker_id"] == "cb-audit"
        assert d["epoch_epoch"] == 3
        assert d["epoch_policy_hash"] == "h3"
        assert d["epoch_issuer"] == "issuer-x"
        assert d["region"] == "us-east"

    def test_to_audit_dict_no_epoch_stamp(self) -> None:
        snap = HeartbeatSnapshot(kernel_id="k-2", sequence=0)
        d = snap.to_audit_dict()
        assert "epoch_epoch" not in d
        assert d["breaker_count"] == 0

    def test_negative_sequence_raises(self) -> None:
        with pytest.raises(ValueError, match="sequence must be >= 0"):
            HeartbeatSnapshot(kernel_id="k", sequence=-1)

    def test_breakers_list_coerced_to_tuple(self) -> None:
        br = self._breaker_reflection()
        snap = HeartbeatSnapshot(
            kernel_id="k",
            sequence=0,
            breakers=[br],  # type: ignore[arg-type]
        )
        assert isinstance(snap.breakers, tuple)
        assert snap.breakers[0] is br

    def test_metadata_frozen_via_mapping_proxy(self) -> None:
        snap = HeartbeatSnapshot(kernel_id="k", sequence=0, metadata={"x": 1})
        assert isinstance(snap.metadata, types.MappingProxyType)
        with pytest.raises(TypeError):
            snap.metadata["blocked"] = True  # type: ignore[index]

    def test_metadata_original_dict_mutation_isolated(self) -> None:
        original: dict = {"y": 2}
        snap = HeartbeatSnapshot(kernel_id="k", sequence=0, metadata=original)
        original["y"] = 999
        assert snap.metadata["y"] == 2

    def test_frozen_immutability(self) -> None:
        snap = HeartbeatSnapshot(kernel_id="k", sequence=0)
        with pytest.raises((TypeError, AttributeError)):
            snap.sequence = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CircuitBreaker.reflect()
# ---------------------------------------------------------------------------


class TestCircuitBreakerReflect:
    def test_reflect_returns_correct_state_closed_breaker(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
        r = cb.reflect()
        assert r.state == "CLOSED"
        assert r.failure_count == 0
        assert r.success_count == 0
        assert r.recovery_timeout == 30.0
        assert r.failure_threshold == 3
        assert r.last_failure_ts == 0.0
        assert isinstance(r, BreakerReflection)

    def test_reflect_after_failures_shows_updated_failure_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        r = cb.reflect()
        assert r.failure_count == 2
        assert r.state == "CLOSED"

    def test_reflect_after_threshold_reached_shows_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        r = cb.reflect()
        assert r.state == "OPEN"
        assert r.failure_count == 2

    def test_reflect_after_success_shows_success_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_success()
        cb.record_success()
        r = cb.reflect()
        assert r.success_count == 2

    def test_reflect_snapshot_decoupled_from_breaker(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        r_before = cb.reflect()
        assert r_before.failure_count == 0
        # Mutate the live breaker
        cb.record_failure()
        cb.record_failure()
        # Old snapshot is unchanged
        assert r_before.failure_count == 0
        # New snapshot reflects current state
        r_after = cb.reflect()
        assert r_after.failure_count == 2

    def test_reflect_includes_owner_id_after_bind(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.bind_to_context("my-chain-id")
        r = cb.reflect()
        assert r.breaker_id == "my-chain-id"

    def test_reflect_breaker_id_empty_when_unbound(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        r = cb.reflect()
        assert r.breaker_id == ""

    def test_reflect_thread_safe_ten_concurrent_readers(self) -> None:
        cb = CircuitBreaker(failure_threshold=10)
        for _ in range(3):
            cb.record_failure()

        results: list[BreakerReflection] = []
        errors: list[Exception] = []

        def snapshot() -> None:
            try:
                results.append(cb.reflect())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=snapshot) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        for r in results:
            assert r.failure_count == 3
            assert r.state == "CLOSED"


# ---------------------------------------------------------------------------
# Reservation.commit()
# ---------------------------------------------------------------------------


class TestReservationCommit:
    """Reservation.commit() state transitions."""

    def _make(self, **overrides: object) -> Reservation:
        defaults: dict = {
            "reservation_id": "res-commit",
            "resource_type": "tokens",
            "amount": 50.0,
        }
        defaults.update(overrides)
        return Reservation(**defaults)  # type: ignore[arg-type]

    def test_commit_from_pending(self) -> None:
        """PENDING -> COMMITTED is valid."""
        r = self._make(state=ReservationState.PENDING)
        committed = r.commit()
        assert committed.state == ReservationState.COMMITTED

    def test_commit_preserves_fields(self) -> None:
        """All fields except state are preserved on commit."""
        stamp = PolicyEpochStamp(epoch=1, policy_hash="abc")
        r = self._make(
            reservation_id="res-fields",
            resource_type="budget",
            amount=99.0,
            epoch_stamp=stamp,
            expires_at=time.time() + 3600,
            metadata={"k": "v"},
        )
        committed = r.commit()
        assert committed.reservation_id == r.reservation_id
        assert committed.resource_type == r.resource_type
        assert committed.amount == r.amount
        assert committed.epoch_stamp is r.epoch_stamp
        assert committed.created_at == r.created_at
        assert committed.expires_at == r.expires_at
        assert dict(committed.metadata) == dict(r.metadata)
        assert committed.state == ReservationState.COMMITTED

    def test_commit_from_committed_raises(self) -> None:
        """COMMITTED -> COMMITTED is invalid."""
        r = self._make(state=ReservationState.COMMITTED)
        with pytest.raises(ValueError, match="only PENDING reservations can be committed"):
            r.commit()

    def test_commit_from_rolled_back_raises(self) -> None:
        """ROLLED_BACK -> COMMITTED is invalid."""
        r = self._make(state=ReservationState.ROLLED_BACK)
        with pytest.raises(ValueError, match="only PENDING reservations can be committed"):
            r.commit()

    def test_commit_expired_raises(self) -> None:
        """Cannot commit an expired reservation."""
        r = self._make(state=ReservationState.PENDING, expires_at=time.time() - 1.0)
        with pytest.raises(ValueError, match="Cannot commit expired reservation"):
            r.commit()

    def test_commit_returns_new_instance(self) -> None:
        """commit() returns a NEW Reservation, not a mutation."""
        r = self._make(state=ReservationState.PENDING)
        committed = r.commit()
        assert committed is not r
        # Original must remain PENDING.
        assert r.state == ReservationState.PENDING


# ---------------------------------------------------------------------------
# Reservation.rollback()
# ---------------------------------------------------------------------------


class TestReservationRollback:
    """Reservation.rollback() state transitions."""

    def _make(self, **overrides: object) -> Reservation:
        defaults: dict = {
            "reservation_id": "res-rollback",
            "resource_type": "tokens",
            "amount": 75.0,
        }
        defaults.update(overrides)
        return Reservation(**defaults)  # type: ignore[arg-type]

    def test_rollback_from_pending(self) -> None:
        """PENDING -> ROLLED_BACK is valid."""
        r = self._make(state=ReservationState.PENDING)
        rolled = r.rollback()
        assert rolled.state == ReservationState.ROLLED_BACK

    def test_rollback_from_committed(self) -> None:
        """COMMITTED -> ROLLED_BACK is valid."""
        r = self._make(state=ReservationState.COMMITTED)
        rolled = r.rollback()
        assert rolled.state == ReservationState.ROLLED_BACK

    def test_rollback_from_rolled_back_raises(self) -> None:
        """ROLLED_BACK -> ROLLED_BACK is invalid (terminal)."""
        r = self._make(state=ReservationState.ROLLED_BACK)
        with pytest.raises(ValueError, match="already in a terminal state"):
            r.rollback()

    def test_rollback_preserves_fields(self) -> None:
        """All fields except state are preserved on rollback."""
        stamp = PolicyEpochStamp(epoch=2, policy_hash="def")
        r = self._make(
            reservation_id="res-rb-fields",
            resource_type="capacity",
            amount=10.0,
            epoch_stamp=stamp,
            expires_at=time.time() + 7200,
            metadata={"tag": "test"},
        )
        rolled = r.rollback()
        assert rolled.reservation_id == r.reservation_id
        assert rolled.resource_type == r.resource_type
        assert rolled.amount == r.amount
        assert rolled.epoch_stamp is r.epoch_stamp
        assert rolled.created_at == r.created_at
        assert rolled.expires_at == r.expires_at
        assert dict(rolled.metadata) == dict(r.metadata)
        assert rolled.state == ReservationState.ROLLED_BACK


# ---------------------------------------------------------------------------
# HeartbeatSnapshot.capture()
# ---------------------------------------------------------------------------


class TestHeartbeatSnapshotCapture:
    """HeartbeatSnapshot.capture() factory method."""

    def _breaker(self, bid: str = "cb-1") -> BreakerReflection:
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

    def test_capture_minimal(self) -> None:
        """Capture with required args only."""
        snap = HeartbeatSnapshot.capture(kernel_id="k-min", sequence=0)
        assert snap.kernel_id == "k-min"
        assert snap.sequence == 0
        assert snap.epoch_stamp is None
        assert snap.breakers == ()
        assert snap.active_reservations == 0
        assert snap.active_chains == 0
        assert snap.total_decisions == 0
        assert snap.uptime_seconds == 0.0
        assert snap.metadata == {}

    def test_capture_with_breakers(self) -> None:
        """Capture includes breaker reflections."""
        br = self._breaker("cb-cap")
        snap = HeartbeatSnapshot.capture(
            kernel_id="k-br",
            sequence=3,
            breakers=(br,),
        )
        assert len(snap.breakers) == 1
        assert snap.breakers[0].breaker_id == "cb-cap"

    def test_capture_timestamp_is_current(self) -> None:
        """Timestamp is set at capture time."""
        before = time.time()
        snap = HeartbeatSnapshot.capture(kernel_id="k-ts", sequence=1)
        after = time.time()
        assert before <= snap.timestamp <= after
