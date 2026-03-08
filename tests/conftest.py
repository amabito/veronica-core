"""Shared test fixtures for veronica-core test suite.

Common fixtures extracted from repeated patterns across 157 test files.
All fixtures are opt-in -- existing tests are not modified.
"""

from __future__ import annotations

import pytest

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook, DenyAllMemoryGovernanceHook
from veronica_core.memory.types import MemoryAction, MemoryOperation
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import VerificationResult


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


# ---------------------------------------------------------------------------
# Memory governance fixtures (Issue #53)
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_governor_allow() -> MemoryGovernor:
    """MemoryGovernor with fail_closed=False and a DefaultMemoryGovernanceHook."""
    gov = MemoryGovernor(fail_closed=False)
    gov.add_hook(DefaultMemoryGovernanceHook())
    return gov


@pytest.fixture
def memory_governor_deny() -> MemoryGovernor:
    """MemoryGovernor with fail_closed=True and a DenyAllMemoryGovernanceHook."""
    gov = MemoryGovernor(fail_closed=True)
    gov.add_hook(DenyAllMemoryGovernanceHook())
    return gov


@pytest.fixture
def memory_operation() -> MemoryOperation:
    """Default MemoryOperation with WRITE action and agent_id='test'."""
    return MemoryOperation(action=MemoryAction.WRITE, agent_id="test")


def _make_valid_bundle(policy_id: str = "test-policy") -> PolicyBundle:
    """Build a content-hashed PolicyBundle suitable for FrozenPolicyView construction."""
    rule = PolicyRule(rule_id="r1", rule_type="budget")
    rules: tuple[PolicyRule, ...] = (rule,)
    # Build bundle first, then derive content_hash via public API.
    bundle = PolicyBundle(
        metadata=PolicyMetadata(policy_id=policy_id),
        rules=rules,
    )
    computed_hash = bundle.content_hash()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=computed_hash)
    return PolicyBundle(metadata=meta, rules=rules)


@pytest.fixture
def frozen_policy_view() -> FrozenPolicyView:
    """A valid FrozenPolicyView wrapping a single budget rule."""
    bundle = _make_valid_bundle()
    result = VerificationResult(valid=True, errors=(), warnings=())
    return FrozenPolicyView(bundle, result)


@pytest.fixture
def policy_view_holder(frozen_policy_view: FrozenPolicyView) -> PolicyViewHolder:
    """PolicyViewHolder initialised with the frozen_policy_view fixture."""
    return PolicyViewHolder(initial=frozen_policy_view)
