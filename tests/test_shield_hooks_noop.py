"""Tests for shield hook protocols and noop implementations (PR-1C)."""

from veronica_core.shield import ToolCallContext
from veronica_core.shield.hooks import (
    BudgetBoundaryHook,
    EgressBoundaryHook,
    PreDispatchHook,
    RetryBoundaryHook,
)
from veronica_core.shield.noop import (
    NoopBudgetBoundaryHook,
    NoopEgressBoundaryHook,
    NoopPreDispatchHook,
    NoopRetryBoundaryHook,
)

CTX = ToolCallContext(request_id="test")


class TestNoopHooksReturnNone:
    """Every noop hook method returns None."""

    def test_pre_dispatch(self):
        assert NoopPreDispatchHook().before_llm_call(CTX) is None

    def test_egress(self):
        assert NoopEgressBoundaryHook().before_egress(CTX, "https://x", "GET") is None

    def test_retry(self):
        assert NoopRetryBoundaryHook().on_error(CTX, RuntimeError("boom")) is None

    def test_budget(self):
        assert NoopBudgetBoundaryHook().before_charge(CTX, 0.01) is None


class TestProtocolConformance:
    """Noop classes satisfy their respective Protocols."""

    def test_pre_dispatch(self):
        assert isinstance(NoopPreDispatchHook(), PreDispatchHook)

    def test_egress(self):
        assert isinstance(NoopEgressBoundaryHook(), EgressBoundaryHook)

    def test_retry(self):
        assert isinstance(NoopRetryBoundaryHook(), RetryBoundaryHook)

    def test_budget(self):
        assert isinstance(NoopBudgetBoundaryHook(), BudgetBoundaryHook)
