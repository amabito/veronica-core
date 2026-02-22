"""Tests for DegradationLadder (P2-3)."""
from __future__ import annotations

import pytest

from veronica_core.runtime_policy import (
    PolicyDecision,
    allow,
    deny,
    model_downgrade,
    rate_limit_decision,
)
from veronica_core.shield.degradation import DegradationConfig, DegradationLadder, NoOpTrimmer


# ---------------------------------------------------------------------------
# DegradationLadder threshold tests
# ---------------------------------------------------------------------------


def _ladder_with_map(model_map: dict | None = None) -> DegradationLadder:
    return DegradationLadder(
        DegradationConfig(
            model_map=model_map or {"gpt-4o": "gpt-4o-mini"},
            rate_limit_ms=500,
        )
    )


def test_below_all_thresholds_returns_none():
    ladder = _ladder_with_map()
    result = ladder.evaluate(cost_accumulated=0.79, max_cost_usd=1.0, current_model="gpt-4o")
    assert result is None


def test_model_downgrade_tier():
    ladder = _ladder_with_map()
    result = ladder.evaluate(cost_accumulated=0.81, max_cost_usd=1.0, current_model="gpt-4o")
    assert result is not None
    assert result.degradation_action == "MODEL_DOWNGRADE"
    assert result.fallback_model == "gpt-4o-mini"
    assert result.allowed is True


def test_model_downgrade_model_not_in_map():
    ladder = _ladder_with_map(model_map={"gpt-4o": "gpt-4o-mini"})
    # "unknown-model" not in map → no downgrade → returns None
    result = ladder.evaluate(cost_accumulated=0.81, max_cost_usd=1.0, current_model="unknown-model")
    assert result is None


def test_context_trim_tier():
    ladder = _ladder_with_map()
    result = ladder.evaluate(cost_accumulated=0.86, max_cost_usd=1.0, current_model="gpt-4o")
    assert result is not None
    assert result.degradation_action == "CONTEXT_TRIM"
    assert result.allowed is True


def test_rate_limit_tier():
    ladder = _ladder_with_map()
    result = ladder.evaluate(cost_accumulated=0.91, max_cost_usd=1.0, current_model="gpt-4o")
    assert result is not None
    assert result.degradation_action == "RATE_LIMIT"
    assert result.rate_limit_ms == 500
    assert result.allowed is True


def test_zero_max_cost_returns_none():
    ladder = _ladder_with_map()
    result = ladder.evaluate(cost_accumulated=1.0, max_cost_usd=0.0)
    assert result is None


# ---------------------------------------------------------------------------
# apply_rate_limit
# ---------------------------------------------------------------------------


def test_apply_rate_limit_calls_sleep(monkeypatch):
    import time

    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    ladder = _ladder_with_map()
    decision = rate_limit_decision(delay_ms=1500)
    ladder.apply_rate_limit(decision)

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(1.5)


def test_apply_rate_limit_zero_ms_no_sleep(monkeypatch):
    import time

    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    ladder = _ladder_with_map()
    decision = PolicyDecision(allowed=True, policy_type="rate_limit", rate_limit_ms=0)
    ladder.apply_rate_limit(decision)

    assert sleep_calls == []


# ---------------------------------------------------------------------------
# apply_context_trim
# ---------------------------------------------------------------------------


def test_apply_context_trim_noop():
    ladder = DegradationLadder(DegradationConfig(trimmer=NoOpTrimmer()))
    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    result = ladder.apply_context_trim(msgs)
    assert result == msgs


def test_custom_trimmer():
    class HalfTrimmer:
        def trim(self, messages: list) -> list:
            return messages[len(messages) // 2 :]

    ladder = DegradationLadder(DegradationConfig(trimmer=HalfTrimmer()))
    msgs = [{"role": "user", "content": str(i)} for i in range(10)]
    result = ladder.apply_context_trim(msgs)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# PolicyDecision backward compatibility
# ---------------------------------------------------------------------------


def test_policy_decision_backward_compat():
    d = PolicyDecision(allowed=True, policy_type="budget")
    assert d.allowed is True
    assert d.policy_type == "budget"
    assert d.reason == ""
    assert d.partial_result is None
    # New fields default to None/0
    assert d.degradation_action is None
    assert d.fallback_model is None
    assert d.rate_limit_ms == 0


# ---------------------------------------------------------------------------
# PolicyDecision factory helpers
# ---------------------------------------------------------------------------


def test_model_downgrade_helper():
    d = model_downgrade(current_model="gpt-4o", fallback_model="gpt-4o-mini", reason="budget")
    assert d.allowed is True
    assert d.policy_type == "model_downgrade"
    assert d.degradation_action == "MODEL_DOWNGRADE"
    assert d.fallback_model == "gpt-4o-mini"
    assert d.reason == "budget"


def test_rate_limit_helper():
    d = rate_limit_decision(delay_ms=2000, reason="throttled")
    assert d.allowed is True
    assert d.policy_type == "rate_limit"
    assert d.degradation_action == "RATE_LIMIT"
    assert d.rate_limit_ms == 2000
    assert d.reason == "throttled"


def test_allow_helper():
    d = allow()
    assert d.allowed is True
    assert d.policy_type == "allow"


def test_allow_helper_custom_type():
    d = allow(policy_type="custom")
    assert d.policy_type == "custom"


def test_deny_helper():
    d = deny(policy_type="budget", reason="over limit")
    assert d.allowed is False
    assert d.policy_type == "budget"
    assert d.reason == "over limit"
