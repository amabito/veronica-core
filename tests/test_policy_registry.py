"""Tests for veronica_core.policy.registry."""

from __future__ import annotations

import warnings

import pytest

from veronica_core.policy.registry import PolicyRegistry, _BUILTIN_FACTORIES
from veronica_core.policy.schema import PolicyValidationError


class TestPolicyRegistry:
    def test_default_singleton_is_same_instance(self) -> None:
        r1 = PolicyRegistry.default()
        r2 = PolicyRegistry.default()
        assert r1 is r2

    def test_new_instance_has_all_builtins(self) -> None:
        registry = PolicyRegistry()
        for name in _BUILTIN_FACTORIES:
            factory = registry.get_rule_type(name)
            assert callable(factory)

    def test_builtin_types_at_import_time(self) -> None:
        registry = PolicyRegistry()
        expected = {
            "token_budget",
            "cost_ceiling",
            "rate_limit",
            "circuit_breaker",
            "step_limit",
            "time_limit",
        }
        assert expected.issubset(set(registry.known_types()))

    def test_register_custom_rule_type(self) -> None:
        registry = PolicyRegistry()
        sentinel = object()

        def my_factory(params):  # noqa: ANN001
            return sentinel

        registry.register_rule_type("my_custom", my_factory)
        assert registry.get_rule_type("my_custom") is my_factory

    def test_retrieve_registered_custom_type(self) -> None:
        registry = PolicyRegistry()
        calls = []

        def factory(params):  # noqa: ANN001
            calls.append(params)
            return object()

        registry.register_rule_type("tracked_type", factory)
        retrieved = registry.get_rule_type("tracked_type")
        retrieved({"key": "value"})
        assert calls == [{"key": "value"}]

    def test_unknown_rule_type_raises_policy_validation_error(self) -> None:
        registry = PolicyRegistry()
        with pytest.raises(PolicyValidationError) as exc_info:
            registry.get_rule_type("does_not_exist")
        assert "does_not_exist" in exc_info.value.errors[0]
        assert exc_info.value.field_name == "type"

    def test_double_registration_emits_warning_and_overwrites(self) -> None:
        registry = PolicyRegistry()
        factory_a = lambda p: "a"  # noqa: E731
        factory_b = lambda p: "b"  # noqa: E731
        registry.register_rule_type("dup_type", factory_a)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry.register_rule_type("dup_type", factory_b)
        # A UserWarning should have been emitted.
        assert any(issubclass(warning.category, UserWarning) for warning in w)
        # New factory wins.
        assert registry.get_rule_type("dup_type")({}) == "b"

    def test_register_empty_name_raises(self) -> None:
        registry = PolicyRegistry()
        with pytest.raises(PolicyValidationError):
            registry.register_rule_type("", lambda p: None)

    def test_known_types_returns_sorted_list(self) -> None:
        registry = PolicyRegistry()
        types = registry.known_types()
        assert types == sorted(types)

    def test_builtin_token_budget_factory_creates_hook(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("token_budget")
        hook = factory({"max_output_tokens": 500})
        from veronica_core.shield.token_budget import TokenBudgetHook

        assert isinstance(hook, TokenBudgetHook)

    def test_builtin_circuit_breaker_factory_creates_instance(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("circuit_breaker")
        cb = factory({"failure_threshold": 3})
        from veronica_core.circuit_breaker import CircuitBreaker

        assert isinstance(cb, CircuitBreaker)

    def test_builtin_rate_limit_factory_creates_hook(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("rate_limit")
        hook = factory({"max_calls": 10, "window_seconds": 30.0})
        from veronica_core.shield.budget_window import BudgetWindowHook

        assert isinstance(hook, BudgetWindowHook)

    def test_builtin_cost_ceiling_factory_creates_enforcer(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("cost_ceiling")
        enforcer = factory({"limit_usd": 5.0})
        from veronica_core.budget import BudgetEnforcer

        assert isinstance(enforcer, BudgetEnforcer)

    def test_builtin_step_limit_factory_creates_guard(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("step_limit")
        guard = factory({"max_steps": 10})
        from veronica_core.agent_guard import AgentStepGuard

        assert isinstance(guard, AgentStepGuard)

    def test_builtin_time_limit_factory_creates_policy(self) -> None:
        registry = PolicyRegistry()
        factory = registry.get_rule_type("time_limit")
        policy = factory({})
        from veronica_core.shield.time_policy import TimeAwarePolicy

        assert isinstance(policy, TimeAwarePolicy)
