"""Tests for veronica_core.kernel.decision -- DecisionEnvelope and helpers.

Coverage:
- DecisionEnvelope creation and field defaults
- frozen immutability
- .allowed / .denied for all 7 decision values
- to_audit_dict() completeness
- make_envelope() uniqueness (100 calls)
- ReasonCode enum accessibility
- Validation: empty audit_id, unknown decision, metadata frozen
- Adversarial: None metadata, empty strings, dict mutation attempt
- Integration: PolicyDecision.envelope field
- Integration: SafetyEvent.envelope field
- Integration: MemoryGovernanceDecision.envelope + to_audit_dict() merge
"""

from __future__ import annotations

import time
import types
import uuid

import pytest

from veronica_core.kernel.decision import DecisionEnvelope, ReasonCode, make_envelope
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
)
from veronica_core.runtime_policy import PolicyDecision
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(**kwargs: object) -> DecisionEnvelope:
    """Create a minimal valid DecisionEnvelope for testing."""
    defaults: dict = {
        "decision": "ALLOW",
        "policy_hash": "abc123",
        "reason_code": ReasonCode.UNKNOWN.value,
        "reason": "test reason",
        "audit_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "policy_epoch": 0,
        "issuer": "TestComponent",
        "metadata": {},
    }
    defaults.update(kwargs)
    return DecisionEnvelope(**defaults)


# ---------------------------------------------------------------------------
# DecisionEnvelope -- creation
# ---------------------------------------------------------------------------


class TestDecisionEnvelopeCreation:
    def test_all_fields_stored(self) -> None:
        audit_id = str(uuid.uuid4())
        ts = time.time()
        env = DecisionEnvelope(
            decision="DENY",
            policy_hash="deadbeef",
            reason_code="BUDGET_EXCEEDED",
            reason="over budget",
            audit_id=audit_id,
            timestamp=ts,
            policy_epoch=7,
            issuer="BudgetEnforcer",
            metadata={"cost_usd": 1.5},
        )
        assert env.decision == "DENY"
        assert env.policy_hash == "deadbeef"
        assert env.reason_code == "BUDGET_EXCEEDED"
        assert env.reason == "over budget"
        assert env.audit_id == audit_id
        assert env.timestamp == ts
        assert env.policy_epoch == 7
        assert env.issuer == "BudgetEnforcer"
        assert env.metadata["cost_usd"] == 1.5

    def test_empty_metadata_default(self) -> None:
        env = _make_envelope()
        assert len(env.metadata) == 0

    def test_metadata_is_mapping_proxy(self) -> None:
        env = _make_envelope(metadata={"key": "value"})
        assert isinstance(env.metadata, types.MappingProxyType)

    def test_metadata_from_empty_dict_is_proxy(self) -> None:
        env = _make_envelope(metadata={})
        assert isinstance(env.metadata, types.MappingProxyType)


# ---------------------------------------------------------------------------
# DecisionEnvelope -- frozen immutability
# ---------------------------------------------------------------------------


class TestDecisionEnvelopeFrozen:
    def test_cannot_set_decision(self) -> None:
        env = _make_envelope()
        with pytest.raises((AttributeError, TypeError)):
            env.decision = "DENY"  # type: ignore[misc]

    def test_cannot_set_audit_id(self) -> None:
        env = _make_envelope()
        with pytest.raises((AttributeError, TypeError)):
            env.audit_id = "new-id"  # type: ignore[misc]

    def test_cannot_set_metadata(self) -> None:
        env = _make_envelope()
        with pytest.raises((AttributeError, TypeError)):
            env.metadata = {}  # type: ignore[misc]

    def test_metadata_mutation_attempt_raises(self) -> None:
        env = _make_envelope(metadata={"x": 1})
        with pytest.raises(TypeError):
            env.metadata["x"] = 99  # type: ignore[index]


# ---------------------------------------------------------------------------
# DecisionEnvelope -- .allowed and .denied properties
# ---------------------------------------------------------------------------


class TestDecisionEnvelopeProperties:
    @pytest.mark.parametrize(
        "decision, expected_allowed, expected_denied",
        [
            ("ALLOW", True, False),
            ("DENY", False, True),
            ("HALT", False, True),
            ("DEGRADE", True, False),
            ("QUARANTINE", True, False),
            ("RETRY", True, False),
            ("QUEUE", True, False),
        ],
    )
    def test_allowed_denied(
        self, decision: str, expected_allowed: bool, expected_denied: bool
    ) -> None:
        env = _make_envelope(decision=decision)
        assert env.allowed is expected_allowed
        assert env.denied is expected_denied


# ---------------------------------------------------------------------------
# DecisionEnvelope -- to_audit_dict()
# ---------------------------------------------------------------------------


