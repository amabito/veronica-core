"""Tests for shield core types (PR-1B)."""

import dataclasses

import pytest

from veronica_core.shield import Decision, ToolCallContext


class TestDecision:
    """Decision enum tests."""

    def test_string_comparison(self):
        """Decision members compare equal to their string value (serialization contract)."""
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

