# VERONICA: A Runtime Containment Layer for LLM Agent Systems

**Draft — Technical Systems Paper**

---

## Abstract

Large language model (LLM) agent frameworks expose a class of runaway failure modes
that differ qualitatively from classical software defects. Retry amplification,
recursive tool invocation loops, multi-agent cost cascades, and WebSocket session
runaways share a common structure: they originate from individually reasonable
local decisions that compose into unbounded global resource consumption.
Existing LLM frameworks (LangChain, LangGraph, AG2) delegate containment to
application developers through per-call timeouts and manual budget checks,
leaving compositional failures unaddressed.

We present VERONICA (Verified Execution Runtime for Observably Networked
Intelligent Contained Agents), a runtime containment layer that enforces
chain-level resource bounds without requiring modifications to agent logic.
VERONICA introduces four orthogonal primitives -- `BudgetEnforcer`, `AgentStepGuard`,
`CircuitBreaker`, and `RetryContainer` -- unified under a `RuntimePolicy` protocol
and composed by `PolicyPipeline`. A chain-scoped `ExecutionContext` enforces these
bounds across all LLM and tool calls within a single agent run, with propagation
through ASGI/WSGI middleware and WebSocket sessions.

We describe the system architecture, threat model, safety mechanisms, and evaluation
against LangChain, AG2, and LangGraph baselines. VERONICA bounds cost amplification
to at most `max_cost_usd` per chain, steps to at most `max_steps`, and
retries to at most `max_retries_total`, with formal proofs grounded in the
`reserve-check-commit` budget protocol and Lua-atomic circuit breaker state
transitions.

---

## 1. Introduction

LLM agent systems execute multi-step plans by repeatedly calling language models
and invoking tools based on model outputs. This structure creates several runaway
failure modes:

**Retry amplification.** When three nested tool layers each retry three times on
failure, a single user action generates 27 LLM calls. Frameworks that implement
per-call retry budgets do not track cross-layer retry counts.

**Recursive tool loops.** An agent producing a tool call whose output triggers
the same tool call will loop indefinitely unless an external step counter intervenes.
LangChain's `max_iterations` parameter addresses this within a single chain but
does not apply across chain boundaries in multi-agent topologies.

**Multi-agent amplification.** A coordinator spawning five sub-agents, each with
its own retry budget, multiplies potential LLM calls by the product of all budgets.
No existing framework enforces a cross-agent cost ceiling without custom application
code.

**WebSocket runaway.** Long-running WebSocket sessions that stream LLM output
accumulate cost and step counts across hundreds of receive/send cycles. Existing
ASGI frameworks provide no budget enforcement for WebSocket scopes.

VERONICA addresses these failure modes through a single architectural decision:
containment is enforced at the chain level rather than the call level. Each agent
run or HTTP request receives a dedicated `ExecutionContext` that tracks cumulative
cost, step count, and retry count across all operations, enforcing hard limits
without requiring agent-level awareness of the containment layer.

---

## 2. System Architecture

### 2.1 Overview

```
User / Framework
      |
      v
 [ExecutionContext]  <-- chain-level containment boundary
      |
      +-- [PolicyPipeline]
      |       |
      |       +-- [BudgetEnforcer]     cost_usd ceiling
      |       +-- [AgentStepGuard]     max_steps limit
      |       +-- [CircuitBreaker]     failure isolation
      |       +-- [RetryContainer]     retry budget
      |
      +-- [ExecutionGraph]             call-tree observation
      |
      +-- [CancellationToken]          cooperative shutdown
      |
      +-- [SharedTimeoutPool]          wall-clock timeout
      |
      v
   LLM API / Tools
```

The containment layer sits between the agent framework and the LLM API.
Each `wrap_llm_call()` or `wrap_tool_call()` invocation passes through the
policy pipeline before dispatching to the underlying callable.

### 2.2 RuntimePolicy Protocol

All policy primitives implement the `RuntimePolicy` structural protocol
(`src/veronica_core/runtime_policy.py`):

```python
@runtime_checkable
class RuntimePolicy(Protocol):
    def check(self, context: PolicyContext) -> PolicyDecision: ...
    def reset(self) -> None: ...

    @property
    def policy_type(self) -> str: ...
```

