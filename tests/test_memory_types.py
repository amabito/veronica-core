"""Tests for memory governance types.

Covers: MemoryAction, MemoryProvenance, MemoryOperation,
        MemoryPolicyContext, GovernanceVerdict, MemoryGovernanceDecision.
"""

from __future__ import annotations

import pytest

from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
)


class TestMemoryActionValues:
    def test_memory_action_values(self) -> None:
        """All seven MemoryAction members must be present."""
        expected = {
            "read",
            "write",
            "retrieve",
            "archive",
            "consolidate",
            "delete",
            "quarantine",
        }
        actual = {a.value for a in MemoryAction}
        assert actual == expected

    def test_memory_action_str_mixin(self) -> None:
        """MemoryAction must compare equal to its string value (str mixin)."""
        assert MemoryAction.READ == "read"
        assert MemoryAction.WRITE == "write"
        assert MemoryAction.QUARANTINE == "quarantine"


class TestMemoryProvenanceValues:
    def test_memory_provenance_values(self) -> None:
        """All four MemoryProvenance members must be present."""
        expected = {"verified", "unverified", "quarantined", "unknown"}
        actual = {p.value for p in MemoryProvenance}
        assert actual == expected

    def test_memory_provenance_str_mixin(self) -> None:
        """MemoryProvenance must compare equal to its string value."""
        assert MemoryProvenance.VERIFIED == "verified"
        assert MemoryProvenance.UNKNOWN == "unknown"


class TestMemoryOperationCreation:
    def test_memory_operation_creation(self) -> None:
        """MemoryOperation should create successfully with only action."""
        op = MemoryOperation(action=MemoryAction.READ)
        assert op.action is MemoryAction.READ
        assert op.resource_id == ""
        assert op.agent_id == ""
        assert op.namespace == ""
        assert op.content_hash == ""
        assert op.content_size_bytes == 0
        assert op.provenance is MemoryProvenance.UNKNOWN
        from collections.abc import Mapping

        assert isinstance(op.metadata, Mapping)
        assert op.timestamp > 0

    def test_memory_operation_all_fields(self) -> None:
        """MemoryOperation should accept all fields correctly."""
        op = MemoryOperation(
            action=MemoryAction.WRITE,
            resource_id="mem-001",
            agent_id="agent-42",
            namespace="episodic",
            content_hash="abc123",
            content_size_bytes=512,
            provenance=MemoryProvenance.VERIFIED,
            metadata={"source": "test"},
        )
        assert op.resource_id == "mem-001"
        assert op.namespace == "episodic"
        assert op.content_size_bytes == 512

    def test_memory_operation_invalid_action_type(self) -> None:
        """MemoryOperation must reject non-MemoryAction values for action."""
        with pytest.raises(TypeError, match="MemoryAction"):
            MemoryOperation(action="write")  # type: ignore[arg-type]

    def test_memory_operation_negative_size_rejected(self) -> None:
        """MemoryOperation must reject negative content_size_bytes."""
        with pytest.raises(ValueError, match="content_size_bytes"):
            MemoryOperation(action=MemoryAction.WRITE, content_size_bytes=-1)

    def test_memory_operation_zero_size_allowed(self) -> None:
        """Zero content_size_bytes is valid (e.g. READ operations)."""
        op = MemoryOperation(action=MemoryAction.READ, content_size_bytes=0)
        assert op.content_size_bytes == 0

    def test_memory_operation_is_frozen(self) -> None:
        """MemoryOperation must be immutable (frozen dataclass)."""
        op = MemoryOperation(action=MemoryAction.DELETE)
        with pytest.raises((AttributeError, TypeError)):
            op.resource_id = "changed"  # type: ignore[misc]


class TestMemoryPolicyContextDefaults:
    def test_memory_policy_context_defaults(self) -> None:
        """MemoryPolicyContext must set sensible defaults for all optional fields."""
        op = MemoryOperation(action=MemoryAction.READ)
        ctx = MemoryPolicyContext(operation=op)
        assert ctx.chain_id == ""
        assert ctx.request_id == ""
        assert ctx.trust_level == ""
        assert ctx.total_memory_ops_in_chain == 0
        assert ctx.total_bytes_written_in_chain == 0
        assert ctx.active_quarantine_count == 0

    def test_memory_policy_context_all_fields(self) -> None:
        """MemoryPolicyContext should store all provided fields."""
        op = MemoryOperation(action=MemoryAction.WRITE)
        ctx = MemoryPolicyContext(
            operation=op,
            chain_id="chain-1",
            request_id="req-99",
            trust_level="trusted",
            total_memory_ops_in_chain=5,
            total_bytes_written_in_chain=1024,
            active_quarantine_count=2,
        )
        assert ctx.chain_id == "chain-1"
        assert ctx.total_bytes_written_in_chain == 1024


class TestGovernanceVerdictValues:
    def test_governance_verdict_values(self) -> None:
        """All four GovernanceVerdict members must be present."""
        expected = {"allow", "deny", "quarantine", "degrade"}
        actual = {v.value for v in GovernanceVerdict}
        assert actual == expected

    def test_governance_verdict_str_mixin(self) -> None:
        """GovernanceVerdict must compare equal to its string value."""
        assert GovernanceVerdict.ALLOW == "allow"
        assert GovernanceVerdict.DENY == "deny"


class TestMemoryGovernanceDecision:
    def test_governance_decision_allowed_verdicts(self) -> None:
        """ALLOW, QUARANTINE, and DEGRADE verdicts must set allowed=True."""
        for verdict in (
            GovernanceVerdict.ALLOW,
            GovernanceVerdict.QUARANTINE,
            GovernanceVerdict.DEGRADE,
        ):
            decision = MemoryGovernanceDecision(verdict=verdict)
            assert decision.allowed is True, f"Expected allowed=True for {verdict}"
            assert decision.denied is False

    def test_governance_decision_denied(self) -> None:
        """DENY verdict must set denied=True and allowed=False."""
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.DENY)
        assert decision.denied is True
        assert decision.allowed is False

    def test_governance_decision_to_audit_dict(self) -> None:
        """to_audit_dict() must include verdict, reason, policy_id and operation fields."""
        op = MemoryOperation(
            action=MemoryAction.WRITE,
            resource_id="res-1",
            agent_id="agent-1",
            namespace="semantic",
            provenance=MemoryProvenance.VERIFIED,
            content_size_bytes=256,
        )
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="approved",
            policy_id="allow_writes",
            operation=op,
            audit_metadata={"extra": "value"},
        )
        d = decision.to_audit_dict()
        assert d["verdict"] == "allow"
        assert d["reason"] == "approved"
        assert d["policy_id"] == "allow_writes"
        assert d["operation_action"] == "write"
        assert d["operation_resource_id"] == "res-1"
        assert d["operation_agent_id"] == "agent-1"
        assert d["operation_namespace"] == "semantic"
        assert d["operation_provenance"] == "verified"
        assert d["operation_content_size_bytes"] == 256
        assert d["extra"] == "value"

    def test_governance_decision_to_audit_dict_without_operation(self) -> None:
        """to_audit_dict() must not include operation keys when operation is None."""
        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="no op",
            policy_id="test",
        )
        d = decision.to_audit_dict()
        assert d["verdict"] == "deny"
        assert "operation_action" not in d
        assert "operation_resource_id" not in d

    def test_governance_decision_is_frozen(self) -> None:
        """MemoryGovernanceDecision must be immutable."""
        decision = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)
        with pytest.raises((AttributeError, TypeError)):
            decision.verdict = GovernanceVerdict.DENY  # type: ignore[misc]
