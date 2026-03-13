"""Tests for VeronicaStateMachine."""

import logging
import time
from veronica_core.state import VeronicaState, VeronicaStateMachine


class TestVeronicaStateMachine:
    """Test state machine core logic."""

    def test_initial_state(self):
        """Test initial state is IDLE."""
        sm = VeronicaStateMachine()
        assert sm.current_state == VeronicaState.IDLE
        assert sm.fail_counts == {}
        assert sm.cooldowns == {}

    def test_state_transition(self):
        """Test state transitions are recorded."""
        sm = VeronicaStateMachine()
        sm.transition(VeronicaState.SCREENING, "Test transition")

        assert sm.current_state == VeronicaState.SCREENING
        assert len(sm.state_history) == 1
        assert sm.state_history[0].from_state == VeronicaState.IDLE
        assert sm.state_history[0].to_state == VeronicaState.SCREENING
        assert sm.state_history[0].reason == "Test transition"

    def test_noop_transition(self):
        """Test no-op transition (same state)."""
        sm = VeronicaStateMachine()
        sm.transition(VeronicaState.IDLE, "No change")

        # No transition recorded
        assert len(sm.state_history) == 0

    def test_fail_counter(self):
        """Test fail counter increments."""
        sm = VeronicaStateMachine(cooldown_fails=3)
        entity = "test_task"

        # First fail
        activated = sm.record_fail(entity)
        assert not activated
        assert sm.fail_counts[entity] == 1

        # Second fail
        activated = sm.record_fail(entity)
        assert not activated
        assert sm.fail_counts[entity] == 2

        # Third fail triggers cooldown and resets fail_counts to 0
        activated = sm.record_fail(entity)
        assert activated
        assert sm.fail_counts[entity] == 0  # M5 fix: reset on cooldown trigger
        assert entity in sm.cooldowns

    def test_cooldown_activation(self):
        """Test cooldown is activated after threshold."""
        sm = VeronicaStateMachine(cooldown_fails=2, cooldown_seconds=10)
        entity = "test_task"

        sm.record_fail(entity)
        activated = sm.record_fail(entity)

        assert activated
        assert sm.is_in_cooldown(entity)

        # Check cooldown expiry is set correctly
        assert sm.cooldowns[entity] > time.time()
        assert sm.cooldowns[entity] <= time.time() + 10

    def test_pass_resets_fail_counter(self):
        """Test record_pass resets fail counter."""
        sm = VeronicaStateMachine()
        entity = "test_task"

        sm.record_fail(entity)
        sm.record_fail(entity)
        assert sm.fail_counts[entity] == 2

        sm.record_pass(entity)
        assert entity not in sm.fail_counts

    def test_cooldown_expiry(self):
        """Test cooldown expires after duration."""
        from tests.conftest import wait_for

        sm = VeronicaStateMachine(cooldown_fails=1, cooldown_seconds=0.1)
        entity = "test_task"

        sm.record_fail(entity)
        assert sm.is_in_cooldown(entity)

        # Wait for cooldown to expire
        time.sleep(0.2)
        wait_for(
            lambda: not sm.is_in_cooldown(entity),
            msg="Expected cooldown to expire",
        )

    def test_cleanup_expired(self):
        """Test cleanup_expired removes expired cooldowns."""
        sm = VeronicaStateMachine(cooldown_fails=1, cooldown_seconds=0.1)

        sm.record_fail("task_1")
        sm.record_fail("task_2")

        assert len(sm.cooldowns) == 2

        # Poll cleanup_expired until both entries are returned. Using is_in_cooldown
        # would eagerly remove entries before our explicit call, leaving nothing to
        # return here. Polling cleanup_expired directly avoids that race.
        deadline = time.monotonic() + 2.0
        expired: list[str] = []
        while time.monotonic() < deadline:
            expired = sm.cleanup_expired()
            if len(expired) == 2:
                break
            time.sleep(0.01)

        assert len(expired) == 2, f"Expected 2 expired cooldowns, got {expired}"
        assert "task_1" in expired
        assert "task_2" in expired
        assert len(sm.cooldowns) == 0
        assert len(sm.fail_counts) == 0

    def test_serialization_roundtrip(self):
        """Test to_dict/from_dict roundtrip."""
        sm = VeronicaStateMachine(cooldown_fails=5, cooldown_seconds=300)
        sm.transition(VeronicaState.SCREENING, "Start screening")
        sm.record_fail("task_1")
        sm.record_fail("task_2")

        # Serialize
        data = sm.to_dict()

        # Deserialize
        sm2 = VeronicaStateMachine.from_dict(data)

        assert sm2.cooldown_fails == 5
        assert sm2.cooldown_seconds == 300
        assert sm2.current_state == VeronicaState.SCREENING
        assert sm2.fail_counts == {"task_1": 1, "task_2": 1}
        assert len(sm2.state_history) == 1
        assert sm2.state_history[0].to_state == VeronicaState.SCREENING

    def test_history_truncation(self):
        """Test state history is truncated to last 100."""
        sm = VeronicaStateMachine()

        # Create 150 transitions
        for i in range(150):
            sm.transition(VeronicaState.SCREENING, f"Transition {i}")
            sm.transition(VeronicaState.IDLE, f"Back to idle {i}")

        # Should only keep last 100
        assert len(sm.state_history) == 100

    def test_get_stats(self):
        """Test get_stats returns correct data."""
        sm = VeronicaStateMachine(cooldown_fails=2, cooldown_seconds=60)
        sm.transition(VeronicaState.SCREENING, "Start")
        sm.record_fail("task_1")
        sm.record_fail("task_2")
        sm.record_fail("task_2")  # Activate cooldown

        stats = sm.get_stats()

        assert stats["current_state"] == "SCREENING"
        assert "task_2" in stats["active_cooldowns"]
        # M5 fix: fail_counts resets to 0 when cooldown triggers
        assert stats["fail_counts"] == {"task_1": 1, "task_2": 0}
        assert stats["total_transitions"] == 1
        assert stats["last_transition"]["to"] == "SCREENING"


