"""VERONICA demo -- ASCII timeline renderer for Event sequences."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from veronica.runtime.events import Event
from veronica.runtime.models import Severity

# Event types to suppress from timeline output
_SKIP_TYPES: frozenset[str] = frozenset(
    {
        "scheduler.inflight.inc",
        "scheduler.inflight.dec",
    }
)

# Column widths for alignment
_TYPE_COL_WIDTH = 36
_DETAIL_COL_WIDTH = 60

# Severity markers for the rendered line
_SEVERITY_MARKER: dict[str, str] = {
    Severity.DEBUG.value: " ",
    Severity.INFO.value: " ",
    Severity.WARN.value: "!",
    Severity.ERROR.value: "E",
    Severity.CRITICAL.value: "C",
}


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime."""
    # datetime.fromisoformat handles +00:00 in Python 3.11+
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _elapsed_ms(t0_iso: str, ts_iso: str) -> int:
    """Return elapsed milliseconds between two ISO timestamps."""
    t0 = _parse_iso(t0_iso)
    t1 = _parse_iso(ts_iso)
    delta = t1 - t0
    return max(0, int(delta.total_seconds() * 1000))


def _extract_detail(event: Event) -> str:
    """Map event type to a concise detail string from its payload."""
    p = event.payload
    etype = event.type

    # LLM calls
    if etype == "llm.call.started":
        model = p.get("model") or p.get("model_id") or ""
        tokens = p.get("max_tokens") or ""
        parts = [f"model={model}"] if model else []
        if tokens:
            parts.append(f"max_tokens={tokens}")
        return "  ".join(parts)

    if etype == "llm.call.succeeded":
        model = p.get("model") or p.get("model_id") or ""
        tokens_out = p.get("tokens_out") or p.get("completion_tokens") or ""
        latency = p.get("latency_ms") or ""
        parts = [f"model={model}"] if model else []
        if tokens_out:
            parts.append(f"tokens_out={tokens_out}")
        if latency:
            parts.append(f"latency={latency}ms")
        return "  ".join(parts)

    if etype == "llm.call.failed":
        error = p.get("error") or p.get("error_type") or p.get("reason") or ""
        model = p.get("model") or ""
        parts = [f"error={error}"] if error else []
        if model:
            parts.append(f"model={model}")
        return "  ".join(parts)

    # Tool calls
    if etype == "tool.call.started":
        tool = p.get("tool") or p.get("tool_name") or ""
        return f"tool={tool}" if tool else ""

    if etype == "tool.call.succeeded":
        tool = p.get("tool") or p.get("tool_name") or ""
        latency = p.get("latency_ms") or ""
        parts = [f"tool={tool}"] if tool else []
        if latency:
            parts.append(f"latency={latency}ms")
        return "  ".join(parts)

    if etype == "tool.call.failed":
        tool = p.get("tool") or p.get("tool_name") or ""
        error = p.get("error") or p.get("error_type") or p.get("reason") or ""
        parts = [f"tool={tool}"] if tool else []
        if error:
            parts.append(f"error={error}")
        return "  ".join(parts)

    # Retry
    if etype == "retry.scheduled":
        attempt = p.get("attempt") or ""
        delay = p.get("delay_ms") or p.get("delay_seconds") or ""
        reason = p.get("reason") or ""
        parts = [f"attempt={attempt}"] if attempt else []
        if delay:
            parts.append(f"delay={delay}ms")
        if reason:
            parts.append(f"reason={reason}")
        return "  ".join(parts)

    if etype == "retry.exhausted":
        max_attempts = p.get("max_attempts") or p.get("attempts") or ""
        reason = p.get("reason") or ""
        parts = [f"max_attempts={max_attempts}"] if max_attempts else []
        if reason:
            parts.append(f"reason={reason}")
        return "  ".join(parts)

    # Circuit breaker
    if etype in {"breaker.opened", "breaker.half_open", "breaker.closed"}:
        name = p.get("name") or p.get("breaker_name") or ""
        failures = p.get("failure_count") or p.get("failures") or ""
        parts = [f"name={name}"] if name else []
        if failures:
            parts.append(f"failures={failures}")
        return "  ".join(parts)

    # Budget
    if etype in {"budget.check", "budget.exceeded", "budget.reserve.ok",
                 "budget.reserve.denied", "budget.commit", "budget.threshold_crossed"}:
        used = p.get("used_usd") or p.get("used") or ""
        limit = p.get("limit_usd") or p.get("limit") or ""
        threshold = p.get("threshold") or ""
        parts = []
        if used:
            parts.append(f"used=${used}")
        if limit:
            parts.append(f"limit=${limit}")
        if threshold:
            parts.append(f"threshold={threshold}")
        return "  ".join(parts)

    # Control / degrade
    if etype == "control.degrade.level_changed":
        old = p.get("old_level") or p.get("from") or ""
        new = p.get("new_level") or p.get("to") or ""
        reason = p.get("reason") or ""
        parts = []
        if old and new:
            parts.append(f"{old} -> {new}")
        elif new:
            parts.append(f"level={new}")
        if reason:
            parts.append(f"reason={reason}")
        return "  ".join(parts)

    if etype == "control.decision.made":
        action = p.get("action") or p.get("decision") or ""
        reason = p.get("reason") or ""
        parts = [f"action={action}"] if action else []
        if reason:
            parts.append(f"reason={reason}")
        return "  ".join(parts)

    if etype in {"control.action.model_downgrade"}:
        old_model = p.get("old_model") or p.get("from") or ""
        new_model = p.get("new_model") or p.get("to") or ""
        if old_model and new_model:
            return f"{old_model} -> {new_model}"
        return new_model or old_model

    if etype == "control.action.max_tokens_capped":
        cap = p.get("cap") or p.get("max_tokens") or ""
        return f"cap={cap}" if cap else ""

    if etype == "control.action.tools_blocked":
        tools = p.get("tools") or []
        if isinstance(tools, list):
            return f"tools={','.join(str(t) for t in tools)}"
        return str(tools)

    if etype == "control.action.scheduler_mode_changed":
        mode = p.get("mode") or p.get("new_mode") or ""
        return f"mode={mode}" if mode else ""

    # Abort / timeout / loop
    if etype == "abort.triggered":
        reason = p.get("reason") or ""
        return f"reason={reason}" if reason else ""

    if etype == "timeout.triggered":
        timeout_s = p.get("timeout_s") or p.get("timeout_seconds") or ""
        return f"timeout={timeout_s}s" if timeout_s else ""

    if etype == "loop.detected":
        count = p.get("count") or p.get("loop_count") or ""
        return f"loop_count={count}" if count else ""

    # Run/session/step lifecycle
    if etype in {"run.created", "run.finished"}:
        status = p.get("status") or ""
        return f"status={status}" if status else ""

    if etype in {"session.created", "session.finished"}:
        agent = p.get("agent_name") or p.get("agent") or ""
        status = p.get("status") or ""
        parts = [f"agent={agent}"] if agent else []
        if status:
            parts.append(f"status={status}")
        return "  ".join(parts)

    if etype in {"step.started", "step.succeeded", "step.failed"}:
        kind = p.get("kind") or ""
        return f"kind={kind}" if kind else ""

    # Scheduler
    if etype in {"scheduler.admit.allowed", "scheduler.admit.queued",
                 "scheduler.admit.rejected", "scheduler.queue.enqueued",
                 "scheduler.queue.dequeued", "scheduler.queue.dropped",
                 "scheduler.priority_boost"}:
        priority = p.get("priority") or ""
        reason = p.get("reason") or ""
        parts = [f"priority={priority}"] if priority else []
        if reason:
            parts.append(f"reason={reason}")
        return "  ".join(parts)

    # Partial preserved
    if etype == "partial.preserved":
        ref = p.get("result_ref") or p.get("ref") or ""
        return f"ref={ref[:24]}..." if len(ref) > 24 else (f"ref={ref}" if ref else "")

    # Fallback: first non-empty payload value
    for v in p.values():
        if v:
            s = str(v)
            return s[:_DETAIL_COL_WIDTH] if len(s) > _DETAIL_COL_WIDTH else s

    return ""


