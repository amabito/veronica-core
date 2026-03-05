"""Policy rule type registry for VERONICA Declarative Policy Layer.

Maps rule type names to factory callables that create veronica-core components.
Builtin rule types are auto-registered at import time.
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import Any, Callable

from veronica_core.policy.schema import PolicyValidationError

logger = logging.getLogger(__name__)

# Callable signature: factory(params: dict) -> Any (a hook/component instance)
_RuleFactory = Callable[[dict[str, Any]], Any]


def _make_token_budget(params: dict[str, Any]) -> Any:
    from veronica_core.shield.token_budget import TokenBudgetHook

    return TokenBudgetHook(
        max_output_tokens=int(params.get("max_output_tokens", 100_000)),
        max_total_tokens=int(params.get("max_total_tokens", 0)),
        degrade_threshold=float(params.get("degrade_threshold", 0.8)),
    )


def _make_cost_ceiling(params: dict[str, Any]) -> Any:
    from veronica_core.budget import BudgetEnforcer

    return BudgetEnforcer(
        limit_usd=float(params.get("limit_usd", 10.0)),
    )


def _make_rate_limit(params: dict[str, Any]) -> Any:
    from veronica_core.shield.budget_window import BudgetWindowHook

    return BudgetWindowHook(
        max_calls=int(params.get("max_calls", 100)),
        window_seconds=float(params.get("window_seconds", 60.0)),
        degrade_threshold=float(params.get("degrade_threshold", 0.8)),
    )


def _make_circuit_breaker(params: dict[str, Any]) -> Any:
    from veronica_core.circuit_breaker import CircuitBreaker

    return CircuitBreaker(
        failure_threshold=int(params.get("failure_threshold", 5)),
        recovery_timeout=float(params.get("recovery_timeout", 60.0)),
    )


def _make_step_limit(params: dict[str, Any]) -> Any:
    from veronica_core.agent_guard import AgentStepGuard

    return AgentStepGuard(
        max_steps=int(params.get("max_steps", 25)),
    )


def _make_time_limit(params: dict[str, Any]) -> Any:
    from datetime import time as _time
    from veronica_core.shield.time_policy import TimeAwarePolicy

    return TimeAwarePolicy(
        weekend_multiplier=float(params.get("weekend_multiplier", 0.85)),
        offhour_multiplier=float(params.get("offhour_multiplier", 0.90)),
        work_start=_time(
            int(params.get("work_start_hour", 9)),
            int(params.get("work_start_minute", 0)),
        ),
        work_end=_time(
            int(params.get("work_end_hour", 18)),
            int(params.get("work_end_minute", 0)),
        ),
    )


def _make_metric_rule(params: dict[str, Any]) -> Any:
    from veronica_core.policy.metrics_policy import (
        MetricRule,
        MetricsDrivenPolicy,
        get_default_ingester,
    )

    rules_raw = params.get("rules")
    if rules_raw is not None and not isinstance(rules_raw, list):
        raise TypeError(
            f"metric_rule 'rules' must be a list, got {type(rules_raw).__name__}"
        )
    rules: list[MetricRule] = []
    for r in rules_raw or []:
        if not isinstance(r, dict):
            raise TypeError(
                f"Each rule must be a dict, got {type(r).__name__}"
            )
        # Explicit None-guards: YAML/JSON null maps to Python None even when
        # the key is present. Fall back to intended defaults rather than
        # propagating TypeError from float(None).
        raw_threshold = r.get("threshold")
        threshold_val = float(raw_threshold) if raw_threshold is not None else 0.0
        raw_label = r.get("label")
        label_val = str(raw_label) if raw_label is not None else ""
        raw_agent_id = r.get("agent_id")
        agent_id_val = str(raw_agent_id) if raw_agent_id is not None else None
        rules.append(
            MetricRule(
                metric=str(r.get("metric") or "total_cost_usd"),
                operator=str(r.get("operator") or "gt"),
                threshold=threshold_val,
                action=str(r.get("action") or "warn"),
                agent_id=agent_id_val,
                label=label_val,
            )
        )
    ingester = get_default_ingester()
    agent_id = params.get("agent_id")
    agent_id = str(agent_id) if agent_id is not None else None
    return MetricsDrivenPolicy(rules=rules, ingester=ingester, agent_id=agent_id)


_BUILTIN_FACTORIES: dict[str, _RuleFactory] = {
    "token_budget": _make_token_budget,
    "cost_ceiling": _make_cost_ceiling,
    "rate_limit": _make_rate_limit,
    "circuit_breaker": _make_circuit_breaker,
    "step_limit": _make_step_limit,
    "time_limit": _make_time_limit,
    "metric_rule": _make_metric_rule,
}


class PolicyRegistry:
    """Registry mapping rule type names to factory callables.

    A single default instance is available as ``PolicyRegistry.default()``.
    Custom rule types can be registered via ``register_rule_type()``.
    """

    def __init__(self) -> None:
        self._factories: dict[str, _RuleFactory] = dict(_BUILTIN_FACTORIES)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "PolicyRegistry":
        """Return the module-level default singleton instance."""
        return _DEFAULT_REGISTRY

    def register_rule_type(self, name: str, factory: _RuleFactory) -> None:
        """Register a custom rule type factory.

        If the name is already registered, a warning is emitted and the
        existing registration is overwritten (last-write-wins semantics).

        Args:
            name:    Rule type identifier (must match ``RuleSchema.type``).
            factory: Callable ``(params: dict) -> component``.
        """
        if not name or not isinstance(name, str):
            raise PolicyValidationError(
                ["register_rule_type: name must be a non-empty string"],
                field_name="name",
            )
        with self._lock:
            if name in self._factories:
                warnings.warn(
                    f"PolicyRegistry: overwriting existing rule type {name!r}",
                    UserWarning,
                    stacklevel=2,
                )
            self._factories[name] = factory

    def get_rule_type(self, name: str) -> _RuleFactory:
        """Return the factory for *name*.

        Raises:
            PolicyValidationError: if *name* is not registered.
        """
        with self._lock:
            factory = self._factories.get(name)
            if factory is not None:
                return factory
            known = sorted(self._factories)
        raise PolicyValidationError(
            [f"Unknown rule type {name!r}. Registered types: {known}"],
            field_name="type",
        )

    def known_types(self) -> list[str]:
        """Return sorted list of all registered rule type names."""
        with self._lock:
            return sorted(self._factories)


# Module-level default singleton — created after the class definition.
_DEFAULT_REGISTRY = PolicyRegistry()
