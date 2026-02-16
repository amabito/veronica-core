"""Tests for guard interface."""

import pytest
from veronica_core.guards import VeronicaGuard, PermissiveGuard


class TestPermissiveGuard:
    """Test default PermissiveGuard."""

    def test_never_cooldown(self):
        """Test PermissiveGuard never triggers early cooldown."""
        guard = PermissiveGuard()

        assert not guard.should_cooldown("any_entity", {"error_rate": 0.9})
        assert not guard.should_cooldown("any_entity", {"consecutive_fails": 100})

    def test_all_states_valid(self):
        """Test PermissiveGuard accepts all states."""
        guard = PermissiveGuard()

        assert guard.validate_state({"fail_counts": {"unknown_entity": 5}})
        assert guard.validate_state({"anything": "goes"})


class CustomTestGuard(VeronicaGuard):
    """Custom guard for testing."""

    def should_cooldown(self, entity: str, context: dict) -> bool:
        """Cooldown if error_rate > 50%."""
        return context.get("error_rate", 0) > 0.5

    def validate_state(self, state_data: dict) -> bool:
        """Only accept valid entities."""
        valid_entities = {"task_1", "task_2", "task_3"}
        fail_counts = state_data.get("fail_counts", {})
        return all(e in valid_entities for e in fail_counts.keys())


class TestCustomGuard:
    """Test custom guard implementation."""

    def test_cooldown_on_high_error_rate(self):
        """Test cooldown activates on high error rate."""
        guard = CustomTestGuard()

        assert guard.should_cooldown("task_1", {"error_rate": 0.6})
        assert guard.should_cooldown("task_1", {"error_rate": 1.0})

    def test_no_cooldown_on_low_error_rate(self):
        """Test no cooldown on low error rate."""
        guard = CustomTestGuard()

        assert not guard.should_cooldown("task_1", {"error_rate": 0.2})
        assert not guard.should_cooldown("task_1", {"error_rate": 0.5})

    def test_validate_state_accepts_valid_entities(self):
        """Test validate_state accepts valid entities."""
        guard = CustomTestGuard()

        state_data = {
            "fail_counts": {"task_1": 2, "task_2": 1}
        }
        assert guard.validate_state(state_data)

    def test_validate_state_rejects_invalid_entities(self):
        """Test validate_state rejects invalid entities."""
        guard = CustomTestGuard()

        state_data = {
            "fail_counts": {"task_1": 2, "unknown_task": 1}
        }
        assert not guard.validate_state(state_data)

    def test_hooks_are_optional(self):
        """Test hooks don't need to be implemented."""
        guard = CustomTestGuard()

        # Should not raise
        guard.on_cooldown_activated("task_1", {})
        guard.on_cooldown_expired("task_1")
