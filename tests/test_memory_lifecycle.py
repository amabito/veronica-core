"""Tests for memory provenance lifecycle state machine.

Covers: valid transitions, forbidden transitions, trust requirements,
        quarantine entry/exit, verified promotion, degrade tightening,
        and adversarial inputs.
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.memory.lifecycle import (
    ProvenanceLifecycle,
    TransitionResult,
    _ALLOWED_TRANSITIONS,
    _DEGRADE_TIGHTENING,
    _TRUST_REQUIREMENTS,
)
from veronica_core.memory.types import MemoryProvenance


@pytest.fixture
def lifecycle() -> ProvenanceLifecycle:
    return ProvenanceLifecycle()


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Permitted transitions with sufficient trust."""

    def test_unknown_to_unverified(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.UNVERIFIED,
            trust_level="provisional",
        )
        assert result.allowed
        assert result.reason == "ok"

    def test_unknown_to_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.QUARANTINED,
            trust_level="untrusted",
        )
        assert result.allowed

    def test_unverified_to_verified(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNVERIFIED,
            MemoryProvenance.VERIFIED,
            trust_level="trusted",
        )
        assert result.allowed

    def test_unverified_to_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNVERIFIED,
            MemoryProvenance.QUARANTINED,
            trust_level="untrusted",
        )
        assert result.allowed

    def test_quarantined_to_unverified(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.QUARANTINED,
            MemoryProvenance.UNVERIFIED,
            trust_level="trusted",
        )
        assert result.allowed

    def test_quarantined_to_verified(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.QUARANTINED,
            MemoryProvenance.VERIFIED,
            trust_level="privileged",
        )
        assert result.allowed

    def test_verified_to_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.VERIFIED,
            MemoryProvenance.QUARANTINED,
            trust_level="untrusted",
        )
        assert result.allowed

    def test_same_state_is_noop(self, lifecycle: ProvenanceLifecycle) -> None:
        for state in MemoryProvenance:
            result = lifecycle.validate_transition(state, state)
            assert result.allowed
            assert result.reason == "already_in_state"


# ---------------------------------------------------------------------------
# Forbidden transitions
# ---------------------------------------------------------------------------


class TestForbiddenTransitions:
    """Transitions that are structurally not allowed."""

    def test_unknown_to_verified_forbidden(self, lifecycle: ProvenanceLifecycle) -> None:
        """Cannot skip UNVERIFIED and go directly to VERIFIED."""
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.VERIFIED,
            trust_level="privileged",
        )
        assert not result.allowed
        assert result.reason == "forbidden"

    def test_verified_to_unverified_forbidden(self, lifecycle: ProvenanceLifecycle) -> None:
        """Verified content cannot be demoted to unverified (use quarantine)."""
        result = lifecycle.validate_transition(
            MemoryProvenance.VERIFIED,
            MemoryProvenance.UNVERIFIED,
            trust_level="privileged",
        )
        assert not result.allowed
        assert result.reason == "forbidden"

    def test_verified_to_unknown_forbidden(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.VERIFIED,
            MemoryProvenance.UNKNOWN,
            trust_level="privileged",
        )
        assert not result.allowed

    def test_quarantined_to_unknown_forbidden(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.QUARANTINED,
            MemoryProvenance.UNKNOWN,
            trust_level="privileged",
        )
        assert not result.allowed


# ---------------------------------------------------------------------------
# Trust requirements
# ---------------------------------------------------------------------------


class TestTrustRequirements:
    """Transitions require minimum trust levels."""

    def test_unverified_to_verified_needs_trusted(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNVERIFIED,
            MemoryProvenance.VERIFIED,
            trust_level="provisional",
        )
        assert not result.allowed
        assert result.reason == "trust_insufficient"
        assert "trusted" in result.message

    def test_quarantined_to_verified_needs_privileged(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.QUARANTINED,
            MemoryProvenance.VERIFIED,
            trust_level="trusted",
        )
        assert not result.allowed
        assert result.reason == "trust_insufficient"

    def test_quarantined_to_unverified_needs_trusted(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.QUARANTINED,
            MemoryProvenance.UNVERIFIED,
            trust_level="provisional",
        )
        assert not result.allowed

    def test_unknown_to_unverified_needs_provisional(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.UNVERIFIED,
            trust_level="untrusted",
        )
        assert not result.allowed

    def test_empty_trust_treated_as_untrusted(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.UNVERIFIED,
            trust_level="",
        )
        assert not result.allowed
        assert result.reason == "trust_insufficient"


