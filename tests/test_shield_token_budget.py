"""Tests for TokenBudgetHook."""

from __future__ import annotations

import pytest

from veronica_core.shield import (
    Decision,
    ShieldPipeline,
    TokenBudgetHook,
    ToolCallContext,
)
from veronica_core.shield.config import ShieldConfig, TokenBudgetConfig
from veronica_core.shield.hooks import PreDispatchHook

CTX = ToolCallContext(request_id="test", tool_name="llm")


class TestTokenBudgetOff:
    """When budget is very large, hook never triggers."""

    def test_service_allows_calls_when_token_budget_is_effectively_unlimited(self):
        hook = TokenBudgetHook(max_output_tokens=1_000_000)
        for _ in range(10):
            assert hook.before_llm_call(CTX) is None


class TestTokenBudgetOn:
    """Basic HALT behavior."""

    def test_service_halts_calls_once_output_token_budget_is_fully_consumed(self):
        hook = TokenBudgetHook(max_output_tokens=100)
        hook.record_usage(output_tokens=100)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_service_allows_calls_when_output_token_usage_is_below_limit(self):
        hook = TokenBudgetHook(max_output_tokens=100)
        hook.record_usage(output_tokens=50)
        assert hook.before_llm_call(CTX) is None

    def test_service_halts_calls_when_token_usage_exceeds_configured_limit(self):
        hook = TokenBudgetHook(max_output_tokens=100)
        hook.record_usage(output_tokens=150)
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestTokenBudgetDegrade:
    """DEGRADE zone tests."""

    def test_service_degrades_when_token_usage_reaches_degrade_threshold(self):
        hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=0.8)
        hook.record_usage(output_tokens=80)
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_service_remains_degraded_for_all_usage_between_threshold_and_hard_limit(self):
        hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=0.8)
        hook.record_usage(output_tokens=90)
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_service_allows_calls_when_token_usage_is_below_degrade_threshold(self):
        hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=0.8)
        hook.record_usage(output_tokens=79)
        assert hook.before_llm_call(CTX) is None

    def test_setting_degrade_threshold_to_maximum_disables_degrade_zone(self):
        hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=1.0)
        hook.record_usage(output_tokens=99)
        assert hook.before_llm_call(CTX) is None
        hook.record_usage(output_tokens=1)
        assert hook.before_llm_call(CTX) is Decision.HALT


class TestTokenBudgetTotal:
    """max_total_tokens tests."""

    def test_service_halts_when_combined_input_and_output_tokens_exceed_total_limit(self):
        hook = TokenBudgetHook(max_output_tokens=1000, max_total_tokens=200)
        hook.record_usage(output_tokens=50, input_tokens=150)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_service_degrades_when_combined_token_usage_reaches_total_degrade_threshold(self):
        hook = TokenBudgetHook(
            max_output_tokens=1000, max_total_tokens=200, degrade_threshold=0.8
        )
        hook.record_usage(output_tokens=50, input_tokens=110)
        assert hook.before_llm_call(CTX) is Decision.DEGRADE

    def test_service_ignores_total_token_limit_when_it_is_set_to_zero(self):
        hook = TokenBudgetHook(max_output_tokens=1000, max_total_tokens=0)
        hook.record_usage(output_tokens=50, input_tokens=999999)
        assert hook.before_llm_call(CTX) is None


class TestTokenBudgetBoundary:
    def test_service_halts_immediately_when_output_token_limit_is_zero(self):
        hook = TokenBudgetHook(max_output_tokens=0)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_usage_counters_accurately_reflect_recorded_token_consumption(self):
        hook = TokenBudgetHook(max_output_tokens=1000)
        hook.record_usage(output_tokens=100, input_tokens=200)
        assert hook.output_total == 100
        assert hook.input_total == 200
        assert hook.total == 300


class TestTokenBudgetProtocol:
    def test_is_pre_dispatch(self):
        assert isinstance(TokenBudgetHook(max_output_tokens=10), PreDispatchHook)


