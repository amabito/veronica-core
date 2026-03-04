# Amplification Model for LLM Agent Systems

This document defines a formal model for resource amplification in LLM agent systems
and proves that VERONICA containment bounds amplification to configurable limits.

---

## 1. Definitions

Let an **LLM agent system** be a directed computation graph where nodes are LLM calls
or tool invocations, and edges represent data dependencies or sequential execution.

**Definition 1 (Call Node).** A call node `v` is a single LLM API call or tool
invocation. It has:
- `cost(v)`: USD cost of the call (>= 0)
- `tokens_in(v)`: prompt tokens consumed
- `tokens_out(v)`: completion tokens produced
- `retries(v)`: number of retry attempts (>= 0)

**Definition 2 (Agent Chain).** An agent chain `C` is a sequence of call nodes
`v_1, v_2, ..., v_n` executed within a single agent run. The chain has:
- `total_cost(C) = sum(cost(v_i) for i in 1..n)`
- `total_steps(C) = n`
- `total_retries(C) = sum(retries(v_i) for i in 1..n)`

**Definition 3 (Amplification Factor).** The amplification factor `A(C)` of a chain
is the ratio of actual calls made to the minimum calls required for the task:
```
A(C) = total_steps(C) / expected_steps
```
In pathological cases (retry explosion, recursive loops), `A(C)` is unbounded.

---

## 2. Retry Amplification

### 2.1 Single-Layer Retry

A single call with `r` retries on failure generates at most `1 + r` LLM calls.

### 2.2 Multi-Layer Retry Amplification

Consider a nested call structure with `L` layers, where each layer independently
retries on failure with `r_i` retries at layer `i`. The worst-case total calls is:

```
Total_calls = product((1 + r_i) for i in 1..L)
```

**Example: Three-layer system, three retries per layer**

```
Layer 1: r_1 = 3  ->  at most 4 calls
Layer 2: r_2 = 3  ->  each Layer 1 call triggers at most 4 calls
Layer 3: r_3 = 3  ->  each Layer 2 call triggers at most 4 calls

Total_calls = (1 + 3)^3 = 4^3 = 64 LLM calls
```

From a single user action, 64 LLM calls are generated in the worst case.
At $0.01/call, this is $0.64 per user action instead of $0.01.

### 2.3 VERONICA Containment Bound (Retry)

`ExecutionContext` enforces `max_retries_total = R`. The bound is:

```
Total_retries <= R
Total_calls <= 1 + R
```

**Example: Same three-layer system with R = 10**

```
Total_retries <= 10
Total_calls <= 11

Bound ratio: 11 / 64 = 0.172  (83% reduction in worst case)
```

For any finite `R`, the retry amplification is bounded regardless of nesting depth.

---

## 3. Agent Amplification

### 3.1 Multi-Agent Topology

Consider a coordinator agent that spawns `K` sub-agents, each of which may spawn
further agents. In a tree of depth `D` where each agent spawns `K` sub-agents:

```
Total_agents = sum(K^d for d in 0..D) = (K^(D+1) - 1) / (K - 1)
```

If each agent independently makes `S` calls with `r` retries:

```
Total_calls = Total_agents * S * (1 + r)
```

**Example: K=5 sub-agents, D=2 depth, S=10 steps, r=3 retries**

```
Total_agents = 1 + 5 + 25 = 31 agents
Total_calls = 31 * 10 * 4 = 1,240 LLM calls
```

At $0.01/call, this is $12.40 from a single coordinator invocation.

### 3.2 VERONICA Containment Bound (Cost)

`ExecutionContext` with a shared `BudgetBackend` (e.g., `RedisBudgetBackend`)
enforces `max_cost_usd = C` across all agents in the tree:

```
total_cost(all_agents) <= C
```

Independent of the number of agents, depth, or individual step counts.

**Example: Same topology with C = $1.00**

```
Actual cost <= $1.00  (regardless of agent count or retry depth)
Uncontained worst case: $12.40
Savings: at least $11.40
```

### 3.3 Combined Bound

For a multi-agent system with shared `ExecutionContext`:

```
total_cost <= C             (BudgetEnforcer)
total_steps <= S            (AgentStepGuard)
total_retries <= R          (ExecutionContext.max_retries_total)
wall_clock_time <= T_ms     (SharedTimeoutPool)
```

All four bounds hold simultaneously and independently. A violation of any one
bound results in `Decision.HALT` for all subsequent calls.