# ---------------------------------------------------------------------------
# Degrade provenance tightening
# ---------------------------------------------------------------------------


class TestDegradeTightening:
    """DEGRADE verdict tightens provenance (never loosens)."""

    def test_verified_degrades_to_unverified(self, lifecycle: ProvenanceLifecycle) -> None:
        assert lifecycle.degrade_provenance(MemoryProvenance.VERIFIED) is MemoryProvenance.UNVERIFIED

    def test_unverified_degrades_to_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        assert lifecycle.degrade_provenance(MemoryProvenance.UNVERIFIED) is MemoryProvenance.QUARANTINED

    def test_quarantined_stays_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        assert lifecycle.degrade_provenance(MemoryProvenance.QUARANTINED) is MemoryProvenance.QUARANTINED

    def test_unknown_degrades_to_quarantined(self, lifecycle: ProvenanceLifecycle) -> None:
        assert lifecycle.degrade_provenance(MemoryProvenance.UNKNOWN) is MemoryProvenance.QUARANTINED


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------


class TestConvenienceMethods:
    """Convenience API for common checks."""

    def test_can_promote_to_verified(self, lifecycle: ProvenanceLifecycle) -> None:
        assert lifecycle.can_promote_to_verified(MemoryProvenance.UNVERIFIED, "trusted")
        assert not lifecycle.can_promote_to_verified(MemoryProvenance.UNVERIFIED, "provisional")
        assert not lifecycle.can_promote_to_verified(MemoryProvenance.UNKNOWN, "privileged")

    def test_quarantine_entry_conditions(self, lifecycle: ProvenanceLifecycle) -> None:
        conditions = lifecycle.quarantine_entry_conditions()
        assert "unknown" in conditions
        assert "unverified" in conditions
        assert "verified" in conditions
        assert all(v == "untrusted" for v in conditions.values())

    def test_all_transitions(self, lifecycle: ProvenanceLifecycle) -> None:
        matrix = lifecycle.all_transitions()
        assert set(matrix.keys()) == {"unknown", "unverified", "quarantined", "verified"}
        assert "quarantined" in matrix["unknown"]
        assert "unverified" in matrix["unknown"]
        assert "verified" not in matrix["unknown"]


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialLifecycle:
    """Adversarial inputs must not crash or bypass rules."""

    def test_invalid_from_state_type(self, lifecycle: ProvenanceLifecycle) -> None:
        with pytest.raises(TypeError, match="from_state must be MemoryProvenance"):
            lifecycle.validate_transition("unknown", MemoryProvenance.VERIFIED)  # type: ignore[arg-type]

    def test_invalid_to_state_type(self, lifecycle: ProvenanceLifecycle) -> None:
        with pytest.raises(TypeError, match="to_state must be MemoryProvenance"):
            lifecycle.validate_transition(MemoryProvenance.UNKNOWN, "verified")  # type: ignore[arg-type]

    def test_invalid_degrade_input(self, lifecycle: ProvenanceLifecycle) -> None:
        with pytest.raises(TypeError, match="current must be MemoryProvenance"):
            lifecycle.degrade_provenance("verified")  # type: ignore[arg-type]

    def test_unknown_trust_level_denied(self, lifecycle: ProvenanceLifecycle) -> None:
        """Unknown trust string -> fail-closed."""
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.UNVERIFIED,
            trust_level="superadmin",
        )
        assert not result.allowed
        assert result.reason == "trust_insufficient"

    def test_transition_result_is_frozen(self, lifecycle: ProvenanceLifecycle) -> None:
        result = lifecycle.validate_transition(
            MemoryProvenance.UNKNOWN,
            MemoryProvenance.UNVERIFIED,
            trust_level="provisional",
        )
        with pytest.raises(AttributeError):
            result.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Adversarial Round 2
