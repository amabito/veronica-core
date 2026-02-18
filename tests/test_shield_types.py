"""Tests for shield core types (PR-1B)."""

import dataclasses

import pytest

from veronica_core.shield import Decision, ToolCallContext


class TestDecision:
    """Decision enum tests."""

    def test_values_are_strings(self):
        """Decision members are str-valued for serialization."""
        for member in Decision:
            assert isinstance(member.value, str)
            assert member.value == member.name

    def test_stable_members(self):
        """All six expected members exist."""
        expected = {"ALLOW", "RETRY", "HALT", "DEGRADE", "QUARANTINE", "QUEUE"}
        assert {m.name for m in Decision} == expected

    def test_string_comparison(self):
        """Decision members compare equal to their string value."""
        assert Decision.ALLOW == "ALLOW"
        assert Decision.HALT == "HALT"


class TestToolCallContext:
    """ToolCallContext dataclass tests."""

    def test_frozen(self):
        """ToolCallContext rejects attribute assignment."""
        ctx = ToolCallContext(request_id="r1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.request_id = "r2"

    def test_metadata_default_distinct(self):
        """Each instance gets its own metadata dict."""
        a = ToolCallContext(request_id="a")
        b = ToolCallContext(request_id="b")
        assert a.metadata is not b.metadata
        assert a.metadata == {}

    def test_optional_fields_default_none(self):
        """All optional fields default to None."""
        ctx = ToolCallContext(request_id="r1")
        assert ctx.user_id is None
        assert ctx.tool_name is None
        assert ctx.tokens_in is None
        assert ctx.cost_usd is None

    def test_full_construction(self):
        """All fields can be populated."""
        ctx = ToolCallContext(
            request_id="r1",
            user_id="u1",
            session_id="s1",
            tool_name="bash",
            model="gpt-5.2-codex",
            endpoint="https://api.example.com",
            tokens_in=100,
            tokens_out=200,
            cost_usd=0.005,
            metadata={"key": "value"},
        )
        assert ctx.request_id == "r1"
        assert ctx.model == "gpt-5.2-codex"
        assert ctx.metadata == {"key": "value"}
