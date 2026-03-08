"""OTelMetricsIngester — Parse OTel spans and accumulate per-agent metrics.

Supported span attribute namespaces:
  AG2 native:       span_type in {conversation, agent, llm, tool, code_execution}
                    + gen_ai.* attributes
  veronica-core:    veronica.cost_usd, veronica.decision
  Generic OTel LLM: llm.token.count.total, llm.cost (OpenLLMetry / semantic conventions)

Thread-safe: one lock per agent_id in _locks dict; _locks dict itself is
protected by _global_lock to allow concurrent per-agent updates.

Zero external dependencies (stdlib only).
"""

# nogil-audited: 2026-03-08
# Findings:
#   - Two-level locking: _global_lock guards the _agents dict (creation/lookup);
#     each _AgentState has its own lock guarding per-agent counters.
#   - get_all_agents() correctly snapshots agent_ids under _global_lock, then
#     acquires per-agent locks individually (no nested lock held).
#   - No changes needed.

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AG2 span types we recognise
# ---------------------------------------------------------------------------

_AG2_SPAN_TYPES = frozenset({"conversation", "agent", "llm", "tool", "code_execution"})


@dataclass
class AgentMetrics:
    """Accumulated metrics for a single agent or span source.

    Fields are updated atomically by OTelMetricsIngester.ingest_span().

    Attributes:
        total_tokens:   Sum of all tokens (input + output) across ingested spans.
        total_cost:     Cumulative USD cost across ingested spans.
        avg_latency_ms: Running average of span durations in milliseconds.
        error_rate:     Fraction of spans classified as errors (0.0 – 1.0).
        last_active:    Monotonic timestamp of the most recent ingested span.
        call_count:     Total number of spans ingested for this agent.
    """

    total_tokens: int = 0
    total_cost: float = 0.0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    last_active: float = 0.0
    call_count: int = 0

    # Internal accumulators — not part of the public API but stored on the
    # dataclass to avoid a parallel dict.
    _error_count: int = field(default=0, repr=False, compare=False)
    _latency_sum_ms: float = field(default=0.0, repr=False, compare=False)

    @property
    def error_count(self) -> int:
        """Public accessor for the error count accumulator."""
        return self._error_count


class _AgentState:
    """Internal mutable state for one agent, protected by its own lock."""

    def __init__(self, window_sec: float, max_cost_window_size: int) -> None:
        self.lock = threading.Lock()
        self.window_sec = window_sec
        # Sliding window: (monotonic_ts, cost) pairs; bounded to prevent unbounded growth
        self.cost_window: deque[tuple[float, float]] = deque(
            maxlen=max_cost_window_size
        )

        self.total_tokens: int = 0
        self.total_cost: float = 0.0
        self.latency_sum_ms: float = 0.0
        self.error_count: int = 0
        self.call_count: int = 0
        self.last_active: float = 0.0

    def snapshot(self) -> AgentMetrics:
        """Return an immutable snapshot (caller must hold self.lock)."""
        avg_lat = self.latency_sum_ms / self.call_count if self.call_count > 0 else 0.0
        err_rate = self.error_count / self.call_count if self.call_count > 0 else 0.0
        return AgentMetrics(
            total_tokens=self.total_tokens,
            total_cost=self.total_cost,
            avg_latency_ms=avg_lat,
            error_rate=err_rate,
            last_active=self.last_active,
            call_count=self.call_count,
            _error_count=self.error_count,
        )


def _extract_duration_ms(span: dict) -> Optional[float]:
    """Compute duration_ms from start_time / end_time.

    Accepts epoch floats (seconds), epoch ints (nanoseconds via large value),
    or already-millisecond floats stored under duration_ms.
    Returns None if timing data is missing or invalid.
    """
    # Direct duration_ms field
    if "duration_ms" in span:
        v = span["duration_ms"]
        if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0:
            return float(v)

    start = span.get("start_time")
    end = span.get("end_time")
    if start is None or end is None:
        return None

    # Convert nanoseconds to seconds if values are suspiciously large
    def _to_sec(v: float | int) -> float:
        v = float(v)
        # Values > 1e12 are likely nanoseconds (Unix ns epoch starts ~1.7e18)
        if v > 1e12:
            return v / 1e9
        return v

    try:
        start_sec = _to_sec(start)
        end_sec = _to_sec(end)
        duration_s = end_sec - start_sec
        if duration_s < 0 or not math.isfinite(duration_s):
            return None
        return duration_s * 1000.0
    except (TypeError, ValueError):
        return None


