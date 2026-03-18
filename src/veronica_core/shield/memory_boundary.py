"""MemoryBoundaryHook -- ShieldPipeline hook for trust-based memory access control.

Intercepts memory_read and memory_write tool calls (produced by wrap_memory_call)
and enforces per-agent namespace access rules.

Design:
- MemoryBoundaryConfig: declarative allow-list for agent/namespace read+write rules.
- MemoryBoundaryHook: implements PostDispatchHook and MemoryGovernanceHook protocols.
  It evaluates each memory call against explicit rules and, if a TrustBasedPolicyRouter
  is attached, also enforces trust-level-based namespace isolation.
- Default (no config): allow all -- backward compatible.
- Integration with MemoryGovernor: hook can be registered directly as a
  MemoryGovernanceHook so it participates in the governor chain.
"""

from __future__ import annotations

__all__ = [
    "MemoryBoundaryConfig",
    "MemoryBoundaryHook",
    "MemoryAccessRule",
]

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
)
from veronica_core.shield.types import ToolCallContext

if TYPE_CHECKING:
    from veronica_core.a2a.escalation import TrustEscalationTracker
    from veronica_core.a2a.router import TrustBasedPolicyRouter
    from veronica_core.a2a.types import TrustLevel

logger = logging.getLogger(__name__)

# Tool call kinds that represent memory operations.
_MEMORY_READ_KIND = "memory_read"
_MEMORY_WRITE_KIND = "memory_write"

# Metadata key names used when extracting memory call information from
# ToolCallContext.metadata (as set by wrap_memory_call).
_META_KIND = "kind"
_META_AGENT_ID = "agent_id"
_META_NAMESPACE = "namespace"


@dataclass(frozen=True)
class MemoryAccessRule:
    """Declarative rule granting or denying access for an agent to a namespace.

    Fields:
        agent_id: Target agent.  Use "*" as a wildcard to match all agents.
        namespace: Target memory namespace.  Use "*" to match all namespaces.
        allow_read: Whether this agent can read from this namespace.
        allow_write: Whether this agent can write to this namespace.
    """

    agent_id: str
    namespace: str
    allow_read: bool = True
    allow_write: bool = True


@dataclass
class MemoryBoundaryConfig:
    """Configuration for MemoryBoundaryHook.

    When rules is empty and default_allow is False (default), all memory
    accesses are denied -- fail-closed by default.

    Rule evaluation order (by specificity score, highest wins):
    1. Exact agent_id + exact namespace match (score 3).
    2. Exact agent_id + wildcard namespace ("*") (score 2).
    3. Wildcard agent_id ("*") + exact namespace match (score 1).
    4. Wildcard agent_id + wildcard namespace (score 0).
    5. No match -> default_allow determines the outcome.

    Args:
        rules: Ordered list of MemoryAccessRule instances.
        default_allow: Verdict when no rule matches (True = allow, False = deny).
    """

    rules: list[MemoryAccessRule] = field(default_factory=list)
    default_allow: bool = False


def _match_rule(
    rule: MemoryAccessRule,
    agent_id: str,
    namespace: str,
) -> bool:
    """Return True when the rule applies to the given agent_id and namespace."""
    agent_match = rule.agent_id == "*" or rule.agent_id == agent_id
    ns_match = rule.namespace == "*" or rule.namespace == namespace
    return agent_match and ns_match


def _rule_specificity(rule: MemoryAccessRule) -> int:
    """Higher means more specific (exact > wildcard).  Used to pick best match."""
    score = 0
    if rule.agent_id != "*":
        score += 2
    if rule.namespace != "*":
        score += 1
    return score


