"""Memory policy rule compiler and evaluator for VERONICA Core.

Compiles PolicyRule instances with rule_type="memory" into a
MemoryGovernanceHook that the MemoryGovernor can execute.

The compiler validates rule parameters at compile time (fail-fast on
unknown or invalid parameters).  The evaluator applies compiled rules
deterministically in priority order with fail-closed semantics.

No memory backend or storage is implemented here.
"""

from __future__ import annotations

__all__ = [
    "MemoryRuleCompiler",
    "CompiledMemoryRule",
    "MemoryRuleEvaluator",
]

from dataclasses import dataclass, field
from typing import Any

from veronica_core.memory.types import (
    DegradeDirective,
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
    MemoryView,
    ExecutionMode,
    ThreatContext,
)
from veronica_core.policy.bundle import PolicyRule

_POLICY_ID = "memory_rule"

# All parameters the compiler recognises.  Unknown keys are rejected.
_KNOWN_PARAMETERS: frozenset[str] = frozenset({
    "action",
    "actions",
    "allowed_views",
    "allowed_modes",
    "allowed_provenance",
    "namespace",
    "namespaces",
    "verified_only",
    "max_packet_tokens",
    "max_raw_replay_ratio",
    "require_compaction_if_over_budget",
    "bridge_allow_archive",
    "bridge_require_signature",
    "verdict",
})

_VERDICT_MAP: dict[str, GovernanceVerdict] = {
    v.value: v for v in GovernanceVerdict
}

# Derived from _VERDICT_MAP to avoid duplication.
_VALID_VERDICTS: frozenset[str] = frozenset(_VERDICT_MAP)

# Canonical action values.
_VALID_ACTIONS: frozenset[str] = frozenset(a.value for a in MemoryAction)
_VALID_VIEWS: frozenset[str] = frozenset(v.value for v in MemoryView)
_VALID_MODES: frozenset[str] = frozenset(m.value for m in ExecutionMode)
_VALID_PROVENANCE: frozenset[str] = frozenset(p.value for p in MemoryProvenance)


# ------------------------------------------------------------------
# Compiled rule -- immutable, pre-validated
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledMemoryRule:
    """Pre-validated, immutable representation of a memory policy rule.

    Created by MemoryRuleCompiler.compile() -- never by callers directly.
    """

    rule_id: str
    priority: int
    # Match conditions (empty means "match all").
    actions: frozenset[str] = field(default_factory=frozenset)
    allowed_views: frozenset[str] = field(default_factory=frozenset)
    allowed_modes: frozenset[str] = field(default_factory=frozenset)
    allowed_provenance: frozenset[str] = field(default_factory=frozenset)
    namespaces: frozenset[str] = field(default_factory=frozenset)
    # Constraints.
    verified_only: bool = False
    max_packet_tokens: int = 0
    max_raw_replay_ratio: float = 1.0
    require_compaction_if_over_budget: bool = False
    bridge_allow_archive: bool | None = None
    bridge_require_signature: bool | None = None
    # Verdict (default: deny for fail-closed).
    verdict: GovernanceVerdict = GovernanceVerdict.DENY


# ------------------------------------------------------------------
# Compiler
# ------------------------------------------------------------------