class TestTokenBudgetPipelineIntegration:
    def test_pipeline_halts_at_limit(self):
        hook = TokenBudgetHook(max_output_tokens=100)
        pipe = ShieldPipeline(pre_dispatch=hook)
        assert pipe.before_llm_call(CTX) is Decision.ALLOW
        hook.record_usage(output_tokens=100)
        assert pipe.before_llm_call(CTX) is Decision.HALT

    def test_pipeline_records_safety_event(self):
        hook = TokenBudgetHook(max_output_tokens=100)
        pipe = ShieldPipeline(pre_dispatch=hook)
        hook.record_usage(output_tokens=100)
        pipe.before_llm_call(CTX)
        events = pipe.get_events()
        assert len(events) == 1
        assert events[0].event_type == "TOKEN_BUDGET_EXCEEDED"
        assert events[0].decision is Decision.HALT


class TestTokenBudgetConfigDefaults:
    def test_default_disabled(self):
        cfg = TokenBudgetConfig()
        assert cfg.enabled is False

    def test_shield_config_default_disabled(self):
        cfg = ShieldConfig()
        assert cfg.token_budget.enabled is False

    def test_shield_is_any_enabled_true_when_token_budget_on(self):
        cfg = ShieldConfig(token_budget=TokenBudgetConfig(enabled=True))
        assert cfg.is_any_enabled is True


class TestTokenBudgetPendingReservation:
    """TOCTOU fix: pending reservation tests."""

    def test_pending_reservation_blocks_second_concurrent_call(self):
        # max=100, estimated_out=60 per call: first reserves 60, second projected=120 -> HALT
        hook = TokenBudgetHook(max_output_tokens=100)
        ctx_estimated = ToolCallContext(request_id="r1", tool_name="llm", tokens_out=60)
        result1 = hook.before_llm_call(ctx_estimated)
        assert result1 is None  # First call passes and reserves 60
        assert hook.pending_output == 60

        result2 = hook.before_llm_call(ctx_estimated)
        assert result2 is Decision.HALT  # Second call: 0 + 60 + 60 = 120 >= 100

    def test_record_usage_releases_pending(self):
        hook = TokenBudgetHook(max_output_tokens=200)
        ctx_estimated = ToolCallContext(request_id="r1", tool_name="llm", tokens_out=80)
        hook.before_llm_call(ctx_estimated)  # reserves 80
        assert hook.pending_output == 80

        hook.record_usage(output_tokens=50)  # actual=50, releases min(80,50)=50 from pending
        assert hook.pending_output == 30     # 80-50=30 remaining pending
        assert hook.output_total == 50

    def test_release_reservation_releases_pending(self):
        hook = TokenBudgetHook(max_output_tokens=200)
        ctx_estimated = ToolCallContext(request_id="r1", tool_name="llm", tokens_out=80)
        hook.before_llm_call(ctx_estimated)  # reserves 80
        assert hook.pending_output == 80

        hook.release_reservation(estimated_out=80)
        assert hook.pending_output == 0
        # After release, another call with same estimate should pass
        result = hook.before_llm_call(ctx_estimated)
        assert result is None


class TestTokenBudgetIntegrationWiring:
    def test_disabled_allows_all(self):
        from veronica_core.integration import VeronicaIntegration

        vi = VeronicaIntegration(shield=ShieldConfig())
        for _ in range(5):
            assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW

    def test_enabled_halts_at_limit(self):
        from veronica_core.integration import VeronicaIntegration

        cfg = ShieldConfig(
            token_budget=TokenBudgetConfig(enabled=True, max_output_tokens=100)
        )
        vi = VeronicaIntegration(shield=cfg)
        # Access the hook to record usage
        hook = vi._shield_pipeline._pre_dispatch
        hook.record_usage(output_tokens=100)
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.HALT

    def test_safe_mode_takes_priority(self):
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.shield.config import SafeModeConfig

        cfg = ShieldConfig(
            safe_mode=SafeModeConfig(enabled=True),
            token_budget=TokenBudgetConfig(enabled=True, max_output_tokens=1000000),
        )
        vi = VeronicaIntegration(shield=cfg)
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.HALT
