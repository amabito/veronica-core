"""Tests for VeronicaStateMachine."""

import pytest
import time
from veronica_core.state import VeronicaState, VeronicaStateMachine, StateTransition


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

        # Third fail triggers cooldown
        activated = sm.record_fail(entity)
        assert activated
        assert sm.fail_counts[entity] == 3
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
        sm = VeronicaStateMachine(cooldown_fails=1, cooldown_seconds=0.1)
        entity = "test_task"

        sm.record_fail(entity)
        assert sm.is_in_cooldown(entity)

        # Wait for cooldown to expire
        time.sleep(0.2)
        assert not sm.is_in_cooldown(entity)

    def test_cleanup_expired(self):
        """Test cleanup_expired removes expired cooldowns."""
        sm = VeronicaStateMachine(cooldown_fails=1, cooldown_seconds=0.1)

        sm.record_fail("task_1")
        sm.record_fail("task_2")

        assert len(sm.cooldowns) == 2

        # Wait for expiry
        time.sleep(0.2)

        expired = sm.cleanup_expired()
        assert len(expired) == 2
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
        assert stats["fail_counts"] == {"task_1": 1, "task_2": 2}
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
