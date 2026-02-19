# VERONICA as an Execution OS

---

## The analogy

An operating system does not execute application logic.
It manages the environment in which application logic executes.

It enforces process isolation.
It allocates and reclaims resources.
It bounds execution time through scheduling.
It provides structured mechanisms for process termination.
It separates failure in one process from failure in adjacent processes.

An LLM system has none of these by default.

An agent is not a process with enforced resource limits.
A chain of LLM calls is not scheduled with bounded execution time.
A retry loop does not terminate because a resource quota was exhausted.
A failing component does not stop execution in adjacent components.

VERONICA provides these properties.
That is what "Execution OS for LLM Systems" means.

---

## What an Execution OS manages

### 1. Execution scope

Every agent run, every request chain, is an execution scope.

In VERONICA, this is `ExecutionContext`:
a lifespan-scoped container that owns one chain of LLM and tool calls.
It enforces hard limits on cost, steps, retries, and wall-clock time.
Every call that executes inside it is bounded by those limits.
No call escapes the context's containment conditions.

```python
with ExecutionContext(config=ExecutionConfig(
    max_cost_usd=1.00,
    max_steps=50,
    max_retries_total=10,
    timeout_ms=30_000,
)) as ctx:
    ctx.wrap_llm_call(fn=my_agent_step)
```

### 2. Execution graph

An OS maintains a process table: a structured record of what is running,
what resources it holds, and what state it is in.

VERONICA maintains an execution graph.
Every LLM call and tool call is a typed node: `kind` (llm|tool|system),
lifecycle state (`created` → `running` → `success|fail|halt`),
cost, token counts, and stop reason.

The graph is not a trace for an observability backend.
It is a structural model used by the containment layer itself
to make enforcement decisions and expose auditable evidence.

```python
snap = ctx.get_graph_snapshot()
# snap["aggregates"]["llm_calls_per_root"]  -> amplification factor
# snap["nodes"]["n000003"]["stop_reason"]   -> why a call was halted
```

### 3. Resource accounting

An OS charges resource consumption to processes.
Memory pages, CPU cycles, file descriptors — bounded per process.

VERONICA charges cost and call volume to execution chains.
`total_cost_usd` accumulates across every call in the chain.
`total_llm_calls` tracks amplification: how many model calls one root request generated.
These are not metrics for a dashboard. They are enforcement inputs.
When accumulated cost crosses `max_cost_usd`, the next call is halted before dispatch.

### 4. Failure isolation

An OS provides process isolation: a crash in one process does not corrupt another.

VERONICA provides failure domain isolation between execution chains.
A `CircuitBreaker` in OPEN state halts calls in the affected chain
without affecting chains that share the same pipeline configuration.
`SafetyEvent` records provide structured evidence of what halted a call and why.
No raw prompt content is stored. The evidence is auditable without being sensitive.

### 5. Divergence detection

An OS can detect processes that are consuming resources without making progress
(spin locks, infinite loops, livelock) and take corrective action.

VERONICA detects agent divergence using a lightweight heuristic:
a ring buffer of the last K=8 call signatures `(kind, name)`.
When the same signature repeats consecutively beyond threshold
(tool: 3 times, llm: 5 times), a `divergence_suspected` SafetyEvent is emitted.
Execution continues; the event is a signal, not a termination.
Deduplication prevents event spam.

---

## What an Execution OS does not manage

An OS does not manage what applications do inside their allocated resources.
It enforces the boundary, not the behavior inside it.

VERONICA does not:
- Inspect or evaluate the content of LLM calls
- Modify prompts or completions
- Route calls to different models
- Assess output quality or factual accuracy
- Provide conversation memory or context management

Those are application-layer concerns.
VERONICA manages the execution environment, not the execution logic.

---

## Relationship to orchestration

Orchestration decides what to call and in what order.
An Execution OS enforces what the orchestration layer is permitted to do.

```
Application
     |
     v
Orchestrator        <- decides: what to call, in what order
     |
     v
Execution OS        <- enforces: cost, steps, retries, timeout, failure isolation
(VERONICA)
     |
     v
LLM Provider
```

These are different responsibilities. They can coexist in the same process.
VERONICA does not replace an orchestrator. It wraps every call the orchestrator dispatches.

---

## The five enforcement properties

| OS concept | VERONICA implementation |
|------------|------------------------|
| Process resource quota | `max_cost_usd`, `max_steps` per `ExecutionContext` |
| Execution time limit | `timeout_ms` enforced at `ExecutionContext` level |
| Retry budget | `max_retries_total` per chain |
| Circuit breaker / isolation | `CircuitBreaker` with CLOSED/OPEN/HALF_OPEN states |
| Divergence / livelock detection | Ring-buffer signature heuristic, warn-severity event |

---

## Why "OS" and not "middleware"

Middleware typically refers to a processing layer between two systems:
a component that intercepts requests, applies transformations or checks, and passes them on.

An OS is not middleware. It defines the execution environment.
It does not pass calls through; it owns the resource model within which calls execute.

VERONICA is closer to an OS than to middleware because:
- It owns the execution scope (`ExecutionContext` is not a filter, it is a scope)
- It maintains internal state about every call that has executed (the execution graph)
- It enforces limits unconditionally, not as a pluggable policy that can be bypassed
- Its containment decisions are structural, not advisory

The distinction is architectural, not cosmetic.

---

*VERONICA v0.9.0 — Runtime Containment Layer*
