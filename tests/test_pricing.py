"""Tests for veronica_core.pricing auto cost estimation (P1-1)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.pricing import (
    Pricing,
    PRICING_TABLE,
    _UNKNOWN_MODEL_FALLBACK,
    estimate_cost_usd,
    extract_usage_from_response,
    resolve_model_pricing,
)


# ---------------------------------------------------------------------------
# resolve_model_pricing
# ---------------------------------------------------------------------------


def test_known_model_exact():
    """gpt-4o exact match returns correct pricing."""
    pricing = resolve_model_pricing("gpt-4o")
    assert pricing.input_per_1k == pytest.approx(0.005)
    assert pricing.output_per_1k == pytest.approx(0.015)


def test_known_model_prefix():
    """'gpt-4o-2024-11-20' should match 'gpt-4o' via prefix."""
    pricing = resolve_model_pricing("gpt-4o-2024-11-20")
    assert pricing == PRICING_TABLE["gpt-4o"]


def test_unknown_model_fallback():
    """Completely unknown model returns the conservative fallback."""
    pricing = resolve_model_pricing("my-custom-model-xyz")
    assert pricing == _UNKNOWN_MODEL_FALLBACK
    assert pricing.input_per_1k == pytest.approx(0.030)
    assert pricing.output_per_1k == pytest.approx(0.060)


def test_empty_model_fallback():
    """Empty string returns fallback."""
    pricing = resolve_model_pricing("")
    assert pricing == _UNKNOWN_MODEL_FALLBACK


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_cost_usd_known():
    """gpt-4o: 1000 input + 500 output = 1*0.005 + 0.5*0.015 = 0.0125 USD."""
    cost = estimate_cost_usd("gpt-4o", 1000, 500)
    assert cost == pytest.approx(0.0125)


def test_estimate_cost_usd_zero_tokens():
    """Zero tokens on any model yields 0.0."""
    assert estimate_cost_usd("gpt-4o", 0, 0) == 0.0


def test_estimate_cost_usd_unknown_model():
    """Unknown model uses fallback pricing but still returns a positive value."""
    cost = estimate_cost_usd("totally-unknown-model", 1000, 1000)
    expected = (1000 / 1000.0) * 0.030 + (1000 / 1000.0) * 0.060
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# extract_usage_from_response
# ---------------------------------------------------------------------------


def test_extract_usage_openai_object():
    """SimpleNamespace with prompt_tokens/completion_tokens -> (100, 50)."""
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)
    response = SimpleNamespace(usage=usage)
    result = extract_usage_from_response(response)
    assert result == (100, 50)


def test_extract_usage_anthropic_object():
    """SimpleNamespace with input_tokens/output_tokens -> (200, 100)."""
    usage = SimpleNamespace(input_tokens=200, output_tokens=100)
    response = SimpleNamespace(usage=usage)
    result = extract_usage_from_response(response)
    assert result == (200, 100)


def test_extract_usage_dict_openai():
    """Dict with usage.prompt_tokens/completion_tokens format."""
    response = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    result = extract_usage_from_response(response)
    assert result == (100, 50)


def test_extract_usage_dict_anthropic():
    """Dict with usage.input_tokens/output_tokens format."""
    response = {"usage": {"input_tokens": 200, "output_tokens": 100}}
    result = extract_usage_from_response(response)
    assert result == (200, 100)


def test_extract_usage_none():
    """None input returns None."""
    assert extract_usage_from_response(None) is None


def test_extract_usage_no_usage_attr():
    """Object without usage attribute returns None."""
    assert extract_usage_from_response(object()) is None


def test_extract_usage_dict_no_usage_key():
    """Dict without 'usage' key returns None."""
    assert extract_usage_from_response({"result": "ok"}) is None


# ---------------------------------------------------------------------------
# Integration tests with ExecutionContext
# ---------------------------------------------------------------------------


def test_wrap_llm_call_auto_cost():
    """ExecutionContext auto-calculates cost from model + response_hint."""
    from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions

    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
    mock_response = SimpleNamespace(usage=usage)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    with ExecutionContext(config=config) as ctx:
        ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(model="gpt-4o", response_hint=mock_response),
        )
        snap = ctx.get_snapshot()

    # gpt-4o: 1000 input + 500 output = 0.0125 USD
    assert snap.cost_usd_accumulated == pytest.approx(0.0125)


def test_wrap_llm_call_no_response_hint_emits_warning():
    """No response_hint with a known model emits COST_ESTIMATION_SKIPPED event."""
    from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    with ExecutionContext(config=config) as ctx:
        ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(model="gpt-4o"),
        )
        snap = ctx.get_snapshot()

    event_types = [e.event_type for e in snap.events]
    assert "COST_ESTIMATION_SKIPPED" in event_types
    # No cost should be accumulated since no usage was available
    assert snap.cost_usd_accumulated == pytest.approx(0.0)


def test_negative_tokens_raise_value_error():
    """estimate_cost_usd raises ValueError for negative token counts."""
    with pytest.raises((ValueError, Exception)):
        estimate_cost_usd("gpt-4o", -1, 0)


def test_exact_match_only_no_substring():
    """A model name that embeds 'gpt-4o-mini' but is not a prefix match falls back."""
    # "my-enterprise-gpt-4o-mini-v2" is not a prefix of any key, and no key is
    # a prefix of it, so it must return the unknown fallback (not gpt-4o or gpt-4o-mini).
    pricing = resolve_model_pricing("my-enterprise-gpt-4o-mini-v2")
    assert pricing == _UNKNOWN_MODEL_FALLBACK


def test_wrap_llm_call_cost_estimate_hint_takes_precedence():
    """Explicit cost_estimate_hint overrides auto calculation."""
    from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions

    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
    mock_response = SimpleNamespace(usage=usage)

    config = ExecutionConfig(max_cost_usd=10.0, max_steps=10, max_retries_total=5)
    with ExecutionContext(config=config) as ctx:
        ctx.wrap_llm_call(
            fn=lambda: None,
            options=WrapOptions(
                model="gpt-4o",
                response_hint=mock_response,
                cost_estimate_hint=0.99,  # explicit hint wins
            ),
        )
        snap = ctx.get_snapshot()

    assert snap.cost_usd_accumulated == pytest.approx(0.99)
