"""VERONICA Degrade strategy -- decision engine (pure functions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, Enum


class DegradeLevel(IntEnum):
    NORMAL = 0
    SOFT = 1
    HARD = 2
    EMERGENCY = 3


class SchedulerMode(str, Enum):
    NORMAL = "normal"
    QUEUE_PREFER = "queue_prefer"
    REJECT_PREFER = "reject_prefer"


@dataclass
class ControlSignals:
    budget_utilization: float = 0.0
    breaker_state: str = "closed"
    recent_error_rate: float = 0.0
    recent_timeouts: int = 0
    recent_retries: int = 0
    queue_depth: int = 0
    consecutive_failures: int = 0


@dataclass
class RequestMeta:
    kind: str = "llm_call"
    priority: str = "P1"
    model: str = ""
    cheap_model: str = ""
    max_tokens: int = 4096
    tool_name: str = ""
    read_only_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass
class DegradeConfig:
    budget_soft: float = 0.8
    budget_hard: float = 0.9
    budget_emergency: float = 0.98
    error_rate_soft: float = 0.3
    error_rate_hard: float = 0.5
    error_rate_emergency: float = 0.8
    timeout_soft: int = 3
    timeout_hard: int = 6
    timeout_emergency: int = 10
    consecutive_fail_soft: int = 3
    consecutive_fail_hard: int = 5
    consecutive_fail_emergency: int = 10
    max_tokens_pct_level1: float = 0.7
    max_tokens_pct_level2: float = 0.5
    max_tokens_floor_level1: int = 128
    max_tokens_floor_level2: int = 64
    max_tokens_cap_level3: int = 64
    recovery_window_s: float = 60.0


@dataclass
class Decision:
    level: DegradeLevel = DegradeLevel.NORMAL
    allow_llm: bool = True
    allow_tools: bool = True
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    model_override: str | None = None
    max_tokens_cap: int | None = None
    retry_cap_override: int | None = None
    scheduler_mode: SchedulerMode = SchedulerMode.NORMAL
    reason_codes: list[str] = field(default_factory=list)


class DegradedToolBlocked(Exception):
    def __init__(self, tool_name: str, level: DegradeLevel) -> None:
        self.tool_name = tool_name
        self.level = level
        super().__init__(f"Tool '{tool_name}' blocked at degrade level {level.name}")


class DegradedRejected(Exception):
    def __init__(self, priority: str, level: DegradeLevel) -> None:
        self.priority = priority
        self.level = level
        super().__init__(f"LLM call priority={priority} rejected at degrade level {level.name}")


def compute_level(signals: ControlSignals, config: DegradeConfig) -> tuple[DegradeLevel, list[str]]:
    """Compute degrade level from signals. Returns (level, reason_codes). Takes MAX across all dimensions."""
    level = DegradeLevel.NORMAL
    reasons: list[str] = []

    # Budget
    if signals.budget_utilization >= config.budget_emergency:
        level = max(level, DegradeLevel.EMERGENCY); reasons.append("budget_emergency")
    elif signals.budget_utilization >= config.budget_hard:
        level = max(level, DegradeLevel.HARD); reasons.append("budget_hard")
    elif signals.budget_utilization >= config.budget_soft:
        level = max(level, DegradeLevel.SOFT); reasons.append("budget_soft")

    # Breaker
    if signals.breaker_state == "open":
        level = max(level, DegradeLevel.EMERGENCY); reasons.append("breaker_open")
    elif signals.breaker_state == "half_open":
        level = max(level, DegradeLevel.HARD); reasons.append("breaker_half_open")

    # Error rate
    if signals.recent_error_rate >= config.error_rate_emergency:
        level = max(level, DegradeLevel.EMERGENCY); reasons.append("error_rate_critical")
    elif signals.recent_error_rate >= config.error_rate_hard:
        level = max(level, DegradeLevel.HARD); reasons.append("error_rate_high")
    elif signals.recent_error_rate >= config.error_rate_soft:
        level = max(level, DegradeLevel.SOFT); reasons.append("error_rate_elevated")

    # Timeouts
    if signals.recent_timeouts >= config.timeout_emergency:
        level = max(level, DegradeLevel.EMERGENCY); reasons.append("timeout_storm")
    elif signals.recent_timeouts >= config.timeout_hard:
        level = max(level, DegradeLevel.HARD); reasons.append("timeout_frequent")
    elif signals.recent_timeouts >= config.timeout_soft:
        level = max(level, DegradeLevel.SOFT); reasons.append("timeout_elevated")

    # Consecutive failures
    if signals.consecutive_failures >= config.consecutive_fail_emergency:
        level = max(level, DegradeLevel.EMERGENCY); reasons.append("consecutive_failures_critical")
    elif signals.consecutive_failures >= config.consecutive_fail_hard:
        level = max(level, DegradeLevel.HARD); reasons.append("consecutive_failures_high")
    elif signals.consecutive_failures >= config.consecutive_fail_soft:
        level = max(level, DegradeLevel.SOFT); reasons.append("consecutive_failures")

    return level, reasons


def decide(signals: ControlSignals, request: RequestMeta, config: DegradeConfig | None = None) -> Decision:
    """Pure function: signals + request -> Decision."""
    cfg = config or DegradeConfig()
    level, reasons = compute_level(signals, cfg)
    d = Decision(level=level, reason_codes=list(reasons))

    if level == DegradeLevel.NORMAL:
        return d

    if level >= DegradeLevel.SOFT:
        if request.cheap_model:
            d.model_override = request.cheap_model
            d.reason_codes.append("model_downgrade")
        cap = int(request.max_tokens * cfg.max_tokens_pct_level1)
        d.max_tokens_cap = max(cap, cfg.max_tokens_floor_level1)
        if request.kind == "tool_call" and request.tool_name not in request.read_only_tools:
            d.allow_tools = False
            d.allowed_tools = request.read_only_tools
        d.retry_cap_override = 2

    if level >= DegradeLevel.HARD:
        cap = int(request.max_tokens * cfg.max_tokens_pct_level2)
        d.max_tokens_cap = max(cap, cfg.max_tokens_floor_level2)
        d.allow_tools = False
        d.allowed_tools = frozenset()
        d.retry_cap_override = 1
        d.scheduler_mode = SchedulerMode.QUEUE_PREFER

    if level >= DegradeLevel.EMERGENCY:
        if request.kind == "llm_call" and request.priority != "P0":
            d.allow_llm = False
            d.reason_codes.append("non_p0_blocked")
        d.max_tokens_cap = cfg.max_tokens_cap_level3
        d.retry_cap_override = 0
        d.scheduler_mode = SchedulerMode.REJECT_PREFER

    return d