class TestDecisionEnvelopeToAuditDict:
    def test_includes_all_core_keys(self) -> None:
        env = _make_envelope(
            decision="ALLOW",
            policy_hash="ph",
            reason_code="UNKNOWN",
            reason="ok",
            policy_epoch=3,
            issuer="TestIssuer",
        )
        d = env.to_audit_dict()
        assert "decision" in d
        assert "policy_hash" in d
        assert "reason_code" in d
        assert "reason" in d
        assert "audit_id" in d
        assert "timestamp" in d
        assert "policy_epoch" in d
        assert "issuer" in d

    def test_metadata_merged_inline(self) -> None:
        env = _make_envelope(metadata={"custom_field": "hello"})
        d = env.to_audit_dict()
        assert d["custom_field"] == "hello"

    def test_values_match_fields(self) -> None:
        audit_id = str(uuid.uuid4())
        ts = time.time()
        env = DecisionEnvelope(
            decision="HALT",
            policy_hash="ph123",
            reason_code="CIRCUIT_OPEN",
            reason="breaker open",
            audit_id=audit_id,
            timestamp=ts,
            policy_epoch=5,
            issuer="CircuitBreaker",
            metadata={},
        )
        d = env.to_audit_dict()
        assert d["decision"] == "HALT"
        assert d["policy_hash"] == "ph123"
        assert d["reason_code"] == "CIRCUIT_OPEN"
        assert d["audit_id"] == audit_id
        assert d["timestamp"] == ts
        assert d["policy_epoch"] == 5
        assert d["issuer"] == "CircuitBreaker"


# ---------------------------------------------------------------------------
# make_envelope() -- factory
# ---------------------------------------------------------------------------


class TestMakeEnvelope:
    def test_returns_decision_envelope(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="TestIssuer",
        )
        assert isinstance(env, DecisionEnvelope)

    def test_audit_id_is_non_empty_uuid(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="TestIssuer",
        )
        assert env.audit_id
        # Must parse as a valid UUID
        uuid.UUID(env.audit_id)

    def test_timestamp_is_recent(self) -> None:
        before = time.time()
        env = make_envelope(
            decision="DENY",
            reason_code=ReasonCode.BUDGET_EXCEEDED,
            reason="over limit",
            issuer="BudgetEnforcer",
        )
        after = time.time()
        assert before <= env.timestamp <= after

    def test_unique_audit_ids_100_calls(self) -> None:
        ids = {
            make_envelope(
                decision="ALLOW",
                reason_code=ReasonCode.UNKNOWN,
                reason="ok",
                issuer="X",
            ).audit_id
            for _ in range(100)
        }
        assert len(ids) == 100

    def test_reason_code_enum_converted_to_string(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.CIRCUIT_OPEN,
            reason="open",
            issuer="CB",
        )
        assert env.reason_code == "CIRCUIT_OPEN"
        assert isinstance(env.reason_code, str)

    def test_reason_code_raw_string_accepted(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code="CUSTOM_CODE",
            reason="custom",
            issuer="X",
        )
        assert env.reason_code == "CUSTOM_CODE"

    def test_policy_hash_default_empty(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="X",
        )
        assert env.policy_hash == ""

    def test_policy_epoch_default_zero(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="X",
        )
        assert env.policy_epoch == 0

    def test_metadata_none_becomes_empty_proxy(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="X",
            metadata=None,
        )
        assert isinstance(env.metadata, types.MappingProxyType)
        assert len(env.metadata) == 0

    def test_metadata_dict_passed_through(self) -> None:
        env = make_envelope(
            decision="ALLOW",
            reason_code=ReasonCode.UNKNOWN,
            reason="ok",
            issuer="X",
            metadata={"k": "v"},
        )
        assert env.metadata["k"] == "v"

    def test_all_optional_fields(self) -> None:
        env = make_envelope(
            decision="QUARANTINE",
            reason_code=ReasonCode.MEMORY_GOVERNANCE_QUARANTINE,
            reason="quarantined",
            issuer="MemoryGovernanceHook",
            policy_hash="sha256hex",
            policy_epoch=42,
            metadata={"ns": "user"},
        )
        assert env.decision == "QUARANTINE"
        assert env.policy_hash == "sha256hex"
        assert env.policy_epoch == 42
        assert env.metadata["ns"] == "user"


# ---------------------------------------------------------------------------
# ReasonCode enum
# ---------------------------------------------------------------------------


