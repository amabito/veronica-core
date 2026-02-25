"""Chain-level execution containment for VERONICA.

Public surface exported from this package:
- ExecutionContext: lifespan-scoped container for one agent run or request chain
- ExecutionConfig: hard limits (cost, steps, retries, timeout)
- ChainMetadata: immutable chain descriptor (org, team, service, IDs, tags)
- WrapOptions: per-call options passed alongside the wrapped callable
- ContextSnapshot: immutable snapshot of chain state at a point in time
- NodeRecord: record of a single LLM or tool call within the chain
- CancellationToken: simple threading.Event wrapper for cooperative cancellation
- ExecutionGraph: directed acyclic graph tracking every node in one agent chain
"""

from veronica_core.containment.execution_context import (
    CancellationToken,
    ChainMetadata,
    ContextSnapshot,
    ExecutionConfig,
    ExecutionContext,
    NodeRecord,
    WrapOptions,
    get_current_partial_buffer,
    attach_partial_buffer,
)
from veronica_core.containment.execution_graph import ExecutionGraph

__all__ = [
    "CancellationToken",
    "ChainMetadata",
    "ContextSnapshot",
    "ExecutionConfig",
    "ExecutionContext",
    "ExecutionGraph",
    "NodeRecord",
    "WrapOptions",
    "get_current_partial_buffer",
    "attach_partial_buffer",
]