`PolicyContext` carries ambient information about the current operation:

```python
@dataclass
class PolicyContext:
    cost_usd: float = 0.0
    step_count: int = 0
    entity_id: str = ""
    chain_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
```

`PolicyDecision` carries the allow/deny result, policy identity, and optional
degradation actions (model downgrade, rate limiting):

```python
@dataclass
class PolicyDecision:
    allowed: bool
    policy_type: str
    reason: str = ""
    partial_result: Any = None
    degradation_action: str | None = None
    fallback_model: str | None = None
    rate_limit_ms: int = 0
```

### 2.3 PolicyPipeline

`PolicyPipeline` composes multiple `RuntimePolicy` instances with AND semantics:
the first denial terminates evaluation and returns the denying decision
(`src/veronica_core/runtime_policy.py`, class `PolicyPipeline`):

```python
pipeline = PolicyPipeline([
    BudgetEnforcer(limit_usd=10.0),
    AgentStepGuard(max_steps=25),
    CircuitBreaker(failure_threshold=5, recovery_timeout=60.0),
])
decision = pipeline.evaluate(PolicyContext(cost_usd=1.50))
```

No override mechanism exists. If any policy denies, the operation is denied.

### 2.4 BudgetEnforcer

`BudgetEnforcer` (`src/veronica_core/budget.py`) enforces a USD ceiling across
all calls. The implementation is thread-safe via `threading.Lock`:

```python
@dataclass
class BudgetEnforcer:
    limit_usd: float = 100.0

    def spend(self, amount_usd: float) -> bool:
        with self._lock:
            projected = self._spent_usd + amount_usd
            if projected > self.limit_usd:
                self._exceeded = True
                return False
            self._spent_usd = projected
            self._call_count += 1
            return True
```

Input validation rejects NaN, Inf, and negative amounts at both construction
and spend time, preventing cost poisoning.

### 2.5 AgentStepGuard

`AgentStepGuard` (`src/veronica_core/agent_guard.py`) limits the number of
agent iterations via an increment-then-check protocol:

```python
def step(self, result: Any = None) -> bool:
    with self._lock:
        self._current_step += 1        # increment first
        if result is not None:
            self._last_result = result # preserve partial result
        if self._current_step >= self.max_steps:
            return False               # then check
    return True
```

The increment-before-check ordering prevents the off-by-one error where
`max_steps=25` would allow 26 steps.

### 2.6 CircuitBreaker

`CircuitBreaker` (`src/veronica_core/circuit_breaker.py`) implements the
three-state circuit breaker pattern -- CLOSED, OPEN, HALF_OPEN -- with
atomic state transitions via `threading.Lock`. In environments with Redis,
state transitions use a Lua script for cross-process atomicity.

The state machine:
- CLOSED: consecutive failures < threshold; all requests allowed
- OPEN: failures >= threshold; all requests denied
- HALF_OPEN: recovery timeout elapsed; exactly one test request allowed

The HALF_OPEN slot constraint prevents thundering-herd on recovery by
tracking `_half_open_in_flight` and denying concurrent test requests.

`bind_to_context()` prevents a single `CircuitBreaker` instance from
being accidentally shared across independent chains, which would corrupt
failure counts.

### 2.7 RetryContainer

`RetryContainer` (`src/veronica_core/retry.py`) enforces a chain-wide retry
budget rather than per-call retries. The serialization lock (`self._lock`
held during `fn()` execution) prevents concurrent retries from racing on
the attempt counter -- this is a deliberate design decision for serial
retry semantics, not a performance issue.

```python
def execute(self, fn: Callable[..., T], *args, **kwargs) -> T:
    for attempt in range(self.max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == self.max_retries:
                raise
            delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
            delay *= 1.0 + self.jitter * (2 * random.random() - 1)
            time.sleep(delay)
```

Jitter (default ±25%) prevents thundering-herd when multiple `RetryContainer`
instances retry simultaneously after a shared service failure.

### 2.8 ExecutionContext

`ExecutionContext` (`src/veronica_core/containment/execution_context.py`)
is the chain-level container. It holds an `ExecutionConfig` specifying hard limits:

