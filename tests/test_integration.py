"""Tests for VeronicaIntegration (main API)."""

import concurrent.futures
import pytest
import time
import tempfile
import shutil
from pathlib import Path

from veronica_core import VeronicaIntegration, VeronicaState
from veronica_core.backends import JSONBackend, MemoryBackend
from veronica_core.guards import VeronicaGuard


class StrictGuard(VeronicaGuard):
    """Guard that activates cooldown on first fail."""

    def should_cooldown(self, entity: str, context: dict) -> bool:
        return context.get("force_cooldown", False)

    def validate_state(self, state_data: dict) -> bool:
        # Reject states with > 10 fail count
        fail_counts = state_data.get("fail_counts", {})
        return all(count <= 10 for count in fail_counts.values())


class TestVeronicaIntegration:
    """Test integration API."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory."""
        tmpdir = Path(tempfile.mkdtemp())
        yield tmpdir
        shutil.rmtree(tmpdir)

    def test_initialization(self):
        """Test basic initialization."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            cooldown_fails=3,
            cooldown_seconds=60,
            backend=backend,
        )

        assert veronica.state.cooldown_fails == 3
        assert veronica.state.cooldown_seconds == 60
        assert veronica.state.current_state == VeronicaState.SCREENING

    def test_fail_tracking(self):
        """Test fail counter increments."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(cooldown_fails=3, backend=backend)

        entity = "test_task"

        # First fail
        cooldown = veronica.record_fail(entity)
        assert not cooldown
        assert veronica.get_fail_count(entity) == 1

        # Second fail
        cooldown = veronica.record_fail(entity)
        assert not cooldown
        assert veronica.get_fail_count(entity) == 2

        # Third fail activates cooldown
        cooldown = veronica.record_fail(entity)
        assert cooldown
        assert veronica.is_in_cooldown(entity)

    def test_pass_resets_counter(self):
        """Test record_pass resets fail counter."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(backend=backend)

        entity = "test_task"

        veronica.record_fail(entity)
        veronica.record_fail(entity)
        assert veronica.get_fail_count(entity) == 2

        veronica.record_pass(entity)
        assert veronica.get_fail_count(entity) == 0

    def test_guard_early_cooldown(self):
        """Test guard can trigger early cooldown."""
        backend = MemoryBackend()
        guard = StrictGuard()
        veronica = VeronicaIntegration(
            cooldown_fails=10,  # High threshold
            backend=backend,
            guard=guard,
        )

        entity = "test_task"

        # First fail with force_cooldown context
        context = {"force_cooldown": True}
        cooldown = veronica.record_fail(entity, context=context)

        # Guard should activate cooldown immediately
        assert cooldown
        assert veronica.is_in_cooldown(entity)

    def test_guard_state_validation(self):
        """Test guard validates state before save."""
        backend = MemoryBackend()
        guard = StrictGuard()
        veronica = VeronicaIntegration(backend=backend, guard=guard)

        entity = "test_task"

        # Add 11 fails (exceeds guard limit)
        for _ in range(11):
            veronica.record_fail(entity)

        # Manual save should fail validation
        success = veronica.save()
        assert not success  # Guard rejects state with fail_count > 10

    def test_auto_save(self):
        """Test auto-save triggers after interval."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            auto_save_interval=5,  # Save every 5 operations
            backend=backend,
        )

        # Record 4 fails (no auto-save yet)
        for i in range(4):
            veronica.record_fail(f"task_{i}")

        # No data saved yet
        assert backend._data is None

        # 5th fail triggers auto-save
        veronica.record_fail("task_5")

        # Data should be saved
        assert backend._data is not None

    def test_persistence_roundtrip(self, temp_dir):
        """Test state persists and reloads correctly."""
        path = temp_dir / "state.json"
        backend = JSONBackend(path)

        # Session 1: Create state
        v1 = VeronicaIntegration(cooldown_fails=5, backend=backend)
        v1.record_fail("task_1")
        v1.record_fail("task_2")
        v1.save()

        # Session 2: Load state
        backend2 = JSONBackend(path)
        v2 = VeronicaIntegration(backend=backend2)

        assert v2.get_fail_count("task_1") == 1
        assert v2.get_fail_count("task_2") == 1
        assert v2.state.cooldown_fails == 5

    def test_cooldown_remaining(self):
        """Test get_cooldown_remaining returns correct value."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            cooldown_fails=1,
            cooldown_seconds=10,
            backend=backend,
        )

        entity = "test_task"

        # No cooldown initially
        assert veronica.get_cooldown_remaining(entity) is None

        # Activate cooldown
        veronica.record_fail(entity)

        remaining = veronica.get_cooldown_remaining(entity)
        assert remaining is not None
        assert 0 < remaining <= 10

    def test_cleanup_expired(self):
        """Test cleanup_expired removes expired cooldowns."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            cooldown_fails=1,
            cooldown_seconds=0.1,
            backend=backend,
        )

        # Activate cooldown for 2 entities
        veronica.record_fail("task_1")
        veronica.record_fail("task_2")

        assert len(veronica.state.cooldowns) == 2

        # Wait for expiry
        time.sleep(0.2)

        # Cleanup
        veronica.cleanup_expired()

        assert len(veronica.state.cooldowns) == 0

    def test_get_stats(self):
        """Test get_stats returns comprehensive data."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(backend=backend)

        veronica.record_fail("task_1")
        veronica.record_fail("task_2")

        stats = veronica.get_stats()

        assert stats["current_state"] == "SCREENING"
        assert stats["fail_counts"] == {"task_1": 1, "task_2": 1}
        assert stats["active_cooldowns"] == {}


# ---------------------------------------------------------------------------
# TOCTOU fix regression tests — record_fail() lock correctness (v0.10.4)
# ---------------------------------------------------------------------------


class TestRecordFailLockCorrectness:
    """Verify that record_fail() holds _op_lock for the full state mutation.

    These tests guard against the TOCTOU race where two concurrent callers
    could both observe fail_count < threshold, both call set_cooldown, and
    activate cooldown more than once (or skip activation entirely).
    """

    def test_record_fail_op_lock_is_held_during_mutation(self) -> None:
        """_op_lock must be acquired by record_fail before state mutation.

        We verify this by checking that the lock is a reentrant-safe RLock
        or that calling record_fail from a thread that holds the lock does
        not deadlock (Python's threading.Lock is not re-entrant, so the
        test below uses a timeout-based approach).
        """
        import threading

        backend = MemoryBackend()
        veronica = VeronicaIntegration(cooldown_fails=5, backend=backend)

        lock_was_free_after_call = threading.Event()

        def call_record_fail() -> None:
            veronica.record_fail("pair_x")
            # After record_fail returns, the lock must be released
            acquired = veronica._op_lock.acquire(blocking=True, timeout=0.5)
            if acquired:
                veronica._op_lock.release()
                lock_was_free_after_call.set()

        t = threading.Thread(target=call_record_fail)
        t.start()
        t.join(timeout=2.0)

        assert lock_was_free_after_call.is_set(), (
            "_op_lock was not released after record_fail() returned — potential deadlock"
        )

    def test_concurrent_record_fail_does_not_double_activate_cooldown(self) -> None:
        """Concurrent record_fail calls must activate cooldown exactly once.

        With cooldown_fails=2, exactly two concurrent record_fail calls should
        activate cooldown at most once.  Without the lock, both threads could
        read fail_count==1 simultaneously and race to activate.
        """
        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            cooldown_fails=2,
            cooldown_seconds=60,
            backend=backend,
        )

        activations: list[bool] = []

        def call_fail() -> None:
            result = veronica.record_fail("pair_concurrent")
            activations.append(result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call_fail) for _ in range(2)]
            concurrent.futures.wait(futures)

        # Exactly one of the two calls should have returned True (cooldown activated)
        assert sum(activations) == 1, (
            f"Expected exactly 1 cooldown activation, got {sum(activations)}: {activations}"
        )
        assert veronica.is_in_cooldown("pair_concurrent")

    def test_record_fail_with_guard_under_concurrent_access(self) -> None:
        """Guard-triggered cooldown must be safe under concurrent record_fail."""

        class ImmediateGuard(VeronicaGuard):
            def should_cooldown(self, entity: str, context: dict) -> bool:  # noqa: D102
                return True

            def validate_state(self, state_data: dict) -> bool:  # noqa: D102
                return True

        backend = MemoryBackend()
        veronica = VeronicaIntegration(
            cooldown_fails=100,  # high threshold — guard triggers before normal path
            cooldown_seconds=60,
            backend=backend,
            guard=ImmediateGuard(),
        )

        results: list[bool] = []

        def call_fail() -> None:
            result = veronica.record_fail("pair_guard", context={"x": 1})
            results.append(result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(call_fail) for _ in range(4)]
            concurrent.futures.wait(futures)

        # All calls must complete without exception
        assert len(results) == 4
        # Entity must be in cooldown after guard activation
        assert veronica.is_in_cooldown("pair_guard")
