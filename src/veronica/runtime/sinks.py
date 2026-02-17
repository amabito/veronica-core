"""VERONICA Runtime event sinks — pluggable event output destinations."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from veronica.runtime.events import Event, event_to_dict

logger = logging.getLogger(__name__)


@runtime_checkable
class EventSink(Protocol):
    """Protocol for event sinks."""

    def emit(self, event: Event) -> None: ...


class NullSink:
    """Sink that discards all events. Used when VERONICA_EVENTS=0."""

    def emit(self, event: Event) -> None:
        pass


class StdoutSink:
    """Print events as single-line JSON to stdout."""

    def __init__(self, min_severity: str | None = None) -> None:
        self._min_severity = min_severity
        self._severity_order = ["debug", "info", "warn", "error", "critical"]

    def emit(self, event: Event) -> None:
        if self._min_severity:
            event_level = self._severity_order.index(event.severity.value)
            min_level = self._severity_order.index(self._min_severity)
            if event_level < min_level:
                return
        line = json.dumps(event_to_dict(event), ensure_ascii=False, default=str)
        print(line, flush=True)


class JsonlFileSink:
    """Append events as JSON lines to a file. Thread-safe. Supports query."""

    def __init__(self, path: str | Path = "./veronica-events.jsonl") -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: Event) -> None:
        line = json.dumps(event_to_dict(event), ensure_ascii=False, default=str)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def query_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """Scan the JSONL file and return events matching the given run_id, sorted by ts."""
        results: list[dict[str, Any]] = []
        if not self._path.exists():
            return results
        with self._lock:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("run_id") == run_id:
                            results.append(record)
                    except json.JSONDecodeError:
                        continue
        results.sort(key=lambda e: e.get("ts", ""))
        return results

    def close(self) -> None:
        """No-op for file sink (opened/closed per write)."""
        pass


class CompositeSink:
    """Fan-out to multiple sinks. Individual sink errors are logged, not raised."""

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = list(sinks)

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                logger.warning(
                    "CompositeSink: sink %s failed for event %s",
                    type(sink).__name__,
                    event.type,
                    exc_info=True,
                )

    def query_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """Delegate to first sink that supports query."""
        for sink in self._sinks:
            if hasattr(sink, "query_by_run_id"):
                return sink.query_by_run_id(run_id)  # type: ignore[union-attr]
        return []


class ReporterBridgeSink:
    """Bridge to existing VeronicaReporter for backward compatibility.

    Maps new runtime event types to the legacy chain-based event types
    used by the VeronicaReporter cloud SDK.
    """

    # Mapping from new event types to legacy types
    _TYPE_MAP: dict[str, str] = {
        "run.created": "chain_start",
        "run.finished": "chain_end",
        "llm.call.started": "call_attempt",
        "llm.call.succeeded": "call_attempt",
        "llm.call.failed": "call_attempt",
        "tool.call.started": "call_attempt",
        "tool.call.succeeded": "call_attempt",
        "tool.call.failed": "call_attempt",
        "retry.scheduled": "retry",
        "retry.exhausted": "retry",
        "budget.exceeded": "budget_exceeded",
        "loop.detected": "runaway_detected",
        "breaker.opened": "breaker_open",
    }

    def __init__(self, reporter: Any) -> None:
        """Accept a VeronicaReporter instance (duck-typed to avoid import dependency)."""
        self._reporter = reporter

    def emit(self, event: Event) -> None:
        legacy_type = self._TYPE_MAP.get(event.type)
        if legacy_type is None:
            return  # Skip events that have no legacy mapping
        chain_id = event.run_id
        payload = dict(event.payload)
        payload["original_type"] = event.type
        if event.session_id:
            payload["session_id"] = event.session_id
        if event.step_id:
            payload["step_id"] = event.step_id
        try:
            self._reporter.send(legacy_type, chain_id, payload)
        except Exception:
            logger.warning(
                "ReporterBridgeSink: failed to send %s as %s",
                event.type,
                legacy_type,
                exc_info=True,
            )


def create_default_sinks(
    jsonl_path: str | Path = "./veronica-events.jsonl",
) -> list[EventSink]:
    """Create default sinks based on VERONICA_EVENTS env var.

    Set VERONICA_EVENTS=0 to disable all event output.
    """
    if os.environ.get("VERONICA_EVENTS") == "0":
        return [NullSink()]
    return [StdoutSink(), JsonlFileSink(jsonl_path)]
