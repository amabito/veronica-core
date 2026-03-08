"""Shared test fixtures for veronica-core test suite.

Common fixtures extracted from repeated patterns across 157 test files.
All fixtures are opt-in -- existing tests are not modified.
"""

from __future__ import annotations

import pytest

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions


@pytest.fixture
def default_config() -> ExecutionConfig:
    """Standard ExecutionConfig for testing."""
    return ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=100,
        max_retries_total=5,
        timeout_ms=0,
    )


@pytest.fixture
def ctx(default_config: ExecutionConfig):
    """ExecutionContext with default config, auto-closed after test."""
    context = ExecutionContext(config=default_config)
    yield context
    try:
        context.close()
    except Exception:
        pass


@pytest.fixture
def strict_config() -> ExecutionConfig:
    """Strict ExecutionConfig (low limits for testing enforcement)."""
    return ExecutionConfig(
        max_cost_usd=0.10,
        max_steps=5,
        max_retries_total=2,
        timeout_ms=5000,
    )


@pytest.fixture
def strict_ctx(strict_config: ExecutionConfig):
    """ExecutionContext with strict limits, auto-closed after test."""
    context = ExecutionContext(config=strict_config)
    yield context
    try:
        context.close()
    except Exception:
        pass


@pytest.fixture
def wrap_options() -> WrapOptions:
    """Default WrapOptions for testing."""
    return WrapOptions(
        operation_name="test_op",
        cost_estimate_hint=0.01,
    )
