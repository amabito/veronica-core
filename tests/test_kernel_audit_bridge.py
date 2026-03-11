"""Tests for veronica_core.kernel.audit_bridge -- governance event bridge.

Coverage:
- emit_governance_event with HALT -> True, event written
- emit_governance_event with DEGRADE -> True, event written
- emit_governance_event with QUARANTINE -> True, event written
- emit_governance_event with ALLOW -> False, nothing written
- emit_governance_event with DENY -> False, nothing written
- should_emit for all 7 known decision types
- Adversarial: empty string decision, None-like cases
- Verify audit event contains correct fields from envelope
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veronica_core.audit.log import AuditLog
from veronica_core.kernel.audit_bridge import emit_governance_event, should_emit
from veronica_core.kernel.decision import DecisionEnvelope, ReasonCode, make_envelope

from .conftest import make_test_audit_log, read_jsonl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    decision: str = "HALT",
    reason_code: str = ReasonCode.POLICY_UNSIGNED.value,
    reason: str = "test reason",
    issuer: str = "TestIssuer",
    policy_hash: str = "abc123",
    policy_epoch: int = 1,
    metadata: dict[str, Any] | None = None,
) -> DecisionEnvelope:
    """Build a DecisionEnvelope with controllable fields."""
    return make_envelope(
        decision=decision,
        reason_code=reason_code,
        reason=reason,
        issuer=issuer,
        policy_hash=policy_hash,
        policy_epoch=policy_epoch,
        metadata=metadata or {},
    )


def _make_audit_log(tmp_path: Path) -> AuditLog:
    return make_test_audit_log(tmp_path)


def _read_audit_events(audit_log: AuditLog) -> list[dict[str, Any]]:
    return read_jsonl(audit_log)


# ---------------------------------------------------------------------------
# should_emit -- predicate for all 7 decision types
# ---------------------------------------------------------------------------


class TestShouldEmit:
    def test_halt_is_governance_relevant(self) -> None:
        assert should_emit("HALT") is True

    def test_degrade_is_governance_relevant(self) -> None:
        assert should_emit("DEGRADE") is True

    def test_quarantine_is_governance_relevant(self) -> None:
        assert should_emit("QUARANTINE") is True

    def test_allow_is_not_governance_relevant(self) -> None:
        assert should_emit("ALLOW") is False

    def test_deny_is_not_governance_relevant(self) -> None:
        assert should_emit("DENY") is False

    def test_retry_is_not_governance_relevant(self) -> None:
        assert should_emit("RETRY") is False

    def test_queue_is_not_governance_relevant(self) -> None:
        assert should_emit("QUEUE") is False

    def test_empty_string_is_not_governance_relevant(self) -> None:
        assert should_emit("") is False

    def test_unknown_string_is_not_governance_relevant(self) -> None:
        assert should_emit("UNKNOWN_VERDICT") is False


# ---------------------------------------------------------------------------
# emit_governance_event -- governance decisions emit and return True
# ---------------------------------------------------------------------------


class TestEmitGovernanceEventGovernanceDecisions:
    def test_halt_emits_event_returns_true(self, tmp_path: Path) -> None:
        """HALT decision produces an audit event and returns True."""
        envelope = _make_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is True
        events = _read_audit_events(audit_log)
        assert len(events) == 1

    def test_halt_event_type_is_governance_halt(self, tmp_path: Path) -> None:
        """HALT decision writes event_type='GOVERNANCE_HALT'."""
        envelope = _make_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        events = _read_audit_events(audit_log)
        assert events[0]["event_type"] == "GOVERNANCE_HALT"

    def test_degrade_emits_event_returns_true(self, tmp_path: Path) -> None:
        """DEGRADE decision produces an audit event and returns True."""
        envelope = _make_envelope(decision="DEGRADE")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is True
        events = _read_audit_events(audit_log)
        assert len(events) == 1

    def test_degrade_event_type_is_governance_degrade(self, tmp_path: Path) -> None:
        """DEGRADE decision writes event_type='GOVERNANCE_DEGRADE'."""
        envelope = _make_envelope(decision="DEGRADE")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        events = _read_audit_events(audit_log)
        assert events[0]["event_type"] == "GOVERNANCE_DEGRADE"

    def test_quarantine_emits_event_returns_true(self, tmp_path: Path) -> None:
        """QUARANTINE decision produces an audit event and returns True."""
        envelope = _make_envelope(decision="QUARANTINE")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is True
        events = _read_audit_events(audit_log)
        assert len(events) == 1

    def test_quarantine_event_type_is_governance_quarantine(self, tmp_path: Path) -> None:
        """QUARANTINE decision writes event_type='GOVERNANCE_QUARANTINE'."""
        envelope = _make_envelope(decision="QUARANTINE")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        events = _read_audit_events(audit_log)
        assert events[0]["event_type"] == "GOVERNANCE_QUARANTINE"


# ---------------------------------------------------------------------------
# emit_governance_event -- non-governance decisions skip and return False
# ---------------------------------------------------------------------------


class TestEmitGovernanceEventNonGovernanceDecisions:
    def test_allow_returns_false(self, tmp_path: Path) -> None:
        """ALLOW decision skips emission and returns False."""
        envelope = _make_envelope(decision="ALLOW")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is False

    def test_allow_writes_no_events(self, tmp_path: Path) -> None:
        """ALLOW decision does not write any audit events."""
        envelope = _make_envelope(decision="ALLOW")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        events = _read_audit_events(audit_log)
        assert events == []

    def test_deny_returns_false(self, tmp_path: Path) -> None:
        """DENY decision skips emission and returns False."""
        envelope = _make_envelope(decision="DENY")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is False

    def test_deny_writes_no_events(self, tmp_path: Path) -> None:
        """DENY decision does not write any audit events."""
        envelope = _make_envelope(decision="DENY")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        events = _read_audit_events(audit_log)
        assert events == []

    def test_retry_returns_false(self, tmp_path: Path) -> None:
        """RETRY decision skips emission and returns False."""
        envelope = _make_envelope(decision="RETRY")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is False

    def test_queue_returns_false(self, tmp_path: Path) -> None:
        """QUEUE decision skips emission and returns False."""
        envelope = _make_envelope(decision="QUEUE")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is False


# ---------------------------------------------------------------------------
# Audit event field verification
# ---------------------------------------------------------------------------


class TestEmitGovernanceEventFieldVerification:
    def test_event_contains_decision_field(self, tmp_path: Path) -> None:
        """Audit event data carries the decision field from the envelope."""
        envelope = _make_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["decision"] == "HALT"

    def test_event_contains_reason_code_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the reason_code from the envelope."""
        envelope = _make_envelope(
            decision="HALT", reason_code=ReasonCode.CIRCUIT_OPEN.value
        )
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["reason_code"] == "CIRCUIT_OPEN"

    def test_event_contains_reason_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the reason string from the envelope."""
        envelope = _make_envelope(decision="HALT", reason="budget exceeded at 12.5 USD")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["reason"] == "budget exceeded at 12.5 USD"

    def test_event_contains_audit_id_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the audit_id from the envelope."""
        envelope = _make_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["audit_id"] == envelope.audit_id

    def test_event_contains_policy_hash_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the policy_hash from the envelope."""
        envelope = _make_envelope(decision="HALT", policy_hash="feedbeef" * 8)
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["policy_hash"] == "feedbeef" * 8

    def test_event_contains_policy_epoch_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the policy_epoch from the envelope."""
        envelope = _make_envelope(decision="HALT", policy_epoch=42)
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["policy_epoch"] == 42

    def test_event_contains_issuer_from_envelope(self, tmp_path: Path) -> None:
        """Audit event data carries the issuer from the envelope."""
        envelope = _make_envelope(decision="HALT", issuer="BudgetEnforcer")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["issuer"] == "BudgetEnforcer"

    def test_event_with_envelope_metadata_includes_metadata(
        self, tmp_path: Path
    ) -> None:
        """Envelope metadata is forwarded to the governance event."""
        envelope = _make_envelope(
            decision="DEGRADE",
            metadata={"extra_key": "extra_value"},
        )
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data.get("metadata", {}).get("extra_key") == "extra_value"

    def test_event_without_envelope_metadata_no_crash(self, tmp_path: Path) -> None:
        """Envelope with empty metadata emits event without crashing."""
        envelope = _make_envelope(decision="QUARANTINE", metadata={})
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(envelope, audit_log)

        assert result is True


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridge:
    def test_multiple_governance_events_written_in_order(
        self, tmp_path: Path
    ) -> None:
        """Multiple emit calls write sequential audit events."""
        audit_log = _make_audit_log(tmp_path)
        envelopes = [
            _make_envelope(decision="HALT"),
            _make_envelope(decision="DEGRADE"),
            _make_envelope(decision="QUARANTINE"),
        ]

        for env in envelopes:
            emit_governance_event(env, audit_log)

        events = _read_audit_events(audit_log)
        assert len(events) == 3
        assert events[0]["event_type"] == "GOVERNANCE_HALT"
        assert events[1]["event_type"] == "GOVERNANCE_DEGRADE"
        assert events[2]["event_type"] == "GOVERNANCE_QUARANTINE"

    def test_interleaved_governance_and_non_governance_only_writes_governance(
        self, tmp_path: Path
    ) -> None:
        """Non-governance decisions do not contribute audit entries."""
        audit_log = _make_audit_log(tmp_path)
        decisions = ["ALLOW", "HALT", "DENY", "DEGRADE", "RETRY", "QUARANTINE", "QUEUE"]

        for decision in decisions:
            env = _make_envelope(decision=decision)
            emit_governance_event(env, audit_log)

        events = _read_audit_events(audit_log)
        # Only HALT, DEGRADE, QUARANTINE should have been written.
        assert len(events) == 3
        written_types = {e["event_type"] for e in events}
        assert written_types == {"GOVERNANCE_HALT", "GOVERNANCE_DEGRADE", "GOVERNANCE_QUARANTINE"}

    def test_audit_id_in_event_matches_envelope_audit_id(
        self, tmp_path: Path
    ) -> None:
        """The audit_id in the written event is identical to the envelope's audit_id."""
        envelope = _make_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["audit_id"] == envelope.audit_id

    def test_empty_policy_hash_forwarded_correctly(self, tmp_path: Path) -> None:
        """An envelope with empty policy_hash is forwarded without modification."""
        envelope = _make_envelope(decision="HALT", policy_hash="")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["policy_hash"] == ""

    def test_zero_policy_epoch_forwarded_correctly(self, tmp_path: Path) -> None:
        """An envelope with policy_epoch=0 is forwarded without modification."""
        envelope = _make_envelope(decision="DEGRADE", policy_epoch=0)
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(envelope, audit_log)

        data = _read_audit_events(audit_log)[0]["data"]
        assert data["policy_epoch"] == 0
