"""Tests for InputCompressionHook runtime compression (v0.5.1)."""

from __future__ import annotations



from veronica_core.shield.input_compression import (
    Compressor,
    InputCompressionHook,
    TemplateCompressor,
    _extract_important_lines,
    estimate_tokens,
)
from veronica_core.shield.types import Decision, ToolCallContext

CTX = ToolCallContext(request_id="test-runtime", tool_name="llm")


# ---------------------------------------------------------------------------
# TemplateCompressor
# ---------------------------------------------------------------------------

class TestTemplateCompressor:
    def test_compress_preserves_numbers(self):
        tc = TemplateCompressor()
        text = "The budget is $5000.\nSome filler text here.\nDeadline is 2026-03-01."
        result = tc.compress(text, target_tokens=500)
        assert "5000" in result
        assert "2026-03-01" in result

    def test_compress_preserves_constraints(self):
        tc = TemplateCompressor()
        text = "You must never exceed 100 calls.\nRandom other content.\nAlways validate input."
        result = tc.compress(text, target_tokens=500)
        assert "must" in result.lower() or "never" in result.lower()
        assert "Always" in result or "always" in result

    def test_compress_returns_template_format(self):
        tc = TemplateCompressor()
        text = "Purpose line.\n" + "filler " * 200
        result = tc.compress(text, target_tokens=100)
        assert "[Purpose]" in result
        assert "[Constraints]" in result
        assert "[Key Data]" in result
        assert "=== COMPRESSED INPUT (VERONICA) ===" in result

    def test_compress_reduces_size(self):
        tc = TemplateCompressor()
        # Multi-line input with realistic content
        text = "\n".join([f"Line {i}: some content here" for i in range(500)])
        result = tc.compress(text, target_tokens=500)
        assert estimate_tokens(result) < estimate_tokens(text)


# ---------------------------------------------------------------------------
# _extract_important_lines
# ---------------------------------------------------------------------------

class TestExtractImportantLines:
    def test_numbers_are_important(self):
        imp, oth = _extract_important_lines("budget is 5000\nhello world")
        assert any("5000" in line for line in imp)
        assert any("hello" in line for line in oth)

    def test_dates_are_important(self):
        imp, _ = _extract_important_lines("deadline 2026-01-15\nfoo bar")
        assert any("2026-01-15" in line for line in imp)

    def test_constraints_are_important(self):
        imp, _ = _extract_important_lines("must not exceed limit\nrandom stuff")
        assert any("must" in line for line in imp)

    def test_empty_lines_skipped(self):
        imp, oth = _extract_important_lines("\n\n\nhello\n\n")
        assert len(imp) + len(oth) == 1


# ---------------------------------------------------------------------------
# compress_if_needed -- below threshold
# ---------------------------------------------------------------------------

class TestCompressIfNeededAllow:
    def test_short_input_passes_through(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=200
        )
        text = "short"
        out, decision = hook.compress_if_needed(text, CTX)
        assert out == text
        assert decision is None


# ---------------------------------------------------------------------------
# compress_if_needed -- compression succeeds
# ---------------------------------------------------------------------------

class TestCompressIfNeededSuccess:
    def test_compresses_above_threshold(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=500
        )
        text = "x " * 500  # ~250 tokens -> above 100
        out, decision = hook.compress_if_needed(text, CTX)
        assert decision is Decision.DEGRADE
        assert "COMPRESSED" in out

    def test_records_two_events(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=500
        )
        text = "x " * 500
        hook.compress_if_needed(text, CTX)
        events = hook.get_events()
        assert len(events) == 2
        assert events[0].event_type == "INPUT_COMPRESSED"
        assert events[1].event_type == "COMPRESSION_APPLIED"

    def test_evidence_has_compression_ratio(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=5000
        )
        text = "\n".join([f"Line {i}: some verbose content" for i in range(500)])
        hook.compress_if_needed(text, CTX)
        ev = hook.last_evidence
        assert "before_tokens" in ev
        assert "after_tokens" in ev
        assert "compression_ratio" in ev
        assert ev["after_tokens"] < ev["before_tokens"]

    def test_evidence_has_sha256(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=100, halt_threshold_tokens=500
        )
        text = "budget is 5000 and must not exceed limit " * 20
        hook.compress_if_needed(text, CTX)
        ev = hook.last_evidence
        assert len(ev["input_sha256"]) == 64


# ---------------------------------------------------------------------------
# compress_if_needed -- compression fails
# ---------------------------------------------------------------------------