# ---------------------------------------------------------------------------


class TestAdversarialLifecycleRound2:
    """Second wave of adversarial tests -- boundary abuse, state completeness,
    concurrency determinism, and type confusion.
    """

    # ------------------------------------------------------------------
    # Boundary abuse: trust_level string edge cases
    # ------------------------------------------------------------------

    def test_whitespace_only_trust_level_denied(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Whitespace-only trust_level must not be treated as a known level."""
        # Arrange
        from_state = MemoryProvenance.UNKNOWN
        to_state = MemoryProvenance.UNVERIFIED

        # Act -- "  ".lower().strip() == "" -> falls back to "untrusted"
        result = lifecycle.validate_transition(
            from_state, to_state, trust_level="   "
        )

        # Assert -- "untrusted" rank (0) < required "provisional" rank (1) -> deny
        assert not result.allowed
        assert result.reason == "trust_insufficient"

    def test_mixed_case_trusted_is_normalized(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Mixed-case trust_level must be case-folded before lookup."""
        # Arrange
        from_state = MemoryProvenance.UNVERIFIED
        to_state = MemoryProvenance.VERIFIED

        # Act -- "TRUSTED" -> lower() -> "trusted" -> rank 2, required 2 -> allow
        result = lifecycle.validate_transition(
            from_state, to_state, trust_level="TRUSTED"
        )

        # Assert
        assert result.allowed
        assert result.reason == "ok"

    def test_mixed_case_privileged_is_normalized(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """'Privileged' (title-case) must be accepted after normalization."""
        # Arrange
        from_state = MemoryProvenance.QUARANTINED
        to_state = MemoryProvenance.VERIFIED

        # Act
        result = lifecycle.validate_transition(
            from_state, to_state, trust_level="Privileged"
        )

        # Assert
        assert result.allowed

    def test_trust_level_with_null_byte_denied(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Null byte embedded in trust_level must be treated as unknown -> fail-closed."""
        # Arrange
        from_state = MemoryProvenance.UNKNOWN
        to_state = MemoryProvenance.UNVERIFIED

        # Act -- "trusted\x00" is not in TRUST_RANK after strip
        result = lifecycle.validate_transition(
            from_state, to_state, trust_level="trusted\x00"
        )

        # Assert -- fail-closed: unknown trust level -> denied
        assert not result.allowed
        assert result.reason == "trust_insufficient"

    def test_trust_level_with_leading_trailing_whitespace_normalized(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Leading/trailing spaces around a valid trust level must be stripped."""
        # Arrange
        from_state = MemoryProvenance.UNKNOWN
        to_state = MemoryProvenance.UNVERIFIED

        # Act -- "  provisional  " -> strip -> "provisional" -> rank 1, required 1 -> allow
        result = lifecycle.validate_transition(
            from_state, to_state, trust_level="  provisional  "
        )

        # Assert
        assert result.allowed

    def test_trust_level_with_special_chars_denied(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """SQL-injection-style or shell-special trust_level must fail-closed."""
        # Arrange
        from_state = MemoryProvenance.UNVERIFIED
        to_state = MemoryProvenance.VERIFIED
        malicious_inputs = [
            "trusted'; DROP TABLE--",
            "trusted OR 1=1",
            "$(privileged)",
            "trusted\ntrusted",
        ]

        for bad_trust in malicious_inputs:
            # Act
            result = lifecycle.validate_transition(
                from_state, to_state, trust_level=bad_trust
            )
            # Assert -- none of these are in TRUST_RANK -> denied
            assert not result.allowed, (
                f"Expected deny for trust_level={bad_trust!r} but got allowed"
            )
            assert result.reason == "trust_insufficient"

    # ------------------------------------------------------------------
    # State completeness: verify all MemoryProvenance values are covered
    # ------------------------------------------------------------------

    def test_allowed_transitions_covers_all_provenance_values(self) -> None:
        """Every MemoryProvenance value must appear as a key in _ALLOWED_TRANSITIONS.

        A missing key means degrade/quarantine logic silently falls back to
        frozenset() and blocks ALL transitions from that state with no error.
        """
        # Arrange
        all_states = set(MemoryProvenance)

        # Act
        covered = set(_ALLOWED_TRANSITIONS.keys())

        # Assert
        assert covered == all_states, (
            f"_ALLOWED_TRANSITIONS is missing keys: {all_states - covered}"
        )

    def test_degrade_tightening_covers_all_provenance_values(self) -> None:
        """Every MemoryProvenance value must appear as a key in _DEGRADE_TIGHTENING.

        A missing key causes a KeyError at runtime when degrade_provenance() is
        called on that state.
        """
        # Arrange
        all_states = set(MemoryProvenance)

        # Act
        covered = set(_DEGRADE_TIGHTENING.keys())

        # Assert
        assert covered == all_states, (
            f"_DEGRADE_TIGHTENING is missing keys: {all_states - covered}"
        )

    def test_trust_requirements_covers_all_allowed_edges(self) -> None:
        """Every (from, to) edge in _ALLOWED_TRANSITIONS must have a trust requirement.

        A missing edge causes validate_transition() to default to "privileged"
        (the fallback in .get(..., "privileged")).  This is a safe default but
        should be explicit, not accidental.
        """
        # Arrange: collect all reachable edges from the transition matrix
        missing_edges = []
        for from_state, targets in _ALLOWED_TRANSITIONS.items():
            for to_state in targets:
                edge = (from_state, to_state)
                if edge not in _TRUST_REQUIREMENTS:
                    missing_edges.append(edge)

        # Assert -- document any implicit "privileged" defaults
        assert not missing_edges, (
            "The following edges fall back to implicit 'privileged' trust "
            "(add explicit entries to _TRUST_REQUIREMENTS to suppress): "
            + ", ".join(f"{f.value}->{t.value}" for f, t in missing_edges)
        )

    def test_degrade_provenance_is_monotonically_tightening(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Repeated degrade_provenance calls must never loosen provenance.

        VERIFIED -> UNVERIFIED -> QUARANTINED -> QUARANTINED (fixed point).
        Each step must be <= the previous in trust rank (higher rank = more trusted).
        """
        # Arrange -- map provenance to a "tightness" rank (lower = more restricted).
        # UNKNOWN is treated as less trusted than QUARANTINED for degrade purposes:
        # degrade(UNKNOWN) -> QUARANTINED means UNKNOWN is looser than QUARANTINED.
        # Tightness order (ascending restriction): VERIFIED > UNVERIFIED > UNKNOWN > QUARANTINED.
        # Degrade must only move toward lower tightness rank (i.e. more restricted).
        tightness: dict[MemoryProvenance, int] = {
            MemoryProvenance.VERIFIED: 3,
            MemoryProvenance.UNVERIFIED: 2,
            MemoryProvenance.UNKNOWN: 1,
            MemoryProvenance.QUARANTINED: 0,
        }

        for start in MemoryProvenance:
            # Act -- apply degrade 5 times to ensure fixed-point convergence
            current = start
            prev_rank = tightness[current]
            for _ in range(5):
                degraded = lifecycle.degrade_provenance(current)
                degraded_rank = tightness[degraded]

                # Assert -- rank must not increase (never loosen trust)
                assert degraded_rank <= prev_rank, (
                    f"degrade_provenance({current.value}) -> {degraded.value} "
                    f"loosened trust (rank {prev_rank} -> {degraded_rank})"
                )
                current = degraded
                prev_rank = degraded_rank

    def test_degrade_provenance_reaches_fixed_point(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """All provenance states must converge to QUARANTINED under repeated degradation."""
        for start in MemoryProvenance:
            # Act -- degrade until stable
            current = start
            for _ in range(10):
                next_state = lifecycle.degrade_provenance(current)
                if next_state is current:
                    break
                current = next_state

            # Assert -- fixed point must be QUARANTINED (not UNKNOWN or UNVERIFIED)
            assert current is MemoryProvenance.QUARANTINED, (
                f"Starting from {start.value}, degradation fixed point is "
                f"{current.value}, expected QUARANTINED"
            )

    # ------------------------------------------------------------------
    # Concurrent access: determinism under threading
    # ------------------------------------------------------------------

    def test_concurrent_validate_transition_is_deterministic(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """100 parallel validate_transition calls must all return identical results.

        ProvenanceLifecycle is stateless; this confirms no shared mutable state
        leaks between threads.
        """
        # Arrange
        results: list[TransitionResult] = []
        lock = threading.Lock()
        n_threads = 100

        def worker() -> None:
            result = lifecycle.validate_transition(
                MemoryProvenance.UNVERIFIED,
                MemoryProvenance.VERIFIED,
                trust_level="trusted",
            )
            with lock:
                results.append(result)

        # Act
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Assert -- all results must be identical
        assert len(results) == n_threads
        first = results[0]
        for r in results[1:]:
            assert r.allowed == first.allowed
            assert r.reason == first.reason
            assert r.from_state is first.from_state
            assert r.to_state is first.to_state

    def test_concurrent_degrade_provenance_is_deterministic(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """100 parallel degrade_provenance calls must return identical results."""
        # Arrange
        results: list[MemoryProvenance] = []
        lock = threading.Lock()
        n_threads = 100

        def worker() -> None:
            result = lifecycle.degrade_provenance(MemoryProvenance.VERIFIED)
            with lock:
                results.append(result)

        # Act
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Assert
        assert len(results) == n_threads
        assert all(r is MemoryProvenance.UNVERIFIED for r in results)

    # ------------------------------------------------------------------
    # Type confusion: non-enum values must raise TypeError
    # ------------------------------------------------------------------

    def test_string_enum_value_as_from_state_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Passing the string value of an enum ('verified') must raise TypeError.

        MemoryProvenance is a str-Enum; callers may accidentally pass the string
        value directly instead of the enum member.
        """
        with pytest.raises(TypeError, match="from_state must be MemoryProvenance"):
            lifecycle.validate_transition(
                "verified",  # type: ignore[arg-type]
                MemoryProvenance.QUARANTINED,
                trust_level="privileged",
            )

    def test_string_enum_value_as_to_state_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Passing the string value of an enum as to_state must raise TypeError."""
        with pytest.raises(TypeError, match="to_state must be MemoryProvenance"):
            lifecycle.validate_transition(
                MemoryProvenance.UNVERIFIED,
                "quarantined",  # type: ignore[arg-type]
                trust_level="untrusted",
            )

    def test_none_as_from_state_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """None from_state must raise TypeError, not AttributeError or silent bypass."""
        with pytest.raises(TypeError, match="from_state must be MemoryProvenance"):
            lifecycle.validate_transition(
                None,  # type: ignore[arg-type]
                MemoryProvenance.VERIFIED,
                trust_level="privileged",
            )

    def test_none_as_to_state_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """None to_state must raise TypeError."""
        with pytest.raises(TypeError, match="to_state must be MemoryProvenance"):
            lifecycle.validate_transition(
                MemoryProvenance.UNVERIFIED,
                None,  # type: ignore[arg-type]
                trust_level="trusted",
            )

    def test_integer_trust_level_denied(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Integer trust_level (e.g. 3 for 'privileged') must fail-closed.

        The method signature accepts str; passing an int triggers AttributeError
        on .lower() -- verify the caller gets a clean failure, not a crash
        that leaks internal state.
        """
        with pytest.raises((AttributeError, TypeError)):
            lifecycle.validate_transition(
                MemoryProvenance.UNVERIFIED,
                MemoryProvenance.VERIFIED,
                trust_level=3,  # type: ignore[arg-type]
            )

    def test_none_as_current_in_degrade_provenance_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """None current in degrade_provenance must raise TypeError."""
        with pytest.raises(TypeError, match="current must be MemoryProvenance"):
            lifecycle.degrade_provenance(None)  # type: ignore[arg-type]

    def test_integer_as_from_state_raises(
        self, lifecycle: ProvenanceLifecycle
    ) -> None:
        """Integer from_state (e.g. 0) must raise TypeError rather than indexing the matrix."""
        with pytest.raises(TypeError, match="from_state must be MemoryProvenance"):
            lifecycle.validate_transition(
                0,  # type: ignore[arg-type]
                MemoryProvenance.UNVERIFIED,
                trust_level="trusted",
            )
