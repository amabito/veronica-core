"""Adversarial tests for veronica_core.kernel.audit_bridge -- attacker mindset.

Covers distinct code branches NOT exercised by test_kernel_audit_bridge.py:
- Concurrent writes (10 threads) -> hash chain integrity
- Case/Unicode variants that MUST NOT match the governance set
- HMAC-signed chain: correct key, wrong key, tampered line
- Envelope with extreme metadata (empty -> no key emitted; 100-key; nested)
- Boundary: all 7 decision types, exact True/False count, event_type prefix
- Field fidelity: all envelope fields survive round-trip, audit_id is UUID4
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.kernel.audit_bridge import emit_governance_event, should_emit
from veronica_core.kernel.decision import ReasonCode, make_envelope
from veronica_core.security.policy_signing import PolicySigner

from .conftest import make_test_audit_log, make_test_signer, read_jsonl

# ---------------------------------------------------------------------------
# UUID4 pattern -- 8-4-4-4-12 hex, version nibble = 4, variant nibble = 8/9/a/b
# ---------------------------------------------------------------------------
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_signer(key_seed: bytes = b"test-key") -> PolicySigner:
    return make_test_signer(key_bytes=key_seed)


def _make_audit_log(path: Path, signer: PolicySigner | None = None) -> AuditLog:
    return make_test_audit_log(path, signer=signer)


def _read_all_entries(audit_log: AuditLog) -> list[dict[str, Any]]:
    return read_jsonl(audit_log)


def _make_halt_envelope(**kwargs: Any):
    """Convenience wrapper: HALT envelope with optional overrides."""
    defaults = dict(
        decision="HALT",
        reason_code=ReasonCode.POLICY_UNSIGNED.value,
        reason="test",
        issuer="TestIssuer",
        policy_hash="abc",
        policy_epoch=1,
    )
    defaults.update(kwargs)
    return make_envelope(**defaults)


# ---------------------------------------------------------------------------
# 1. Concurrent access
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeConcurrent:
    """Race conditions: 10 threads write governance events simultaneously."""

    def test_10_threads_halt_all_events_written(self, tmp_path: Path) -> None:
        """10 concurrent HALT emits must all land in the log -- no lost writes."""
        audit_log = _make_audit_log(tmp_path)
        results: list[bool] = []
        lock = threading.Lock()

        def emit_one() -> None:
            env = _make_halt_envelope(decision="HALT")
            result = emit_governance_event(env, audit_log)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=emit_one) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every call should have returned True.
        assert all(results), "Some concurrent HALT emits returned False"
        entries = _read_all_entries(audit_log)
        assert len(entries) == 10, f"Expected 10 entries, got {len(entries)}"

    def test_10_threads_halt_hash_chain_valid_after_concurrent_writes(
        self, tmp_path: Path
    ) -> None:
        """Hash chain must remain internally consistent after 10 concurrent writes."""
        audit_log = _make_audit_log(tmp_path)

        threads = [
            threading.Thread(
                target=emit_governance_event,
                args=(_make_halt_envelope(decision="HALT"), audit_log),
            )
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert audit_log.verify_chain() is True

    def test_10_threads_mixed_decisions_exact_governance_count(
        self, tmp_path: Path
    ) -> None:
        """Mixed decisions: only HALT/DEGRADE/QUARANTINE entries appear in log."""
        # 4 governance + 6 non-governance across 10 threads.
        decisions = [
            "HALT",
            "DEGRADE",
            "QUARANTINE",
            "HALT",
            "ALLOW",
            "DENY",
            "RETRY",
            "QUEUE",
            "ALLOW",
            "DENY",
        ]
        audit_log = _make_audit_log(tmp_path)
        emitted: list[bool] = []
        lock = threading.Lock()

        def emit_one(decision: str) -> None:
            env = _make_halt_envelope(decision=decision)
            result = emit_governance_event(env, audit_log)
            with lock:
                emitted.append(result)

        threads = [
            threading.Thread(target=emit_one, args=(d,)) for d in decisions
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        governance_count = sum(emitted)
        assert governance_count == 4, (
            f"Expected exactly 4 governance events, got {governance_count}"
        )
        entries = _read_all_entries(audit_log)
        assert len(entries) == 4

    def test_concurrent_writes_produce_no_corrupted_jsonl_lines(
        self, tmp_path: Path
    ) -> None:
        """Every line in the log must parse as valid JSON after concurrent writes."""
        audit_log = _make_audit_log(tmp_path)
        threads = [
            threading.Thread(
                target=emit_governance_event,
                args=(_make_halt_envelope(decision="QUARANTINE"), audit_log),
            )
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        log_path = audit_log._path
        assert log_path.exists()
        for line_no, raw in enumerate(
            log_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"Corrupted JSONL at line {line_no}: {exc!r}\n  raw={raw!r}"
                )


# ---------------------------------------------------------------------------
# 2. Case sensitivity / Unicode attacks on should_emit
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeCaseSensitivity:
    """should_emit must be case-exact; no fuzzy matching or Unicode folding."""

    def test_lowercase_halt_returns_false(self) -> None:
        """'halt' (lowercase) is NOT in the governance set."""
        assert should_emit("halt") is False

    def test_mixed_case_halt_returns_false(self) -> None:
        """'Halt' (title-case) is NOT in the governance set."""
        assert should_emit("Halt") is False

    def test_halt_with_null_byte_suffix_returns_false(self) -> None:
        """'HALT\x00' (null byte suffix) is NOT governance-relevant."""
        assert should_emit("HALT\x00") is False

    def test_halt_with_trailing_whitespace_returns_false(self) -> None:
        """'HALT ' (trailing space) is NOT governance-relevant."""
        assert should_emit("HALT ") is False

    def test_halt_with_leading_whitespace_returns_false(self) -> None:
        """' HALT' (leading space) is NOT governance-relevant."""
        assert should_emit(" HALT") is False

    def test_cyrillic_lookalike_halt_returns_false(self) -> None:
        """Cyrillic characters that visually resemble Latin letters must not match.

        Cyrillic capital H (\u041d) looks like ASCII H but is a different code point.
        """
        # U+041d = Cyrillic CAPITAL LETTER EN (looks like H)
        cyrillic_halt = "\u041dALT"
        assert should_emit(cyrillic_halt) is False

    def test_fullwidth_halt_returns_false(self) -> None:
        """Full-width ASCII 'HALT' (Unicode block FF00-FFEF) is NOT governance-relevant."""
        # U+FF28 U+FF21 U+FF2C U+FF34 = full-width H A L T
        fullwidth = "\uff28\uff21\uff2c\uff34"
        assert should_emit(fullwidth) is False

    def test_lowercase_degrade_returns_false(self) -> None:
        """'degrade' (lowercase) is NOT in the governance set."""
        assert should_emit("degrade") is False

    def test_lowercase_quarantine_returns_false(self) -> None:
        """'quarantine' (lowercase) is NOT in the governance set."""
        assert should_emit("quarantine") is False


# ---------------------------------------------------------------------------
# 3. HMAC-signed audit chain integrity
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeHMACChain:
    """verify_chain with a signer must detect key mismatches and tampering."""

    def test_signed_chain_verifies_with_same_key(self, tmp_path: Path) -> None:
        """Three governance events written with signer verify correctly with same key."""
        signer = _make_signer(b"test-key")
        audit_log = _make_audit_log(tmp_path, signer=signer)

        for decision in ("HALT", "DEGRADE", "QUARANTINE"):
            emit_governance_event(_make_halt_envelope(decision=decision), audit_log)

        assert audit_log.verify_chain(signer=signer) is True

    def test_signed_chain_fails_verification_with_different_key(
        self, tmp_path: Path
    ) -> None:
        """Chain signed with key-A fails verify_chain when signer uses key-B."""
        signer_a = _make_signer(b"key-alpha")
        signer_b = _make_signer(b"key-beta")
        audit_log = _make_audit_log(tmp_path, signer=signer_a)

        emit_governance_event(_make_halt_envelope(decision="HALT"), audit_log)
        emit_governance_event(_make_halt_envelope(decision="DEGRADE"), audit_log)

        assert audit_log.verify_chain(signer=signer_b) is False

    def test_tampered_line_fails_chain_verification(self, tmp_path: Path) -> None:
        """Mutating one field in a stored JSONL entry invalidates verify_chain."""
        signer = _make_signer(b"test-key")
        audit_log = _make_audit_log(tmp_path, signer=signer)

        for decision in ("HALT", "DEGRADE", "QUARANTINE"):
            emit_governance_event(_make_halt_envelope(decision=decision), audit_log)

        # Tamper: overwrite the second line's decision value.
        log_path = audit_log._path
        lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
        second_entry = json.loads(lines[1])
        second_entry["data"]["decision"] = "ALLOW"  # corrupt the decision
        lines[1] = json.dumps(second_entry, separators=(",", ":")) + "\n"
        log_path.write_text("".join(lines), encoding="utf-8")

        # Both hash-chain and HMAC checks should fail.
        assert audit_log.verify_chain(signer=signer) is False

    def test_unsigned_log_verify_chain_without_signer_returns_true(
        self, tmp_path: Path
    ) -> None:
        """Chain written without a signer passes verify_chain() (no signer arg)."""
        audit_log = _make_audit_log(tmp_path)

        for decision in ("HALT", "DEGRADE", "QUARANTINE"):
            emit_governance_event(_make_halt_envelope(decision=decision), audit_log)

        assert audit_log.verify_chain() is True

    def test_unsigned_entries_fail_verify_chain_when_signer_supplied(
        self, tmp_path: Path
    ) -> None:
        """Entries without hmac fields fail verify_chain when a signer is passed.

        An unsigned log must not silently pass HMAC verification.
        """
        audit_log_unsigned = _make_audit_log(tmp_path)
        emit_governance_event(_make_halt_envelope(decision="HALT"), audit_log_unsigned)

        # Supply a signer to verify_chain even though no hmac was written.
        signer = _make_signer(b"test-key")
        assert audit_log_unsigned.verify_chain(signer=signer) is False


# ---------------------------------------------------------------------------
# 4. Envelope with extreme metadata
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeExtremeMetadata:
    """Metadata edge cases: empty, very large, deeply nested."""

    def test_empty_metadata_emit_succeeds_no_metadata_key_in_event(
        self, tmp_path: Path
    ) -> None:
        """Empty metadata dict -> emit succeeds; 'metadata' key absent from event data."""
        env = _make_halt_envelope(decision="HALT", metadata={})
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(env, audit_log)

        assert result is True
        data = _read_all_entries(audit_log)[0]["data"]
        # write_governance_event only includes metadata when truthy (non-empty).
        assert "metadata" not in data, (
            "Empty metadata dict must not add a 'metadata' key to the event"
        )

    def test_100_metadata_keys_all_present_in_event(self, tmp_path: Path) -> None:
        """100-key metadata survives round-trip through emit -> audit log."""
        big_meta = {f"key_{i:03d}": f"value_{i}" for i in range(100)}
        env = _make_halt_envelope(decision="DEGRADE", metadata=big_meta)
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(env, audit_log)

        assert result is True
        data = _read_all_entries(audit_log)[0]["data"]
        stored_meta = data.get("metadata", {})
        for k, v in big_meta.items():
            assert stored_meta.get(k) == v, f"Metadata key {k!r} not round-tripped"

    def test_nested_metadata_structure_preserved(self, tmp_path: Path) -> None:
        """Metadata with nested dicts survives JSON round-trip."""
        nested_meta = {
            "span": {"trace_id": "abc123", "span_id": "xyz789"},
            "counts": {"retries": 3, "failures": 1},
        }
        env = _make_halt_envelope(decision="QUARANTINE", metadata=nested_meta)
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(env, audit_log)

        assert result is True
        data = _read_all_entries(audit_log)[0]["data"]
        stored = data.get("metadata", {})
        assert stored.get("span") == {"trace_id": "abc123", "span_id": "xyz789"}
        assert stored.get("counts") == {"retries": 3, "failures": 1}

    def test_single_key_metadata_present_in_event(self, tmp_path: Path) -> None:
        """Single non-empty metadata dict causes 'metadata' key to appear in event."""
        env = _make_halt_envelope(decision="HALT", metadata={"agent_id": "mark-2"})
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(env, audit_log)

        data = _read_all_entries(audit_log)[0]["data"]
        assert "metadata" in data
        assert data["metadata"]["agent_id"] == "mark-2"


# ---------------------------------------------------------------------------
# 5. Boundary decisions
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeBoundary:
    """All 7 known decision types: exact True/False counts and event_type format."""

    # Decision -> expected should_emit result
    _ALL_DECISIONS = [
        ("ALLOW", False),
        ("DENY", False),
        ("HALT", True),
        ("DEGRADE", True),
        ("QUARANTINE", True),
        ("RETRY", False),
        ("QUEUE", False),
    ]

    def test_governance_set_is_exactly_three_decisions(self) -> None:
        """Exactly 3 of the 7 known decisions are governance-relevant."""
        true_count = sum(1 for _, expected in self._ALL_DECISIONS if expected)
        false_count = sum(1 for _, expected in self._ALL_DECISIONS if not expected)
        assert true_count == 3
        assert false_count == 4

    @pytest.mark.parametrize("decision,expected", _ALL_DECISIONS)
    def test_should_emit_each_known_decision(
        self, decision: str, expected: bool
    ) -> None:
        """should_emit returns the documented True/False for every known decision."""
        assert should_emit(decision) is expected

    def test_deny_does_not_emit_governance_event(self, tmp_path: Path) -> None:
        """DENY is a denial decision but NOT governance-relevant -- no audit event."""
        env = _make_halt_envelope(decision="DENY")
        audit_log = _make_audit_log(tmp_path)

        result = emit_governance_event(env, audit_log)

        assert result is False
        assert _read_all_entries(audit_log) == []

    @pytest.mark.parametrize("decision", ["HALT", "DEGRADE", "QUARANTINE"])
    def test_event_type_prefix_for_governance_decision(
        self, decision: str, tmp_path: Path
    ) -> None:
        """event_type is always 'GOVERNANCE_{decision}' for each governance decision."""
        env = _make_halt_envelope(decision=decision)
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(env, audit_log)

        entries = _read_all_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["event_type"] == f"GOVERNANCE_{decision}"

    def test_sequential_all_decisions_exact_three_entries_written(
        self, tmp_path: Path
    ) -> None:
        """Emitting all 7 decisions in sequence writes exactly 3 audit entries."""
        audit_log = _make_audit_log(tmp_path)
        all_decisions = [d for d, _ in self._ALL_DECISIONS]
        true_results = []

        for decision in all_decisions:
            env = _make_halt_envelope(decision=decision)
            if emit_governance_event(env, audit_log):
                true_results.append(decision)

        entries = _read_all_entries(audit_log)
        assert len(entries) == 3
        assert set(true_results) == {"HALT", "DEGRADE", "QUARANTINE"}


# ---------------------------------------------------------------------------
# 6. Audit event field fidelity
# ---------------------------------------------------------------------------


class TestAdversarialAuditBridgeFieldFidelity:
    """Every envelope field must survive the emit -> audit log -> JSON round-trip."""

    def test_all_envelope_fields_round_trip_through_audit_event(
        self, tmp_path: Path
    ) -> None:
        """All distinct recognisable values from the envelope appear in the stored event."""
        # Arrange: craft distinct sentinel values for every field.
        sentinel_policy_hash = "a1b2c3d4" * 8  # 64 hex chars
        sentinel_reason_code = ReasonCode.BUDGET_EXCEEDED.value
        sentinel_reason = "sentinel reason for round-trip test"
        sentinel_issuer = "SentinelIssuer_42"
        sentinel_policy_epoch = 99
        sentinel_metadata = {"round_trip_key": "round_trip_value"}

        env = make_envelope(
            decision="HALT",
            reason_code=sentinel_reason_code,
            reason=sentinel_reason,
            issuer=sentinel_issuer,
            policy_hash=sentinel_policy_hash,
            policy_epoch=sentinel_policy_epoch,
            metadata=sentinel_metadata,
        )
        audit_log = _make_audit_log(tmp_path)

        # Act
        emit_governance_event(env, audit_log)

        # Assert: every field present in data
        data = _read_all_entries(audit_log)[0]["data"]
        assert data["decision"] == "HALT"
        assert data["policy_hash"] == sentinel_policy_hash
        assert data["reason_code"] == sentinel_reason_code
        assert data["reason"] == sentinel_reason
        assert data["audit_id"] == env.audit_id
        assert data["policy_epoch"] == sentinel_policy_epoch
        assert data["issuer"] == sentinel_issuer
        assert data["metadata"]["round_trip_key"] == "round_trip_value"

    def test_audit_id_in_emitted_event_is_valid_uuid4(self, tmp_path: Path) -> None:
        """The audit_id stored in the audit event matches UUID4 format."""
        env = _make_halt_envelope(decision="HALT")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(env, audit_log)

        data = _read_all_entries(audit_log)[0]["data"]
        stored_audit_id = data.get("audit_id", "")
        assert isinstance(stored_audit_id, str), "audit_id must be a string"
        assert _UUID4_RE.match(stored_audit_id), (
            f"audit_id {stored_audit_id!r} is not a valid UUID4"
        )

    def test_audit_id_in_event_matches_envelope_audit_id_exactly(
        self, tmp_path: Path
    ) -> None:
        """The stored audit_id is byte-for-byte identical to the envelope's audit_id."""
        env = _make_halt_envelope(decision="DEGRADE")
        audit_log = _make_audit_log(tmp_path)

        emit_governance_event(env, audit_log)

        data = _read_all_entries(audit_log)[0]["data"]
        assert data["audit_id"] == env.audit_id

    def test_multiple_distinct_envelopes_have_distinct_audit_ids(
        self, tmp_path: Path
    ) -> None:
        """Each envelope generates a unique audit_id -- no collisions across 20 calls."""
        audit_log = _make_audit_log(tmp_path)
        ids: list[str] = []

        for _ in range(20):
            env = _make_halt_envelope(decision="HALT")
            emit_governance_event(env, audit_log)
            ids.append(env.audit_id)

        assert len(set(ids)) == 20, "Duplicate audit_ids detected across envelopes"
