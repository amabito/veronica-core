"""Tests for ViewPolicyEvaluator.

Covers:
- AGENT_PRIVATE: owner allows, non-owner denies
- LOCAL_WORKING: any trust level allows
- TEAM_SHARED: untrusted write denies, provisional read allows
- VERIFIED_ARCHIVE: write denies (except CONSOLIDATION mode for trusted+)
- QUARANTINED: privileged read allows, others deny
- REPLAY mode: all writes denied
- AUDIT_REVIEW mode: all writes denied; quarantined trusted+ read allowed
- SIMULATION mode: writes to VERIFIED_ARCHIVE / SESSION_STATE denied
- CONSOLIDATION mode: VERIFIED_ARCHIVE write allowed for trusted+
- LIVE mode: untrusted access to VERIFIED_ARCHIVE denied
- ThreatContext attached on DENY
"""

from __future__ import annotations

from veronica_core.memory.types import (
    ExecutionMode,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryView,
)
from veronica_core.memory.view_policy import ViewPolicyEvaluator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_op(agent_id: str = "agent-owner") -> MemoryOperation:
    return MemoryOperation(action=MemoryAction.READ, agent_id=agent_id)


def _write_op(agent_id: str = "agent-owner") -> MemoryOperation:
    return MemoryOperation(action=MemoryAction.WRITE, agent_id=agent_id)


def _ctx(
    *,
    view: MemoryView = MemoryView.LOCAL_WORKING,
    mode: ExecutionMode = ExecutionMode.LIVE,
    trust: str = "trusted",
    op: MemoryOperation | None = None,
) -> MemoryPolicyContext:
    base_op = op or _read_op()
    return MemoryPolicyContext(
        operation=base_op,
        trust_level=trust,
        memory_view=view,
        execution_mode=mode,
    )


def _eval(
    evaluator: ViewPolicyEvaluator,
    operation: MemoryOperation,
    *,
    view: MemoryView = MemoryView.LOCAL_WORKING,
    mode: ExecutionMode = ExecutionMode.LIVE,
    trust: str = "trusted",
) -> MemoryGovernanceDecision:
    ctx = _ctx(view=view, mode=mode, trust=trust, op=operation)
    return evaluator.before_op(operation, ctx)


# ---------------------------------------------------------------------------
# AGENT_PRIVATE
# ---------------------------------------------------------------------------


class TestAgentPrivateView:
    def test_agent_private_owner_allows(self) -> None:
        ev = ViewPolicyEvaluator(owner_agent_id="alice")
        op = _read_op(agent_id="alice")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="trusted")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_agent_private_non_owner_denies(self) -> None:
        ev = ViewPolicyEvaluator(owner_agent_id="alice")
        op = _read_op(agent_id="bob")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="trusted")
        assert result.verdict is GovernanceVerdict.DENY

    def test_agent_private_non_owner_write_denies(self) -> None:
        ev = ViewPolicyEvaluator(owner_agent_id="alice")
        op = _write_op(agent_id="mallory")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="privileged")
        assert result.verdict is GovernanceVerdict.DENY

    def test_agent_private_empty_owner_denies_all(self) -> None:
        ev = ViewPolicyEvaluator(owner_agent_id="")
        op = _read_op(agent_id="someone")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="privileged")
        assert result.verdict is GovernanceVerdict.DENY

    def test_agent_private_empty_owner_empty_agent_denies(self) -> None:
        """Empty owner + empty agent_id must deny (fail-closed, not match)."""
        ev = ViewPolicyEvaluator(owner_agent_id="")
        op = MemoryOperation(action=MemoryAction.READ, agent_id="")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="trusted")
        assert result.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# LOCAL_WORKING
# ---------------------------------------------------------------------------


class TestLocalWorkingView:
    def test_local_working_untrusted_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(ev, op, view=MemoryView.LOCAL_WORKING, trust="untrusted")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_local_working_any_trust_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        for trust in ("untrusted", "provisional", "trusted", "privileged"):
            op = _write_op()
            result = _eval(ev, op, view=MemoryView.LOCAL_WORKING, trust=trust)
            assert result.verdict is GovernanceVerdict.ALLOW, (
                f"Failed for trust={trust}"
            )


# ---------------------------------------------------------------------------
# TEAM_SHARED
# ---------------------------------------------------------------------------


class TestTeamSharedView:
    def test_team_shared_provisional_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(ev, op, view=MemoryView.TEAM_SHARED, trust="provisional")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_team_shared_untrusted_read_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(ev, op, view=MemoryView.TEAM_SHARED, trust="untrusted")
        assert result.verdict is GovernanceVerdict.DENY

    def test_team_shared_trusted_write_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.TEAM_SHARED, trust="trusted")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_team_shared_untrusted_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.TEAM_SHARED, trust="untrusted")
        assert result.verdict is GovernanceVerdict.DENY

    def test_team_shared_provisional_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.TEAM_SHARED, trust="provisional")
        assert result.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# VERIFIED_ARCHIVE
# ---------------------------------------------------------------------------


