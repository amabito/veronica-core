"""VERONICA Budget enforcer -- pre-check, reserve, commit, threshold alerts."""
from __future__ import annotations

from typing import Any

from veronica.budget.ledger import BudgetLedger
from veronica.budget.policy import BudgetPolicy, Scope, WindowKind
from veronica.runtime.events import EventBus, EventTypes, make_event
from veronica.runtime.models import Labels, Run, RunStatus, Severity
from veronica.runtime.state_machine import InvalidTransitionError, transition_run

DEFAULT_LLM_COST_USD: float = 0.01
DEFAULT_TOOL_COST_USD: float = 0.001

_ALL_WINDOWS: list[WindowKind] = [WindowKind.MINUTE, WindowKind.HOUR, WindowKind.DAY]


class BudgetExceeded(Exception):
    """Raised when a budget limit would be exceeded."""

    def __init__(
        self,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        limit_usd: float,
        used_usd: float,
    ) -> None:
        self.scope = scope
        self.scope_id = scope_id
        self.window = window
        self.limit_usd = limit_usd
        self.used_usd = used_usd
        super().__init__(
            f"Budget exceeded: {scope.value}/{scope_id} window={window.value} "
            f"used={used_usd:.6f} limit={limit_usd:.6f}"
        )


class BudgetEnforcer:
    """Enforces budget policy by gating LLM/tool calls and emitting events."""

    def __init__(
        self,
        policy: BudgetPolicy,
        ledger: BudgetLedger,
        bus: EventBus,
    ) -> None:
        self._policy = policy
        self._ledger = ledger
        self._bus = bus
        # Dedup set: (scope.value, scope_id, window.value, window_id, threshold)
        self._thresholds_fired: set[tuple[str, str, str, str, float]] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pre_check_and_reserve(
        self,
        run_id: str,
        labels: Labels,
        kind: str,
        estimated_cost_usd: float | None = None,
    ) -> float:
        """Check all scopes/windows, then reserve. Raises BudgetExceeded if any limit violated.

        Returns the estimated cost that was reserved.
        """
        est = estimated_cost_usd
        if est is None:
            est = DEFAULT_LLM_COST_USD if kind == "llm_call" else DEFAULT_TOOL_COST_USD

        scopes = self._build_scope_list(labels)

        # Phase 1: dry-run check ALL scopes x ALL windows
        for scope, scope_id in scopes:
            limit_set = self._policy.get_limit(scope, scope_id)
            for window in _ALL_WINDOWS:
                limit = limit_set.limit_for(window)
                used = self._ledger.used(scope, scope_id, window)
                if used + est > limit:
                    self._emit_denied(run_id, labels, scope, scope_id, window, limit, used, est)
                    raise BudgetExceeded(scope, scope_id, window, limit, used + est)

        # Phase 2: reserve in ALL scopes x ALL windows
        for scope, scope_id in scopes:
            for window in _ALL_WINDOWS:
                self._ledger.reserve(scope, scope_id, window, est)

        self._emit_reserve_ok(run_id, labels, scopes, est)
        return est

    def post_charge(
        self,
        run: Run,
        labels: Labels,
        reserved_usd: float,
        actual_cost_usd: float | None = None,
    ) -> None:
        """Commit spend, check thresholds, and emit budget.commit event."""
        actual = actual_cost_usd if actual_cost_usd is not None else reserved_usd
        scopes = self._build_scope_list(labels)

        for scope, scope_id in scopes:
            limit_set = self._policy.get_limit(scope, scope_id)
            for window in _ALL_WINDOWS:
                self._ledger.commit(scope, scope_id, window, reserved_usd, actual)
                limit = limit_set.limit_for(window)
                used = self._ledger.committed(scope, scope_id, window)
                self._check_thresholds(run, scope, scope_id, window, limit, used)

        self._emit_commit(run.run_id, labels, scopes, reserved_usd, actual)

    def release_reservation(
        self,
        labels: Labels,
        reserved_usd: float,
    ) -> None:
        """Release a reservation without committing (e.g. on call failure)."""
        scopes = self._build_scope_list(labels)
        for scope, scope_id in scopes:
            for window in _ALL_WINDOWS:
                self._ledger.release(scope, scope_id, window, reserved_usd)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_scope_list(self, labels: Labels) -> list[tuple[Scope, str]]:
        """Return (Scope, scope_id) pairs for all non-empty label fields."""
        result: list[tuple[Scope, str]] = []
        if labels.org:
            result.append((Scope.ORG, labels.org))
        if labels.team:
            result.append((Scope.TEAM, labels.team))
        if labels.user:
            result.append((Scope.USER, labels.user))
        if labels.service:
            result.append((Scope.SERVICE, labels.service))
        return result

    def _check_thresholds(
        self,
        run: Run,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        limit: float,
        used: float,
    ) -> None:
        """Fire threshold events and transition Run status as needed."""
        if limit <= 0:
            return

        ratio = used / limit
        window_id = self._ledger.window_id(window)

        for threshold in self._policy.thresholds:
            if ratio < threshold:
                continue
            dedup_key = (scope.value, scope_id, window.value, window_id, threshold)
            if dedup_key in self._thresholds_fired:
                continue
            self._thresholds_fired.add(dedup_key)

            self._emit_threshold(run.run_id, scope, scope_id, window, limit, used, threshold)

            # State transitions: >= 90% -> DEGRADED, >= 100% -> HALTED
            if threshold >= 1.0:
                old_status = run.status
                if not _is_terminal(old_status):
                    try:
                        transition_run(run, RunStatus.HALTED, reason="budget_exceeded")
                    except InvalidTransitionError:
                        pass
                    else:
                        self._emit_state_changed(
                            run.run_id, old_status, RunStatus.HALTED, "budget_exceeded"
                        )
            elif threshold >= 0.9:
                old_status = run.status
                if old_status is RunStatus.RUNNING:
                    try:
                        transition_run(run, RunStatus.DEGRADED, reason="budget_threshold_90pct")
                    except InvalidTransitionError:
                        pass
                    else:
                        self._emit_state_changed(
                            run.run_id, old_status, RunStatus.DEGRADED, "budget_threshold_90pct"
                        )

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    def _emit_denied(
        self,
        run_id: str,
        labels: Labels,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        limit_usd: float,
        used_usd: float,
        estimated_usd: float,
    ) -> None:
        payload: dict[str, Any] = {
            "scope": scope.value,
            "scope_id": scope_id,
            "window": window.value,
            "limit_usd": limit_usd,
            "used_usd": used_usd,
            "estimated_usd": estimated_usd,
        }
        event = make_event(
            EventTypes.BUDGET_RESERVE_DENIED,
            run_id,
            severity=Severity.WARN,
            labels=labels,
            payload=payload,
        )
        self._bus.emit(event)

    def _emit_reserve_ok(
        self,
        run_id: str,
        labels: Labels,
        scopes: list[tuple[Scope, str]],
        estimated_usd: float,
    ) -> None:
        payload: dict[str, Any] = {
            "estimated_usd": estimated_usd,
            "scopes": [{"scope": s.value, "scope_id": sid} for s, sid in scopes],
        }
        event = make_event(
            EventTypes.BUDGET_RESERVE_OK,
            run_id,
            labels=labels,
            payload=payload,
        )
        self._bus.emit(event)

    def _emit_commit(
        self,
        run_id: str,
        labels: Labels,
        scopes: list[tuple[Scope, str]],
        reserved_usd: float,
        actual_usd: float,
    ) -> None:
        payload: dict[str, Any] = {
            "reserved_usd": reserved_usd,
            "actual_usd": actual_usd,
            "scopes": [{"scope": s.value, "scope_id": sid} for s, sid in scopes],
        }
        event = make_event(
            EventTypes.BUDGET_COMMIT,
            run_id,
            labels=labels,
            payload=payload,
        )
        self._bus.emit(event)

    def _emit_threshold(
        self,
        run_id: str,
        scope: Scope,
        scope_id: str,
        window: WindowKind,
        limit_usd: float,
        used_usd: float,
        threshold: float,
    ) -> None:
        severity = Severity.CRITICAL if threshold >= 1.0 else Severity.WARN
        payload: dict[str, Any] = {
            "scope": scope.value,
            "scope_id": scope_id,
            "window": window.value,
            "threshold": threshold,
            "limit_usd": limit_usd,
            "used_usd": used_usd,
            "ratio": used_usd / limit_usd if limit_usd > 0 else 0.0,
        }
        event = make_event(
            EventTypes.BUDGET_THRESHOLD_CROSSED,
            run_id,
            severity=severity,
            payload=payload,
        )
        self._bus.emit(event)

    def _emit_state_changed(
        self,
        run_id: str,
        old_status: RunStatus,
        new_status: RunStatus,
        reason: str,
    ) -> None:
        payload: dict[str, Any] = {
            "from": old_status.value,
            "to": new_status.value,
            "reason": reason,
        }
        event = make_event(
            EventTypes.RUN_STATE_CHANGED,
            run_id,
            severity=Severity.WARN,
            payload=payload,
        )
        self._bus.emit(event)


def _is_terminal(status: RunStatus) -> bool:
    return status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