def _extract_tokens(attrs: dict) -> int:
    """Return total token count from various attribute naming conventions."""
    # OpenLLMetry / semantic conventions
    for key in (
        "llm.token.count.total",
        "llm.token.count",
        "gen_ai.usage.total_tokens",
    ):
        v = attrs.get(key)
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return int(v)

    # Sum input + output
    input_tokens = 0
    output_tokens = 0
    for key in (
        "llm.token.count.prompt",
        "gen_ai.usage.prompt_tokens",
        "llm.input_tokens",
    ):
        v = attrs.get(key)
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            input_tokens = int(v)
            break
    for key in (
        "llm.token.count.completion",
        "gen_ai.usage.completion_tokens",
        "llm.output_tokens",
    ):
        v = attrs.get(key)
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            output_tokens = int(v)
            break

    return input_tokens + output_tokens


def _extract_cost(attrs: dict) -> float:
    """Return USD cost from various attribute naming conventions."""
    for key in ("veronica.cost_usd", "llm.cost", "gen_ai.usage.cost"):
        v = attrs.get(key)
        if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) >= 0:
            return float(v)
    return 0.0


def _is_error_span(span: dict, attrs: dict) -> bool:
    """Return True if the span represents an error or failure."""
    # OTel status
    status = span.get("status") or span.get("status_code") or ""
    if isinstance(status, str) and status.upper() in (
        "ERROR",
        "STATUS_CODE_ERROR",
        "UNSET_ERROR",
    ):
        return True

    # veronica decision that indicates failure
    decision = attrs.get("veronica.decision", "")
    if isinstance(decision, str) and decision.upper() in ("HALT", "ERROR"):
        return True

    # Explicit error flag
    if attrs.get("error") is True or attrs.get("exception.type"):
        return True

    return False


def _resolve_agent_id(span: dict, attrs: dict) -> str:
    """Determine the agent_id for this span.

    Priority:
    1. attrs["veronica.agent_id"]
    2. attrs["ag2.agent_id"] / attrs["gen_ai.agent.id"]
    3. attrs["agent_id"]
    4. span["agent_id"]
    5. span name or "unknown"
    """
    for key in (
        "veronica.agent_id",
        "ag2.agent_id",
        "gen_ai.agent.id",
        "agent_id",
        "ag2.source",
        "source_name",
    ):
        v = attrs.get(key) or span.get(key)
        if v and isinstance(v, str):
            return v

    # Fall back to span name
    name = span.get("name") or span.get("span_name") or ""
    return str(name) if name else "unknown"


