"""VERONICA Runtime data models."""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def generate_uuidv7() -> str:
    """Generate a UUIDv7 (time-ordered) using stdlib only (RFC 9562)."""
    timestamp_ms = int(time.time() * 1000)
    rand_bytes = os.urandom(10)
    b = bytearray(16)
    # Bytes 0-5: 48-bit big-endian timestamp in milliseconds
    for i in range(6):
        b[5 - i] = (timestamp_ms >> (8 * i)) & 0xFF
    # Byte 6: version (7) in high nibble + 4 bits of random
    b[6] = 0x70 | (rand_bytes[0] & 0x0F)
    # Byte 7: random
    b[7] = rand_bytes[1]
    # Byte 8: variant (10) in high 2 bits + 6 bits of random
    b[8] = 0x80 | (rand_bytes[2] & 0x3F)
    # Bytes 9-15: random
    b[9:16] = rand_bytes[3:10]
    return str(uuid.UUID(bytes=bytes(b)))


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def make_result_ref(content: str) -> str:
    """Create a result reference: sha256[:16] + ':' + content[:200]."""
    h = hashlib.sha256(content.encode()).hexdigest()[:16]
    preview = content[:200]
    return f"{h}:{preview}"


# --- Enums ---

class RunStatus(str, Enum):
    RUNNING = "running"
    DEGRADED = "degraded"
    HALTED = "halted"
    QUARANTINED = "quarantined"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class SessionStatus(str, Enum):
    RUNNING = "running"
    HALTED = "halted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class StepStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class StepKind(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    RETRY = "retry"
    BREAKER_CHECK = "breaker_check"
    BUDGET_CHECK = "budget_check"
    ABORT = "abort"
    TIMEOUT = "timeout"
    LOOP_DETECT = "loop_detect"


class Severity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


# --- Supporting dataclasses ---

@dataclass
class Budget:
    limit_usd: float = 0.0
    used_usd: float = 0.0
    limit_tokens: int = 0
    used_tokens: int = 0


@dataclass
class Labels:
    org: str = ""
    team: str = ""
    service: str = ""
    user: str = ""
    env: str = ""
    model_default: str = ""


@dataclass
class SessionCounters:
    steps_total: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    retries_total: int = 0


@dataclass
class StepError:
    type: str = ""
    message: str = ""
    retryable: bool = False
    classified_reason: str = ""


# --- Main dataclasses ---

@dataclass
class Run:
    run_id: str = field(default_factory=generate_uuidv7)
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    status: RunStatus = RunStatus.RUNNING
    labels: Labels = field(default_factory=Labels)
    budget: Budget = field(default_factory=Budget)
    error_summary: str | None = None


@dataclass
class Session:
    session_id: str = field(default_factory=generate_uuidv7)
    run_id: str = ""
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    status: SessionStatus = SessionStatus.RUNNING
    agent_name: str = ""
    max_steps: int = 100
    loop_detection_on: bool = True
    counters: SessionCounters = field(default_factory=SessionCounters)


@dataclass
class Step:
    step_id: str = field(default_factory=generate_uuidv7)
    session_id: str = ""
    run_id: str = ""
    parent_step_id: str | None = None
    kind: StepKind = StepKind.LLM_CALL
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    status: StepStatus = StepStatus.STARTED
    model: str | None = None
    tool: str | None = None
    provider: str | None = None
    latency_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    result_ref: str | None = None
    error: StepError | None = None
