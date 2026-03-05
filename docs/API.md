# VERONICA Core API Reference

Version: 2.5.0

This document covers the public API. For internal implementation details, see the source files directly.

---

## Table of Contents

1. [Quickstart](#quickstart)
2. [Execution Containment](#execution-containment)
3. [Circuit Breaker](#circuit-breaker)
4. [Distributed Budget](#distributed-budget)
5. [MCP Adapters](#mcp-adapters)
6. [Middleware](#middleware)
7. [Metrics](#metrics)
8. [Protocols](#protocols)
9. [OTel Feedback Loop](#otel-feedback-loop)
10. [Decorator Injection](#decorator-injection)
11. [Breaking Changes (v2.0–v2.4)](#breaking-changes)

---

## Quickstart

`veronica_core.quickstart` — 2-line setup shortcut (v1.4.0+).

```python
import veronica_core

ctx = veronica_core.init("$5.00")
# ... LLM calls are now cost-bounded ...
veronica_core.shutdown()

# Or as a context manager:
with veronica_core.init("$5.00"):
    ...
```

### `init`

```python
def init(
    budget: str,
    *,
    max_steps: int = 1000,
    max_retries_total: int = 50,
    timeout_ms: int = 0,
    on_halt: Literal["raise", "warn", "silent"] = "raise",
    patch_openai: bool = False,
    patch_anthropic: bool = False,
) -> ExecutionContext
```

Creates a global `ExecutionContext`. `budget` is a USD string (`"$5.00"` or `"5"`). Returns the context (also usable as a context manager).

### `shutdown`

```python
def shutdown() -> None
```

Closes the global context. Called automatically at process exit.

### `get_context`

```python
def get_context() -> ExecutionContext | None
```

Returns the active global context, or `None` if not initialized.

---

## Execution Containment

`veronica_core.containment` — chain-level enforcement (v0.9.0+).

### `ExecutionConfig`

Frozen dataclass. All limits must be non-negative.

```python
@dataclass(frozen=True)
class ExecutionConfig:
    max_cost_usd: float
    max_steps: int
    max_retries_total: int
    timeout_ms: int = 0
    budget_backend: BudgetBackend | None = None  # cross-process backend
    redis_url: str | None = None                 # convenience: auto-create RedisBudgetBackend
```

`redis_url` is a shorthand; set `budget_backend` directly for full control.

### `ExecutionContext`

Chain-level containment. Enforces cost, step, retry, and timeout limits.

```python
class ExecutionContext:
    def __init__(
        self,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
        metadata: ChainMetadata | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        parent: ExecutionContext | None = None,
        metrics: ContainmentMetricsProtocol | None = None,
    ) -> None
```

**Context manager:** `__enter__` returns self; `__exit__` calls `close()`.

**Key methods:**

```python
def wrap_llm_call(
    self,
    fn: Callable[[], Any],
    options: WrapOptions | None = None,
) -> Decision

def wrap_tool_call(
    self,
    fn: Callable[[], Any],
    options: WrapOptions | None = None,
) -> Decision

def get_snapshot(self) -> ContextSnapshot

def abort(self, reason: str) -> None

def close(self) -> None  # idempotent, thread-safe (v2.3.1+)
```

`close()` cancels any pending timeout, clears partial buffers, and marks the context closed. All subsequent wrap calls return `Decision.HALT`.

### `ExecutionGraph` — observer/subscriber API (v2.3.0+)

```python
class ExecutionGraph:
    def add_observer(self, observer: ExecutionGraphObserver) -> None
    def remove_observer(self, observer: ExecutionGraphObserver) -> None
    def add_subscriber(self, fn: Callable[[NodeEvent], None]) -> None
    def remove_subscriber(self, fn: Callable[[NodeEvent], None]) -> None
```

Registration is identity-based (no duplicates). Callbacks are copy-on-write safe for lock-free iteration. Subscriber exceptions are swallowed and logged.

### `NodeEvent`

Frozen dataclass emitted on terminal node transitions.

```python
@dataclass(frozen=True)
class NodeEvent:
    node_id: str
    status: str         # "success" | "fail" | "halt"
    kind: str           # "llm" | "tool"
    name: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    depth: int
    elapsed_ms: float
    chain_id: str
    model: str | None
    error_class: str | None
    stop_reason: str | None
```

### `WrapOptions`

Per-call options for `wrap_llm_call` / `wrap_tool_call`.

```python
@dataclass(frozen=True)
class WrapOptions:
    operation_name: str = ""
    cost_estimate_hint: float = 0.0
    timeout_ms: int | None = None
    retry_policy_override: int | None = None
    model: str | None = None
    response_hint: Any = None
    partial_buffer: PartialResultBuffer | None = None
    reconciliation_callback: Any = None
```

### `ContextSnapshot`

Immutable point-in-time view of chain state.

```python
@dataclass(frozen=True)
class ContextSnapshot:
    chain_id: str
    request_id: str
    step_count: int
    cost_usd_accumulated: float
    retries_used: int
    aborted: bool
    abort_reason: str | None
    elapsed_ms: float
    nodes: list[NodeRecord]
    events: list[SafetyEvent]
    graph_summary: dict[str, Any] | None
    parent_chain_id: str | None
```

### `ChainMetadata`

```python
@dataclass(frozen=True)
class ChainMetadata:
    request_id: str
    chain_id: str
    org_id: str = ""
    team: str = ""
    service: str = ""
    user_id: str | None = None
    model: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
```

### `CancellationToken`

```python
class CancellationToken:
    def cancel(self) -> None          # idempotent
    @property
    def is_cancelled(self) -> bool
    def wait(self, timeout_s: float | None = None) -> bool
```

### Helper functions

```python
def get_current_partial_buffer() -> PartialResultBuffer | None
def attach_partial_buffer(buf: PartialResultBuffer) -> None
```

---

## Circuit Breaker

`veronica_core.circuit_breaker`

### `CircuitBreaker`

```python
@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    failure_predicate: FailurePredicate | None = None
```

`FailurePredicate = Callable[[BaseException], bool]`

**Methods:**

```python
def check(self, ctx: PolicyContext) -> PolicyDecision
def record_success(self) -> None
def record_failure(self, exc: BaseException | None = None) -> None
def bind_to_context(self, chain_id: str) -> None
```

### `CircuitState`

```python
class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
```

### Predicate helpers

```python
def ignore_exception_types(*types: type[BaseException]) -> FailurePredicate
def count_exception_types(*types: type[BaseException]) -> FailurePredicate
def ignore_status_codes(*codes: int) -> FailurePredicate
```

---

## Distributed Budget

`veronica_core.distributed`

### `BudgetBackend` (Protocol)

```python
class BudgetBackend(Protocol):
    def add(self, amount: float) -> float: ...
    def get(self) -> float: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...
```

### `ReservableBudgetBackend` (Protocol)

Extends `BudgetBackend` with two-phase reserve/commit/rollback.

```python
class ReservableBudgetBackend(BudgetBackend, Protocol):
    def reserve(self, amount: float, ceiling: float) -> str: ...  # returns reservation_id
    def commit(self, reservation_id: str) -> float: ...
    def rollback(self, reservation_id: str) -> None: ...
    def get_reserved(self) -> float: ...
```

`reserve()` raises `OverflowError` if ceiling would be exceeded; `ValueError` for invalid amount. `commit()` / `rollback()` raise `KeyError` if reservation not found (expired or already processed). Reservations auto-expire after 60 seconds.

### `LocalBudgetBackend`

```python
class LocalBudgetBackend:
    def __init__(self) -> None
```

In-process, thread-safe. Supports reserve/commit/rollback.

### `RedisBudgetBackend`

```python
class RedisBudgetBackend:
    def __init__(
        self,
        redis_url: str,
        chain_id: str,
        ttl_seconds: int = 3600,
        fallback_on_error: bool = True,
    ) -> None
```

Cross-process via Redis `INCRBYFLOAT`. Falls back to `LocalBudgetBackend` on connection failure when `fallback_on_error=True`. Reconciles delta on reconnect.

### `get_default_backend`

```python
def get_default_backend(
    redis_url: str | None = None,
    chain_id: str | None = None,
) -> BudgetBackend
```

### `DistributedCircuitBreaker`

```python
class DistributedCircuitBreaker:
    # Redis-backed circuit breaker for cross-process state
```

### `get_default_circuit_breaker`

```python
def get_default_circuit_breaker() -> DistributedCircuitBreaker
```

### `CircuitSnapshot`

Immutable snapshot of distributed circuit breaker state.

---

## MCP Adapters

`veronica_core.adapters.mcp` and `veronica_core.adapters.mcp_async`

Does not require the MCP SDK. Wraps arbitrary sync/async callables.

### `MCPToolCost`

```python
@dataclass(frozen=True)
class MCPToolCost:
    tool_name: str
    cost_per_call: float = 0.0
    cost_per_token: float = 0.0
```

### `MCPToolResult`

```python
@dataclass(frozen=True)
class MCPToolResult:
    success: bool
    result: Any = None
    error: str | None = None
    decision: Decision = Decision.ALLOW   # v2.4: changed from str to Decision enum
    cost_usd: float = 0.0
```

**v2.4 note:** `decision` is now `Decision` (a `str` subclass). `result.decision == "ALLOW"` still works. Code passing `decision` to APIs requiring a plain `str` (not a subclass) must call `result.decision.value`.

### `MCPToolStats`

```python
@dataclass
class MCPToolStats:
    tool_name: str
    call_count: int = 0
    total_cost_usd: float = 0.0
    error_count: int = 0
    avg_duration_ms: float = 0.0
```

### `MCPContainmentAdapter` (sync)

```python
class MCPContainmentAdapter:
    def __init__(
        self,
        execution_context: ExecutionContext,
        tool_costs: dict[str, MCPToolCost] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        default_cost_per_call: float = 0.001,
        timeout_seconds: float | None = None,
        failure_predicate: FailurePredicate | None = None,
    ) -> None

    def wrap_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_fn: Callable[..., Any],
    ) -> MCPToolResult

    def get_tool_stats(self) -> dict[str, MCPToolStats]
```

### `AsyncMCPContainmentAdapter`

```python
class AsyncMCPContainmentAdapter:
    def __init__(
        self,
        execution_context: ExecutionContext,
        tool_costs: dict[str, MCPToolCost] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        default_cost_per_call: float = 0.001,
        timeout_seconds: float | None = None,
        failure_predicate: FailurePredicate | None = None,
    ) -> None

    async def wrap_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_fn: AsyncCallFn,
    ) -> MCPToolResult

    async def get_tool_stats_async(self) -> dict[str, MCPToolStats]
    def get_tool_stats(self) -> dict[str, MCPToolStats]  # best-effort, non-blocking
```

`_backend_supports_reserve` is cached at init time (v2.4). `_ensure_stats()` has a fast-path for existing tools (v2.4).

### `wrap_mcp_server`

```python
def wrap_mcp_server(
    server: Any,
    execution_context: ExecutionContext,
    **adapter_kwargs: Any,
) -> AsyncMCPContainmentAdapter
```

Wraps an MCP server instance with an `AsyncMCPContainmentAdapter`.

---

## Middleware

`veronica_core.middleware`

### `VeronicaASGIMiddleware`

ASGI3 middleware. Creates one `ExecutionContext` per HTTP request. Returns HTTP 429 when the context is aborted pre-flight or post-call (before response start). WebSocket sessions receive `websocket.close` code 1008 on budget/step exhaustion.

```python
class VeronicaASGIMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
    ) -> None
```

### `VeronicaWSGIMiddleware`

WSGI equivalent with identical semantics.

```python
class VeronicaWSGIMiddleware:
    def __init__(
        self,
        app: WSGIApp,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
    ) -> None
```

### `get_current_execution_context`

```python
def get_current_execution_context() -> ExecutionContext | None
```

Returns the `ExecutionContext` bound to the current request via `ContextVar`, or `None` outside a request.

---

## Metrics

`veronica_core.metrics`

### `ContainmentMetricsProtocol`

See [Protocols](#protocols). Pass a conforming object as `metrics=` to `ExecutionContext`.

### `LoggingContainmentMetrics`

Reference implementation. Forwards telemetry to Python's logging module.

```python
class LoggingContainmentMetrics:
    def __init__(
        self,
        log_level: int = logging.DEBUG,
        logger_name: str | None = None,
    ) -> None

    def record_cost(self, agent_id: str, cost_usd: float) -> None
    def record_tokens(self, agent_id: str, tokens_in: int, tokens_out: int) -> None
    def record_decision(self, agent_id: str, decision: str) -> None
    def record_circuit_state(self, service_id: str, state: str) -> None
    def record_latency(self, agent_id: str, latency_ms: float) -> None
```

---

## Protocols

`veronica_core.protocols` — all `@runtime_checkable`.

### `ContainmentMetricsProtocol`

```python
class ContainmentMetricsProtocol(Protocol):
    def record_cost(self, agent_id: str, cost_usd: float) -> None: ...
    def record_tokens(self, agent_id: str, tokens_in: int, tokens_out: int) -> None: ...
    def record_decision(self, agent_id: str, decision: str) -> None: ...
    def record_circuit_state(self, service_id: str, state: str) -> None: ...
    def record_latency(self, agent_id: str, latency_ms: float) -> None: ...
```

### `FrameworkAdapterProtocol`

```python
class FrameworkAdapterProtocol(Protocol):
    def extract_cost(self, result: Any) -> float: ...
    def extract_tokens(self, result: Any) -> tuple[int, int]: ...
    def handle_halt(self, reason: str) -> Any: ...
    def handle_degrade(self, reason: str, suggestion: str) -> Any: ...
```

### `PlannerProtocol`

Stateless policy proposer. Proposes limits; does not enforce.

```python
class PlannerProtocol(Protocol):
    def propose_policy(self, chain_metadata: Any, prior_events: list) -> dict: ...
    def on_safety_event(self, event: Any) -> None: ...
```

### `ExecutionGraphObserver`

```python
class ExecutionGraphObserver(Protocol):
    def on_node_start(self, node_id: str, kind: str, name: str) -> None: ...
    def on_node_end(self, node_id: str, status: str, cost_usd: float) -> None: ...
    def on_decision(self, node_id: str, decision: str, reason: str) -> None: ...
```

### `AsyncBudgetBackendProtocol`

Async variant of `BudgetBackend` for use with asyncio-native backends.

### `ReconciliationCallback`

Callback type for post-failover budget reconciliation.

---

## OTel Feedback Loop

`veronica_core.otel_feedback` (v2.2.0+)

### `AgentMetrics`

Accumulated per-agent metrics from ingested OTel spans.

```python
@dataclass
class AgentMetrics:
    total_tokens: int = 0
    total_cost: float = 0.0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0       # fraction 0.0–1.0
    last_active: float = 0.0      # monotonic timestamp
    call_count: int = 0

    @property
    def error_count(self) -> int: ...
```

### `OTelMetricsIngester`

Thread-safe span parser. Supports AG2 native spans, generic OTel LLM spans, and veronica-core spans.

```python
class OTelMetricsIngester:
    def __init__(
        self,
        window_sec: float = 60.0,
        max_agents: int = 10_000,
        max_cost_window_size: int = 100_000,
    ) -> None

    def ingest_span(self, span: dict[str, Any]) -> None
    def get_metrics(self, agent_id: str) -> AgentMetrics | None
    def reset(self) -> None
```

Agent cardinality is capped at `max_agents` to prevent unbounded state growth. Non-finite metric values are filtered via `math.isfinite()`.

### `MetricRule`

Declarative threshold rule.

```python
@dataclass
class MetricRule:
    metric: str              # AgentMetrics field name
    operator: str            # "gt" | "lt" | "gte" | "lte" | "eq"
    threshold: float         # must be finite (NaN/inf rejected)
    action: str              # "halt" | "degrade" | "warn"
    agent_id: str | None = None  # None = apply to all agents
```

`MetricRule.__post_init__` rejects NaN/inf thresholds and empty `metric`/`operator`/`action`.

### `MetricsDrivenPolicy`

Implements `RuntimePolicy`. Evaluates rules with severity ordering: halt > degrade > warn.

```python
class MetricsDrivenPolicy:
    def __init__(
        self,
        rules: list[MetricRule],
        ingester: OTelMetricsIngester | None = None,
    ) -> None

    def check(self, ctx: PolicyContext) -> PolicyDecision
```

---

## Decorator Injection

`veronica_core.inject`

### `veronica_guard`

```python
def veronica_guard(
    max_cost_usd: float = 1.0,
    max_steps: int = 25,
    max_retries_total: int = 3,
) -> Callable[[F], F]
```

Decorator wrapping a callable in a policy-enforced boundary. Raises `VeronicaHalt` when a policy denies execution (configurable via `on_halt` in `init()`).

### `VeronicaHalt`

```python
class VeronicaHalt(RuntimeError):
    reason: str
    decision: PolicyDecision | None
```

### `GuardConfig`

```python
@dataclass
class GuardConfig:
    max_cost_usd: float = 1.0
    max_steps: int = 25
    max_retries_total: int = 3
```

Documentation/IDE helper — same fields as `veronica_guard` kwargs.

### Helper functions

```python
def is_guard_active() -> bool
def get_active_container() -> AIContainer | None
```

---

## Breaking Changes

### v2.4.0

- **`MCPToolResult.decision`** type changed from `str` to `Decision` enum. `Decision` inherits from `str`, so `== "ALLOW"` comparisons still work. Code passing `decision` to APIs requiring plain `str` must use `result.decision.value`.

### v2.3.1

- **`AIcontainer` alias** removed. Use `AIContainer`.
- **`VeronicaPersistence`** removed. Use `JSONBackend` or `MemoryBackend`.
- **`GuardConfig.timeout_ms`** removed. Use `ExecutionContext(config=ExecutionConfig(timeout_ms=...))`.

### v2.0

- **Two-phase budget**: `ReservableBudgetBackend` adds `reserve()` / `commit()` / `rollback()`. Existing `BudgetBackend` implementations remain compatible (reserve capability detected via `hasattr`).
- **`AsyncBudgetBackendProtocol`** and **`ReconciliationCallback`** added to `veronica_core.protocols`.
- **WebSocket containment**: `VeronicaASGIMiddleware` now enforces budget/step limits for WebSocket sessions. `websocket.close` code 1008 sent on exhaustion.
- **`ExecutionContext(metrics=)`** parameter added. Pass a `ContainmentMetricsProtocol` object to enable telemetry emission from the containment kernel.
