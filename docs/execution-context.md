# ExecutionContext — Chain-Level Containment for Agent Runs

## 1. Problem

`ShieldPipeline` is per-call. Each invocation of `before_llm_call`, `before_egress`,
or `before_charge` operates on a single `ToolCallContext` with no memory of prior calls
in the same agent run.

In a 10-step agent loop this means:

- Budget is enforced per-call, not for the run as a whole. Ten calls each costing $0.09
  under a $0.10 per-call limit add up to $0.90 with no chain-level stop.
- Retry counters reset between calls. A flaky provider can be retried indefinitely across
  calls without any global retry budget draining.
- There is no place to cancel all in-flight work when a timeout or user abort fires.

`ExecutionContext` provides a lifespan-scoped container that travels with a request graph
(the *chain*), wraps `ShieldPipeline`, and enforces invariants that span multiple calls.

---

## 2. Definitions

| Term | Meaning |
|---|---|
| **ExecutionContext** | Lifespan-scoped containment state for one request or agent run. Created once per chain, shared across all nodes within that chain. |
| **Chain** | The logical request graph root — one user action, background job, or agent run. Identified by `chain_id`. |
| **Node** | One LLM call or tool call within the chain. Each `wrap_llm_call` / `wrap_tool_call` invocation creates a `NodeRecord`. |
| **BudgetWindow** | Sliding-window or fixed-ceiling call-count limit enforced by `BudgetWindowHook`. Operates at the per-call level; `ExecutionContext` adds a chain-level USD cost ceiling on top. |
| **SafetyEvent** | Normalized incident record emitted by `ShieldPipeline` or by `ExecutionContext` on chain-level stops. Defined in `veronica_core.shield.event`. |

---

## 3. Required Invariants

These invariants hold for the lifetime of an `ExecutionContext` instance:

1. **Chain-level retry budget** — `ExecutionConfig.max_retries_total` is a ceiling across
   all nodes. Once `retries_used >= max_retries_total`, every subsequent wrap call returns
   `Decision.HALT` without executing the wrapped function.

2. **Hard cost ceiling** — once `cost_usd_accumulated >= ExecutionConfig.max_cost_usd`,
   every subsequent `wrap_llm_call` / `wrap_tool_call` returns `Decision.HALT` without
   executing the wrapped function and without consulting the pipeline.

3. **Step limit** — once `step_count >= ExecutionConfig.max_steps`, every subsequent wrap
   call returns `Decision.HALT`. Prevents runaway agent loops.

4. **Timeout cancellation** — when `timeout_ms` elapses, a cancellation signal propagates
   to all operations currently executing under the context. New wrap calls return
   `Decision.HALT` immediately.

5. **Partial-result preservation** — before any forced stop (step limit, cost ceiling,
   timeout, explicit abort), the context records a snapshot via `get_snapshot()` so callers
   can retrieve intermediate results.

---

## 4. API Surface

### Import path

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig
from veronica_core.containment import ChainMetadata, WrapOptions, ContextSnapshot
```

### ChainMetadata

Immutable descriptor for the chain. All fields except `request_id` and `chain_id` are optional.

```python
@dataclass(frozen=True)
class ChainMetadata:
    request_id: str           # Unique ID for this specific request
    chain_id: str             # Logical grouping ID (agent run, job, user action)
    org_id: str               # Organisation identifier (for multi-tenant deployments)
    team: str                 # Team or cost-centre name
    service: str              # Service or agent name
    user_id: str | None       # End-user ID, if known
    model: str | None         # Primary model name (e.g. "gpt-4o"), if known
    tags: dict[str, str]      # Arbitrary key/value labels (e.g. {"env": "prod"})
```

### ExecutionConfig

Hard limits for one chain. All values must be positive.

```python
@dataclass(frozen=True)
class ExecutionConfig:
    max_cost_usd: float        # Chain-level USD spending ceiling (hard stop)
    max_steps: int             # Maximum number of wrap calls before HALT
    max_retries_total: int     # Chain-wide retry budget across all nodes
    timeout_ms: int            # Wall-clock timeout in milliseconds (0 = disabled)
```

### WrapOptions

Per-call options passed alongside the callable. All fields are optional.

```python
@dataclass(frozen=True)
class WrapOptions:
    operation_name: str              # Human-readable label for this node (default: "")
    cost_estimate_hint: float        # Estimated cost in USD; used for pre-flight check
    timeout_ms: int | None           # Per-call timeout; overrides config.timeout_ms
    retry_policy_override: int | None  # Max retries for this call only; overrides chain default