def render_event_line(event: Event, t0_iso: str) -> str:
    """Render a single event as a timeline line.

    Format: [+NNNms] <marker> <type padded>  <detail>
    """
    elapsed = _elapsed_ms(t0_iso, event.ts)
    time_col = f"[+{elapsed:>5}ms]"

    marker = _SEVERITY_MARKER.get(event.severity.value, " ")

    type_col = event.type.ljust(_TYPE_COL_WIDTH)[:_TYPE_COL_WIDTH]

    detail = _extract_detail(event)
    if len(detail) > _DETAIL_COL_WIDTH:
        detail = detail[: _DETAIL_COL_WIDTH - 3] + "..."

    return f"{time_col} {marker} {type_col}  {detail}".rstrip()


def render_scenario(name: str, before_text: str, events: Sequence[Event]) -> str:
    """Render a full scenario section: header, before text, timeline, counts.

    Args:
        name: Human-readable scenario name.
        before_text: Multi-line description of what will happen.
        events: Sequence of Event objects to render.

    Returns:
        Formatted string with header, before text, timeline lines, and counts.
    """
    lines: list[str] = []

    # Header
    separator = "=" * 72
    lines.append(separator)
    lines.append(f"  SCENARIO: {name}")
    lines.append(separator)

    # Before text (what the scenario demonstrates)
    for bline in before_text.strip().splitlines():
        lines.append(f"  {bline}")
    lines.append("")

    # Filter events
    visible_events: list[Event] = [
        e
        for e in events
        if e.type not in _SKIP_TYPES and e.severity != Severity.DEBUG
    ]

    if not visible_events:
        lines.append("  (no events to display)")
        lines.append("")
        return "\n".join(lines)

    # Timeline header
    lines.append(f"  {'TIME':>10}   {'TYPE':<{_TYPE_COL_WIDTH}}  DETAIL")
    lines.append("  " + "-" * (10 + 3 + _TYPE_COL_WIDTH + 2 + _DETAIL_COL_WIDTH))

    t0_iso = visible_events[0].ts
    for event in visible_events:
        lines.append("  " + render_event_line(event, t0_iso))

    # Counts
    lines.append("")
    total = len(visible_events)
    by_sev: dict[str, int] = {}
    for e in visible_events:
        by_sev[e.severity.value] = by_sev.get(e.severity.value, 0) + 1

    sev_summary = "  ".join(
        f"{k}={v}"
        for k, v in sorted(by_sev.items(), key=lambda x: list(Severity).index(Severity(x[0])))
        if v > 0
    )
    lines.append(f"  Events: {total}  ({sev_summary})")
    lines.append("")

    return "\n".join(lines)


def render_summary(total_events: int, jsonl_path: str) -> str:
    """Render the final summary block after all scenarios.

    Args:
        total_events: Total number of events written across all scenarios.
        jsonl_path: Path to the JSONL output file.

    Returns:
        Formatted summary string.
    """
    separator = "=" * 72
    lines = [
        separator,
        "  VERONICA LLM Control OS -- Demo Complete",
        separator,
        f"  Total events recorded : {total_events}",
        f"  JSONL output          : {jsonl_path}",
        "",
        "  All scenarios use only the public runtime API.",
        "  No mocks -- real EventBus, real sinks, real policy enforcement.",
        separator,
    ]
    return "\n".join(lines)