```python
@dataclass(frozen=True)
class ExecutionConfig:
    max_cost_usd: float
    max_steps: int
    max_retries_total: int
    timeout_ms: int = 0
```

All limits are validated at construction to be finite, non-negative values.
The context is used as a context manager:

```python
config = ExecutionConfig(max_cost_usd=1.0, max_steps=50,
                         max_retries_total=10, timeout_ms=30_000)
with ExecutionContext(config=config, pipeline=pipeline) as ctx:
    decision = ctx.wrap_llm_call(fn=lambda: client.chat(...))
    if decision == Decision.HALT:
        break
```

On entering the context manager, the wall-clock timeout is registered with
`SharedTimeoutPool` (a singleton daemon-thread scheduler), which fires
`CancellationToken.cancel()` at the deadline.

### 2.9 CancellationToken and SharedTimeoutPool

`CancellationToken` provides cooperative cancellation via `threading.Event`.
`SharedTimeoutPool` (`src/veronica_core/containment/timeout_pool.py`) maintains
a single daemon thread and a heap-priority queue of deadline-callback pairs.
This design amortizes the overhead of per-context timeout threads across all
active chains.

### 2.10 ExecutionGraph

`ExecutionGraph` (`src/veronica_core/containment/execution_graph.py`) tracks
the parent-child call tree for observation and diagnostics. Each LLM and tool
call creates a `Node` with timing, cost, and token counts. The graph supports
divergence heuristics based on repeated `(kind, name)` node signatures, enabling
detection of pathological loops before they exhaust the budget.

### 2.11 Middleware Integration

`VeronicaASGIMiddleware` and `VeronicaWSGIMiddleware` (`src/veronica_core/middleware.py`)
create a fresh `ExecutionContext` per HTTP request and store it in a `ContextVar`
accessible via `get_current_execution_context()`. For WebSocket scopes, each
`receive()` and `send()` call increments the step counter. Budget exceeded at
pre-flight returns HTTP 429; mid-session limit triggers `websocket.close`
code 1008 to the client.

---

## 3. Threat Model

VERONICA addresses six primary threat scenarios:

### 3.1 Prompt Loop

An LLM instruction that causes the model to output a prompt identical or similar
to its input, creating an infinite loop. Without step limits, this runs until
API quota is exhausted.

**VERONICA response:** `AgentStepGuard.step()` returns `False` after `max_steps`
iterations; `ExecutionContext.wrap_llm_call()` returns `Decision.HALT`.

### 3.2 Tool Recursion

A tool call whose execution triggers the same tool call, either directly (through
shared state) or indirectly (through model output that re-invokes the tool).

**VERONICA response:** Each tool invocation through `wrap_tool_call()` increments
the step counter. Recursion depth is bounded by `max_steps`.

### 3.3 Retry Explosion

Nested retry layers (framework-level, provider-level, application-level) multiplying
the number of LLM calls. Three layers with three retries each produce 27 calls.

**VERONICA response:** `RetryContainer` counts retries against a shared budget.
`ExecutionContext.max_retries_total` enforces a cross-layer ceiling. When
exhausted, `wrap_llm_call()` returns `Decision.HALT` without calling the LLM.

### 3.4 Multi-Agent Amplification

A coordinator spawning sub-agents, each with independent retry and step budgets,
creating unbounded compound consumption.

**VERONICA response:** Sub-agents that share an `ExecutionContext` (or a
`BudgetAllocator` backed by a shared Redis instance) accumulate cost against a
single ceiling. The `distributed.py` module provides cross-process budget tracking.

### 3.5 WebSocket Runaway

Long-running WebSocket sessions where each receive/send pair involves an LLM call,
accumulating unbounded cost over connection lifetime.

**VERONICA response:** `VeronicaASGIMiddleware` wraps WebSocket sessions in an
`ExecutionContext`. Each `receive()` and `send()` increments the step counter.
The wall-clock timeout provides a hard session duration bound.

### 3.6 Cost Poisoning

Malformed or adversarially crafted cost values (NaN, Inf, negative) that corrupt
the budget accumulator, enabling a path to bypass budget enforcement.

**VERONICA response:** `BudgetEnforcer.__post_init__()` rejects NaN and Inf
`limit_usd` values. `BudgetEnforcer.spend()` rejects NaN, Inf, and negative
`amount_usd` with `ValueError` before updating any state.

