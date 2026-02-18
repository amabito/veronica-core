"""Tests for InputCompressionHook."""

from __future__ import annotations

import pytest

from veronica_core.shield import (
    Decision,
    InputCompressionHook,
    ToolCallContext,
)
from veronica_core.shield.config import InputCompressionConfig, ShieldConfig
from veronica_core.shield.input_compression import estimate_tokens, _sha256
from veronica_core.shield.pipeline import _HOOK_EVENT_TYPES

CTX = ToolCallContext(request_id="test", tool_name="llm")


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        assert estimate_tokens("hi") == 0  # 2 // 4 == 0

    def test_known_length(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100

    def test_integer_division(self):
        text = "a" * 7
        assert estimate_tokens(text) == 1  # 7 // 4 == 1


# ---------------------------------------------------------------------------
# _sha256
# ---------------------------------------------------------------------------

class TestSha256:
    def test_deterministic(self):
        assert _sha256("hello") == _sha256("hello")

    def test_different_input(self):
        assert _sha256("hello") != _sha256("world")

    def test_hex_length(self):
        assert len(_sha256("test")) == 64


# ---------------------------------------------------------------------------
# InputCompressionHook — basic
# ---------------------------------------------------------------------------

class TestInputCompressionBasic:
    def test_allow_below_threshold(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 100  # 25 tokens
        assert hook.check_input(text, CTX) is None

    def test_degrade_at_threshold(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 400  # 100 tokens
        assert hook.check_input(text, CTX) is Decision.DEGRADE

    def test_degrade_between_thresholds(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 600  # 150 tokens
        assert hook.check_input(text, CTX) is Decision.DEGRADE

    def test_halt_at_halt_threshold(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 800  # 200 tokens
        assert hook.check_input(text, CTX) is Decision.HALT

    def test_halt_above_halt_threshold(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 1200  # 300 tokens
        assert hook.check_input(text, CTX) is Decision.HALT


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class TestInputCompressionEvidence:
    def test_no_evidence_when_allow(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        hook.check_input("short", CTX)
        assert hook.last_evidence is None

    def test_evidence_on_degrade(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 400
        hook.check_input(text, CTX)
        ev = hook.last_evidence
        assert ev is not None
        assert ev["estimated_tokens"] == 100
        assert ev["decision"] == "DEGRADE"
        assert len(ev["input_sha256"]) == 64

    def test_evidence_on_halt(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "a" * 800
        hook.check_input(text, CTX)
        ev = hook.last_evidence
        assert ev is not None
        assert ev["decision"] == "HALT"
        assert ev["compression_threshold"] == 100
        assert ev["halt_threshold"] == 200

    def test_evidence_contains_sha256(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=20
        )
        text = "a" * 100
        hook.check_input(text, CTX)
        ev = hook.last_evidence
        assert ev["input_sha256"] == _sha256(text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestInputCompressionValidation:
    def test_halt_must_exceed_compress(self):
        with pytest.raises(ValueError, match="must be greater"):
            InputCompressionHook(
                compression_threshold_tokens=100, halt_threshold_tokens=100
            )

    def test_halt_less_than_compress_raises(self):
        with pytest.raises(ValueError, match="must be greater"):
            InputCompressionHook(
                compression_threshold_tokens=200, halt_threshold_tokens=100
            )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestInputCompressionProperties:
    def test_threshold_properties(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=500, halt_threshold_tokens=1000
        )
        assert hook.compression_threshold_tokens == 500
        assert hook.halt_threshold_tokens == 1000


# ---------------------------------------------------------------------------
# before_llm_call (passthrough)
# ---------------------------------------------------------------------------

class TestInputCompressionPreDispatch:
    def test_before_llm_call_always_none(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=20
        )
        assert hook.before_llm_call(CTX) is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestInputCompressionConfig:
    def test_default_disabled(self):
        cfg = InputCompressionConfig()
        assert cfg.enabled is False

    def test_shield_config_default_disabled(self):
        cfg = ShieldConfig()
        assert cfg.input_compression.enabled is False

    def test_shield_is_any_enabled_true(self):
        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(enabled=True)
        )
        assert cfg.is_any_enabled is True

    def test_from_dict_round_trip(self):
        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(
                enabled=True,
                compression_threshold_tokens=2000,
                halt_threshold_tokens=5000,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.input_compression.enabled is True
        assert restored.input_compression.compression_threshold_tokens == 2000
        assert restored.input_compression.halt_threshold_tokens == 5000


# ---------------------------------------------------------------------------
# Pipeline event type
# ---------------------------------------------------------------------------

class TestInputCompressionEventType:
    def test_event_type_registered(self):
        assert "InputCompressionHook" in _HOOK_EVENT_TYPES
        assert _HOOK_EVENT_TYPES["InputCompressionHook"] == "INPUT_TOO_LARGE"


# ---------------------------------------------------------------------------
# Integration wiring
# ---------------------------------------------------------------------------

class TestInputCompressionIntegration:
    def test_disabled_no_hook(self):
        from veronica_core.integration import VeronicaIntegration

        vi = VeronicaIntegration(shield=ShieldConfig())
        assert vi._input_compression_hook is None

    def test_enabled_creates_hook(self):
        from veronica_core.integration import VeronicaIntegration

        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(
                enabled=True,
                compression_threshold_tokens=500,
                halt_threshold_tokens=1000,
            )
        )
        vi = VeronicaIntegration(shield=cfg)
        assert vi._input_compression_hook is not None
        assert vi._input_compression_hook.compression_threshold_tokens == 500
        assert vi._input_compression_hook.halt_threshold_tokens == 1000

    def test_hook_check_input_works(self):
        from veronica_core.integration import VeronicaIntegration

        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(
                enabled=True,
                compression_threshold_tokens=100,
                halt_threshold_tokens=200,
            )
        )
        vi = VeronicaIntegration(shield=cfg)
        hook = vi._input_compression_hook
        text = "a" * 800  # 200 tokens -> HALT
        assert hook.check_input(text, CTX) is Decision.HALT
