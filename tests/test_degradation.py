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


# ---------------------------------------------------------------------------
# DegradationLadder tier transitions — parametrized table
# ---------------------------------------------------------------------------
#
# Given: a DegradationLadder configured with model_map={"gpt-4o": "gpt-4o-mini"}
# When:  evaluate() is called with (cost_accumulated, max_cost_usd, current_model)
# Then:  the returned action matches expected_action


@pytest.mark.parametrize(
    "cost_accumulated,max_cost_usd,current_model,expected_action,expected_fallback",
    [
        # Given: usage is below all thresholds
        # When: evaluate called
        # Then: no degradation triggered
        (0.79, 1.0, "gpt-4o", None, None),
        # Given: usage crosses the model-downgrade tier (80%)
        # When: evaluate called with a model present in the map
        # Then: MODEL_DOWNGRADE action returned with correct fallback
        (0.81, 1.0, "gpt-4o", "MODEL_DOWNGRADE", "gpt-4o-mini"),
        # Given: usage crosses the context-trim tier (85%)
        # When: evaluate called
        # Then: CONTEXT_TRIM action returned
        (0.86, 1.0, "gpt-4o", "CONTEXT_TRIM", None),
        # Given: usage crosses the rate-limit tier (90%)
        # When: evaluate called
        # Then: RATE_LIMIT action returned
        (0.91, 1.0, "gpt-4o", "RATE_LIMIT", None),
        # Given: max_cost_usd is zero (budget tracking disabled)
        # When: evaluate called even with 100% usage
        # Then: no degradation triggered
        (1.0, 0.0, "gpt-4o", None, None),
        # Given: usage crosses model-downgrade tier but model is not in the map
        # When: evaluate called
        # Then: no degradation triggered (model not mapped)
        (0.81, 1.0, "unknown-model", None, None),
    ],
)
def test_degradation_tier_transitions(
    cost_accumulated: float,
    max_cost_usd: float,
    current_model: str,
    expected_action: str | None,
    expected_fallback: str | None,
) -> None:
    # Given
    ladder = _ladder_with_map()

    # When
    result = ladder.evaluate(
        cost_accumulated=cost_accumulated,
        max_cost_usd=max_cost_usd,
        current_model=current_model,
    )

    # Then
    if expected_action is None:
        assert result is None
    else:
        assert result is not None
        assert result.degradation_action == expected_action
        assert result.allowed is True
        if expected_fallback is not None:
            assert result.fallback_model == expected_fallback


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
