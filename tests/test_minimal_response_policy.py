"""Tests for MinimalResponsePolicy."""

from __future__ import annotations

import pytest

from veronica_core.policies.minimal_response import MinimalResponsePolicy
from veronica_core.shield.types import Decision


class TestMinimalResponseDisabled:
    """When disabled, all methods return inputs unchanged."""

    def test_inject_returns_original(self):
        policy = MinimalResponsePolicy(enabled=False)
        msg = "You are a helpful assistant."
        assert policy.inject(msg) is msg

    def test_wrap_request_returns_original(self):
        policy = MinimalResponsePolicy(enabled=False)
        req = {"system": "Hello", "model": "gpt-4"}
        result = policy.wrap_request(req)
        assert result is req
        assert "_original_system" not in result


class TestMinimalResponseEnabled:
    """When enabled, constraints are injected."""

    def test_inject_appends_constraints(self):
        policy = MinimalResponsePolicy(enabled=True)
        msg = "You are a helpful assistant."
        result = policy.inject(msg)
        assert result.startswith(msg)
        assert "RESPONSE CONSTRAINTS" in result
        assert "VERONICA MinimalResponsePolicy" in result

    def test_inject_contains_bullet_limit(self):
        policy = MinimalResponsePolicy(enabled=True, max_bullets=3)
        result = policy.inject("test")
        assert "at most 3 bullet points" in result

    def test_inject_no_questions_by_default(self):
        policy = MinimalResponsePolicy(enabled=True)
        result = policy.inject("test")
        assert "No follow-up questions." in result

    def test_inject_allows_questions_when_configured(self):
        policy = MinimalResponsePolicy(enabled=True, allow_questions=True, max_questions=2)
        result = policy.inject("test")
        assert "At most 2 question" in result

    def test_inject_default_max_bullets_is_5(self):
        policy = MinimalResponsePolicy(enabled=True)
        result = policy.inject("test")
        assert "at most 5 bullet points" in result


class TestWrapRequest:
    """wrap_request preserves original and modifies system."""

    def test_preserves_original_system(self):
        policy = MinimalResponsePolicy(enabled=True)
        req = {"system": "Original prompt", "model": "gpt-4"}
        result = policy.wrap_request(req)
        assert result["_original_system"] == "Original prompt"
        assert "RESPONSE CONSTRAINTS" in result["system"]
        assert result["model"] == "gpt-4"

    def test_does_not_mutate_input(self):
        policy = MinimalResponsePolicy(enabled=True)
        req = {"system": "Original"}
        result = policy.wrap_request(req)
        assert req.get("_original_system") is None  # Input not mutated

    def test_missing_system_key_returns_unchanged(self):
        policy = MinimalResponsePolicy(enabled=True)
        req = {"model": "gpt-4", "prompt": "Hello"}
        result = policy.wrap_request(req)
        assert result is req


class TestCreateEvent:
    """create_event returns correct SafetyEvent."""

    def test_event_type_is_policy_applied(self):
        policy = MinimalResponsePolicy(enabled=True)
        event = policy.create_event(request_id="req-1")
        assert event.event_type == "POLICY_APPLIED"
        assert event.decision is Decision.ALLOW
        assert event.reason == "minimal_response_policy applied"
        assert event.hook == "MinimalResponsePolicy"
        assert event.request_id == "req-1"

    def test_event_without_request_id(self):
        policy = MinimalResponsePolicy()
        event = policy.create_event()
        assert event.request_id is None


class TestEdgeCases:
    """Edge cases for robustness."""

    def test_empty_system_message(self):
        policy = MinimalResponsePolicy(enabled=True)
        result = policy.inject("")
        assert "RESPONSE CONSTRAINTS" in result

    def test_very_long_system_message(self):
        policy = MinimalResponsePolicy(enabled=True)
        long_msg = "x" * 100_000
        result = policy.inject(long_msg)
        assert result.startswith(long_msg)
        assert "RESPONSE CONSTRAINTS" in result