class TestFromDictMutableReference:
    """from_dict() must not share mutable references with the input data."""

    def test_fail_counts_is_independent_copy(self):
        """Mutating original data dict after from_dict() must not affect the state machine."""
        data = {
            "fail_counts": {"task_a": 2},
            "cooldowns": {},
            "current_state": "IDLE",
            "state_history": [],
        }
        sm = VeronicaStateMachine.from_dict(data)

        # Mutate the original data
        data["fail_counts"]["task_a"] = 99
        data["fail_counts"]["injected"] = 7

        # State machine must be unaffected
        assert sm.fail_counts == {"task_a": 2}
        assert "injected" not in sm.fail_counts

    def test_cooldowns_is_independent_copy(self):
        """Mutating original cooldowns dict after from_dict() must not affect the state machine."""
        future_ts = 9999999999.0
        data = {
            "fail_counts": {},
            "cooldowns": {"task_b": future_ts},
            "current_state": "IDLE",
            "state_history": [],
        }
        sm = VeronicaStateMachine.from_dict(data)

        # Mutate original cooldowns
        data["cooldowns"]["task_b"] = 0.0
        data["cooldowns"]["extra"] = 1.0

        # State machine must retain original value
        assert sm.cooldowns["task_b"] == future_ts
        assert "extra" not in sm.cooldowns

    def test_from_dict_roundtrip_isolation(self):
        """to_dict() → from_dict() → mutate original → instance unaffected."""
        sm_orig = VeronicaStateMachine(cooldown_fails=3)
        sm_orig.fail_counts["pair_x"] = 1

        snapshot = sm_orig.to_dict()
        sm_restored = VeronicaStateMachine.from_dict(snapshot)

        # Mutate snapshot after restore
        snapshot["fail_counts"]["pair_x"] = 42

        # Restored instance must be unaffected
        assert sm_restored.fail_counts["pair_x"] == 1


class TestFromDictCorruptedStateHistory:
    """L5: from_dict must skip invalid state_history entries with a warning."""

    def test_corrupted_from_state_is_skipped(self, caplog):
        """Invalid from_state string causes entry to be skipped with a warning."""
        data = {
            "cooldown_fails": 3,
            "cooldown_seconds": 600,
            "fail_counts": {},
            "cooldowns": {},
            "current_state": "IDLE",
            "state_history": [
                {
                    "from_state": "INVALID_STATE",
                    "to_state": "SCREENING",
                    "timestamp": 1000.0,
                    "reason": "bad entry",
                },
                {
                    "from_state": "IDLE",
                    "to_state": "SCREENING",
                    "timestamp": 2000.0,
                    "reason": "valid entry",
                },
            ],
        }

        with caplog.at_level(logging.WARNING, logger="veronica_core.state"):
            sm = VeronicaStateMachine.from_dict(data)

        # Only the valid entry should be in state_history
        assert len(sm.state_history) == 1
        assert sm.state_history[0].reason == "valid entry"
        # A warning should have been logged for the invalid entry
        assert any(
            "Skipping invalid state_history entry" in r.message for r in caplog.records
        )

    def test_corrupted_to_state_is_skipped(self, caplog):
        """Invalid to_state string causes entry to be skipped with a warning."""
        data = {
            "current_state": "IDLE",
            "state_history": [
                {
                    "from_state": "IDLE",
                    "to_state": "NO_SUCH_STATE",
                    "timestamp": 1000.0,
                    "reason": "bad to_state",
                },
            ],
        }

        with caplog.at_level(logging.WARNING, logger="veronica_core.state"):
            sm = VeronicaStateMachine.from_dict(data)

        assert len(sm.state_history) == 0
        assert any(
            "Skipping invalid state_history entry" in r.message for r in caplog.records
        )

    def test_missing_key_in_entry_is_skipped(self, caplog):
        """Entry missing required key is skipped with a warning."""
        data = {
            "current_state": "IDLE",
            "state_history": [
                {
                    # Missing 'from_state'
                    "to_state": "SCREENING",
                    "timestamp": 1000.0,
                    "reason": "missing from_state",
                },
                {
                    "from_state": "IDLE",
                    "to_state": "SCREENING",
                    "timestamp": 2000.0,
                    "reason": "valid",
                },
            ],
        }

        with caplog.at_level(logging.WARNING, logger="veronica_core.state"):
            sm = VeronicaStateMachine.from_dict(data)

        assert len(sm.state_history) == 1
        assert sm.state_history[0].reason == "valid"

    def test_all_valid_entries_are_preserved(self):
        """When all entries are valid, all are preserved."""
        sm_orig = VeronicaStateMachine()
        sm_orig.transition(VeronicaState.SCREENING, "start")

        data = sm_orig.to_dict()
        sm_restored = VeronicaStateMachine.from_dict(data)

        assert len(sm_restored.state_history) == 1
        assert sm_restored.state_history[0].to_state == VeronicaState.SCREENING