class OTelMetricsIngester:
    """Thread-safe ingester that parses OTel span dicts and accumulates per-agent metrics.

    Supports AG2, veronica-core, and generic OpenLLMetry span formats.

    Args:
        window_sec: Sliding window duration in seconds for rate calculations.
            Default 3600 (1 hour).

    Example::

        ingester = OTelMetricsIngester()
        ingester.ingest_span({
            "name": "llm_call",
            "agent_id": "assistant",
            "start_time": 1700000000.0,
            "end_time":   1700000001.5,
            "attributes": {
                "veronica.cost_usd": 0.003,
                "llm.token.count.total": 1200,
            },
        })
        metrics = ingester.get_agent_metrics("assistant")
        # metrics.total_cost == 0.003
    """

    _DEFAULT_MAX_AGENTS = 10_000
    _DEFAULT_MAX_COST_WINDOW_SIZE = 100_000

    def __init__(
        self,
        window_sec: float = 3600.0,
        max_agents: int = _DEFAULT_MAX_AGENTS,
        max_cost_window_size: int = _DEFAULT_MAX_COST_WINDOW_SIZE,
    ) -> None:
        if window_sec <= 0:
            raise ValueError(f"window_sec must be > 0, got {window_sec}")
        if max_agents <= 0:
            raise ValueError(f"max_agents must be > 0, got {max_agents}")
        if max_cost_window_size <= 0:
            raise ValueError(
                f"max_cost_window_size must be > 0, got {max_cost_window_size}"
            )
        self._window_sec = window_sec
        self._max_agents = max_agents
        self._max_cost_window_size = max_cost_window_size
        self._global_lock = threading.Lock()
        self._agents: dict[str, _AgentState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_span(self, span: dict) -> None:
        """Parse an OTel span dict and update the corresponding agent's metrics.

        Silently ignores malformed or unsupported spans (never raises).

        Args:
            span: OTel span as a plain dict. Expected keys (all optional):
                - name / span_name: span name
                - span_type: AG2 span type classification
                - agent_id / attributes.veronica.agent_id / ...: agent identifier
                - start_time / end_time: epoch seconds (or nanoseconds)
                - duration_ms: alternative to start/end times
                - attributes: dict of span attributes
                - status / status_code: "ERROR" marks error spans
        """
        try:
            self._ingest_span_internal(span)
        except Exception:
            # Never propagate — metrics collection must not crash the caller
            logger.debug(
                "OTelMetricsIngester: ingest_span failed for span %r",
                span.get("name", "?"),
                exc_info=True,
            )

    def get_agent_metrics(self, agent_id: str) -> AgentMetrics:
        """Return a snapshot of metrics for the given agent.

        Returns a zeroed AgentMetrics if the agent has never been seen.

        Args:
            agent_id: Agent identifier as used in span resolution.

        Returns:
            AgentMetrics snapshot (immutable copy).
        """
        state = self._get_state_if_exists(agent_id)
        if state is None:
            return AgentMetrics()
        with state.lock:
            return state.snapshot()

    def get_all_agents(self) -> dict[str, AgentMetrics]:
        """Return snapshots for all tracked agents.

        Returns:
            Dict mapping agent_id to AgentMetrics snapshot.
        """
        with self._global_lock:
            agent_ids = list(self._agents.keys())

        result: dict[str, AgentMetrics] = {}
        for agent_id in agent_ids:
            state = self._get_state_if_exists(agent_id)
            if state is not None:
                with state.lock:
                    result[agent_id] = state.snapshot()
        return result

    def reset(self, agent_id: Optional[str] = None) -> None:
        """Clear accumulated metrics.

        Note: agent entries are **not** removed from the internal registry; only
        their counters are zeroed.  This means that after a full reset,
        ``get_all_agents()`` still returns all previously-seen agent ids (each
        with zeroed metrics), and subsequent ``ingest_span()`` calls continue to
        accumulate against the same state objects.  If you need to reclaim
        memory for agents that are no longer active, create a new
        ``OTelMetricsIngester`` instance instead.

        Args:
            agent_id: If given, reset only that agent. If None, reset all agents.
        """
        if agent_id is not None:
            state = self._get_state_if_exists(agent_id)
            if state is not None:
                with state.lock:
                    self._reset_state(state)
        else:
            with self._global_lock:
                states = list(self._agents.values())
            for state in states:
                with state.lock:
                    self._reset_state(state)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ingest_span_internal(self, span: dict) -> None:
        """Core ingestion logic (caller: ingest_span wraps in try/except)."""
        if not isinstance(span, dict):
            return

        attrs: dict = span.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        # Filter to supported span types for AG2 spans
        span_type = span.get("span_type") or attrs.get("span_type") or ""
        if span_type and span_type not in _AG2_SPAN_TYPES:
            # Unknown AG2 span type — skip
            return

        agent_id = _resolve_agent_id(span, attrs)
        duration_ms = _extract_duration_ms(span)
        tokens = _extract_tokens(attrs)
        cost = _extract_cost(attrs)
        is_error = _is_error_span(span, attrs)
        now = time.monotonic()

        state = self._get_or_create_state(agent_id)
        if state is None:
            return  # max_agents limit reached; silently drop
        with state.lock:
            state.call_count += 1
            state.total_tokens += max(0, tokens)
            if cost > 0:
                state.total_cost += cost
                # Maintain sliding window for rate calculations
                state.cost_window.append((now, cost))
                self._prune_window(state, now)
            if duration_ms is not None and duration_ms >= 0:
                state.latency_sum_ms += duration_ms
            if is_error:
                state.error_count += 1
            state.last_active = now

    def _get_or_create_state(self, agent_id: str) -> Optional[_AgentState]:
        """Return existing state or create a new one (global lock for creation only).

        Returns None if max_agents limit is reached and agent_id is new.
        """
        with self._global_lock:
            if agent_id not in self._agents:
                if len(self._agents) >= self._max_agents:
                    return None
                self._agents[agent_id] = _AgentState(
                    self._window_sec, self._max_cost_window_size
                )
            return self._agents[agent_id]

    def _get_state_if_exists(self, agent_id: str) -> Optional[_AgentState]:
        """Return existing state or None without creating."""
        with self._global_lock:
            return self._agents.get(agent_id)

    @staticmethod
    def _reset_state(state: _AgentState) -> None:
        """Reset all counters on state (caller must hold state.lock)."""
        state.total_tokens = 0
        state.total_cost = 0.0
        state.latency_sum_ms = 0.0
        state.error_count = 0
        state.call_count = 0
        state.last_active = 0.0
        state.cost_window.clear()

    @staticmethod
    def _prune_window(state: _AgentState, now: float) -> None:
        """Remove cost_window entries older than window_sec (caller holds state.lock)."""
        cutoff = now - state.window_sec
        while state.cost_window and state.cost_window[0][0] < cutoff:
            state.cost_window.popleft()