class TestReasonCode:
    def test_all_codes_accessible(self) -> None:
        expected = [
            "BUDGET_EXCEEDED",
            "STEP_LIMIT",
            "RETRY_BUDGET",
            "TIMEOUT",
            "CIRCUIT_OPEN",
            "MEMORY_GOVERNANCE_DENIED",
            "MEMORY_GOVERNANCE_QUARANTINE",
            "POLICY_UNSIGNED",
            "POLICY_HASH_MISMATCH",
            "POLICY_EPOCH_ROLLBACK",
            "ABORTED",
            "SAFE_MODE",
            "SHELL_BLOCKED",
            "NETWORK_BLOCKED",
            "FILE_BLOCKED",
            "TRUST_VIOLATION",
            "UNKNOWN",
        ]
        for name in expected:
            member = ReasonCode[name]
            assert member.value == name

    def test_is_str_enum(self) -> None:
        assert isinstance(ReasonCode.BUDGET_EXCEEDED, str)
        assert ReasonCode.BUDGET_EXCEEDED == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestDecisionEnvelopeValidation:
    def test_empty_audit_id_raises(self) -> None:
        with pytest.raises(ValueError, match="audit_id"):
            DecisionEnvelope(
                decision="ALLOW",
                policy_hash="",
                reason_code="UNKNOWN",
                reason="ok",
                audit_id="",  # invalid
                timestamp=time.time(),
                policy_epoch=0,
                issuer="X",
                metadata={},
            )

    def test_unknown_decision_raises(self) -> None:
        with pytest.raises(ValueError, match="decision"):
            DecisionEnvelope(
                decision="EXPLODE",  # not in known set
                policy_hash="",
                reason_code="UNKNOWN",
                reason="ok",
                audit_id=str(uuid.uuid4()),
                timestamp=time.time(),
                policy_epoch=0,
                issuer="X",
                metadata={},
            )

    def test_metadata_frozen_post_construction(self) -> None:
        original = {"mutable": True}
        env = DecisionEnvelope(
            decision="ALLOW",
            policy_hash="",
            reason_code="UNKNOWN",
            reason="ok",
            audit_id=str(uuid.uuid4()),
            timestamp=time.time(),
            policy_epoch=0,
            issuer="X",
            metadata=original,
        )
        # Mutating the original dict after construction must not affect envelope
        original["mutable"] = False
        assert env.metadata["mutable"] is True
        # Direct mutation of the proxy must fail
        with pytest.raises(TypeError):
            env.metadata["new_key"] = "bad"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialDecisionEnvelope:
    """Adversarial tests for DecisionEnvelope -- attacker mindset."""

    def test_none_metadata_via_make_envelope_safe(self) -> None:
        """make_envelope(metadata=None) must not crash and produce empty proxy."""
        env = make_envelope("ALLOW", ReasonCode.UNKNOWN, "ok", "X", metadata=None)
        assert isinstance(env.metadata, types.MappingProxyType)
        assert len(env.metadata) == 0

    def test_empty_string_fields_accepted(self) -> None:
        """Empty strings in non-validated string fields must not raise."""
        env = make_envelope(
            decision="ALLOW",
            reason_code="",
            reason="",
            issuer="",
            policy_hash="",
        )
        assert env.reason_code == ""
        assert env.reason == ""
        assert env.issuer == ""

    def test_dict_mutation_after_make_envelope_has_no_effect(self) -> None:
        """Mutating the dict passed to make_envelope must not affect the envelope."""
        d: dict = {"a": 1}
        env = make_envelope("ALLOW", ReasonCode.UNKNOWN, "ok", "X", metadata=d)
        d["a"] = 999
        assert env.metadata["a"] == 1

    def test_deeply_nested_metadata_value_stored_verbatim(self) -> None:
        """Nested values in metadata are stored as-is (shallow copy only)."""
        inner = {"nested": True}
        env = make_envelope(
            "ALLOW", ReasonCode.UNKNOWN, "ok", "X", metadata={"outer": inner}
        )
        assert env.metadata["outer"]["nested"] is True

    def test_large_policy_epoch_accepted(self) -> None:
        env = make_envelope("ALLOW", ReasonCode.UNKNOWN, "ok", "X", policy_epoch=2**31)
        assert env.policy_epoch == 2**31

    def test_negative_policy_epoch_accepted(self) -> None:
        """Negative epoch is not validated -- callers must ensure correctness."""
        env = make_envelope("ALLOW", ReasonCode.UNKNOWN, "ok", "X", policy_epoch=-1)
        assert env.policy_epoch == -1

    def test_all_decisions_round_trip_through_to_audit_dict(self) -> None:
        """Every known decision value survives a to_audit_dict() round trip."""
        decisions = ["ALLOW", "DENY", "HALT", "DEGRADE", "QUARANTINE", "RETRY", "QUEUE"]
        for decision in decisions:
            env = make_envelope(decision, ReasonCode.UNKNOWN, "ok", "X")
            d = env.to_audit_dict()
            assert d["decision"] == decision


# ---------------------------------------------------------------------------
# Integration: PolicyDecision
# ---------------------------------------------------------------------------


