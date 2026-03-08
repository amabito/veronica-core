"""Integration tests for v3.3 policy metadata -> audit event wiring.

Tests:
1.  _ChainEventLog.emit_chain_event with policy_metadata -- appears in SafetyEvent.metadata
2.  _ChainEventLog.emit_chain_event without policy_metadata -- metadata is empty dict
3.  ExecutionContext with PolicyViewHolder -- get_snapshot() has policy_metadata
4.  ExecutionContext without PolicyViewHolder -- get_snapshot() has policy_metadata=None
5.  ExecutionContext emitting chain event enriches with policy metadata
6.  PolicyViewHolder.current is None -- _get_policy_audit_metadata returns None
"""

from __future__ import annotations


from veronica_core.containment._chain_event_log import _ChainEventLog
from veronica_core.containment import ExecutionConfig, ExecutionContext
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import PolicyVerifier
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(max_cost: float = 10.0, max_steps: int = 50, max_retries: int = 10) -> ExecutionConfig:
    return ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=max_steps,
        max_retries_total=max_retries,
    )


def _make_verified_bundle(policy_id: str = "test-policy") -> tuple[PolicyBundle, FrozenPolicyView]:
    """Build a minimal valid PolicyBundle and its FrozenPolicyView."""
    bundle = PolicyBundle(
        metadata=PolicyMetadata(
            policy_id=policy_id,
            version="1.0.0",
            issuer="test-issuer",
        ),
        rules=(
            PolicyRule(rule_id="r1", rule_type="budget"),
        ),
    )
    result = PolicyVerifier().verify(bundle)
    view = FrozenPolicyView(bundle, result)
    return bundle, view


def _make_holder_with_view(policy_id: str = "test-policy") -> PolicyViewHolder:
    """Build a PolicyViewHolder pre-loaded with a valid view."""
    _, view = _make_verified_bundle(policy_id)
    return PolicyViewHolder(initial=view)


# ---------------------------------------------------------------------------
# Category 1-2: _ChainEventLog.emit_chain_event + policy_metadata
# ---------------------------------------------------------------------------


def test_chain_event_log_emit_with_policy_metadata_stored_in_event():
    """emit_chain_event with policy_metadata stores it under 'policy' key."""
    log = _ChainEventLog()
    policy_meta = {"policy_id": "p1", "version": "1.0.0", "rule_count": 3}

    log.emit_chain_event(
        stop_reason="aborted",
        detail="test detail",
        request_id="req-001",
        policy_metadata=policy_meta,
    )

    events = log.snapshot()
    assert len(events) == 1
    assert events[0].metadata.get("policy") == policy_meta


def test_chain_event_log_emit_without_policy_metadata_has_empty_metadata():
    """emit_chain_event without policy_metadata produces empty metadata dict."""
    log = _ChainEventLog()

    log.emit_chain_event(
        stop_reason="aborted",
        detail="test detail",
        request_id="req-002",
        policy_metadata=None,
    )

    events = log.snapshot()
    assert len(events) == 1
    # metadata must be empty dict, not None, when no policy_metadata provided.
    assert events[0].metadata == {}


def test_chain_event_log_emit_policy_metadata_none_explicit_omits_policy_key():
    """Passing policy_metadata=None must not inject 'policy' key into metadata."""
    log = _ChainEventLog()

    log.emit_chain_event(
        stop_reason="budget_exceeded",
        detail="over budget",
        request_id="req-003",
        policy_metadata=None,
    )

    events = log.snapshot()
    assert "policy" not in events[0].metadata


def test_chain_event_log_emit_with_empty_dict_policy_metadata():
    """policy_metadata={} is distinct from None -- stored under 'policy' key."""
    log = _ChainEventLog()

    log.emit_chain_event(
        stop_reason="aborted",
        detail="detail",
        request_id="req-004",
        policy_metadata={},
    )

    events = log.snapshot()
    assert events[0].metadata.get("policy") == {}


# ---------------------------------------------------------------------------
# Category 3-4: ExecutionContext get_snapshot() policy_metadata
# ---------------------------------------------------------------------------


def test_execution_context_with_policy_view_holder_snapshot_has_policy_metadata():
    """get_snapshot() includes policy_metadata when a PolicyViewHolder is set."""
    holder = _make_holder_with_view("snap-policy")
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    snap = ctx.get_snapshot()

    assert snap.policy_metadata is not None
    assert snap.policy_metadata["policy_id"] == "snap-policy"
    assert "version" in snap.policy_metadata
    assert "rule_count" in snap.policy_metadata


def test_execution_context_without_policy_view_holder_snapshot_has_none():
    """get_snapshot() policy_metadata is None when no PolicyViewHolder is set."""
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=None)

    snap = ctx.get_snapshot()

    assert snap.policy_metadata is None


