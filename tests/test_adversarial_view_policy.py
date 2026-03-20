"""Adversarial tests for ViewPolicyEvaluator -- attacker mindset.

Attack categories:
1. Unknown/garbage trust levels -- unknown strings, null bytes, whitespace, None coercion
2. Write action classification -- all WRITE_ACTIONS treated as writes; READ/RETRIEVE are not
3. Privilege escalation attempts -- untrusted/provisional/trusted agents exceeding their rank
4. Null/missing context -- None context defaults to LOCAL_WORKING, LIVE, "untrusted"
5. Concurrent access -- 10 threads, mixed views/modes/trust, all decisions deterministic
6. SESSION_STATE access matrix -- full trust x action x mode grid
7. PROVISIONAL_ARCHIVE access -- untrusted read/write, provisional read, trusted write
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

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


def _op(
    action: MemoryAction = MemoryAction.READ,
    agent_id: str = "agent-x",
    **kwargs: Any,
) -> MemoryOperation:
    return MemoryOperation(action=action, agent_id=agent_id, resource_id="r", **kwargs)


def _ctx(
    view: MemoryView = MemoryView.LOCAL_WORKING,
    mode: ExecutionMode = ExecutionMode.LIVE,
    trust: str = "untrusted",
    operation: MemoryOperation | None = None,
) -> MemoryPolicyContext:
    op = operation or _op()
    return MemoryPolicyContext(
        operation=op,
        memory_view=view,
        execution_mode=mode,
        trust_level=trust,
    )


def _eval(
    evaluator: ViewPolicyEvaluator,
    action: MemoryAction,
    view: MemoryView,
    mode: ExecutionMode,
    trust: str,
    agent_id: str = "agent-x",
) -> MemoryGovernanceDecision:
    op = _op(action=action, agent_id=agent_id)
    ctx = _ctx(view=view, mode=mode, trust=trust, operation=op)
    return evaluator.before_op(op, ctx)


# ---------------------------------------------------------------------------
# 1. Unknown/Garbage Trust Levels
# ---------------------------------------------------------------------------


class TestAdversarialGarbageTrustLevels:
    """Garbage or malformed trust_level values must fall back to rank 0 (untrusted)."""

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    def test_empty_string_trust_treated_as_untrusted(self) -> None:
        """trust_level="" must resolve to rank 0 -- denied from VERIFIED_ARCHIVE."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(view=MemoryView.VERIFIED_ARCHIVE, mode=ExecutionMode.LIVE, trust="")

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.denied, "empty trust must be treated as untrusted (rank 0)"

    def test_wrong_case_TRUSTED_treated_as_trusted(self) -> None:
        """trust_level='TRUSTED' must match 'trusted' via .lower() -- rank 2."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(
            view=MemoryView.VERIFIED_ARCHIVE, mode=ExecutionMode.LIVE, trust="TRUSTED"
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert -- .lower() normalizes "TRUSTED" -> rank 2 -> allowed
        assert decision.allowed, "'TRUSTED' (upper) must match 'trusted' after .lower()"

    def test_nonexistent_level_admin_treated_as_untrusted(self) -> None:
        """trust_level='admin' is not in _TRUST_RANK -- defaults to rank 0."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(
            view=MemoryView.VERIFIED_ARCHIVE, mode=ExecutionMode.LIVE, trust="admin"
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.denied, (
            "'admin' is not a recognised trust level -- must be rank 0"
        )

    def test_null_byte_injection_in_trust_treated_as_untrusted(self) -> None:
        """Null byte injection must not bypass trust parsing."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(
            view=MemoryView.VERIFIED_ARCHIVE,
            mode=ExecutionMode.LIVE,
            trust="privileged\x00injected",
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.denied, (
            "null byte injected trust must not grant privileged rank"
        )

    def test_whitespace_around_trust_treated_as_untrusted(self) -> None:
        """Leading/trailing whitespace must not cause 'trusted' match."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(
            view=MemoryView.SESSION_STATE, mode=ExecutionMode.LIVE, trust=" trusted "
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert -- " trusted ".lower() == " trusted " which is NOT in _TRUST_RANK
        assert decision.denied, "' trusted ' (with spaces) must not match 'trusted'"

    def test_none_coerced_to_string_treated_as_untrusted(self) -> None:
        """trust_level=str(None)='None' is not a valid trust level -- rank 0."""
        # Arrange
        op = _op(action=MemoryAction.READ)
        # MemoryPolicyContext.trust_level is typed str; pass str(None) to simulate coercion.
        ctx = _ctx(
            view=MemoryView.SESSION_STATE,
            mode=ExecutionMode.LIVE,
            trust=str(None),  # "None"
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.denied, "str(None)='None' must be treated as rank 0 (untrusted)"


# ---------------------------------------------------------------------------
# 2. Write Action Classification
# ---------------------------------------------------------------------------


class TestAdversarialWriteActionClassification:
    """Every write action must be classified as a write; read actions must not be."""

    WRITE_ACTIONS = [
        MemoryAction.WRITE,
        MemoryAction.ARCHIVE,
        MemoryAction.CONSOLIDATE,
        MemoryAction.DELETE,
        MemoryAction.QUARANTINE,
    ]
    READ_ACTIONS = [
        MemoryAction.READ,
        MemoryAction.RETRIEVE,
    ]

    def setup_method(self) -> None:
        # LOCAL_WORKING allows all trust levels for read/write, so verdicts isolate
        # the write-classification logic only for the REPLAY-mode write-deny rule.
        self.evaluator = ViewPolicyEvaluator()

    @pytest.mark.parametrize("action", WRITE_ACTIONS)
    def test_write_action_denied_in_replay_mode(self, action: MemoryAction) -> None:
        """Every write action must be denied in REPLAY mode."""
        # Arrange
        op = _op(action=action)
        ctx = _ctx(
            view=MemoryView.LOCAL_WORKING, mode=ExecutionMode.REPLAY, trust="privileged"
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.denied, (
            f"{action.value} must be classified as a write (denied in REPLAY)"
        )

    @pytest.mark.parametrize("action", READ_ACTIONS)
    def test_read_action_allowed_in_replay_mode(self, action: MemoryAction) -> None:
        """READ and RETRIEVE must NOT be classified as writes -- allowed in REPLAY."""
        # Arrange
        op = _op(action=action)
        ctx = _ctx(
            view=MemoryView.LOCAL_WORKING, mode=ExecutionMode.REPLAY, trust="untrusted"
        )

        # Act
        decision = self.evaluator.before_op(op, ctx)

        # Assert
        assert decision.allowed, (
            f"{action.value} must NOT be a write -- must be allowed in REPLAY"
        )


# ---------------------------------------------------------------------------
# 3. Privilege Escalation Attempts
# ---------------------------------------------------------------------------


class TestAdversarialPrivilegeEscalation:
    """Agents must not exceed their trust rank to access restricted views."""

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    def test_untrusted_read_verified_archive_in_live_denied(self) -> None:
        """Untrusted agent reading VERIFIED_ARCHIVE in LIVE mode must be denied."""
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.VERIFIED_ARCHIVE,
            ExecutionMode.LIVE,
            "untrusted",
        )
        assert decision.denied

    def test_provisional_read_verified_archive_in_live_denied(self) -> None:
        """Provisional agent (rank 1) is below trusted (rank 2) -- VERIFIED_ARCHIVE denied."""
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.VERIFIED_ARCHIVE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_provisional_write_team_shared_denied(self) -> None:
        """Provisional agent writing to TEAM_SHARED must be denied (requires trusted)."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.TEAM_SHARED,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_trusted_read_quarantined_in_live_denied(self) -> None:
        """Trusted agent reading QUARANTINED in LIVE mode must be denied (requires privileged)."""
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.QUARANTINED,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.denied

    def test_untrusted_write_verified_archive_in_consolidation_denied(self) -> None:
        """Untrusted agent writing VERIFIED_ARCHIVE in CONSOLIDATION must be denied."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.VERIFIED_ARCHIVE,
            ExecutionMode.CONSOLIDATION,
            "untrusted",
        )
        assert decision.denied

    def test_provisional_write_verified_archive_in_consolidation_denied(self) -> None:
        """Provisional agent writing VERIFIED_ARCHIVE in CONSOLIDATION must be denied."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.VERIFIED_ARCHIVE,
            ExecutionMode.CONSOLIDATION,
            "provisional",
        )
        assert decision.denied

    def test_simulation_mode_privileged_write_session_state_denied(self) -> None:
        """Even privileged agent cannot write SESSION_STATE in SIMULATION mode."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.SIMULATION,
            "privileged",
        )
        assert decision.denied

    def test_simulation_mode_trusted_write_verified_archive_denied(self) -> None:
        """Even trusted agent cannot write VERIFIED_ARCHIVE in SIMULATION mode."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.VERIFIED_ARCHIVE,
            ExecutionMode.SIMULATION,
            "trusted",
        )
        assert decision.denied


# ---------------------------------------------------------------------------
# 4. Null/Missing Context
# ---------------------------------------------------------------------------


class TestAdversarialNullContext:
    """context=None must yield safe defaults: LOCAL_WORKING, LIVE, 'untrusted'."""

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    def test_none_context_read_allows_local_working(self) -> None:
        """None context defaults to LOCAL_WORKING -- read allowed for untrusted."""
        op = _op(action=MemoryAction.READ)
        decision = self.evaluator.before_op(op, None)
        assert decision.allowed, "None context -> LOCAL_WORKING -> read always allowed"

    def test_none_context_write_allows_local_working(self) -> None:
        """None context defaults to LOCAL_WORKING -- write allowed (no restriction)."""
        op = _op(action=MemoryAction.WRITE)
        decision = self.evaluator.before_op(op, None)
        assert decision.allowed, (
            "None context -> LOCAL_WORKING -> write allowed for any trust"
        )

    def test_none_context_defaults_to_live_mode(self) -> None:
        """Verify that None context does NOT grant REPLAY/AUDIT_REVIEW/CONSOLIDATION privileges.

        The default mode is LIVE. An untrusted agent with LIVE+LOCAL_WORKING gets
        ALLOW for reads. If the default were CONSOLIDATION, trusted writes to
        VERIFIED_ARCHIVE would be allowed incorrectly.

        We probe this by using a custom evaluator path: we cannot inject a view
        from None context, so we verify via the known LIVE+LOCAL_WORKING behaviour:
        any trust level may read/write LOCAL_WORKING in LIVE.
        """
        # The real probe: in REPLAY mode, writes would be denied even for LOCAL_WORKING.
        # With None context (LIVE), LOCAL_WORKING write must be allowed.
        op = _op(action=MemoryAction.WRITE)
        decision = self.evaluator.before_op(op, None)
        assert decision.allowed, "LIVE mode (default) allows LOCAL_WORKING writes"

    def test_none_context_untrusted_cannot_read_verified_archive_in_live(self) -> None:
        """Demonstrate that None context yields untrusted -- VERIFIED_ARCHIVE read denied.

        Since None context fixes view=LOCAL_WORKING, we verify the trust default
        indirectly: build a real LIVE+VERIFIED_ARCHIVE context with no trust
        (empty string, which resolves to rank 0) and confirm it is denied.
        """
        op = _op(action=MemoryAction.READ)
        ctx = _ctx(view=MemoryView.VERIFIED_ARCHIVE, mode=ExecutionMode.LIVE, trust="")
        decision = self.evaluator.before_op(op, ctx)
        assert decision.denied, "rank-0 trust must not read VERIFIED_ARCHIVE in LIVE"


# ---------------------------------------------------------------------------
# 5. Concurrent Access
# ---------------------------------------------------------------------------


class TestAdversarialConcurrentAccess:
    """10 threads hitting the same ViewPolicyEvaluator must all get valid decisions."""

    NUM_THREADS = 10

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    def test_concurrent_mixed_views_all_return_valid_verdicts(self) -> None:
        """No thread should raise or return an invalid verdict under concurrent load."""
        # Arrange
        scenarios = [
            (
                MemoryAction.READ,
                MemoryView.LOCAL_WORKING,
                ExecutionMode.LIVE,
                "untrusted",
            ),
            (MemoryAction.WRITE, MemoryView.TEAM_SHARED, ExecutionMode.LIVE, "trusted"),
            (
                MemoryAction.READ,
                MemoryView.VERIFIED_ARCHIVE,
                ExecutionMode.LIVE,
                "trusted",
            ),
            (
                MemoryAction.WRITE,
                MemoryView.VERIFIED_ARCHIVE,
                ExecutionMode.CONSOLIDATION,
                "trusted",
            ),
            (
                MemoryAction.READ,
                MemoryView.SESSION_STATE,
                ExecutionMode.LIVE,
                "trusted",
            ),
            (
                MemoryAction.WRITE,
                MemoryView.SESSION_STATE,
                ExecutionMode.LIVE,
                "privileged",
            ),
            (
                MemoryAction.READ,
                MemoryView.PROVISIONAL_ARCHIVE,
                ExecutionMode.LIVE,
                "provisional",
            ),
            (
                MemoryAction.READ,
                MemoryView.QUARANTINED,
                ExecutionMode.AUDIT_REVIEW,
                "trusted",
            ),
            (
                MemoryAction.READ,
                MemoryView.VERIFIED_ARCHIVE,
                ExecutionMode.LIVE,
                "untrusted",
            ),
            (
                MemoryAction.WRITE,
                MemoryView.LOCAL_WORKING,
                ExecutionMode.REPLAY,
                "privileged",
            ),
        ]
        assert len(scenarios) == self.NUM_THREADS

        results: list[MemoryGovernanceDecision | BaseException] = [
            None
        ] * self.NUM_THREADS  # type: ignore[list-item]

        def run(
            index: int,
            action: MemoryAction,
            view: MemoryView,
            mode: ExecutionMode,
            trust: str,
        ) -> None:
            try:
                results[index] = _eval(self.evaluator, action, view, mode, trust)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc

        threads = [
            threading.Thread(target=run, args=(i, *scenarios[i]))
            for i in range(self.NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Assert
        for i, result in enumerate(results):
            assert not isinstance(result, BaseException), (
                f"Thread {i} raised {result!r}"
            )
            assert isinstance(result, MemoryGovernanceDecision), (
                f"Thread {i} did not return a MemoryGovernanceDecision"
            )
            assert result.verdict in GovernanceVerdict, (
                f"Thread {i} verdict {result.verdict!r} is not a valid GovernanceVerdict"
            )

    def test_concurrent_same_scenario_deterministic(self) -> None:
        """10 threads with identical inputs must all get identical verdicts."""
        # Arrange -- a scenario that is clearly DENY
        results: list[MemoryGovernanceDecision | None] = [None] * self.NUM_THREADS

        def run(index: int) -> None:
            results[index] = _eval(
                self.evaluator,
                MemoryAction.READ,
                MemoryView.VERIFIED_ARCHIVE,
                ExecutionMode.LIVE,
                "untrusted",
            )

        threads = [
            threading.Thread(target=run, args=(i,)) for i in range(self.NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Assert
        assert all(r is not None for r in results)
        assert all(r.denied for r in results), "All threads must agree: DENY"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 6. SESSION_STATE Access Matrix
# ---------------------------------------------------------------------------


class TestAdversarialSessionStateMatrix:
    """Full trust x action x mode grid for SESSION_STATE."""

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    # --- Read matrix ---

    def test_session_state_read_untrusted_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "untrusted",
        )
        assert decision.denied

    def test_session_state_read_provisional_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_session_state_read_trusted_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.allowed

    def test_session_state_read_privileged_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "privileged",
        )
        assert decision.allowed

    # --- Write matrix (LIVE mode) ---

    def test_session_state_write_untrusted_live_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "untrusted",
        )
        assert decision.denied

    def test_session_state_write_provisional_live_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_session_state_write_trusted_live_denied(self) -> None:
        """In LIVE mode, SESSION_STATE write requires 'privileged' -- trusted is not enough."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.denied

    def test_session_state_write_privileged_live_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.LIVE,
            "privileged",
        )
        assert decision.allowed

    # --- Write matrix (CONSOLIDATION mode) ---

    def test_session_state_write_trusted_consolidation_allowed(self) -> None:
        """In CONSOLIDATION mode, trusted+ may write SESSION_STATE."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.CONSOLIDATION,
            "trusted",
        )
        assert decision.allowed

    def test_session_state_write_provisional_consolidation_denied(self) -> None:
        """Provisional (rank 1) cannot write SESSION_STATE even in CONSOLIDATION."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.CONSOLIDATION,
            "provisional",
        )
        assert decision.denied

    # --- SIMULATION mode override ---

    def test_session_state_write_privileged_simulation_denied(self) -> None:
        """SIMULATION mode forbids writes to SESSION_STATE regardless of trust."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.SIMULATION,
            "privileged",
        )
        assert decision.denied

    def test_session_state_write_trusted_simulation_denied(self) -> None:
        """SIMULATION mode forbids writes to SESSION_STATE regardless of trust."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.SIMULATION,
            "trusted",
        )
        assert decision.denied

    # --- REPLAY mode ---

    def test_session_state_write_privileged_replay_denied(self) -> None:
        """REPLAY mode denies all writes, including SESSION_STATE for privileged."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.SESSION_STATE,
            ExecutionMode.REPLAY,
            "privileged",
        )
        assert decision.denied

    def test_session_state_read_trusted_replay_allowed(self) -> None:
        """REPLAY mode only blocks writes -- reads still follow trust matrix."""
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.SESSION_STATE,
            ExecutionMode.REPLAY,
            "trusted",
        )
        assert decision.allowed


# ---------------------------------------------------------------------------
# 7. PROVISIONAL_ARCHIVE Access
# ---------------------------------------------------------------------------


class TestAdversarialProvisionalArchiveAccess:
    """PROVISIONAL_ARCHIVE: read requires provisional+, write requires trusted+."""

    def setup_method(self) -> None:
        self.evaluator = ViewPolicyEvaluator()

    # --- Read ---

    def test_provisional_archive_read_untrusted_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "untrusted",
        )
        assert decision.denied

    def test_provisional_archive_read_provisional_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.allowed

    def test_provisional_archive_read_trusted_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.allowed

    def test_provisional_archive_read_privileged_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.READ,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "privileged",
        )
        assert decision.allowed

    # --- Write ---

    def test_provisional_archive_write_untrusted_denied(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "untrusted",
        )
        assert decision.denied

    def test_provisional_archive_write_provisional_denied(self) -> None:
        """Provisional (rank 1) cannot write PROVISIONAL_ARCHIVE -- requires trusted (rank 2)."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_provisional_archive_write_trusted_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.allowed

    def test_provisional_archive_write_privileged_allowed(self) -> None:
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "privileged",
        )
        assert decision.allowed

    # --- ARCHIVE and DELETE are also write actions ---

    def test_provisional_archive_archive_action_provisional_denied(self) -> None:
        """ARCHIVE is a write action -- provisional must be denied."""
        decision = _eval(
            self.evaluator,
            MemoryAction.ARCHIVE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "provisional",
        )
        assert decision.denied

    def test_provisional_archive_delete_action_trusted_allowed(self) -> None:
        """DELETE is a write action -- trusted must be allowed."""
        decision = _eval(
            self.evaluator,
            MemoryAction.DELETE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.LIVE,
            "trusted",
        )
        assert decision.allowed

    # --- REPLAY blocks all writes ---

    def test_provisional_archive_write_trusted_replay_denied(self) -> None:
        """REPLAY mode denies all writes to PROVISIONAL_ARCHIVE regardless of trust."""
        decision = _eval(
            self.evaluator,
            MemoryAction.WRITE,
            MemoryView.PROVISIONAL_ARCHIVE,
            ExecutionMode.REPLAY,
            "trusted",
        )
        assert decision.denied