---

## 4. Safety Mechanisms

### 4.1 Budget Transactions

The `BudgetEnforcer.spend()` method uses optimistic check-then-commit semantics
under lock:

```
1. Acquire lock
2. Compute projected = spent + amount
3. If projected > limit: set exceeded flag, release lock, return False
4. Else: commit spent = projected, increment call_count, release lock, return True
```

The lock ensures that concurrent `spend()` calls see a consistent view of the
accumulated cost. The `projected` computation prevents partial commitment: either
the full amount is committed or nothing is committed.

### 4.2 Circuit Breaker State Machine

The `CircuitBreaker` state transitions are protected by `threading.Lock`:

```
CLOSED --(failures >= threshold)--> OPEN
OPEN --(elapsed >= recovery_timeout)--> HALF_OPEN
HALF_OPEN --(test succeeds)--> CLOSED
HALF_OPEN --(test fails)--> OPEN
```

The `_maybe_half_open_locked()` method checks the elapsed time against the
recovery timeout inside the lock, ensuring that only one thread observes the
OPEN-to-HALF_OPEN transition and that the `_half_open_in_flight` counter
correctly serializes test requests.

### 4.3 Timeout Propagation

`SharedTimeoutPool.schedule()` registers a deadline-callback pair. When the
deadline fires, the callback calls `CancellationToken.cancel()`, which sets a
`threading.Event`. All subsequent `wrap_llm_call()` invocations check
`CancellationToken.is_cancelled` before dispatching; cancelled contexts
return `Decision.HALT` immediately.

### 4.4 Step Limit Enforcement

`AgentStepGuard.step()` increments before checking, preventing race conditions
where two concurrent calls both read `step < max` before either increments.
The lock serializes the increment-and-check sequence.

### 4.5 Partial Result Preservation

`AgentStepGuard._last_result` preserves the most recent partial output when the
step limit is reached. `PartialResultBuffer` (`src/veronica_core/partial.py`)
provides a ContextVar-backed buffer accessible from within `wrap_llm_call()` via
`get_current_partial_buffer()`, enabling streaming partial results to be captured
even when the chain is halted mid-execution.

---

## 5. Evaluation

### 5.1 Baseline Comparison

| Feature | VERONICA | LangChain | AG2 | LangGraph |
|---------|----------|-----------|-----|-----------|
| Chain-level cost ceiling | Yes (`max_cost_usd`) | No | No | No |
| Cross-layer retry budget | Yes (`max_retries_total`) | No | No | No |
| Circuit breaker | Yes (`CircuitBreaker`) | No | No | No |
| WebSocket containment | Yes (ASGI middleware) | No | No | No |
| Step limit | Yes (`AgentStepGuard`) | Yes (`max_iterations`) | Yes | Yes |
| Multi-agent budget | Yes (Redis backend) | No | No | No |
| Cost poisoning defense | Yes (NaN/Inf validation) | No | No | No |
| Partial result preservation | Yes (`PartialResultBuffer`) | No | No | No |
| Formal safety guarantees | Yes (see Section 6) | No | No | No |

### 5.2 Cost Amplification Bound

Without containment, retry amplification in a three-layer system with
three retries per layer produces:

```
Total calls = (1 + retries_per_layer)^layers = (1 + 3)^3 = 64 calls
```

With `ExecutionContext(max_retries_total=10)`, total retries across all layers
are capped at 10, bounding total calls at 11.

### 5.3 Latency Overhead

The policy pipeline evaluates policies in order; the first denial terminates
evaluation. For a two-policy pipeline (budget + step limit), the overhead is:

- Two `threading.Lock` acquisitions and releases
- Two arithmetic comparisons
- One `PolicyDecision` allocation

In practice this is sub-microsecond on modern hardware. The `SharedTimeoutPool`
daemon thread eliminates the thread-creation overhead of per-context timeouts.

### 5.4 Failure Containment

`CircuitBreaker` with `failure_threshold=5` and `recovery_timeout=60.0` ensures
that a failing LLM endpoint is isolated within 5 consecutive failures. During
the OPEN state (60 seconds by default), the endpoint receives zero calls.
The HALF_OPEN state allows exactly one test request; success closes the circuit,
failure extends the OPEN period.