---

## 4. Circuit Breaker Amplification

### 4.1 Failure Storm

When an LLM endpoint degrades, every call fails and is retried, amplifying
load on an already-stressed system. Without circuit breaking, a system under
load makes `total_calls * (1 + retries)` calls to a failing endpoint.

### 4.2 VERONICA Circuit Breaker Bound

`CircuitBreaker` with `failure_threshold = F` and `recovery_timeout = T`:

- After `F` consecutive failures, the circuit opens
- During OPEN state (duration >= `T`), zero calls are made to the endpoint
- Exactly one test call is made in HALF_OPEN state

The maximum calls to a failing endpoint before isolation:
```
max_calls_before_open = F + (retries per call)
```

**Example: F=5, r=3**
```
max_calls_before_open = 5 + 5*3 = 20 calls to failing endpoint
Then: 0 calls for >= T seconds
```

---

## 5. Compositional Safety

VERONICA's four bounds are independent. The `PolicyPipeline` evaluates them
with AND semantics: the first violated bound terminates the call:

```python
# From src/veronica_core/runtime_policy.py, class PolicyPipeline
def evaluate(self, context: PolicyContext) -> PolicyDecision:
    for policy in self._policies:
        decision = policy.check(context)
        if not decision.allowed:
            return decision  # First denial wins
    return PolicyDecision(allowed=True, ...)
```

**Theorem (Compositional Bound).** For a `PolicyPipeline` containing
`BudgetEnforcer(limit_usd=C)`, `AgentStepGuard(max_steps=S)`,
and `RetryContainer(max_retries=R)`, the following hold simultaneously:
- `total_cost <= C`
- `total_steps <= S`
- `total_retries <= R`

*Proof sketch:* Each bound is enforced by an independent check-before-commit
protocol under its own lock. Violation of any bound returns `False`/`HALT`
before the underlying callable is invoked. No code path bypasses any check.
QED.

---

## 6. Worked Examples

### Example 1: Web API Agent

An agent that scrapes 10 URLs, summarizes each with an LLM call, then
writes a report. Estimated cost: $0.10.

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig

config = ExecutionConfig(
    max_cost_usd=0.50,     # 5x cost buffer
    max_steps=50,          # 10 URLs * 5 steps each
    max_retries_total=20,  # 2 retries per URL on average
    timeout_ms=120_000,    # 2 minutes
)
```

If the agent enters a loop (e.g., summarizing its own summaries):
- Step limit: halts after 50 steps (~5x expected)
- Cost limit: halts after $0.50 spent (~5x expected)
- Timeout: halts after 2 minutes regardless

### Example 2: Multi-Agent Research System

A coordinator with 5 specialist sub-agents, each making up to 20 calls.
Without containment, worst case: 5 * 20 * (1 + 3) = 400 calls = $4.00.

```python
# Shared Redis backend for cross-agent accounting
config = ExecutionConfig(
    max_cost_usd=1.00,     # Hard ceiling across all agents
    max_steps=200,         # 5 agents * 40 steps each
    max_retries_total=50,  # Shared retry pool
    redis_url="redis://localhost:6379/0",
)
```

Total cost across all 5 agents is bounded at $1.00 regardless of which
agent makes the calls.

### Example 3: Retry Explosion Prevention

Three-layer pipeline (validator -> formatter -> writer), each with r=3 retries.
Worst case without containment: (1+3)^3 = 64 calls.

```python
config = ExecutionConfig(
    max_cost_usd=0.20,
    max_steps=20,
    max_retries_total=9,   # 3 per layer * 3 layers = 9 total
)
```

With `max_retries_total=9`, total calls are bounded at 10 regardless of
how retries distribute across layers.

---

## 7. Notation Summary

| Symbol | Meaning |
|--------|---------|
| `C` | `max_cost_usd` (BudgetEnforcer limit) |
| `S` | `max_steps` (AgentStepGuard limit) |
| `R` | `max_retries_total` (ExecutionContext limit) |
| `T` | `timeout_ms` / 1000 (wall-clock seconds) |
| `F` | `failure_threshold` (CircuitBreaker) |
| `L` | Number of retry layers |
| `r_i` | Retries at layer `i` |
| `K` | Sub-agents per coordinator |
| `D` | Agent tree depth |
| `A(C)` | Amplification factor of chain `C` |