```

### ExecutionContext

Primary class. Can be used as a context manager or standalone.

```python
class ExecutionContext:
    def __init__(
        self,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
        metadata: ChainMetadata | None = None,
    ) -> None: ...

    # Context manager
    def __enter__(self) -> ExecutionContext: ...
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...

    def wrap_llm_call(
        self,
        fn: Callable[[], Any],
        options: WrapOptions | None = None,
    ) -> Decision: ...

    def wrap_tool_call(
        self,
        fn: Callable[[], Any],
        options: WrapOptions | None = None,
    ) -> Decision: ...

    def record_event(self, event: SafetyEvent) -> None: ...

    def get_snapshot(self) -> ContextSnapshot: ...

    def abort(self, reason: str) -> None: ...
```

#### Method reference

`wrap_llm_call(fn, options=None) -> Decision`

Executes `fn` under chain-level containment.

1. Calls `_check_limits()`. If a limit is exceeded, records a `SafetyEvent` and returns
   `Decision.HALT` without calling `fn`.
2. If `pipeline` is set, constructs a `ToolCallContext` and calls
   `pipeline.before_llm_call(ctx)`. A non-ALLOW decision aborts the call.
3. Consults `CircuitBreaker.check()` before dispatching.
4. Calls `fn()`. On success, increments `step_count`, accumulates `cost_usd`, calls
   `pipeline.before_charge()`, and calls `circuit_breaker.record_success()`.
5. On exception, calls `pipeline.on_error()`, calls `circuit_breaker.record_failure()`,
   increments `retries_used`, and returns the pipeline decision or `Decision.RETRY`.
6. Returns `Decision.ALLOW` on clean completion.

`wrap_tool_call(fn, options=None) -> Decision`

Identical to `wrap_llm_call` but uses `kind="tool"` in the `NodeRecord` and calls any
registered tool-call hooks instead of LLM-call hooks.

`record_event(event: SafetyEvent) -> None`

Appends `event` to the chain-level event log. Used by callers that emit their own
`SafetyEvent` instances outside of `wrap_llm_call` / `wrap_tool_call`.

`get_snapshot() -> ContextSnapshot`

Returns an immutable copy of the current chain state. Safe to call at any time, including
from finalisation code after `abort()`.

`abort(reason: str) -> None`

Sets the internal abort flag and signals the `CancellationToken`. All subsequent wrap
calls return `Decision.HALT` immediately. Does not raise an exception.

---

## 5. Execution Graph Tracking

`ExecutionContext` maintains a lightweight in-memory node list. No external tracing
dependency is required.

```python
@dataclass
class NodeRecord:
    node_id: str                        # UUID for this node
    parent_id: str | None               # Parent node_id, or None at chain root
    kind: Literal["llm", "tool"]        # Call type
    operation_name: str                 # From WrapOptions.operation_name
    start_ts: datetime                  # UTC timestamp when wrap was entered
    end_ts: datetime | None             # UTC timestamp when wrap exited (None if in-flight)
    status: Literal[                    # Terminal status
        "ok",
        "halted",
        "aborted",
        "timeout",
        "error",
    ]
    cost_usd: float                     # Actual cost charged (0.0 if not known)
    retries_used: int                   # Retries consumed by this node
```

`ContextSnapshot` captures chain-wide state at a point in time:

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
```

---

## 6. Error Taxonomy

The following stop-reason strings appear in `SafetyEvent.event_type` and
`abort_reason` fields produced by `ExecutionContext`.

| Stop reason | Source | `Decision` returned |
|---|---|---|
| `budget_exceeded` | `_check_limits()` — `cost_usd_accumulated >= max_cost_usd` | `HALT` |
| `retry_budget_exceeded` | `_check_limits()` — `retries_used >= max_retries_total` | `HALT` |
| `circuit_open` | `CircuitBreaker.state == OPEN` at pre-dispatch check | `HALT` |
| `step_limit_exceeded` | `_check_limits()` — `step_count >= max_steps` | `HALT` |
| `timeout` | `timeout_ms` elapsed; `CancellationToken` signalled | `HALT` |
| `aborted` | `ctx.abort()` called by application code | `HALT` |
| `provider_rate_limit` | `BudgetWindowHook` returned `HALT` via pipeline | `HALT` |
| `provider_error` | `CircuitBreaker.record_failure()` then retry budget exhausted | `HALT` |

All chain-level stops emit a `SafetyEvent` with `hook="ExecutionContext"` and the
corresponding `event_type` value.

---

## 7. Integration with Existing Components

### ShieldPipeline

`ExecutionContext` wraps `ShieldPipeline`; it does not replace it.

On each `wrap_llm_call`:

