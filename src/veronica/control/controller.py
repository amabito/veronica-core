"""VERONICA Degrade controller -- stateful, hysteresis, event emission."""
from __future__ import annotations

import time as time_mod
from dataclasses import dataclass
from typing import Any

from veronica.control.decision import (
    ControlSignals,
    Decision,
    DegradeConfig,
    DegradeLevel,
    RequestMeta,
    SchedulerMode,
    compute_level,
    decide,
)
from veronica.runtime.events import EventBus, EventTypes, make_event
from veronica.runtime.models import Severity


@dataclass
class _ScopeState:
    level: DegradeLevel = DegradeLevel.NORMAL
    last_change: float = 0.0
    stable_since: float | None = None
    consecutive_failures: int = 0


class DegradeController:
    def __init__(self, config: DegradeConfig | None = None, bus: EventBus | None = None) -> None:
        self._config = config or DegradeConfig()
        self._bus = bus
        self._states: dict[tuple[str, str], _ScopeState] = {}

    def get_level(self, org: str, team: str) -> DegradeLevel:
        state = self._states.get((org, team))
        return state.level if state else DegradeLevel.NORMAL

    def evaluate(self, org: str, team: str, signals: ControlSignals, request: RequestMeta, run_id: str = "") -> Decision:
        state = self._states.setdefault((org, team), _ScopeState())
        proposed_level, reasons = compute_level(signals, self._config)
        now = time_mod.monotonic()
        old_level = state.level

        # Escalation: immediate
        if proposed_level > state.level:
            state.level = proposed_level
            state.last_change = now
            state.stable_since = now
            self._emit_level_changed(run_id, org, team, old_level, state.level, reasons)
        # Recovery: must be stable for recovery_window_s, recover ONE level at a time
        elif proposed_level < state.level:
            if state.stable_since is None:
                state.stable_since = now
            elif (now - state.stable_since) >= self._config.recovery_window_s:
                state.level = DegradeLevel(state.level - 1)
                state.last_change = now
                state.stable_since = now
                self._emit_level_changed(run_id, org, team, old_level, state.level, ["recovery"])
        else:
            state.stable_since = now

        # Generate decision at controller's stateful level
        decision = decide(signals, request, self._config)
        decision.level = state.level
        decision = self._apply_level_overrides(decision, state.level, request)
        self._emit_decision(run_id, org, team, decision)
        return decision

    def feed_result(self, org: str, team: str, success: bool) -> None:
        state = self._states.setdefault((org, team), _ScopeState())
        if success:
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1

    def get_consecutive_failures(self, org: str, team: str) -> int:
        state = self._states.get((org, team))
        return state.consecutive_failures if state else 0

    def _apply_level_overrides(self, decision: Decision, level: DegradeLevel, request: RequestMeta) -> Decision:
        cfg = self._config
        if level >= DegradeLevel.SOFT:
            if request.cheap_model and not decision.model_override:
                decision.model_override = request.cheap_model
            cap = int(request.max_tokens * cfg.max_tokens_pct_level1)
            decision.max_tokens_cap = max(cap, cfg.max_tokens_floor_level1)
            decision.retry_cap_override = min(
                decision.retry_cap_override if decision.retry_cap_override is not None else 99, 2
            )
        if level >= DegradeLevel.HARD:
            cap = int(request.max_tokens * cfg.max_tokens_pct_level2)
            decision.max_tokens_cap = max(cap, cfg.max_tokens_floor_level2)
            decision.allow_tools = False
            decision.allowed_tools = frozenset()
            decision.retry_cap_override = min(
                decision.retry_cap_override if decision.retry_cap_override is not None else 99, 1
            )
            decision.scheduler_mode = SchedulerMode.QUEUE_PREFER
        if level >= DegradeLevel.EMERGENCY:
            if request.kind == "llm_call" and request.priority != "P0":
                decision.allow_llm = False
            decision.max_tokens_cap = cfg.max_tokens_cap_level3
            decision.retry_cap_override = 0
            decision.scheduler_mode = SchedulerMode.REJECT_PREFER
        return decision

    def _emit_level_changed(
        self,
        run_id: str,
        org: str,
        team: str,
        old: DegradeLevel,
        new: DegradeLevel,
        reasons: list[str],
    ) -> None:
        if not self._bus:
            return
        payload: dict[str, Any] = {
            "org": org,
            "team": team,
            "from_level": old.value,
            "to_level": new.value,
            "reasons": reasons,
        }
        severity = Severity.CRITICAL if new >= DegradeLevel.EMERGENCY else Severity.WARN
        self._bus.emit(make_event(EventTypes.CONTROL_LEVEL_CHANGED, run_id, severity=severity, payload=payload))

    def _emit_decision(self, run_id: str, org: str, team: str, decision: Decision) -> None:
        if not self._bus:
            return
        payload: dict[str, Any] = {
            "org": org,
            "team": team,
            "level": decision.level.value,
            "allow_llm": decision.allow_llm,
            "allow_tools": decision.allow_tools,
            "model_override": decision.model_override,
            "max_tokens_cap": decision.max_tokens_cap,
            "scheduler_mode": decision.scheduler_mode.value,
            "reason_codes": decision.reason_codes,
        }
        self._bus.emit(make_event(EventTypes.CONTROL_DECISION_MADE, run_id, payload=payload))
