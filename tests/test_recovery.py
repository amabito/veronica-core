"""Tests for VERONICA self-healing containment layer (recovery subpackage).

Coverage:
- IntegrityMonitor: clean path, tamper detection, quarantine sticky
- CheckpointManager: capture/restore, signature verification, ring buffer
- HeartbeatProtocol: create/verify, stale rejection, replay prevention
- SentinelMonitor: timeout detection
- RecoveryOrchestrator: full flow
- Adversarial: corrupted signature, forged heartbeat, concurrent access
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.recovery.checkpoint import (
    CheckpointManager,
    ContainmentCheckpoint,
    RestoreResult,
)
from veronica_core.recovery.integrity import IntegrityMonitor, IntegrityVerdict
from veronica_core.recovery.orchestrator import RecoveryAction, RecoveryOrchestrator
from veronica_core.recovery.sentinel import (
    HeartbeatProtocol,
    HeartbeatVerdict,
    SentinelMonitor,
    SignedHeartbeat,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def simple_bundle() -> PolicyBundle:
    meta = PolicyMetadata(policy_id="test-policy", epoch=0)
    rule = PolicyRule(rule_id="r1", rule_type="budget", parameters={"limit": 10.0})
    return PolicyBundle(metadata=meta, rules=(rule,))


@pytest.fixture()
def signing_key() -> bytes:
    return b"test-signing-key-at-least-32-bytes!"


@pytest.fixture()
def checkpoint_mgr(signing_key: bytes) -> CheckpointManager:
    return CheckpointManager(signing_key)


@pytest.fixture()
def heartbeat_proto(signing_key: bytes) -> HeartbeatProtocol:
    return HeartbeatProtocol(signing_key, timeout_ms=200)


class FakeCtx:
    """Minimal fake ExecutionContext for checkpoint capture tests."""

    def __init__(
        self,
        policy_hash: str = "abc123",
        policy_epoch: int = 1,
        budget_remaining: float = 10.0,
        risk_score: float = 0.5,
        circuit_states: dict[str, str] | None = None,
    ) -> None:
        self.policy_hash = policy_hash
        self.policy_epoch = policy_epoch
        self.budget_remaining = budget_remaining
        self.risk_score = risk_score
        self.circuit_states = circuit_states or {}


# ---------------------------------------------------------------------------
# IntegrityMonitor
# ---------------------------------------------------------------------------


class TestIntegrityMonitor:
    def test_clean_path_no_tamper(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=3)
        # First two calls should be CLEAN without triggering verification
        assert monitor.on_call() == IntegrityVerdict.CLEAN
        assert monitor.on_call() == IntegrityVerdict.CLEAN
        # Third call triggers _verify -- still CLEAN
        assert monitor.on_call() == IntegrityVerdict.CLEAN
        assert monitor.call_count == 3

    def test_check_interval_respected(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=10)
        verify_calls = []
        original_verify = monitor._verify_locked

        def recording_verify() -> IntegrityVerdict:
            verify_calls.append(1)
            return original_verify()

        monitor._verify_locked = recording_verify  # type: ignore[method-assign]

        for _ in range(25):
            monitor.on_call()

        # Should have triggered at calls 10, 20 -> 2 verifications
        assert len(verify_calls) == 2

    def test_tamper_detection(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        # First call triggers verify -> CLEAN
        assert monitor.on_call() == IntegrityVerdict.CLEAN

        # Corrupt the stored original hash
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]

        # Next verification should detect tamper
        result = monitor.on_call()
        assert result == IntegrityVerdict.TAMPERED
        assert monitor.is_quarantined

    def test_quarantine_sticky_after_tamper(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        # Trigger tamper
        monitor.on_call()
        assert monitor.is_quarantined

        # All subsequent calls return QUARANTINED without re-verifying
        for _ in range(10):
            assert monitor.on_call() == IntegrityVerdict.QUARANTINED

    def test_force_verify_clean(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1000)
        # force_verify bypasses the interval
        assert monitor.force_verify() == IntegrityVerdict.CLEAN

    def test_force_verify_tampered(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1000)
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        assert monitor.force_verify() == IntegrityVerdict.TAMPERED
        assert monitor.is_quarantined

    def test_force_verify_quarantined_returns_quarantined(
        self, simple_bundle: PolicyBundle
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        monitor.on_call()  # sets quarantined
        assert monitor.force_verify() == IntegrityVerdict.QUARANTINED

    def test_invalid_check_interval(self, simple_bundle: PolicyBundle) -> None:
        with pytest.raises(ValueError, match="check_interval must be >= 1"):
            IntegrityMonitor(simple_bundle, check_interval=0)

    def test_call_count_increments(self, simple_bundle: PolicyBundle) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=100)
        for i in range(1, 6):
            monitor.on_call()
            assert monitor.call_count == i

    def test_thread_safe_concurrent_calls(self, simple_bundle: PolicyBundle) -> None:
        """Multiple threads calling on_call() must not cause races."""
        monitor = IntegrityMonitor(simple_bundle, check_interval=10)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    v = monitor.on_call()
                    assert v in (IntegrityVerdict.CLEAN, IntegrityVerdict.QUARANTINED)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------


class TestCheckpointManager:
    def test_capture_extracts_fields(self, checkpoint_mgr: CheckpointManager) -> None:
        ctx = FakeCtx(
            policy_hash="hash1", policy_epoch=2, budget_remaining=5.5, risk_score=0.1
        )
        cp = checkpoint_mgr.capture(ctx)
        assert cp.policy_hash == "hash1"
        assert cp.policy_epoch == 2
        assert cp.budget_remaining == 5.5
        assert cp.risk_score == 0.1
        assert isinstance(cp.timestamp, float)
        assert cp.signature != ""

    def test_capture_default_fields_on_empty_ctx(
        self, checkpoint_mgr: CheckpointManager
    ) -> None:
        cp = checkpoint_mgr.capture(object())
        assert cp.policy_hash == ""
        assert cp.policy_epoch == 0
        assert cp.budget_remaining == 0.0
        assert cp.circuit_states == {}

    def test_restore_success(self, checkpoint_mgr: CheckpointManager) -> None:
        cp = checkpoint_mgr.capture(FakeCtx())
        result = checkpoint_mgr.restore(cp)
        assert result == RestoreResult.SUCCESS

    def test_restore_invalid_signature(self, checkpoint_mgr: CheckpointManager) -> None:
        cp = checkpoint_mgr.capture(FakeCtx())
        # Corrupt the signature
        forged = ContainmentCheckpoint(
            policy_hash=cp.policy_hash,
            policy_epoch=cp.policy_epoch,
            budget_remaining=cp.budget_remaining,
            circuit_states=cp.circuit_states,
            risk_score=cp.risk_score,
            timestamp=cp.timestamp,
            signature="deadbeef" * 8,
        )
        result = checkpoint_mgr.restore(forged)
        assert result == RestoreResult.SIGNATURE_INVALID

    def test_latest_valid_returns_most_recent(
        self, checkpoint_mgr: CheckpointManager
    ) -> None:
        for i in range(5):
            checkpoint_mgr.capture(FakeCtx(policy_epoch=i))
        latest = checkpoint_mgr.latest_valid()
        assert latest is not None
        assert latest.policy_epoch == 4

    def test_latest_valid_returns_none_when_empty(self, signing_key: bytes) -> None:
        mgr = CheckpointManager(signing_key)
        assert mgr.latest_valid() is None

    def test_ring_buffer_overflow_drops_oldest(self, signing_key: bytes) -> None:
        mgr = CheckpointManager(signing_key, max_checkpoints=3)
        for i in range(5):
            mgr.capture(FakeCtx(policy_epoch=i))
        # Only 3 most recent remain; the deque drops oldest automatically
        latest = mgr.latest_valid()
        assert latest is not None
        assert latest.policy_epoch == 4

    def test_latest_valid_skips_corrupted(self, signing_key: bytes) -> None:
        mgr = CheckpointManager(signing_key, max_checkpoints=3)
        mgr.capture(FakeCtx(policy_epoch=1))

        # Manually add a corrupted checkpoint to the deque
        corrupted = ContainmentCheckpoint(
            policy_hash="x",
            policy_epoch=99,
            budget_remaining=0.0,
            circuit_states={},
            risk_score=0.0,
            timestamp=time.time(),
            signature="badsig",
        )
        with mgr._lock:
            mgr._checkpoints.append(corrupted)

        # Should skip corrupted and return cp_good
        result = mgr.latest_valid()
        assert result is not None
        assert result.policy_epoch == 1

    def test_invalid_signing_key(self) -> None:
        with pytest.raises(ValueError, match="signing_key must be non-empty bytes"):
            CheckpointManager(b"")

    def test_invalid_max_checkpoints(self, signing_key: bytes) -> None:
        with pytest.raises(ValueError, match="max_checkpoints must be >= 1"):
            CheckpointManager(signing_key, max_checkpoints=0)

    def test_frozen_checkpoint_immutable(
        self, checkpoint_mgr: CheckpointManager
    ) -> None:
        cp = checkpoint_mgr.capture(FakeCtx())
        with pytest.raises((TypeError, AttributeError)):
            cp.policy_hash = "tampered"  # type: ignore[misc]

    def test_thread_safe_concurrent_captures(
        self, checkpoint_mgr: CheckpointManager
    ) -> None:
        errors: list[Exception] = []

        def worker(epoch: int) -> None:
            try:
                checkpoint_mgr.capture(FakeCtx(policy_epoch=epoch))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# HeartbeatProtocol
# ---------------------------------------------------------------------------


class TestHeartbeatProtocol:
    def test_create_and_verify_valid(self, heartbeat_proto: HeartbeatProtocol) -> None:
        hb = heartbeat_proto.create_heartbeat({"agent": "mark-1", "status": "ok"})
        verdict = heartbeat_proto.verify_heartbeat(hb)
        assert verdict == HeartbeatVerdict.VALID

    def test_replay_prevention(self, heartbeat_proto: HeartbeatProtocol) -> None:
        hb = heartbeat_proto.create_heartbeat({})
        assert heartbeat_proto.verify_heartbeat(hb) == HeartbeatVerdict.VALID
        # Second use of the same nonce must be rejected
        assert heartbeat_proto.verify_heartbeat(hb) == HeartbeatVerdict.STALE

    def test_stale_heartbeat_rejected(self, signing_key: bytes) -> None:
        proto = HeartbeatProtocol(signing_key, timeout_ms=100)
        hb = proto.create_heartbeat({})
        # Wait longer than 2x timeout (200ms)
        time.sleep(0.25)
        verdict = proto.verify_heartbeat(hb)
        assert verdict == HeartbeatVerdict.STALE

    def test_forged_signature_rejected(
        self, heartbeat_proto: HeartbeatProtocol
    ) -> None:
        hb = heartbeat_proto.create_heartbeat({"x": 1})
        forged = SignedHeartbeat(
            timestamp=hb.timestamp,
            nonce=hb.nonce,
            state_hash=hb.state_hash,
            signature="deadbeef" * 8,
        )
        # Replay check runs first: nonce not yet seen, so proceeds to sig check
        verdict = heartbeat_proto.verify_heartbeat(forged)
        assert verdict == HeartbeatVerdict.INVALID_SIGNATURE

    def test_future_timestamp_rejected(
        self, heartbeat_proto: HeartbeatProtocol
    ) -> None:
        hb = heartbeat_proto.create_heartbeat({})
        future = SignedHeartbeat(
            timestamp=time.time() + 9999,
            nonce=hb.nonce,
            state_hash=hb.state_hash,
            signature=hb.signature,
        )
        verdict = heartbeat_proto.verify_heartbeat(future)
        assert verdict == HeartbeatVerdict.STALE

    def test_invalid_key(self) -> None:
        with pytest.raises(ValueError, match="signing_key must be non-empty bytes"):
            HeartbeatProtocol(b"")

    def test_invalid_timeout(self, signing_key: bytes) -> None:
        with pytest.raises(ValueError, match="timeout_ms must be >= 1"):
            HeartbeatProtocol(signing_key, timeout_ms=0)

    def test_different_keys_incompatible(self, signing_key: bytes) -> None:
        proto_a = HeartbeatProtocol(signing_key, timeout_ms=5000)
        proto_b = HeartbeatProtocol(
            b"different-key-32bytes-padding-xx", timeout_ms=5000
        )
        hb = proto_a.create_heartbeat({"msg": "hello"})
        verdict = proto_b.verify_heartbeat(hb)
        assert verdict == HeartbeatVerdict.INVALID_SIGNATURE

    def test_nonce_deque_overflow_does_not_crash(self, signing_key: bytes) -> None:
        proto = HeartbeatProtocol(signing_key, timeout_ms=60000)
        # Fill up nonce deque (maxlen=1000) and beyond
        for _ in range(1010):
            hb = proto.create_heartbeat({})
            proto.verify_heartbeat(hb)
        # Should still work after overflow
        hb = proto.create_heartbeat({})
        assert proto.verify_heartbeat(hb) == HeartbeatVerdict.VALID


# ---------------------------------------------------------------------------
# SentinelMonitor
# ---------------------------------------------------------------------------


class TestSentinelMonitor:
    def test_send_and_receive_valid(self, heartbeat_proto: HeartbeatProtocol) -> None:
        sentinel = SentinelMonitor(heartbeat_proto)
        hb = sentinel.send({"status": "ok"})
        assert sentinel.last_heartbeat is hb
        verdict = sentinel.receive(hb)
        assert verdict == HeartbeatVerdict.VALID

    def test_no_timeout_immediately_after_init(
        self, heartbeat_proto: HeartbeatProtocol
    ) -> None:
        sentinel = SentinelMonitor(heartbeat_proto)
        assert not sentinel.check_timeout()

    def test_timeout_after_elapsed(self, signing_key: bytes) -> None:
        proto = HeartbeatProtocol(signing_key, timeout_ms=50)
        sentinel = SentinelMonitor(proto)
        time.sleep(0.1)  # Wait longer than 50ms timeout
        assert sentinel.check_timeout()

    def test_receive_valid_resets_timeout(self, signing_key: bytes) -> None:
        proto = HeartbeatProtocol(signing_key, timeout_ms=150)
        sentinel = SentinelMonitor(proto)
        time.sleep(0.1)

        # Receive a valid heartbeat from peer -- creates via same proto
        peer_proto = HeartbeatProtocol(signing_key, timeout_ms=150)
        peer_hb = peer_proto.create_heartbeat({})
        sentinel.receive(peer_hb)

        # Timeout should be reset
        assert not sentinel.check_timeout()

    def test_send_without_state_summary(
        self, heartbeat_proto: HeartbeatProtocol
    ) -> None:
        sentinel = SentinelMonitor(heartbeat_proto)
        hb = sentinel.send()  # No state_summary argument
        assert hb is not None
        assert sentinel.last_heartbeat is hb


# ---------------------------------------------------------------------------
# RecoveryOrchestrator
# ---------------------------------------------------------------------------


class TestRecoveryOrchestrator:
    def _make_orchestrator(
        self,
        simple_bundle: PolicyBundle,
        signing_key: bytes,
        check_interval: int = 100,
        checkpoint_interval: int = 10,
        sentinel: SentinelMonitor | None = None,
    ) -> RecoveryOrchestrator:
        monitor = IntegrityMonitor(simple_bundle, check_interval=check_interval)
        mgr = CheckpointManager(signing_key)
        return RecoveryOrchestrator(
            integrity=monitor,
            checkpoint_mgr=mgr,
            sentinel=sentinel,
            checkpoint_interval=checkpoint_interval,
        )

    def test_clean_path_returns_continue(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        orch = self._make_orchestrator(simple_bundle, signing_key)
        for _ in range(20):
            assert orch.on_call(FakeCtx()) == RecoveryAction.CONTINUE
        assert orch.is_healthy

    def test_tamper_with_valid_checkpoint_returns_restored(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=1)

        # First call: clean, captures checkpoint
        ctx = FakeCtx()
        assert orch.on_call(ctx) == RecoveryAction.CONTINUE

        # Corrupt the monitor's stored hash
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]

        # Next call: tamper detected, restore from checkpoint
        result = orch.on_call(ctx)
        assert result == RecoveryAction.RESTORED

    def test_tamper_with_no_checkpoint_quarantines(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=100)

        # No checkpoint captured yet
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        result = orch.on_call()
        assert result == RecoveryAction.QUARANTINE_ALL
        assert not orch.is_healthy

    def test_quarantined_blocks_all_subsequent_calls(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=100)

        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        orch.on_call()  # triggers quarantine

        for _ in range(10):
            assert orch.on_call(FakeCtx()) == RecoveryAction.QUARANTINE_ALL

    def test_sentinel_timeout_quarantines(
        self,
        simple_bundle: PolicyBundle,
        signing_key: bytes,
    ) -> None:
        proto = HeartbeatProtocol(signing_key, timeout_ms=50)
        sentinel = SentinelMonitor(proto)
        monitor = IntegrityMonitor(simple_bundle, check_interval=100)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, sentinel=sentinel)

        # Wait for sentinel to time out
        time.sleep(0.1)
        result = orch.on_call()
        assert result == RecoveryAction.QUARANTINE_ALL

    def test_is_healthy_false_after_quarantine(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr)
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        orch.on_call()
        assert not orch.is_healthy

    def test_call_count_increments(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        orch = self._make_orchestrator(simple_bundle, signing_key)
        for i in range(1, 6):
            orch.on_call()
            assert orch.call_count == i

    def test_invalid_checkpoint_interval(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle)
        mgr = CheckpointManager(signing_key)
        with pytest.raises(ValueError, match="checkpoint_interval must be >= 1"):
            RecoveryOrchestrator(monitor, mgr, checkpoint_interval=0)

    def test_periodic_checkpoint_capture(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        monitor = IntegrityMonitor(simple_bundle, check_interval=1000)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=3)

        ctx = FakeCtx(budget_remaining=99.0)
        for _ in range(6):
            orch.on_call(ctx)

        # Checkpoint should have been captured at calls 3 and 6
        latest = mgr.latest_valid()
        assert latest is not None
        assert latest.budget_remaining == 99.0

    def test_sentinel_none_does_not_error(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        orch = self._make_orchestrator(simple_bundle, signing_key, sentinel=None)
        assert orch.on_call() == RecoveryAction.CONTINUE


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialRecovery:
    """Adversarial tests -- attacker mindset."""

    def test_corrupted_checkpoint_signature_rejected(self, signing_key: bytes) -> None:
        """Attacker forges a checkpoint by changing policy_epoch."""
        mgr = CheckpointManager(signing_key)
        cp = mgr.capture(FakeCtx(policy_epoch=1))

        # Build a checkpoint with modified epoch but original signature
        forged = ContainmentCheckpoint(
            policy_hash=cp.policy_hash,
            policy_epoch=999,  # Modified
            budget_remaining=cp.budget_remaining,
            circuit_states=cp.circuit_states,
            risk_score=cp.risk_score,
            timestamp=cp.timestamp,
            signature=cp.signature,  # Original sig -- now invalid
        )
        assert mgr.restore(forged) == RestoreResult.SIGNATURE_INVALID

    def test_forged_heartbeat_with_valid_looking_fields_rejected(
        self, signing_key: bytes
    ) -> None:
        """Attacker creates a valid-looking heartbeat with wrong key."""
        attacker_key = b"attacker-key-32-bytes-padding-xx"
        attacker_proto = HeartbeatProtocol(attacker_key, timeout_ms=5000)
        defender_proto = HeartbeatProtocol(signing_key, timeout_ms=5000)

        forged_hb = attacker_proto.create_heartbeat({"status": "ok"})
        verdict = defender_proto.verify_heartbeat(forged_hb)
        assert verdict == HeartbeatVerdict.INVALID_SIGNATURE

    def test_checkpoint_with_negative_epoch_rejected(self) -> None:
        """ContainmentCheckpoint must reject negative policy_epoch."""
        with pytest.raises(ValueError, match="policy_epoch"):
            ContainmentCheckpoint(
                policy_hash="",
                policy_epoch=-1,
                budget_remaining=0.0,
                circuit_states={},
                risk_score=0.0,
                timestamp=0.0,
                signature="",
            )

    def test_concurrent_tamper_and_verify(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        """Race: one thread tampers, many threads call on_call concurrently."""
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=100)

        results: list[RecoveryAction] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(9)

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(50):
                    results.append(orch.on_call())
            except Exception as e:
                errors.append(e)

        def tamper() -> None:
            barrier.wait()
            time.sleep(0.005)
            monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]

        threads = [threading.Thread(target=worker) for _ in range(8)]
        threads.append(threading.Thread(target=tamper))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All results must be valid RecoveryAction values
        valid_actions = set(RecoveryAction)
        for r in results:
            assert r in valid_actions

    def test_integrity_monitor_quarantine_cannot_be_cleared(
        self, simple_bundle: PolicyBundle
    ) -> None:
        """Once quarantined, there is no public method to clear quarantine."""
        monitor = IntegrityMonitor(simple_bundle, check_interval=1)
        monitor._original_hash = "deadbeef" * 8  # type: ignore[assignment]
        monitor.on_call()
        assert monitor.is_quarantined

        # Restore original hash (simulate "fix") -- quarantine must stick
        monitor._original_hash = simple_bundle.content_hash()  # type: ignore[assignment]
        assert monitor.force_verify() == IntegrityVerdict.QUARANTINED

    def test_checkpoint_circuit_states_coerced_to_str(self, signing_key: bytes) -> None:
        """circuit_states values must be coerced to str, not pass through raw."""
        mgr = CheckpointManager(signing_key)

        class CtxWithEnumStates:
            circuit_states = {"entity_a": "OPEN", "entity_b": "CLOSED"}
            policy_hash = ""
            policy_epoch = 0
            budget_remaining = 0.0
            risk_score = 0.0

        cp = mgr.capture(CtxWithEnumStates())
        assert isinstance(cp.circuit_states["entity_a"], str)
        assert cp.circuit_states["entity_a"] == "OPEN"

    def test_heartbeat_nonce_set_stays_consistent_under_concurrent_load(
        self, signing_key: bytes
    ) -> None:
        """Nonce set and deque must remain consistent under concurrent verify calls."""
        proto = HeartbeatProtocol(signing_key, timeout_ms=60000)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    hb = proto.create_heartbeat({})
                    v = proto.verify_heartbeat(hb)
                    assert v in (HeartbeatVerdict.VALID, HeartbeatVerdict.STALE)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_orchestrator_checkpoint_capture_exception_non_fatal(
        self, simple_bundle: PolicyBundle, signing_key: bytes
    ) -> None:
        """Checkpoint capture failure must not quarantine the orchestrator."""
        monitor = IntegrityMonitor(simple_bundle, check_interval=1000)
        mgr = CheckpointManager(signing_key)
        orch = RecoveryOrchestrator(monitor, mgr, checkpoint_interval=1)

        # Make capture raise to simulate failure
        original_capture = mgr.capture
        call_count = [0]

        def failing_capture(ctx: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated capture failure")
            return original_capture(ctx)

        mgr.capture = failing_capture  # type: ignore[method-assign]

        # First call triggers capture which fails -- should still return CONTINUE
        result = orch.on_call(FakeCtx())
        assert result == RecoveryAction.CONTINUE
        assert orch.is_healthy