class TestVerifiedArchiveView:
    def test_verified_archive_trusted_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(ev, op, view=MemoryView.VERIFIED_ARCHIVE, trust="trusted")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_verified_archive_untrusted_read_denies_live(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="untrusted",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_verified_archive_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.VERIFIED_ARCHIVE, trust="privileged")
        assert result.verdict is GovernanceVerdict.DENY

    def test_verified_archive_write_trusted_denies_in_live(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# QUARANTINED
# ---------------------------------------------------------------------------


class TestQuarantinedView:
    def test_quarantined_privileged_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(ev, op, view=MemoryView.QUARANTINED, trust="privileged")
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_quarantined_trusted_read_denies_in_live(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.QUARANTINED,
            mode=ExecutionMode.LIVE,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_quarantined_non_privileged_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        for trust in ("untrusted", "provisional", "trusted"):
            op = _read_op()
            result = _eval(ev, op, view=MemoryView.QUARANTINED, trust=trust)
            assert result.verdict is GovernanceVerdict.DENY, f"Failed for trust={trust}"

    def test_quarantined_write_denies_even_privileged(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.QUARANTINED, trust="privileged")
        assert result.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# REPLAY mode
# ---------------------------------------------------------------------------


class TestReplayMode:
    def test_replay_mode_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.LOCAL_WORKING,
            mode=ExecutionMode.REPLAY,
            trust="privileged",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_replay_mode_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.LOCAL_WORKING,
            mode=ExecutionMode.REPLAY,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_replay_mode_archive_action_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = MemoryOperation(action=MemoryAction.ARCHIVE)
        result = _eval(
            ev,
            op,
            view=MemoryView.PROVISIONAL_ARCHIVE,
            mode=ExecutionMode.REPLAY,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.DENY


# ---------------------------------------------------------------------------
# AUDIT_REVIEW mode
# ---------------------------------------------------------------------------


class TestAuditReviewMode:
    def test_audit_review_mode_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.LOCAL_WORKING,
            mode=ExecutionMode.AUDIT_REVIEW,
            trust="privileged",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_audit_review_quarantined_trusted_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.QUARANTINED,
            mode=ExecutionMode.AUDIT_REVIEW,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_audit_review_quarantined_untrusted_read_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.QUARANTINED,
            mode=ExecutionMode.AUDIT_REVIEW,
            trust="provisional",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_audit_review_non_quarantined_trusted_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.LOCAL_WORKING,
            mode=ExecutionMode.AUDIT_REVIEW,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# SIMULATION mode
# ---------------------------------------------------------------------------


class TestSimulationMode:
    def test_simulation_mode_verified_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.SIMULATION,
            trust="privileged",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_simulation_mode_session_state_write_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.SESSION_STATE,
            mode=ExecutionMode.SIMULATION,
            trust="privileged",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_simulation_mode_provisional_write_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.PROVISIONAL_ARCHIVE,
            mode=ExecutionMode.SIMULATION,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# CONSOLIDATION mode
# ---------------------------------------------------------------------------


class TestConsolidationMode:
    def test_consolidation_mode_verified_write_trusted_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.CONSOLIDATION,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_consolidation_mode_verified_write_provisional_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.CONSOLIDATION,
            trust="provisional",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_consolidation_mode_session_state_write_trusted_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.SESSION_STATE,
            mode=ExecutionMode.CONSOLIDATION,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# LIVE mode
# ---------------------------------------------------------------------------


class TestLiveMode:
    def test_live_mode_untrusted_verified_read_denies(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="untrusted",
        )
        assert result.verdict is GovernanceVerdict.DENY

    def test_live_mode_trusted_verified_read_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.ALLOW

    def test_live_mode_provisional_team_shared_allows(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.TEAM_SHARED,
            mode=ExecutionMode.LIVE,
            trust="provisional",
        )
        assert result.verdict is GovernanceVerdict.ALLOW


# ---------------------------------------------------------------------------
# ThreatContext on DENY
# ---------------------------------------------------------------------------


class TestDenyHasThreatContext:
    def test_deny_has_threat_context(self) -> None:
        ev = ViewPolicyEvaluator(owner_agent_id="owner")
        op = _read_op(agent_id="not-owner")
        result = _eval(ev, op, view=MemoryView.AGENT_PRIVATE, trust="privileged")
        assert result.verdict is GovernanceVerdict.DENY
        assert result.threat_context is not None

    def test_deny_threat_context_effective_view_set(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(
            ev,
            op,
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="trusted",
        )
        assert result.verdict is GovernanceVerdict.DENY
        assert result.threat_context is not None
        assert result.threat_context.effective_view != ""

    def test_policy_id_is_view_policy(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _write_op()
        result = _eval(ev, op, view=MemoryView.VERIFIED_ARCHIVE, trust="trusted")
        assert result.policy_id == "view_policy"


# ---------------------------------------------------------------------------
# after_op is a no-op
# ---------------------------------------------------------------------------


class TestAfterOpNoOp:
    def test_after_op_does_not_raise(self) -> None:
        ev = ViewPolicyEvaluator()
        op = _read_op()
        from veronica_core.memory.types import MemoryGovernanceDecision

        decision = MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            policy_id="view_policy",
            operation=op,
        )
        ev.after_op(op, decision)
        ev.after_op(op, decision, result="ok", error=ValueError("oops"))
