"""Tests for ShieldPipeline and integration wiring (PR-1D)."""

from veronica_core.shield import (
    Decision,
    ShieldBlockedError,
    ShieldPipeline,
    ToolCallContext,
)
from veronica_core.shield.noop import (
    NoopBudgetBoundaryHook,
    NoopEgressBoundaryHook,
    NoopPreDispatchHook,
    NoopRetryBoundaryHook,
)

CTX = ToolCallContext(request_id="test")


class TestPipelineDefaultsAllow:
    """Pipeline with no hooks always returns ALLOW."""

    def test_before_llm_call(self):
        assert ShieldPipeline().before_llm_call(CTX) is Decision.ALLOW

    def test_before_egress(self):
        assert ShieldPipeline().before_egress(CTX, "https://x", "GET") is Decision.ALLOW

    def test_on_error(self):
        assert ShieldPipeline().on_error(CTX, RuntimeError("boom")) is Decision.ALLOW

    def test_before_charge(self):
        assert ShieldPipeline().before_charge(CTX, 0.01) is Decision.ALLOW


class TestPipelineNoopHooksAllow:
    """Pipeline with explicit noop hooks still returns ALLOW."""

    def test_all_noop(self):
        pipe = ShieldPipeline(
            pre_dispatch=NoopPreDispatchHook(),
            egress=NoopEgressBoundaryHook(),
            retry=NoopRetryBoundaryHook(),
            budget=NoopBudgetBoundaryHook(),
        )
        assert pipe.before_llm_call(CTX) is Decision.ALLOW
        assert pipe.before_egress(CTX, "https://x", "POST") is Decision.ALLOW
        assert pipe.on_error(CTX, ValueError("v")) is Decision.ALLOW
        assert pipe.before_charge(CTX, 1.0) is Decision.ALLOW


class TestIntegrationShieldWiring:
    """VeronicaIntegration creates pipeline only when shield is provided."""

    def test_shield_none_no_pipeline(self):
        from veronica_core.integration import VeronicaIntegration

        vi = VeronicaIntegration(shield=None)
        assert vi._shield_pipeline is None

    def test_shield_present_creates_pipeline(self):
        from veronica_core.integration import VeronicaIntegration
        from veronica_core.shield.config import ShieldConfig

        vi = VeronicaIntegration(shield=ShieldConfig())
        assert isinstance(vi._shield_pipeline, ShieldPipeline)
        # Pipeline with no hooks -> ALLOW
        assert vi._shield_pipeline.before_llm_call(CTX) is Decision.ALLOW


class TestShieldBlockedError:
    """ShieldBlockedError carries decision and reason."""

    def test_attributes(self):
        err = ShieldBlockedError(Decision.HALT, "budget exceeded", ctx=CTX)
        assert err.decision is Decision.HALT
        assert err.reason == "budget exceeded"
        assert err.ctx is CTX
        assert "HALT" in str(err)

    def test_no_ctx(self):
        err = ShieldBlockedError(Decision.QUARANTINE, "unsafe")
        assert err.ctx is None
        assert "QUARANTINE" in str(err)
