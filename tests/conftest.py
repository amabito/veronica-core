"""Shared test fixtures for veronica-core test suite.

Common fixtures extracted from repeated patterns across 157 test files.
All fixtures are opt-in -- existing tests are not modified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import DefaultMemoryGovernanceHook, DenyAllMemoryGovernanceHook
from veronica_core.memory.types import MemoryAction, MemoryOperation
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import VerificationResult
from veronica_core.security.policy_signing import PolicySigner


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


# ---------------------------------------------------------------------------
# Kernel test helpers (shared across kernel/startup, audit_bridge, signing tests)
# ---------------------------------------------------------------------------


def make_test_signer(key_bytes: bytes = b"test-key") -> PolicySigner:
    """Return a PolicySigner whose HMAC key is SHA-256(key_bytes).

    Pass the raw seed bytes; SHA-256 is applied here to produce a 32-byte key.
    Each test file that needs a distinct default key passes its own seed and
    stores the result in a module-level constant.
    """
    return PolicySigner(key=hashlib.sha256(key_bytes).digest())


def make_test_audit_log(
    tmp_path: Path,
    filename: str = "audit.jsonl",
    signer: PolicySigner | None = None,
) -> AuditLog:
    """Return an AuditLog rooted at tmp_path/filename.

    Parameters
    ----------
    tmp_path:
        pytest ``tmp_path`` fixture value -- a unique per-test directory.
    filename:
        Name of the JSONL file within ``tmp_path`` (default: ``audit.jsonl``).
    signer:
        Optional PolicySigner for HMAC-signed entries.
    """
    return AuditLog(path=tmp_path / filename, signer=signer)


def read_jsonl(audit_log: AuditLog) -> list[dict[str, Any]]:
    """Return all parsed JSONL entries from an AuditLog's backing file.

    Returns an empty list when the file does not yet exist.
    """
    if not audit_log._path.exists():
        return []
    entries = []
    with audit_log._path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


def make_test_bundle(
    rules: tuple[PolicyRule, ...] = (),
    signature: str = "",
    with_content_hash: bool = True,
    policy_id: str = "test-policy",
) -> PolicyBundle:
    """Build a PolicyBundle, optionally with a correct content_hash.

    Parameters
    ----------
    rules:
        Tuple of PolicyRule objects to include.
    signature:
        Signature string (empty string = unsigned bundle).
    with_content_hash:
        When True (default) a correct content_hash is computed and embedded.
    policy_id:
        The policy_id for the bundle's metadata.
    """
    tmp = PolicyBundle(
        metadata=PolicyMetadata(policy_id=policy_id),
        rules=rules,
    )
    content_hash = tmp.content_hash() if with_content_hash else ""
    return PolicyBundle(
        metadata=PolicyMetadata(
            policy_id=policy_id,
            content_hash=content_hash,
        ),
        rules=rules,
        signature=signature,
    )


# ---------------------------------------------------------------------------
# nogil-tolerant test helpers
# ---------------------------------------------------------------------------


def wait_for(
    predicate: Any,
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
    msg: str = "",
) -> None:
    """Poll *predicate* until it returns truthy, or raise AssertionError.

    Use this instead of ``time.sleep(X); assert condition`` to make tests
    tolerant of free-threaded Python (3.13t nogil) where thread scheduling
    is less predictable.

    Parameters
    ----------
    predicate:
        Callable returning a truthy value when the condition is met.
    timeout:
        Maximum seconds to wait (default 2.0).
    interval:
        Seconds between polls (default 0.01).
    msg:
        Optional message for the AssertionError.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    # Final check
    if not predicate():
        raise AssertionError(msg or f"Condition not met within {timeout}s")


def make_signed_bundle(
    signer: PolicySigner,
    rules: tuple[PolicyRule, ...] = (),
    policy_id: str = "test-policy",
) -> PolicyBundle:
    """Create a PolicyBundle and sign it with the given signer.

    Parameters
    ----------
    signer:
        PolicySigner used to produce the HMAC signature.
    rules:
        Tuple of PolicyRule objects to include (default: empty).
    policy_id:
        The policy_id for the bundle's metadata (default: ``"test-policy"``).
    """
    unsigned = make_test_bundle(rules=rules, policy_id=policy_id)
    sig = signer.sign_bundle(unsigned)
    return PolicyBundle(
        metadata=unsigned.metadata,
        rules=unsigned.rules,
        signature=sig,
    )
