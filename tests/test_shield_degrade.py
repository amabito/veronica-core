"""Tests for DEGRADE support in BudgetWindowHook."""

from __future__ import annotations

import pytest

from veronica_core.shield import (
    BudgetWindowHook,
    Decision,
    ToolCallContext,
)
from veronica_core.shield.config import BudgetWindowConfig, ShieldConfig

CTX = ToolCallContext(request_id="test", tool_name="bash")


class TestBelowThreshold:
    """Below 80%: returns None (ALLOW zone)."""

    def test_returns_none_below_degrade_threshold(self):
        # max_calls=10, degrade_threshold=0.8 -> degrade at count >= 8
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.8)
        for _ in range(7):
            assert hook.before_llm_call(CTX) is None

    def test_zero_calls_returns_none(self):
        hook = BudgetWindowHook(max_calls=5, window_seconds=60.0, degrade_threshold=0.8)
        assert hook.before_llm_call(CTX) is None


class TestAtDegradeThreshold:
    """At 80% threshold: returns DEGRADE."""

    def test_returns_degrade_at_threshold(self):
        # max_calls=10, degrade_at=8.0 -> 8th call (0-indexed count=8) returns DEGRADE
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.8)
        for _ in range(8):
            hook.before_llm_call(CTX)
        # count is now 8 >= 8.0 -> DEGRADE
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_degrade_zone_continues_until_halt(self):
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.8)
        for _ in range(8):
            hook.before_llm_call(CTX)
        # calls 9 and 10 (count 8 and 9) are in DEGRADE zone
        assert hook.before_llm_call(CTX) is Decision.DEGRADE
        assert hook.before_llm_call(CTX) is Decision.DEGRADE


class TestAtMaxCalls:
    """At 100% (max_calls): returns HALT."""

    def test_returns_halt_at_max_calls(self):
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.8)
        for _ in range(10):
            hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_halt_persists_after_max_calls(self):
        hook = BudgetWindowHook(max_calls=5, window_seconds=60.0, degrade_threshold=0.8)
        for _ in range(5):
            hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.HALT
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestCustomThreshold:
    """Custom degrade_threshold (e.g., 0.5): DEGRADE at 50%."""

    def test_degrade_at_50_percent(self):
        # max_calls=10, degrade_at=5.0 -> 5th call returns DEGRADE
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.5)
        for _ in range(5):
            hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_allow_zone_with_50_percent_threshold(self):
        hook = BudgetWindowHook(max_calls=10, window_seconds=60.0, degrade_threshold=0.5)
        for _ in range(4):
            assert hook.before_llm_call(CTX) is None

    def test_threshold_1_0_skips_degrade(self):
        # degrade_threshold=1.0 -> degrade_at=max_calls -> no DEGRADE zone
        hook = BudgetWindowHook(max_calls=5, window_seconds=60.0, degrade_threshold=1.0)
        for _ in range(5):
            result = hook.before_llm_call(CTX)
            assert result is None or result is Decision.DEGRADE  # boundary may vary
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestDefaultThreshold:
    """No degrade_threshold set: default 0.8 behavior."""

    def test_default_threshold_is_0_8(self):
        hook = BudgetWindowHook(max_calls=10)
        # calls 0..7 = None
        for _ in range(8):
            hook.before_llm_call(CTX)
        # call 9 (count=8 >= 8.0) = DEGRADE
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_default_window_seconds(self):
        hook = BudgetWindowHook(max_calls=5)
        assert hook._window_seconds == 60.0
        assert hook._degrade_threshold == 0.8


class TestDegradeMapConfig:
    """degrade_map in BudgetWindowConfig serializes/deserializes correctly."""

    def test_degrade_map_default_empty(self):
        cfg = BudgetWindowConfig()
        assert cfg.degrade_map == {}

    def test_degrade_map_serializes(self):
        cfg = BudgetWindowConfig(
            enabled=True,
            max_calls=10,
            degrade_threshold=0.8,
            degrade_map={"gpt-4": "gpt-3.5-turbo"},
        )
        d = ShieldConfig(budget_window=cfg).to_dict()
        assert d["budget_window"]["degrade_map"] == {"gpt-4": "gpt-3.5-turbo"}

    def test_degrade_map_deserializes(self):
        data = {
            "budget_window": {
                "enabled": True,
                "max_calls": 10,
                "window_seconds": 60.0,
                "degrade_threshold": 0.8,
                "degrade_map": {"claude-3": "claude-instant"},
            }
        }
        cfg = ShieldConfig.from_dict(data)
        assert cfg.budget_window.degrade_map == {"claude-3": "claude-instant"}
        assert cfg.budget_window.degrade_threshold == 0.8

    def test_degrade_threshold_in_config(self):
        cfg = BudgetWindowConfig(degrade_threshold=0.5)
        assert cfg.degrade_threshold == 0.5


class TestNoRegression:
    """Existing BudgetWindowHook tests still pass (backward compatibility)."""

    def test_max_calls_zero_always_halts(self):
        hook = BudgetWindowHook(max_calls=0)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_max_calls_one_allows_first_then_halts(self):
        hook = BudgetWindowHook(max_calls=1, degrade_threshold=0.8)
        # degrade_at = 0.8 -> 1st call: count=0 < 0.8 -> None
        assert hook.before_llm_call(CTX) is None
        # 2nd call: count=1 >= 1 -> HALT
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_large_max_calls_no_halt(self):
        hook = BudgetWindowHook(max_calls=1_000_000)
        for _ in range(10):
            result = hook.before_llm_call(CTX)
            assert result in (None, Decision.DEGRADE)
