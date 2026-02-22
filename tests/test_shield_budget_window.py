"""Tests for BudgetWindowHook."""

from __future__ import annotations

import time

import pytest

from veronica_core.shield import (
    BudgetWindowHook,
    Decision,
    ShieldPipeline,
    ToolCallContext,
)
from veronica_core.shield.config import BudgetWindowConfig, ShieldConfig
from veronica_core.shield.hooks import PreDispatchHook

CTX = ToolCallContext(request_id="test", tool_name="bash")


class TestBudgetWindowOff:
    """When max_calls is very large, hook never HALTs (effectively off)."""

    def test_returns_none_well_below_limit(self):
        hook = BudgetWindowHook(max_calls=1_000_000)
        for _ in range(10):
            assert hook.before_llm_call(CTX) is None


class TestBudgetWindowOn:
    """First max_calls calls return None; (max_calls+1)th call returns HALT."""

    def test_first_calls_allowed(self):
        hook = BudgetWindowHook(max_calls=3, window_seconds=60.0)
        assert hook.before_llm_call(CTX) is None
        assert hook.before_llm_call(CTX) is None
        assert hook.before_llm_call(CTX) is None

    def test_call_after_limit_halts(self):
        hook = BudgetWindowHook(max_calls=3, window_seconds=60.0)
        hook.before_llm_call(CTX)
        hook.before_llm_call(CTX)
        hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_subsequent_calls_also_halt(self):
        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0)
        hook.before_llm_call(CTX)
        hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.HALT
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestBudgetWindowExpiry:
    """After window_seconds, old timestamps are pruned and counter resets."""

    def test_window_expiry_allows_new_calls(self, monkeypatch):
        times = iter([0.0, 0.5, 1.0, 61.5, 62.0])
        monkeypatch.setattr(time, "time", lambda: next(times))

        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0)
        assert hook.before_llm_call(CTX) is None   # t=0.0, count=1
        assert hook.before_llm_call(CTX) is None   # t=0.5, count=2  (already at limit)
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=1.0, HALT

        # After window expires, timestamps at 0.0 and 0.5 are pruned (cutoff = 61.5-60 = 1.5)
        assert hook.before_llm_call(CTX) is None   # t=61.5, count=1
        assert hook.before_llm_call(CTX) is None   # t=62.0, count=2


class TestBudgetWindowBoundary:
    """Boundary: max_calls=1 allows first call, halts second."""

    def test_max_calls_one_allows_first(self):
        hook = BudgetWindowHook(max_calls=1, window_seconds=60.0)
        assert hook.before_llm_call(CTX) is None

    def test_max_calls_one_halts_second(self):
        hook = BudgetWindowHook(max_calls=1, window_seconds=60.0)
        hook.before_llm_call(CTX)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_max_calls_zero_always_halts(self):
        hook = BudgetWindowHook(max_calls=0, window_seconds=60.0)
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestBudgetWindowProtocol:
    """BudgetWindowHook satisfies PreDispatchHook protocol."""

    def test_is_pre_dispatch(self):
        assert isinstance(BudgetWindowHook(max_calls=10), PreDispatchHook)


class TestBudgetWindowPipelineIntegration:
    """Pipeline wired with BudgetWindowHook behaves correctly."""

    def test_pipeline_halts_after_limit(self):
        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0)
        pipe = ShieldPipeline(pre_dispatch=hook)
        assert pipe.before_llm_call(CTX) is Decision.ALLOW  # call 1
        assert pipe.before_llm_call(CTX) is Decision.ALLOW  # call 2
        assert pipe.before_llm_call(CTX) is Decision.HALT   # call 3 (over limit)

    def test_pipeline_without_hook_always_allows(self):
        pipe = ShieldPipeline()
        for _ in range(5):
            assert pipe.before_llm_call(CTX) is Decision.ALLOW


class TestDefaultBudgetWindowConfig:
    """Default BudgetWindowConfig is disabled (non-breaking)."""

    def test_default_disabled(self):
        cfg = BudgetWindowConfig()
        assert cfg.enabled is False

    def test_shield_config_default_budget_window_disabled(self):
        cfg = ShieldConfig()
        assert cfg.budget_window.enabled is False

    def test_shield_config_is_any_enabled_false_by_default(self):
        cfg = ShieldConfig()
        assert cfg.is_any_enabled is False

    def test_shield_config_is_any_enabled_true_when_budget_window_on(self):
        cfg = ShieldConfig(budget_window=BudgetWindowConfig(enabled=True, max_calls=10))
        assert cfg.is_any_enabled is True


class TestBudgetWindowExpiryBoundary:
    """Exact boundary: call at t=window_seconds is expired (< not <=)."""

    def test_boundary_exact_window_not_double_counted(self, monkeypatch):
        # Call at t=0. At t=60, that call should be expired (cutoff = 60-60 = 0, ts=0 < 0 is False).
        # But we want to verify < semantics: ts < cutoff means expired.
        # At t=60: cutoff=0, ts=0 -> 0 < 0 is False -> NOT pruned, still counts.
        # At t=60.001: cutoff=0.001, ts=0 -> 0 < 0.001 -> pruned.
        times = iter([0.0, 60.0, 60.001])
        monkeypatch.setattr(time, "time", lambda: next(times))

        hook = BudgetWindowHook(max_calls=1, window_seconds=60.0)
        assert hook.before_llm_call(CTX) is None        # t=0.0, reserved slot

        # At t=60: cutoff=0.0, ts=0.0 -> 0.0 < 0.0 is False -> NOT expired
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=60.0, still within window

        # At t=60.001: cutoff=0.001, ts=0.0 -> 0.0 < 0.001 -> expired, fresh window
        assert hook.before_llm_call(CTX) is None        # t=60.001, old call pruned


class TestBudgetWindowIntegrationWiring:
    """VeronicaIntegration wires BudgetWindowHook only when enabled."""

    def test_disabled_allows_all(self):
        from veronica_core.integration import VeronicaIntegration

        vi = VeronicaIntegration(shield=ShieldConfig())
        for _ in range(5):
            assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW

    def test_enabled_halts_after_max_calls(self):
        from veronica_core.integration import VeronicaIntegration

        cfg = ShieldConfig(
            budget_window=BudgetWindowConfig(enabled=True, max_calls=3, window_seconds=60.0)
        )
        vi = VeronicaIntegration(shield=cfg)
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW  # call 1
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW  # call 2
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW  # call 3
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.HALT   # call 4

    def test_safe_mode_takes_priority_over_budget_window(self):
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.shield.config import SafeModeConfig

        cfg = ShieldConfig(
            safe_mode=SafeModeConfig(enabled=True),
            budget_window=BudgetWindowConfig(enabled=True, max_calls=1000),
        )
        vi = VeronicaIntegration(shield=cfg)
        # safe_mode blocks all tool calls immediately
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.HALT