class TestPolicyDecisionIntegration:
    def test_envelope_field_defaults_none(self) -> None:
        pd = PolicyDecision(allowed=True, policy_type="budget")
        assert pd.envelope is None

    def test_envelope_field_accepts_decision_envelope(self) -> None:
        env = make_envelope("ALLOW", ReasonCode.BUDGET_EXCEEDED, "ok", "BudgetEnforcer")
        pd = PolicyDecision(allowed=True, policy_type="budget", envelope=env)
        assert pd.envelope is env
        assert pd.envelope.decision == "ALLOW"

    def test_envelope_field_with_deny(self) -> None:
        env = make_envelope(
            "DENY", ReasonCode.BUDGET_EXCEEDED, "over limit", "BudgetEnforcer"
        )
        pd = PolicyDecision(
            allowed=False, policy_type="budget", reason="over limit", envelope=env
        )
        assert pd.envelope is not None
        assert pd.envelope.denied is True


# ---------------------------------------------------------------------------
# Integration: SafetyEvent
# ---------------------------------------------------------------------------


class TestSafetyEventIntegration:
    def test_envelope_field_defaults_none(self) -> None:
        se = SafetyEvent(
            event_type="SAFE_MODE",
            decision=Decision.HALT,
            reason="safe mode active",
            hook="SafeModeHook",
        )
        assert se.envelope is None

    def test_envelope_field_accepts_decision_envelope(self) -> None:
        env = make_envelope("HALT", ReasonCode.SAFE_MODE, "safe mode", "SafeModeHook")
        se = SafetyEvent(
            event_type="SAFE_MODE",
            decision=Decision.HALT,
            reason="safe mode active",
            hook="SafeModeHook",
            envelope=env,
        )
        assert se.envelope is env
        assert se.envelope.reason_code == "SAFE_MODE"

    def test_safety_event_remains_frozen_with_envelope(self) -> None:
        env = make_envelope("HALT", ReasonCode.SAFE_MODE, "safe mode", "SafeModeHook")
        se = SafetyEvent(
            event_type="SAFE_MODE",
            decision=Decision.HALT,
            reason="safe mode active",
            hook="SafeModeHook",
            envelope=env,
        )
        with pytest.raises((AttributeError, TypeError)):
            se.envelope = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration: MemoryGovernanceDecision
# ---------------------------------------------------------------------------


class TestMemoryGovernanceDecisionIntegration:
    def test_envelope_field_defaults_none(self) -> None:
        mgd = MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)
        assert mgd.envelope is None

    def test_envelope_field_accepts_decision_envelope(self) -> None:
        env = make_envelope(
            "QUARANTINE",
            ReasonCode.MEMORY_GOVERNANCE_QUARANTINE,
            "flagged content",
            "MemoryGovernanceHook",
        )
        mgd = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.QUARANTINE,
            reason="flagged content",
            envelope=env,
        )
        assert mgd.envelope is env

    def test_to_audit_dict_merges_envelope_fields(self) -> None:
        env = make_envelope(
            "DENY",
            ReasonCode.MEMORY_GOVERNANCE_DENIED,
            "denied by policy",
            "MemoryGovernanceHook",
            policy_hash="ph_abc",
            policy_epoch=9,
        )
        mgd = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="denied by policy",
            policy_id="pol-1",
            envelope=env,
        )
        d = mgd.to_audit_dict()
        # Original memory governance fields must be present
        assert d["verdict"] == "deny"
        assert d["reason"] == "denied by policy"
        assert d["policy_id"] == "pol-1"
        # Envelope fields must be merged under envelope_ prefix
        assert d["envelope_decision"] == "DENY"
        assert d["envelope_policy_hash"] == "ph_abc"
        assert d["envelope_policy_epoch"] == 9
        assert d["envelope_issuer"] == "MemoryGovernanceHook"
        assert d["envelope_reason_code"] == "MEMORY_GOVERNANCE_DENIED"
        assert "envelope_audit_id" in d
        assert "envelope_timestamp" in d

    def test_to_audit_dict_no_envelope_no_prefix_keys(self) -> None:
        mgd = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="ok",
        )
        d = mgd.to_audit_dict()
        assert not any(k.startswith("envelope_") for k in d)

    def test_to_audit_dict_with_operation_and_envelope(self) -> None:
        op = MemoryOperation(action=MemoryAction.READ, resource_id="r1", agent_id="a1")
        env = make_envelope(
            "ALLOW",
            ReasonCode.UNKNOWN,
            "ok",
            "MemHook",
        )
        mgd = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            operation=op,
            envelope=env,
        )
        d = mgd.to_audit_dict()
        assert d["operation_action"] == "read"
        assert d["operation_resource_id"] == "r1"
        assert d["envelope_decision"] == "ALLOW"