class TestAdversarialStateMachine:
    """Adversarial tests for VeronicaStateMachine -- attacker mindset (M5 fix)."""

    def test_fail_counts_reset_on_cooldown_trigger(self):
        """M5: fail_counts must reset to 0 when cooldown is triggered.

        Without the fix, subsequent record_fail() calls after cooldown expiry
        would re-trigger cooldown after just 1 fail (counts never cleared).
        """
        sm = VeronicaStateMachine(cooldown_fails=3, cooldown_seconds=600)
        pair = "BTC/JPY"

        # Reach threshold to trigger cooldown
        sm.record_fail(pair)
        sm.record_fail(pair)
        triggered = sm.record_fail(pair)

        assert triggered is True
        # M5 fix: fail_counts must be reset to 0 (not remain at 3)
        assert sm.fail_counts[pair] == 0, (
            "fail_counts must reset to 0 on cooldown trigger to prevent "
            "immediate re-trigger after cooldown expiry"
        )

    def test_post_cooldown_expiry_needs_full_threshold_again(self):
        """M5: After cooldown expires, the full threshold of fails is needed again.

        This is the correctness consequence of the fix: if fail_counts were not
        reset on cooldown trigger, a single fail after expiry would immediately
        re-trigger cooldown (threshold already met).
        """
        sm = VeronicaStateMachine(cooldown_fails=3, cooldown_seconds=0)
        pair = "ETH/JPY"

        # Trigger cooldown
        sm.record_fail(pair)
        sm.record_fail(pair)
        sm.record_fail(pair)
        assert pair in sm.cooldowns

        # Expire cooldown instantly (cooldown_seconds=0)
        assert not sm.is_in_cooldown(pair)  # expires + cleanup on first check

        # Now needs full threshold again (2 more fails, not yet at 3)
        sm.record_fail(pair)
        assert not sm.is_in_cooldown(pair)  # 1 fail after expiry: not yet at threshold
        sm.record_fail(pair)
        assert not sm.is_in_cooldown(pair)  # 2 fails: still not at threshold

    def test_concurrent_record_fail_no_race_condition(self):
        """M5: concurrent record_fail() from 10 threads must not corrupt fail_counts."""
        import threading

        sm = VeronicaStateMachine(cooldown_fails=100, cooldown_seconds=600)
        pair = "XRP/JPY"
        cooldowns_triggered = []
        lock = threading.Lock()

        def fail_worker():
            triggered = sm.record_fail(pair)
            if triggered:
                with lock:
                    cooldowns_triggered.append(True)

        threads = [threading.Thread(target=fail_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # fail_counts must equal number of record_fail calls that did not trigger cooldown
        # With threshold=100, no cooldown should have triggered for 10 calls
        assert len(cooldowns_triggered) == 0
        assert sm.fail_counts.get(pair, 0) == 10

    def test_record_fail_after_set_cooldown_does_not_override(self):
        """Calling record_fail() while a manually-set cooldown is active
        should not reset or override the cooldown expiry."""
        sm = VeronicaStateMachine(cooldown_fails=3, cooldown_seconds=600)
        pair = "BTC/JPY"
        future_expiry = time.time() + 9999.0

        # Manually set a long cooldown
        sm.set_cooldown(pair, future_expiry)
        assert sm.is_in_cooldown(pair)

        # record_fail below threshold: should not trigger a new cooldown
        sm.record_fail(pair)
        # Cooldown expiry should remain unchanged (the manually-set one)
        assert sm.cooldowns[pair] == future_expiry


class TestConcurrentStateMachine:
    """Additional concurrency tests using threading.Barrier for synchronization."""

    def test_concurrent_record_pass_and_fail_same_entity(self):
        """10 threads: simultaneous record_pass() + record_fail() on same entity.

        Invariant: fail_counts value must never go negative and the dict must
        be internally consistent (no KeyError, no corruption).
        """
        import threading

        sm = VeronicaStateMachine(cooldown_fails=100, cooldown_seconds=600)
        entity = "BTC/JPY"
        barrier = threading.Barrier(10)
        errors: list[Exception] = []
        lock = threading.Lock()

        def pass_worker():
            barrier.wait()
            try:
                sm.record_pass(entity)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        def fail_worker():
            barrier.wait()
            try:
                sm.record_fail(entity)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        # 5 pass + 5 fail workers race on the same entity
        threads = [threading.Thread(target=pass_worker) for _ in range(5)] + [
            threading.Thread(target=fail_worker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        # fail_counts value must be non-negative (never below 0)
        count = sm.fail_counts.get(entity, 0)
        assert count >= 0, f"fail_counts went negative: {count}"

    def test_concurrent_transition_valid_and_invalid(self):
        """10 threads: concurrent transition() calls (some valid, some invalid).

        Valid transitions must succeed without corrupting state.
        Invalid transitions must raise ValueError without crashing other threads.
        """
        import threading

        sm = VeronicaStateMachine()
        barrier = threading.Barrier(10)
        errors: list[Exception] = []
        valid_transitions: list[bool] = []
        lock = threading.Lock()

        def transition_worker(i: int):
            barrier.wait()
            try:
                if i % 2 == 0:
                    # IDLE -> SCREENING (valid from IDLE)
                    sm.transition(VeronicaState.SCREENING, f"valid-{i}")
                    with lock:
                        valid_transitions.append(True)
                else:
                    # IDLE -> COOLDOWN (invalid from IDLE, must raise ValueError)
                    sm.transition(VeronicaState.COOLDOWN, f"invalid-{i}")
            except ValueError:
                pass  # Expected for invalid transitions
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=transition_worker, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        # State machine must still be in a valid state
        assert sm.current_state in list(VeronicaState)

    def test_cleanup_expired_racing_with_is_in_cooldown(self):
        """cleanup_expired() racing with is_in_cooldown() must not corrupt cooldowns dict."""
        import threading
        from tests.conftest import wait_for

        sm = VeronicaStateMachine(cooldown_fails=1, cooldown_seconds=0.05)
        barrier = threading.Barrier(6)
        errors: list[Exception] = []
        lock = threading.Lock()

        # Seed some expiring cooldowns
        for i in range(3):
            sm.record_fail(f"pair_{i}")

        def cleanup_worker():
            barrier.wait()
            try:
                sm.cleanup_expired()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        def check_worker(pair: str):
            barrier.wait()
            try:
                # is_in_cooldown must not raise even when cleanup runs concurrently
                sm.is_in_cooldown(pair)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        # Wait for cooldowns to actually expire before starting the race
        time.sleep(0.1)
        wait_for(
            lambda: not sm.is_in_cooldown("pair_0")
            and not sm.is_in_cooldown("pair_1")
            and not sm.is_in_cooldown("pair_2"),
            msg="Expected all cooldowns to expire before racing",
        )

        # 3 cleanup + 3 is_in_cooldown threads race
        threads = [threading.Thread(target=cleanup_worker) for _ in range(3)] + [
            threading.Thread(target=check_worker, args=(f"pair_{i}",)) for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"
        # All cooldowns must have been cleaned up (they expired before the race)
        assert len(sm.cooldowns) == 0

    def test_set_cooldown_racing_with_is_in_cooldown(self):
        """set_cooldown() racing with is_in_cooldown() must not return stale results.

        Invariant: is_in_cooldown() must never raise; returned bool may be either
        True or False depending on thread scheduling (both are correct).
        """
        import threading

        sm = VeronicaStateMachine()
        entity = "ETH/JPY"
        barrier = threading.Barrier(10)
        errors: list[Exception] = []
        lock = threading.Lock()

        def setter_worker(expiry: float):
            barrier.wait()
            try:
                sm.set_cooldown(entity, expiry)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        def checker_worker():
            barrier.wait()
            try:
                sm.is_in_cooldown(entity)  # Result may vary; must not raise
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        future = time.time() + 9999.0
        threads = [
            threading.Thread(target=setter_worker, args=(future,)) for _ in range(5)
        ] + [threading.Thread(target=checker_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"
        # After all setters ran, cooldown must be set
        assert sm.is_in_cooldown(entity)
