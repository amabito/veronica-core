"""Execution log models for policy simulation.

ExecutionLogEntry captures a single recorded action (LLM call, tool call,
reply) with its cost, token count, latency, and success status.

ExecutionLog is a collection of entries with factory methods for loading
from JSON files and OTel span exports.

Zero external dependencies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionLogEntry:
    """Single recorded action in an execution log.

    Attributes:
        timestamp: Unix timestamp of the action.
        agent_id:  Identifier of the agent that performed the action.
        action:    Action type: "llm_call", "tool_call", "reply".
        cost_usd:  USD cost of this action.
        tokens:    Total tokens consumed (prompt + completion).
        latency_ms: Wall-clock latency in milliseconds.
        success:   Whether the action completed successfully.
        model:     Model name (optional, for cost estimation).
        metadata:  Extra key-value pairs from the source data.
    """

    timestamp: float
    agent_id: str
    action: str
    cost_usd: float = 0.0
    tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    _VALID_ACTIONS: frozenset[str] = field(
        default=frozenset({"llm_call", "tool_call", "reply"}),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(
                f"ExecutionLogEntry.action={self.action!r} is invalid; "
                f"valid: {sorted(self._VALID_ACTIONS)}"
            )
        if self.cost_usd < 0:
            raise ValueError(
                f"ExecutionLogEntry.cost_usd must be non-negative, got {self.cost_usd}"
            )
        if self.tokens < 0:
            raise ValueError(
                f"ExecutionLogEntry.tokens must be non-negative, got {self.tokens}"
            )


def _entry_from_dict(data: dict[str, Any]) -> ExecutionLogEntry:
    """Construct an ExecutionLogEntry from a plain dict."""
    return ExecutionLogEntry(
        timestamp=float(data.get("timestamp", 0.0)),
        agent_id=str(data.get("agent_id", "")),
        action=str(data.get("action", "llm_call")),
        cost_usd=float(data.get("cost_usd", 0.0)),
        tokens=int(data.get("tokens", 0)),
        latency_ms=float(data.get("latency_ms", 0.0)),
        success=bool(data.get("success", True)),
        model=str(data.get("model", "")),
        metadata=dict(data.get("metadata") or {}),
    )


def _entry_from_otel_span(span: dict[str, Any]) -> ExecutionLogEntry | None:
    """Convert an OTel span dict to an ExecutionLogEntry.

    Supports AG2-style OTel spans with attributes like:
    - gen_ai.system, gen_ai.request.model
    - gen_ai.usage.prompt_tokens, gen_ai.usage.completion_tokens
    - gen_ai.response.finish_reason

    Returns None if the span cannot be parsed.
    """
    try:
        attrs = span.get("attributes") or {}
        name = span.get("name") or ""

        # Determine action type from span name or attributes
        action = "llm_call"
        if "tool" in name.lower() or attrs.get("gen_ai.tool.name"):
            action = "tool_call"

        # Extract timestamps
        start_ns = span.get("start_time", 0)
        end_ns = span.get("end_time", 0)
        # OTel timestamps are nanoseconds; convert to seconds/ms
        timestamp = start_ns / 1e9 if start_ns > 1e15 else float(start_ns)
        latency_ms = (end_ns - start_ns) / 1e6 if end_ns > start_ns else 0.0

        # Extract token counts
        prompt_tokens = int(attrs.get("gen_ai.usage.prompt_tokens", 0))
        completion_tokens = int(attrs.get("gen_ai.usage.completion_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens

        # Determine success from status
        status = span.get("status") or {}
        status_code = (
            status.get("status_code", "OK") if isinstance(status, dict) else "OK"
        )
        success = status_code != "ERROR"

        model = str(attrs.get("gen_ai.request.model", ""))
        agent_id = str(attrs.get("agent.id", attrs.get("gen_ai.agent.id", name)))

        # Cost: not typically in OTel spans, default to 0
        cost_usd = float(attrs.get("gen_ai.usage.cost", 0.0))

        return ExecutionLogEntry(
            timestamp=timestamp,
            agent_id=agent_id,
            action=action,
            cost_usd=cost_usd,
            tokens=total_tokens,
            latency_ms=latency_ms,
            success=success,
            model=model,
            metadata={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )
    except Exception:
        logger.debug("Failed to parse OTel span: %s", span, exc_info=True)
        return None


class ExecutionLog:
    """Collection of ExecutionLogEntry instances.

    Factory methods load entries from JSON files or OTel span exports.
    Entries are sorted by timestamp on construction.
    """

    def __init__(self, entries: list[ExecutionLogEntry] | None = None) -> None:
        self._entries: list[ExecutionLogEntry] = sorted(
            entries or [], key=lambda e: e.timestamp
        )

    @property
    def entries(self) -> list[ExecutionLogEntry]:
        """Return entries sorted by timestamp (shallow copy)."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def _from_parsed(cls, data: Any) -> "ExecutionLog":
        """Build an ExecutionLog from parsed JSON data (dict or list)."""
        if isinstance(data, dict):
            raw_entries = data.get("entries", [])
        elif isinstance(data, list):
            raw_entries = data
        else:
            raise ValueError(
                f"Expected a JSON object or array, got {type(data).__name__}"
            )
        entries = [_entry_from_dict(r) for r in raw_entries if isinstance(r, dict)]
        return cls(entries)

    #: Default maximum file size for from_file() (50 MB).
    _DEFAULT_MAX_SIZE_BYTES: int = 50 * 1024 * 1024

    @classmethod
    def from_file(
        cls, path: str | Path, *, max_size_bytes: int | None = None
    ) -> "ExecutionLog":
        """Load entries from a JSON file.

        Expected format::

            {
              "entries": [
                {"timestamp": 1700000000.0, "agent_id": "bot", "action": "llm_call", ...},
                ...
              ]
            }

        Or a bare list of entry dicts.

        Args:
            path: Path to the JSON log file.
            max_size_bytes: Maximum allowed file size in bytes. Defaults to
                50 MB. Pass 0 to disable the size check.
        """
        file_path = Path(path)
        limit = max_size_bytes if max_size_bytes is not None else cls._DEFAULT_MAX_SIZE_BYTES
        if limit > 0:
            size = file_path.stat().st_size
            if size > limit:
                raise ValueError(
                    f"File {file_path} is {size} bytes, exceeds max_size_bytes={limit}"
                )
        content = file_path.read_text(encoding="utf-8")
        return cls._from_parsed(json.loads(content))

    @classmethod
    def from_otel_export(cls, spans: list[dict[str, Any]]) -> "ExecutionLog":
        """Convert OTel span dicts to an ExecutionLog.

        Spans that cannot be parsed are silently skipped.
        """
        entries: list[ExecutionLogEntry] = []
        for span in spans:
            entry = _entry_from_otel_span(span)
            if entry is not None:
                entries.append(entry)
        return cls(entries)

    @classmethod
    def from_string(cls, content: str) -> "ExecutionLog":
        """Parse a JSON string into an ExecutionLog."""
        return cls._from_parsed(json.loads(content))
