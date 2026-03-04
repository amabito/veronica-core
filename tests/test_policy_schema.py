"""Tests for veronica_core.policy.schema."""

from __future__ import annotations

import pytest

from veronica_core.policy.schema import PolicySchema, PolicyValidationError, RuleSchema


class TestRuleSchema:
    def test_valid_construction(self) -> None:
        rule = RuleSchema(type="token_budget", params={"max_output_tokens": 5000}, on_exceed="halt")
        assert rule.type == "token_budget"
        assert rule.params == {"max_output_tokens": 5000}
        assert rule.on_exceed == "halt"

    def test_default_on_exceed_is_halt(self) -> None:
        rule = RuleSchema(type="rate_limit")
        assert rule.on_exceed == "halt"

    def test_default_params_is_empty_dict(self) -> None:
        rule = RuleSchema(type="circuit_breaker")
        assert rule.params == {}

    def test_none_params_normalised_to_empty_dict(self) -> None:
        rule = RuleSchema.from_dict({"type": "rate_limit", "params": None})
        assert rule.params == {}

    def test_all_valid_on_exceed_values(self) -> None:
        for value in ("halt", "degrade", "queue", "warn", "custom"):
            rule = RuleSchema(type="x", on_exceed=value)
            assert rule.on_exceed == value

    def test_invalid_on_exceed_raises(self) -> None:
        with pytest.raises(PolicyValidationError) as exc_info:
            RuleSchema(type="token_budget", on_exceed="explode")
        assert "on_exceed" in exc_info.value.errors[0].lower()

    def test_from_dict_ignores_unknown_fields(self) -> None:
        rule = RuleSchema.from_dict({
            "type": "token_budget",
            "params": {},
            "on_exceed": "degrade",
            "unknown_future_field": "ignored",
        })
        assert rule.type == "token_budget"
        assert rule.on_exceed == "degrade"


class TestPolicySchema:
    def test_valid_schema_from_dict(self) -> None:
        data = {
            "version": "1.0",
            "name": "My Policy",
            "rules": [
                {"type": "token_budget", "params": {"max_output_tokens": 1000}, "on_exceed": "halt"},
                {"type": "rate_limit", "params": {"max_calls": 50}, "on_exceed": "degrade"},
            ],
        }
        schema = PolicySchema.from_dict(data)
        assert schema.version == "1.0"
        assert schema.name == "My Policy"
        assert len(schema.rules) == 2
        assert schema.rules[0].type == "token_budget"

    def test_empty_rules_list_allowed(self) -> None:
        schema = PolicySchema(version="1.0", name="Empty", rules=[])
        assert schema.rules == []

    def test_none_rules_normalised_to_empty_list(self) -> None:
        data = {"version": "1.0", "name": "NoneRules"}
        schema = PolicySchema.from_dict(data)
        assert schema.rules == []

    def test_missing_version_raises(self) -> None:
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicySchema(version="", name="Test")
        assert "version" in exc_info.value.errors[0].lower()

    def test_missing_name_raises(self) -> None:
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicySchema(version="1.0", name="")
        assert "name" in exc_info.value.errors[0].lower()

    def test_contradictory_rules_halt_and_degrade_raises(self) -> None:
        # Same rule type with both halt and degrade is contradictory.
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicySchema(
                version="1.0",
                name="Bad",
                rules=[
                    RuleSchema(type="token_budget", on_exceed="halt"),
                    RuleSchema(type="token_budget", on_exceed="degrade"),
                ],
            )
        assert "contradictory" in exc_info.value.errors[0].lower()

    def test_different_types_halt_and_degrade_not_contradictory(self) -> None:
        # Contradiction only applies to same type.
        schema = PolicySchema(
            version="1.0",
            name="OK",
            rules=[
                RuleSchema(type="token_budget", on_exceed="halt"),
                RuleSchema(type="rate_limit", on_exceed="degrade"),
            ],
        )
        assert len(schema.rules) == 2

    def test_same_type_halt_and_warn_not_contradictory(self) -> None:
        # halt + warn is not a contradictory pair.
        schema = PolicySchema(
            version="1.0",
            name="OK",
            rules=[
                RuleSchema(type="token_budget", on_exceed="halt"),
                RuleSchema(type="token_budget", on_exceed="warn"),
            ],
        )
        assert len(schema.rules) == 2

    def test_from_dict_ignores_unknown_top_level_fields(self) -> None:
        data = {
            "version": "2.0",
            "name": "Forward Compat",
            "rules": [],
            "future_field": "should be ignored",
            "another_future": 42,
        }
        schema = PolicySchema.from_dict(data)
        assert schema.version == "2.0"
        assert schema.name == "Forward Compat"