1. `ExecutionContext._check_limits()` runs first. If it returns `HALT`, `ShieldPipeline`
   is not consulted.
2. `ExecutionContext._make_tool_ctx(node_id, options)` constructs a `ToolCallContext`
   populated from `ChainMetadata` and `WrapOptions`.
3. `pipeline.before_llm_call(ctx)` is called. A non-ALLOW result aborts the call and
   the decision is forwarded to the caller.
4. After the call succeeds, `pipeline.before_charge(ctx, cost_usd)` is called with the
   actual cost.
5. `pipeline.get_events()` is mirrored into the chain-level event log on each wrap.

### BudgetWindowHook

`BudgetWindowHook` enforces per-window call-count limits within the pipeline. This is
orthogonal to `ExecutionConfig.max_cost_usd`, which enforces a USD ceiling across the
entire chain lifetime regardless of time windows. Both limits apply simultaneously: a call
is blocked if either the window count is exceeded or the chain cost ceiling is reached.

### CircuitBreaker

Before dispatching the wrapped function, `ExecutionContext` calls
`circuit_breaker.check(PolicyContext(cost_usd=..., step_count=...))`. If
`PolicyDecision.allowed` is `False` (circuit OPEN or HALF_OPEN test in progress),
`ExecutionContext` records a `SafetyEvent` with `event_type="circuit_open"` and returns
`Decision.HALT`.

After each call:
- Success → `circuit_breaker.record_success()`
- Exception → `circuit_breaker.record_failure()`

### SafetyEvent

Two sources of `SafetyEvent` feed the chain-level log:

1. `ShieldPipeline` emits events for per-call policy violations (existing behaviour,
   unchanged). `ExecutionContext` copies these into its own log after each wrap.
2. `ExecutionContext` emits its own events for chain-level stops (step limit, cost ceiling,
   timeout, abort). These use `hook="ExecutionContext"`.

The combined log is returned by `get_snapshot().events`.

---

## 8. Backwards Compatibility

- All existing code using `ShieldPipeline` directly continues to work without any changes.
- `ExecutionContext` is strictly opt-in. Passing `pipeline=None` runs containment checks
  only; no shield hooks are consulted.
- An implicit per-call `ExecutionContext` can be constructed by application code if the
  caller holds a pipeline but has not adopted `ExecutionContext` yet. This gives a
  migration path from direct pipeline usage.
- `ToolCallContext`, `Decision`, `SafetyEvent`, and `ShieldPipeline` APIs are unchanged.

---

## 9. Examples

### Example 1 — Single request chain

```python
from veronica_core.containment import (
    ExecutionContext,
    ExecutionConfig,
    ChainMetadata,
    WrapOptions,
)
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision

config = ExecutionConfig(
    max_cost_usd=0.50,
    max_steps=10,
    max_retries_total=3,
    timeout_ms=15_000,
)

meta = ChainMetadata(
    request_id="req-001",
    chain_id="chain-001",
    org_id="acme",
    team="ml-platform",
    service="summariser",
    user_id="user-42",
    model="gpt-4o",
    tags={"env": "prod"},
)

pipeline = ShieldPipeline()  # configure hooks as needed

with ExecutionContext(config=config, pipeline=pipeline, metadata=meta) as ctx:
    decision = ctx.wrap_llm_call(
        fn=lambda: call_llm(prompt="Summarise this document."),
        options=WrapOptions(operation_name="summarise", cost_estimate_hint=0.02),
    )
    if decision != Decision.ALLOW:
        print(f"Call blocked: {decision}")

snap = ctx.get_snapshot()
print(f"Total cost: ${snap.cost_usd_accumulated:.4f}, steps: {snap.step_count}")
```

### Example 2 — Agent loop with step limit

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig, WrapOptions
from veronica_core.shield.types import Decision

config = ExecutionConfig(
    max_cost_usd=2.00,
    max_steps=20,
    max_retries_total=5,
    timeout_ms=60_000,
)

ctx = ExecutionContext(config=config)

try:
    for step in range(100):  # Loop is bounded by ExecutionConfig, not the range
        decision = ctx.wrap_llm_call(
            fn=lambda: agent_step(step),
            options=WrapOptions(operation_name=f"agent_step_{step}"),
        )
        if decision == Decision.HALT:
            snap = ctx.get_snapshot()
            print(
                f"Chain halted at step {snap.step_count}: "
                f"last event={snap.events[-1].event_type if snap.events else 'none'}"
            )
            break
        process_result(step)
finally:
    snap = ctx.get_snapshot()
    print(f"Final: steps={snap.step_count}, cost=${snap.cost_usd_accumulated:.4f}")
```
