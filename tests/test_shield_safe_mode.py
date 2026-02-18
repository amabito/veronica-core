"""Tests for SafeModeHook."""

from veronica_core.shield import Decision, SafeModeHook, ShieldPipeline, ToolCallContext
from veronica_core.shield.config import SafeModeConfig, ShieldConfig
from veronica_core.shield.hooks import PreDispatchHook, RetryBoundaryHook

CTX = ToolCallContext(request_id="test", tool_name="bash")
CTX_NO_TOOL = ToolCallContext(request_id="test")


class TestSafeModeEnabled:
    """Enabled SafeModeHook blocks tool calls and retries."""

    def test_blocks_tool_call(self):
        hook = SafeModeHook(enabled=True)
        assert hook.before_llm_call(CTX) is Decision.HALT

    def test_allows_non_tool_call(self):
        hook = SafeModeHook(enabled=True)
        assert hook.before_llm_call(CTX_NO_TOOL) is None

    def test_blocks_empty_tool_name(self):
        ctx = ToolCallContext(request_id="test", tool_name="")
        hook = SafeModeHook(enabled=True)
        assert hook.before_llm_call(ctx) is Decision.HALT

    def test_blocks_retry(self):
        hook = SafeModeHook(enabled=True)
        assert hook.on_error(CTX, RuntimeError("boom")) is Decision.HALT


class TestSafeModeDisabled:
    """Disabled SafeModeHook returns None (no opinion)."""

    def test_tool_call_defers(self):
        hook = SafeModeHook(enabled=False)
        assert hook.before_llm_call(CTX) is None

    def test_retry_defers(self):
        hook = SafeModeHook(enabled=False)
        assert hook.on_error(CTX, RuntimeError("boom")) is None


class TestSafeModeProtocol:
    """SafeModeHook satisfies PreDispatchHook and RetryBoundaryHook."""

    def test_is_pre_dispatch(self):
        assert isinstance(SafeModeHook(), PreDispatchHook)

    def test_is_retry_boundary(self):
        assert isinstance(SafeModeHook(), RetryBoundaryHook)


class TestSafeModePipelineIntegration:
    """Pipeline wired with SafeModeHook behaves correctly."""

    def test_pipeline_with_safe_mode_blocks(self):
        hook = SafeModeHook(enabled=True)
        pipe = ShieldPipeline(pre_dispatch=hook, retry=hook)
        assert pipe.before_llm_call(CTX) is Decision.HALT
        assert pipe.on_error(CTX, ValueError("v")) is Decision.HALT

    def test_pipeline_without_safe_mode_allows(self):
        pipe = ShieldPipeline()
        assert pipe.before_llm_call(CTX) is Decision.ALLOW
        assert pipe.on_error(CTX, ValueError("v")) is Decision.ALLOW


class TestDefaultShieldConfigUnchanged:
    """Default ShieldConfig produces no behavioral change (non-breaking)."""

    def test_default_config_safe_mode_disabled(self):
        cfg = ShieldConfig()
        assert cfg.safe_mode.enabled is False

    def test_default_config_no_features_enabled(self):
        cfg = ShieldConfig()
        assert cfg.is_any_enabled is False


class TestIntegrationWiring:
    """VeronicaIntegration wires SafeModeHook only when enabled."""

    def test_safe_mode_disabled_no_hook(self):
        from veronica_core.integration import VeronicaIntegration

        vi = VeronicaIntegration(shield=ShieldConfig())
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW

    def test_safe_mode_enabled_blocks(self):
        from veronica_core.integration import VeronicaIntegration

        cfg = ShieldConfig(safe_mode=SafeModeConfig(enabled=True))
        vi = VeronicaIntegration(shield=cfg)
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.HALT
        assert vi._shield_pipeline.on_error(CTX, RuntimeError("x")) is Decision.HALT