class TestCompressIfNeededFailure:
    def test_failure_halts_by_default(self):
        class BrokenCompressor:
            def compress(self, text: str, target_tokens: int) -> str:
                raise RuntimeError("boom")

        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=200,
            compressor=BrokenCompressor(),
        )
        text = "a" * 200  # 50 tokens > 10
        out, decision = hook.compress_if_needed(text, CTX)
        assert decision is Decision.HALT

    def test_failure_fallback_to_original(self):
        class BrokenCompressor:
            def compress(self, text: str, target_tokens: int) -> str:
                raise RuntimeError("boom")

        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=200,
            compressor=BrokenCompressor(),
            fallback_to_original=True,
        )
        text = "a" * 200
        out, decision = hook.compress_if_needed(text, CTX)
        assert decision is Decision.DEGRADE
        assert out == text  # original returned

    def test_failure_records_events(self):
        class BrokenCompressor:
            def compress(self, text: str, target_tokens: int) -> str:
                raise RuntimeError("boom")

        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=200,
            compressor=BrokenCompressor(),
        )
        hook.compress_if_needed("a" * 200, CTX)
        events = hook.get_events()
        assert len(events) == 2
        assert events[0].metadata.get("input_sha256") is not None
        ev = hook.last_evidence
        assert ev["compression_failed"] is True


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------

class TestEscapeHatch:
    def test_disabled_env_skips_compression(self, monkeypatch):
        monkeypatch.setenv("VERONICA_DISABLE_COMPRESSION", "1")
        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=200,
        )
        text = "a" * 200  # 50 tokens > 10
        out, decision = hook.compress_if_needed(text, CTX)
        assert out == text  # NOT compressed
        assert decision is Decision.DEGRADE
        ev = hook.last_evidence
        assert ev["compression_disabled"] is True

    def test_enabled_env_allows_compression(self, monkeypatch):
        monkeypatch.delenv("VERONICA_DISABLE_COMPRESSION", raising=False)
        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=500,
        )
        text = "a" * 200
        out, decision = hook.compress_if_needed(text, CTX)
        assert decision is Decision.DEGRADE
        assert "COMPRESSED" in out


# ---------------------------------------------------------------------------
# Custom compressor
# ---------------------------------------------------------------------------

class TestCustomCompressor:
    def test_custom_compressor_used(self):
        class HalfCompressor:
            def compress(self, text: str, target_tokens: int) -> str:
                return text[:len(text) // 2]

        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=500,
            compressor=HalfCompressor(),
        )
        text = "abcdef" * 100  # 600 chars -> 150 tokens
        out, decision = hook.compress_if_needed(text, CTX)
        assert decision is Decision.DEGRADE
        assert len(out) == 300


# ---------------------------------------------------------------------------
# Compressor Protocol
# ---------------------------------------------------------------------------

class TestCompressorProtocol:
    def test_template_compressor_is_compressor(self):
        assert isinstance(TemplateCompressor(), Compressor)

    def test_custom_class_satisfies_protocol(self):
        class MyCompressor:
            def compress(self, text: str, target_tokens: int) -> str:
                return text

        assert isinstance(MyCompressor(), Compressor)


# ---------------------------------------------------------------------------
# clear_events
# ---------------------------------------------------------------------------

class TestClearEvents:
    def test_clear_events(self):
        hook = InputCompressionHook(
            compression_threshold_tokens=10, halt_threshold_tokens=500,
        )
        hook.compress_if_needed("a" * 200, CTX)
        assert len(hook.get_events()) == 2
        hook.clear_events()
        assert len(hook.get_events()) == 0


# ---------------------------------------------------------------------------
# Config round-trip with fallback_to_original
# ---------------------------------------------------------------------------

class TestConfigFallback:
    def test_config_round_trip(self):
        from veronica_core.shield.config import InputCompressionConfig, ShieldConfig

        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(
                enabled=True,
                fallback_to_original=True,
            )
        )
        d = cfg.to_dict()
        restored = ShieldConfig.from_dict(d)
        assert restored.input_compression.fallback_to_original is True

    def test_integration_wires_fallback(self):
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.shield.config import InputCompressionConfig, ShieldConfig

        cfg = ShieldConfig(
            input_compression=InputCompressionConfig(
                enabled=True,
                compression_threshold_tokens=100,
                halt_threshold_tokens=200,
                fallback_to_original=True,
            )
        )
        vi = VeronicaIntegration(shield=cfg)
        assert vi._input_compression_hook.fallback_to_original is True
