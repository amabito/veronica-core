"""Serialize veronica-core dataclasses to JSON-ready dicts for compliance export.

All functions are pure -- no side effects, no I/O, no imports beyond stdlib
and veronica_core types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from veronica_core.containment.execution_context import (
    ChainMetadata,
    ContextSnapshot,
    NodeRecord,
)
from veronica_core.shield.event import SafetyEvent


def serialize_safety_event(event: SafetyEvent) -> Dict[str, Any]:
    """Convert a SafetyEvent to a JSON-serializable dict."""
    return {
        "event_type": event.event_type,
        "decision": event.decision.value if hasattr(event.decision, "value") else str(event.decision),
        "reason": event.reason,
        "hook": event.hook,
        "request_id": event.request_id,
        "ts": _iso(event.ts),
        "metadata": event.metadata,
    }


def serialize_node_record(node: NodeRecord) -> Dict[str, Any]:
    """Convert a NodeRecord to a JSON-serializable dict."""
    return {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "kind": node.kind,
        "operation_name": node.operation_name,
        "start_ts": _iso(node.start_ts),
        "end_ts": _iso(node.end_ts) if node.end_ts else None,
        "status": node.status,
        "cost_usd": node.cost_usd,
        "retries_used": node.retries_used,
    }


def serialize_snapshot(
    snapshot: ContextSnapshot,
    metadata: Optional[ChainMetadata] = None,
    graph: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ingest payload from a ContextSnapshot.

    Returns a dict with two top-level keys:
      - "chain": chain-level summary (upserted on the server)
      - "events": list of SafetyEvent dicts (batch-inserted)
    """
    chain: Dict[str, Any] = {
        "chain_id": snapshot.chain_id,
        "request_id": snapshot.request_id,
        "step_count": snapshot.step_count,
        "cost_usd": snapshot.cost_usd_accumulated,
        "retries_used": snapshot.retries_used,
        "aborted": snapshot.aborted,
        "abort_reason": snapshot.abort_reason,
        "elapsed_ms": snapshot.elapsed_ms,
        "started_at": _iso(snapshot.nodes[0].start_ts) if snapshot.nodes else _iso(datetime.min),
    }

    if graph is not None:
        chain["graph_summary"] = graph.get("aggregates")

    if metadata is not None:
        chain["service"] = metadata.service
        chain["team"] = metadata.team
        chain["model"] = metadata.model
        chain["tags"] = dict(metadata.tags) if metadata.tags else {}

    events: List[Dict[str, Any]] = [
        serialize_safety_event(e) for e in snapshot.events
    ]

    return {"chain": chain, "events": events}


def _iso(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with timezone."""
    return dt.isoformat()
