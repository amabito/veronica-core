"""Trust escalation tracking for A2A agents."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from veronica_core.a2a.types import TrustLevel, TrustPolicy

logger = logging.getLogger(__name__)

# Trust level ordering for promotion (UNTRUSTED -> PROVISIONAL -> TRUSTED).
# PRIVILEGED is never reached via auto-promotion.
_PROMOTION_ORDER: list[TrustLevel] = [
    TrustLevel.UNTRUSTED,
    TrustLevel.PROVISIONAL,
    TrustLevel.TRUSTED,
]
_PROMOTION_INDEX: dict[TrustLevel, int] = {
    lvl: i for i, lvl in enumerate(_PROMOTION_ORDER)
}


@dataclass
class _AgentRecord:
    """Internal mutable record for a tracked agent."""
    success_count: int = 0
    failure_count: int = 0
    current_trust: TrustLevel = TrustLevel.UNTRUSTED
    promoted_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class TrustEscalationTracker:
    """Tracks agent execution history and promotes/demotes trust levels.

    Thread-safe with per-agent locking (no global lock on hot path).
    Agents beyond the cardinality cap are rejected.

    Args:
        policy: Trust escalation policy configuration.
        max_agents: Maximum number of tracked agents (default 10,000).
    """

    _DEFAULT_MAX_AGENTS: int = 10_000

    def __init__(
        self,
        policy: TrustPolicy,
        max_agents: int = _DEFAULT_MAX_AGENTS,
    ) -> None:
        self._policy = policy
        self._max_agents = max_agents
        self._agents: dict[str, _AgentRecord] = {}
        self._global_lock = threading.Lock()  # protects _agents dict mutations

    def _get_or_create(self, agent_id: str) -> _AgentRecord | None:
        """Return the record for agent_id, creating if needed.

        Returns None if the cardinality cap is reached and agent_id is new.
        """
        record = self._agents.get(agent_id)
        if record is not None:
            return record
        with self._global_lock:
            # Double-check after acquiring lock.
            record = self._agents.get(agent_id)
            if record is not None:
                return record
            if len(self._agents) >= self._max_agents:
                logger.warning(
                    "TrustEscalationTracker: cardinality cap (%d) reached, "
                    "rejecting agent %r",
                    self._max_agents, agent_id,
                )
                return None
            record = _AgentRecord(current_trust=self._policy.default_trust)
            self._agents[agent_id] = record
            return record

    def record_success(self, agent_id: str) -> TrustLevel:
        """Record a successful execution and potentially promote.

        Returns the agent's trust level after the update.
        Returns policy.default_trust if the agent was rejected (cap reached).
        """
        record = self._get_or_create(agent_id)
        if record is None:
            return self._policy.default_trust
        with record.lock:
            record.success_count += 1
            if record.success_count >= self._policy.promotion_threshold:
                self._try_promote(record)
            return record.current_trust

    def record_failure(self, agent_id: str) -> TrustLevel:
        """Record a failed execution and demote to UNTRUSTED.

        Returns the agent's trust level after the update.
        """
        record = self._get_or_create(agent_id)
        if record is None:
            return self._policy.default_trust
        with record.lock:
            record.failure_count += 1
            record.current_trust = TrustLevel.UNTRUSTED
            record.success_count = 0
            return record.current_trust

    def get_trust_level(self, agent_id: str) -> TrustLevel:
        """Return the current trust level for an agent."""
        record = self._agents.get(agent_id)
        if record is None:
            return self._policy.default_trust
        with record.lock:
            return record.current_trust

    def get_stats(self, agent_id: str) -> dict[str, Any]:
        """Return a snapshot of agent statistics."""
        record = self._agents.get(agent_id)
        if record is None:
            return {
                "success_count": 0,
                "failure_count": 0,
                "current_trust": self._policy.default_trust.value,
                "promoted_at": None,
            }
        with record.lock:
            return {
                "success_count": record.success_count,
                "failure_count": record.failure_count,
                "current_trust": record.current_trust.value,
                "promoted_at": record.promoted_at,
            }

    def _try_promote(self, record: _AgentRecord) -> None:
        """Promote *record* one level if eligible (single O(1) lookup)."""
        cur_idx = _PROMOTION_INDEX.get(record.current_trust, -1)
        allow_idx = _PROMOTION_INDEX.get(self._policy.allow_promotion_to, -1)
        next_idx = cur_idx + 1
        if 0 <= next_idx < len(_PROMOTION_ORDER) and next_idx <= allow_idx:
            record.current_trust = _PROMOTION_ORDER[next_idx]
            record.success_count = 0
            record.promoted_at = time.monotonic()