def test_execution_context_snapshot_policy_metadata_contains_audit_fields():
    """policy_metadata in snapshot includes issuer and is_signed audit fields."""
    holder = _make_holder_with_view("audit-policy")
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    snap = ctx.get_snapshot()

    assert snap.policy_metadata is not None
    assert "issuer" in snap.policy_metadata
    assert "is_signed" in snap.policy_metadata
    assert "rule_types" in snap.policy_metadata


# ---------------------------------------------------------------------------
# Category 5: chain events emitted by ExecutionContext are enriched
# ---------------------------------------------------------------------------


def test_execution_context_abort_emits_event_with_policy_metadata():
    """abort() now routes through _emit_chain_event() -- event has policy metadata.

    All chain events are enriched with policy metadata (v3.3 unification).
    """
    holder = _make_holder_with_view("abort-policy")
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    ctx.abort("test reason")

    snap = ctx.get_snapshot()
    aborted_events = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
    assert len(aborted_events) == 1
    assert "policy" in aborted_events[0].metadata
    assert aborted_events[0].metadata["policy"]["policy_id"] == "abort-policy"


def test_execution_context_abort_without_holder_emits_event_with_empty_metadata():
    """abort() without PolicyViewHolder emits event with empty metadata dict."""
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=None)

    ctx.abort("no holder")

    snap = ctx.get_snapshot()
    aborted_events = [e for e in snap.events if e.event_type == "CHAIN_ABORTED"]
    assert len(aborted_events) == 1
    assert "policy" not in aborted_events[0].metadata


def test_execution_context_memory_governance_denied_event_enriched_with_policy_metadata():
    """Memory governance deny path uses _emit_chain_event() -- event has policy metadata."""
    from veronica_core.memory.governor import MemoryGovernor
    from veronica_core.memory.hooks import DenyAllMemoryGovernanceHook

    holder = _make_holder_with_view("mem-gov-policy")
    gov = MemoryGovernor(fail_closed=True)
    gov.add_hook(DenyAllMemoryGovernanceHook())

    ctx = ExecutionContext(
        config=_cfg(),
        policy_view_holder=holder,
        memory_governor=gov,
    )

    # Memory governance denies -- the denial event should carry policy metadata.
    decision = ctx.wrap_llm_call(fn=lambda: None)

    assert decision == Decision.HALT
    snap = ctx.get_snapshot()
    mg_events = [
        e for e in snap.events if "MEMORY" in e.event_type or "memory" in e.event_type.lower()
    ]
    assert len(mg_events) >= 1, "Expected at least one memory governance event"
    # The event emitted via _emit_chain_event() must carry policy_metadata.
    assert mg_events[0].metadata.get("policy") is not None
    assert mg_events[0].metadata["policy"]["policy_id"] == "mem-gov-policy"


# ---------------------------------------------------------------------------
# Category 6: PolicyViewHolder.current is None
# ---------------------------------------------------------------------------


def test_policy_view_holder_current_none_returns_none_for_audit_metadata():
    """_get_policy_audit_metadata returns None when holder has no view loaded."""
    holder = PolicyViewHolder(initial=None)
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    snap = ctx.get_snapshot()

    assert snap.policy_metadata is None


def test_policy_view_holder_swap_to_none_returns_none_for_audit_metadata():
    """After swapping holder's view to None, audit metadata becomes None."""
    holder = _make_holder_with_view("to-clear")
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    # Confirm initial metadata present.
    snap_before = ctx.get_snapshot()
    assert snap_before.policy_metadata is not None

    # Swap out the view.
    holder.swap(None)

    snap_after = ctx.get_snapshot()
    assert snap_after.policy_metadata is None


def test_policy_view_holder_swap_new_view_updates_snapshot_metadata():
    """Swapping in a new view updates the audit metadata in the next snapshot."""
    holder = _make_holder_with_view("original-policy")
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=holder)

    snap_before = ctx.get_snapshot()
    assert snap_before.policy_metadata["policy_id"] == "original-policy"

    # Install a different policy view.
    _, new_view = _make_verified_bundle("replacement-policy")
    holder.swap(new_view)

    snap_after = ctx.get_snapshot()
    assert snap_after.policy_metadata["policy_id"] == "replacement-policy"


def test_execution_context_no_holder_abort_event_has_no_policy_key():
    """When no PolicyViewHolder is set, abort events lack the 'policy' key."""
    ctx = ExecutionContext(config=_cfg(), policy_view_holder=None)
    ctx.abort("no policy holder")

    snap = ctx.get_snapshot()
    for event in snap.events:
        assert "policy" not in event.metadata