class MemoryBoundaryHook:
    """ShieldPipeline and MemoryGovernanceHook for trust-based memory isolation.

    Intercepts memory_read and memory_write calls from ToolCallContext metadata
    (PostDispatchHook path) and also provides before_op / after_op for direct
    integration with MemoryGovernor (MemoryGovernanceHook path).

    Trust-level isolation (when trust_tracker and trusted_namespaces are provided):
    - UNTRUSTED agents cannot access TRUSTED namespaces (read or write).
    - PROVISIONAL agents can read TRUSTED namespaces but cannot write to them.
    - TRUSTED / PRIVILEGED agents can read and write all namespaces.

    Thread safety: rule evaluation is stateless after __init__ (read-only config).
    The deny_count counter is protected by _lock for thread-safe increment.

    Args:
        config: MemoryBoundaryConfig with access rules and default policy.
        trust_router: Optional TrustBasedPolicyRouter.  When provided together
            with trust_tracker and trusted_namespaces, trust-level isolation is
            applied before rule-based evaluation.
        trust_tracker: Optional TrustEscalationTracker used to resolve agent
            trust levels.  Must be provided for trust-level isolation to work.
        trusted_namespaces: Set of namespace names classified as TRUSTED for
            trust-level isolation purposes.  Defaults to empty set (no isolation).
    """

    def __init__(
        self,
        config: MemoryBoundaryConfig | None = None,
        trust_router: "TrustBasedPolicyRouter | None" = None,
        trust_tracker: "TrustEscalationTracker | None" = None,
        trusted_namespaces: frozenset[str] | None = None,
    ) -> None:
        self._config = config if config is not None else MemoryBoundaryConfig()
        self._trust_router = trust_router
        self._trust_tracker = trust_tracker
        self._trusted_namespaces: frozenset[str] = (
            trusted_namespaces if trusted_namespaces is not None else frozenset()
        )
        # Stats counter (best-effort, not safety-critical).
        self._lock = threading.Lock()
        self._deny_count = 0

    # ------------------------------------------------------------------
    # PostDispatchHook protocol (shield pipeline integration)
    # ------------------------------------------------------------------

    def after_llm_call(self, ctx: ToolCallContext, response: Any) -> None:
        """Intercept post-LLM calls to enforce memory access rules.

        Called by ShieldPipeline after an LLM/tool response.  Checks whether
        the call was a memory_read or memory_write and evaluates access rules.
        Raises PermissionError when access is denied so the calling pipeline
        can handle or log the violation.

        Args:
            ctx: Tool call context carrying kind, agent_id, and namespace in
                 the metadata dict.
            response: LLM/tool response (not inspected, may be any type).

        Raises:
            PermissionError: When the memory access is denied by policy.
        """
        kind = ctx.metadata.get(_META_KIND)
        if kind not in (_MEMORY_READ_KIND, _MEMORY_WRITE_KIND):
            return  # Not a memory call -- no opinion.

        raw_agent_id = ctx.metadata.get(_META_AGENT_ID)
        agent_id = str(raw_agent_id) if raw_agent_id is not None else ""
        raw_namespace = ctx.metadata.get(_META_NAMESPACE)
        namespace = str(raw_namespace) if raw_namespace is not None else ""
        is_write = kind == _MEMORY_WRITE_KIND

        allowed, reason = self._evaluate_access(agent_id, namespace, is_write)
        if not allowed:
            with self._lock:
                self._deny_count += 1
            logger.warning(
                "[memory_boundary] denied %s agent=%r namespace=%r reason=%s",
                kind,
                agent_id,
                namespace,
                reason,
            )
            raise PermissionError(
                f"Memory access denied: {reason} "
                f"(agent={agent_id!r}, namespace={namespace!r}, kind={kind})"
            )

    # ------------------------------------------------------------------
    # MemoryGovernanceHook protocol (MemoryGovernor integration)
    # ------------------------------------------------------------------

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Evaluate memory governance before the operation executes.

        Maps MemoryAction.READ and WRITE to the boundary rule evaluation.
        Other actions (RETRIEVE, ARCHIVE, etc.) pass through as ALLOW.

        Args:
            operation: The memory operation being requested.
            context: Ambient chain context (may be None).

        Returns:
            MemoryGovernanceDecision with ALLOW or DENY verdict.
        """
        if operation.action not in (MemoryAction.READ, MemoryAction.WRITE):
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                reason="non-read/write action; pass-through",
                policy_id="memory_boundary",
                operation=operation,
            )

        is_write = operation.action is MemoryAction.WRITE
        agent_id = operation.agent_id or ""
        namespace = operation.namespace or ""

        allowed, reason = self._evaluate_access(agent_id, namespace, is_write)

        if allowed:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.ALLOW,
                reason=reason,
                policy_id="memory_boundary",
                operation=operation,
            )

        with self._lock:
            self._deny_count += 1
        logger.warning(
            "[memory_boundary] before_op denied action=%s agent=%r namespace=%r reason=%s",
            operation.action.value,
            agent_id,
            namespace,
            reason,
        )
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY,
            reason=reason,
            policy_id="memory_boundary",
            operation=operation,
        )

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Post-operation notification.  Logs errors; never raises."""
        if error is not None:
            logger.debug(
                "[memory_boundary] after_op error for action=%s agent=%r: %s",
                operation.action.value,
                operation.agent_id,
                error,
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def deny_count(self) -> int:
        """Total number of memory accesses denied since construction."""
        with self._lock:
            return self._deny_count

    # ------------------------------------------------------------------
    # Internal evaluation
    # ------------------------------------------------------------------

    def _evaluate_access(
        self,
        agent_id: str,
        namespace: str,
        is_write: bool,
    ) -> tuple[bool, str]:
        """Return (allowed, reason) for the given access request.

        Evaluation proceeds in two stages:

        Stage 1 -- Trust-level isolation (if trust_tracker and trusted_namespaces
        are configured). UNTRUSTED agents are always denied here and never reach
        Stage 2 -- explicit rules cannot grant UNTRUSTED agents access to trusted
        namespaces. PROVISIONAL agents are denied write access but fall through
        to Stage 2 for read access, meaning a misconfigured allow-write rule will
        not be reached by PROVISIONAL agents on trusted namespaces. TRUSTED and
        PRIVILEGED agents fall through to Stage 2 unconditionally.

        Stage 2 -- Explicit rule check using MemoryAccessRule entries. Only
        reached when Stage 1 does not produce a definitive verdict.

        This two-stage design is intentional: trust-level guarantees take
        precedence over explicit rules for trusted namespaces, preventing
        over-permissive rules from unintentionally elevating low-trust agents.

        Args:
            agent_id: Requesting agent identifier.
            namespace: Target memory namespace.
            is_write: True for write operations, False for read operations.

        Returns:
            (True, reason_str) if allowed, (False, reason_str) if denied.
        """
        # Stage 1: trust-level isolation (requires tracker + trusted namespaces).
        if self._trust_tracker is not None and self._trusted_namespaces:
            result = self._evaluate_trust_level(agent_id, namespace, is_write)
            if result is not None:
                return result

        # Stage 2: explicit rule-based check.
        return self._evaluate_rules(agent_id, namespace, is_write)

    def _evaluate_trust_level(
        self,
        agent_id: str,
        namespace: str,
        is_write: bool,
    ) -> tuple[bool, str] | None:
        """Evaluate trust-level-based isolation.

        Returns (allowed, reason) when a definitive trust-level decision is
        reached, or None to fall through to rule-based evaluation.

        Trust matrix for TRUSTED namespaces (using existing TrustLevel enum):
        - UNTRUSTED  -> deny read and write.
        - PROVISIONAL -> allow read, deny write (intermediate trust -- read-only).
        - TRUSTED    -> allow read and write (full access, fall through to rules).
        - PRIVILEGED -> allow read and write (full access, fall through to rules).
        - None       -> deny (fail-closed when tracker returns None).

        Note: unknown agents (never seen by the tracker) receive the tracker's
        ``default_trust`` level.  If ``default_trust`` is TRUSTED, unknown agents
        will be granted access.  Use ``default_trust=UNTRUSTED`` (the default)
        for fail-closed behavior on unknown agents.

        Namespaces not in trusted_namespaces are always permitted here
        (trust-level rules only restrict access to TRUSTED namespaces).
        """
        if namespace not in self._trusted_namespaces:
            return None  # Not a trusted namespace; skip trust check.

        try:
            from veronica_core.a2a.types import TrustLevel

            trust_level = self._resolve_trust_level(agent_id)

            # None means trust is unknown -- fail-closed for trusted namespaces.
            if trust_level is None:
                return (
                    False,
                    f"trust level unknown for agent={agent_id!r}; "
                    f"access to trusted namespace={namespace!r} denied",
                )

            # TRUSTED / PRIVILEGED: full access -- fall through to rule check.
            if trust_level in (TrustLevel.TRUSTED, TrustLevel.PRIVILEGED):
                return None

            # UNTRUSTED: deny all access to trusted namespaces.
            if trust_level == TrustLevel.UNTRUSTED:
                return (
                    False,
                    f"untrusted agent={agent_id!r} cannot access trusted namespace={namespace!r}",
                )

            # PROVISIONAL: read-only on trusted namespaces (intermediate trust).
            if is_write:
                return (
                    False,
                    f"provisional agent={agent_id!r} cannot write to trusted namespace={namespace!r}",
                )
            # PROVISIONAL read: fall through to rule check.
            return None

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[memory_boundary] trust level resolution failed for agent=%r: %s; denying",
                agent_id,
                exc,
            )
            return (False, "trust resolution error (see logs)")

    def _resolve_trust_level(self, agent_id: str) -> "TrustLevel | None":
        """Attempt to resolve the trust level for an agent.

        Uses the attached TrustEscalationTracker when available.
        Returns None when no tracker is configured (unknown trust).
        """
        if self._trust_tracker is not None:
            return self._trust_tracker.get_trust_level(agent_id)
        return None

    def _evaluate_rules(
        self,
        agent_id: str,
        namespace: str,
        is_write: bool,
    ) -> tuple[bool, str]:
        """Evaluate explicit MemoryAccessRule entries.

        Returns (allowed, reason_str).  If no rules match, the config's
        default_allow determines the outcome.
        """
        rules = self._config.rules
        if not rules:
            # No rules configured -- apply default policy.
            if self._config.default_allow:
                return (True, "default allow (no rules configured)")
            return (False, "default deny (no rules configured)")

        # Find the most-specific matching rule.
        # Tiebreak: deny-wins (security-conservative) when multiple rules share
        # the same specificity score for the same (agent_id, namespace) pattern.
        best: MemoryAccessRule | None = None
        best_score = -1

        for rule in rules:
            if not _match_rule(rule, agent_id, namespace):
                continue
            score = _rule_specificity(rule)
            if score > best_score:
                best = rule
                best_score = score
            elif score == best_score and best is not None:
                # Prefer the more restrictive rule at equal specificity.
                # A rule is more restrictive if it denies the requested operation.
                rule_denies = (is_write and not rule.allow_write) or (
                    not is_write and not rule.allow_read
                )
                best_denies = (is_write and not best.allow_write) or (
                    not is_write and not best.allow_read
                )
                if rule_denies and not best_denies:
                    best = rule

        if best is None:
            if self._config.default_allow:
                return (True, "default allow (no matching rule)")
            return (False, "default deny (no matching rule)")

        if is_write:
            if best.allow_write:
                return (True, f"rule matched: write allowed (agent={agent_id!r}, ns={namespace!r})")
            return (
                False,
                f"rule matched: write denied for agent={agent_id!r} namespace={namespace!r}",
            )

        if best.allow_read:
            return (True, f"rule matched: read allowed (agent={agent_id!r}, ns={namespace!r})")
        return (
            False,
            f"rule matched: read denied for agent={agent_id!r} namespace={namespace!r}",
        )
