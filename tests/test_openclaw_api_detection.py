"""Test OpenClaw API auto-detection.

Note: The adapter currently only supports .execute() API.
Tests for .run(), callable, and unsupported API detection are marked xfail
as forward-looking tests for a planned multi-API detection feature.
"""

import pytest
from integrations.openclaw.adapter import SafeOpenClawExecutor
from veronica_core.state import VeronicaState

_XFAIL_REASON = "OpenClaw multi-API detection not yet implemented (v0.2.x)"


class StrategyWithExecute:
    """Mock strategy with .execute() API."""

    def execute(self, context):
        return {"method": "execute", "data": context}


class StrategyWithRun:
    """Mock strategy with .run() API."""

    def run(self, context):
        return {"method": "run", "data": context}


class CallableStrategy:
    """Mock callable strategy."""

    def __call__(self, context):
        return {"method": "callable", "data": context}


class UnsupportedStrategy:
    """Mock strategy with unsupported API."""

    def decide(self, context):
        return {"method": "decide", "data": context}


def test_execute_api_detection():
    """Test .execute() API is detected."""
    executor = SafeOpenClawExecutor(StrategyWithExecute())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "success"
    assert result["data"]["method"] == "execute"
    assert result["data"]["data"] == {"test": "data"}


@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)
def test_run_api_detection():
    """Test .run() API is detected."""
    executor = SafeOpenClawExecutor(StrategyWithRun())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "success"
    assert result["data"]["method"] == "run"
    assert result["data"]["data"] == {"test": "data"}


@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)
def test_callable_api_detection():
    """Test callable pattern is detected."""
    executor = SafeOpenClawExecutor(CallableStrategy())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "success"
    assert result["data"]["method"] == "callable"
    assert result["data"]["data"] == {"test": "data"}


@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)
def test_unsupported_api_error():
    """Test unsupported API raises clear error."""
    executor = SafeOpenClawExecutor(UnsupportedStrategy())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "failed"
    assert "OpenClaw API not recognized" in result["reason"]
    assert ".execute(context)" in result["reason"]
    assert ".run(context)" in result["reason"]
    assert "callable" in result["reason"]
    assert "README.md" in result["reason"]


def test_api_priority_execute_over_run():
    """Test .execute() takes priority over .run()."""

    class StrategyWithBoth:
        def execute(self, context):
            return {"method": "execute"}

        def run(self, context):
            return {"method": "run"}

    executor = SafeOpenClawExecutor(StrategyWithBoth())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "success"
    assert result["data"]["method"] == "execute"


@pytest.mark.xfail(strict=False, reason=_XFAIL_REASON)
def test_api_priority_run_over_callable():
    """Test .run() takes priority over callable."""

    class StrategyWithRunAndCallable:
        def run(self, context):
            return {"method": "run"}

        def __call__(self, context):
            return {"method": "callable"}

    executor = SafeOpenClawExecutor(StrategyWithRunAndCallable())
    # Clear SAFE_MODE if persisted from other tests
    if executor.veronica.state.current_state == VeronicaState.SAFE_MODE:
        executor.clear_safe_mode("Test setup")

    result = executor.safe_execute({"test": "data"})

    assert result["status"] == "success"
    assert result["data"]["method"] == "run"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