class MemoryRuleCompiler:
    """Validates and compiles PolicyRule(rule_type='memory') into CompiledMemoryRule.

    Compile-time validation catches bad parameters early so the evaluator
    never encounters invalid data at runtime.

    Usage::

        compiler = MemoryRuleCompiler()
        compiled = compiler.compile(policy_rule)
        # or compile many:
        compiled_rules = compiler.compile_bundle(policy_rules)
    """

    def compile(self, rule: PolicyRule) -> CompiledMemoryRule:
        """Compile a single PolicyRule into a CompiledMemoryRule.

        Raises:
            ValueError: If rule_type is not "memory" or parameters are invalid.
            TypeError: If parameter types are wrong.
        """
        if rule.rule_type != "memory":
            raise ValueError(
                f"MemoryRuleCompiler only handles rule_type='memory', "
                f"got {rule.rule_type!r}"
            )

        params = dict(rule.parameters)
        unknown = set(params) - _KNOWN_PARAMETERS
        if unknown:
            raise ValueError(
                f"Unknown memory rule parameters: {sorted(unknown)}. "
                f"Known: {sorted(_KNOWN_PARAMETERS)}"
            )

        actions = self._parse_string_set(params, "actions", "action", _VALID_ACTIONS)
        allowed_views = self._parse_string_set(
            params, "allowed_views", None, _VALID_VIEWS,
        )
        allowed_modes = self._parse_string_set(
            params, "allowed_modes", None, _VALID_MODES,
        )
        allowed_provenance = self._parse_string_set(
            params, "allowed_provenance", None, _VALID_PROVENANCE,
        )
        namespaces = self._parse_string_set(
            params, "namespaces", "namespace", valid=None,
        )

        verified_only = self._parse_bool(params, "verified_only", default=False)
        max_packet_tokens = self._parse_int(
            params, "max_packet_tokens", default=0, minimum=0,
        )
        max_raw_replay_ratio = self._parse_float(
            params, "max_raw_replay_ratio", default=1.0, minimum=0.0, maximum=1.0,
        )
        require_compaction = self._parse_bool(
            params, "require_compaction_if_over_budget", default=False,
        )
        bridge_allow_archive = self._parse_bool(
            params, "bridge_allow_archive",
        )
        bridge_require_signature = self._parse_bool(
            params, "bridge_require_signature",
        )
        verdict = self._parse_verdict(params)

        return CompiledMemoryRule(
            rule_id=rule.rule_id,
            priority=rule.priority,
            actions=actions,
            allowed_views=allowed_views,
            allowed_modes=allowed_modes,
            allowed_provenance=allowed_provenance,
            namespaces=namespaces,
            verified_only=verified_only,
            max_packet_tokens=max_packet_tokens,
            max_raw_replay_ratio=max_raw_replay_ratio,
            require_compaction_if_over_budget=require_compaction,
            bridge_allow_archive=bridge_allow_archive,
            bridge_require_signature=bridge_require_signature,
            verdict=verdict,
        )

    def compile_bundle(
        self, rules: list[PolicyRule] | tuple[PolicyRule, ...],
    ) -> tuple[CompiledMemoryRule, ...]:
        """Compile multiple PolicyRules, returning sorted by priority.

        Only rules with rule_type="memory" and enabled=True are compiled.
        Non-memory rules are silently skipped.

        Raises:
            ValueError: If any memory rule has invalid parameters.
        """
        compiled: list[CompiledMemoryRule] = []
        for rule in rules:
            if rule.rule_type != "memory" or not rule.enabled:
                continue
            compiled.append(self.compile(rule))
        compiled.sort(key=lambda r: (r.priority, r.rule_id))
        return tuple(compiled)

    # ------------------------------------------------------------------
    # Parameter parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_string_set(
        params: dict[str, Any],
        plural_key: str,
        singular_key: str | None,
        valid: frozenset[str] | None,
    ) -> frozenset[str]:
        """Parse a set of strings from parameters.

        Accepts both plural (list) and singular (str) forms.
        If *valid* is not None, rejects unknown values.
        """
        values: set[str] = set()
        if plural_key in params:
            raw = params[plural_key]
            if isinstance(raw, str):
                values.add(raw)
            elif isinstance(raw, (list, tuple)):
                for v in raw:
                    if not isinstance(v, str):
                        raise TypeError(
                            f"{plural_key} items must be strings, got {type(v).__name__}"
                        )
                    values.add(v)
            else:
                raise TypeError(
                    f"{plural_key} must be a string or list, got {type(raw).__name__}"
                )
        if singular_key and singular_key in params:
            v = params[singular_key]
            if not isinstance(v, str):
                raise TypeError(
                    f"{singular_key} must be a string, got {type(v).__name__}"
                )
            values.add(v)
        if valid is not None and values:
            bad = values - valid
            if bad:
                raise ValueError(
                    f"Invalid values for {plural_key}: {sorted(bad)}. "
                    f"Valid: {sorted(valid)}"
                )
        return frozenset(values)

    @staticmethod
    def _parse_bool(
        params: dict[str, Any], key: str, *, default: bool | None = None,
    ) -> bool | None:
        if key not in params:
            return default
        val = params[key]
        if not isinstance(val, bool):
            raise TypeError(f"{key} must be bool, got {type(val).__name__}")
        return val

    @staticmethod
    def _parse_int(
        params: dict[str, Any], key: str, *, default: int, minimum: int,
    ) -> int:
        if key not in params:
            return default
        val = params[key]
        if not isinstance(val, int) or isinstance(val, bool):
            raise TypeError(f"{key} must be int, got {type(val).__name__}")
        if val < minimum:
            raise ValueError(f"{key} must be >= {minimum}, got {val}")
        return val

    @staticmethod
    def _parse_float(
        params: dict[str, Any],
        key: str,
        *,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        if key not in params:
            return default
        val = params[key]
        if isinstance(val, bool):
            raise TypeError(f"{key} must be a number, got bool")
        if not isinstance(val, (int, float)):
            raise TypeError(f"{key} must be a number, got {type(val).__name__}")
        val = float(val)
        if val < minimum or val > maximum:
            raise ValueError(f"{key} must be in [{minimum}, {maximum}], got {val}")
        return val

    @staticmethod
    def _parse_verdict(params: dict[str, Any]) -> GovernanceVerdict:
        if "verdict" not in params:
            return GovernanceVerdict.DENY
        raw = params["verdict"]
        if not isinstance(raw, str):
            raise TypeError(f"verdict must be a string, got {type(raw).__name__}")
        raw_lower = raw.lower()
        if raw_lower not in _VALID_VERDICTS:
            raise ValueError(
                f"Invalid verdict {raw!r}. Valid: {sorted(_VALID_VERDICTS)}"
            )
        return _VERDICT_MAP[raw_lower]


# ------------------------------------------------------------------
# Evaluator -- implements MemoryGovernanceHook protocol
# ------------------------------------------------------------------


class MemoryRuleEvaluator:
    """Evaluates compiled memory rules as a MemoryGovernanceHook.

    Rules are evaluated in priority order (ascending).  First matching
    rule determines the verdict.  If no rule matches, the evaluator
    returns DENY (fail-closed).

    Usage::

        compiler = MemoryRuleCompiler()
        rules = compiler.compile_bundle(policy_rules)
        evaluator = MemoryRuleEvaluator(rules)
        governor.add_hook(evaluator)
    """

    def __init__(self, rules: tuple[CompiledMemoryRule, ...] = ()) -> None:
        self._rules = rules

    @property
    def rule_count(self) -> int:
        """Number of compiled rules loaded."""
        return len(self._rules)

    # ------------------------------------------------------------------
    # MemoryGovernanceHook protocol
    # ------------------------------------------------------------------

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Evaluate compiled rules against *operation*.

        First matching rule wins.  No match -> DENY (fail-closed).
        """
        if not self._rules:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="no memory rules loaded (fail-closed)",
                policy_id=_POLICY_ID,
                operation=operation,
            )

        for rule in self._rules:
            if self._matches(rule, operation, context):
                return self._apply(rule, operation, context)

        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason="no matching memory rule (fail-closed)",
            policy_id=_POLICY_ID,
            operation=operation,
            threat_context=ThreatContext(
                threat_hypothesis="unmatched memory operation",
                mitigation_applied="deny",
            ),
        )

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """No-op -- rule evaluator has no post-operation side effects."""

    # ------------------------------------------------------------------
    # Rule matching
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(
        rule: CompiledMemoryRule,
        op: MemoryOperation,
        ctx: MemoryPolicyContext | None,
    ) -> bool:
        """Return True if *rule*'s match conditions are satisfied."""
        # Action filter.
        if rule.actions and op.action.value not in rule.actions:
            return False

        # Namespace filter.
        if rule.namespaces and op.namespace not in rule.namespaces:
            return False

        # Provenance filter.
        if rule.allowed_provenance and op.provenance.value not in rule.allowed_provenance:
            return False

        # verified_only filter.
        if rule.verified_only and op.provenance is not MemoryProvenance.VERIFIED:
            return False

        # View filter: fail-closed when context is absent.
        if rule.allowed_views:
            if ctx is None or ctx.memory_view.value not in rule.allowed_views:
                return False

        # Mode filter: fail-closed when context is absent.
        if rule.allowed_modes:
            if ctx is None or ctx.execution_mode.value not in rule.allowed_modes:
                return False

        return True

    # ------------------------------------------------------------------
    # Rule application
    # ------------------------------------------------------------------

    @staticmethod
    def _apply(
        rule: CompiledMemoryRule,
        op: MemoryOperation,
        ctx: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Build a MemoryGovernanceDecision from a matched rule."""
        directive: DegradeDirective | None = None
        threat: ThreatContext | None = None

        if rule.verdict is GovernanceVerdict.DEGRADE:
            directive = DegradeDirective(
                max_packet_tokens=rule.max_packet_tokens,
                verified_only=rule.verified_only,
                summary_required=rule.require_compaction_if_over_budget,
                raw_replay_blocked=rule.max_raw_replay_ratio < 1.0,
            )
            threat = ThreatContext(
                threat_hypothesis="memory rule enforced degradation",
                mitigation_applied="degrade",
                degrade_reason=f"rule {rule.rule_id}",
                compactness_enforced=rule.require_compaction_if_over_budget,
            )
        elif rule.verdict is GovernanceVerdict.DENY:
            threat = ThreatContext(
                threat_hypothesis="memory rule denied operation",
                mitigation_applied="deny",
                effective_scope=f"rule:{rule.rule_id}",
            )
        elif rule.verdict is GovernanceVerdict.QUARANTINE:
            threat = ThreatContext(
                threat_hypothesis="memory rule quarantined operation",
                mitigation_applied="quarantine",
                effective_scope=f"rule:{rule.rule_id}",
            )

        return MemoryGovernanceDecision(
            verdict=rule.verdict,
            reason=f"matched rule {rule.rule_id}",
            policy_id=_POLICY_ID,
            operation=op,
            degrade_directive=directive,
            threat_context=threat,
        )