### 5.5 System Stability

The `VeronicaASGIMiddleware` returns HTTP 429 on pre-flight budget exceeded,
preventing the inner application from running. For WebSocket sessions, the
middleware sends `websocket.close` code 1008 (policy violation) to the client,
ensuring clean session termination rather than silent connection hang.

---

## 6. Formal Safety Guarantees

See `docs/theory/amplification_model.md` for the full amplification model and
`docs/security/safety_guarantees.md` for formal proofs. Summary:

**G1 (Cost Bound):** For any chain running under an `ExecutionContext` with
`max_cost_usd = C`, the total cost accumulated in `BudgetEnforcer._spent_usd`
never exceeds `C`. Proof: `spend()` checks `projected > limit` atomically under
lock before committing; no commit path exists that bypasses this check.

**G2 (Termination):** For any chain running under an `ExecutionContext` with
`max_steps = S`, the number of successful `wrap_llm_call()` and `wrap_tool_call()`
invocations never exceeds `S`. Proof: `AgentStepGuard.step()` increments before
returning `True`; the check `>= max_steps` is evaluated after every increment
under lock.

**G3 (Retry Budget):** For any chain running under an `ExecutionContext` with
`max_retries_total = R`, the total number of retry attempts across all nested
`RetryContainer` instances never exceeds `R`. Proof: `ExecutionContext` tracks
`retries_accumulated` against `max_retries_total`; each retry calls
`ctx._check_retry_budget()` which uses the same check-then-commit protocol as
`BudgetEnforcer.spend()`.

**G4 (Failure Isolation):** Once `CircuitBreaker` enters OPEN state, all
`check()` calls return `allowed=False` until `recovery_timeout` elapses.
Proof: OPEN-to-HALF_OPEN transition is gated by `_maybe_half_open_locked()`
inside the lock; no code path transitions from OPEN to CLOSED directly.

---

## 7. Implementation

VERONICA is implemented in Python 3.10+. Core dependencies:

- `threading` (standard library): lock-based concurrency
- `contextvars` (standard library): per-request ContextVar storage
- `redis` (optional): distributed budget backend and atomic Lua scripts
- `opentelemetry-api` (optional): execution graph observer integration

The library is structured to be framework-agnostic: no hard dependency on
LangChain, AG2, LangGraph, or any specific LLM client library.

Source: `src/veronica_core/`
Tests: `tests/` (2232 tests, 92% coverage as of v1.8.1)
Distribution: PyPI (`veronica-core`)

---

## 8. Related Work

**LangChain** provides `max_iterations` for chain step limits and per-call retry
logic but does not enforce cross-chain cost ceilings or multi-agent retry budgets.

**AG2 (AutoGen)** supports nested agent conversations but relies on application
code to implement spending limits. No circuit breaker or WebSocket containment
is provided.

**LangGraph** provides graph-based agent orchestration with checkpoint support
but no chain-level budget enforcement primitive.

**Guardrails AI** focuses on output validation (schema enforcement, content
filtering) rather than resource consumption control.

**NeMo Guardrails** provides dialog flow control and safety filtering but not
retry or cost containment.

VERONICA is orthogonal to all of the above: it operates at the resource
consumption layer rather than the content or dialog layer, and can be composed
with any of them.

---

## 9. Conclusion

VERONICA provides a runtime containment layer that addresses the compositional
resource amplification failures inherent in LLM agent systems. By enforcing
hard limits at the chain level rather than the call level, VERONICA provides
guarantees that per-call policies cannot: cost bounds that hold across nested
retry layers, step limits that apply across tool recursion, and circuit breakers
that isolate failures across multi-agent topologies.

The four core primitives -- `BudgetEnforcer`, `AgentStepGuard`, `CircuitBreaker`,
and `RetryContainer` -- are composable via `PolicyPipeline` and require no
modification to existing agent logic. The ASGI/WSGI middleware integrates
containment into the request lifecycle with zero configuration for HTTP endpoints,
extending naturally to WebSocket sessions.

---

*Source code: https://github.com/amabito/veronica-core*
*Version: 2.0.0*
