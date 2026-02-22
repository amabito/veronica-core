# Multi-agent Context Linking (P2-2)

## Problem

When Agent A spawns Agent B with its own `ExecutionContext`, costs incurred in B do **not**
count against A's budget by default. This means A could have `max_cost_usd=1.0` but B
could spend `$5.00` without A ever knowing.

```
Agent A (budget: $1.00)
  └── Agent B (budget: $5.00)  ← independent context, no propagation
        └── LLM call: $3.00   ← A never sees this
```

## Solution

Parent-child `ExecutionContext` linking. A child context holds a reference to its parent,
and every cost the child accumulates is propagated up the parent chain automatically.

```
Agent A (budget: $1.00)
  └── Agent B (child of A, budget: $0.50)
        └── LLM call: $0.30
              → B accumulates $0.30
              → B propagates $0.30 to A
              → A now shows $0.30 accumulated
```

If the propagated total reaches A's ceiling, A is marked `aborted=True`, which halts
all subsequent `wrap_llm_call` / `wrap_tool_call` calls in A.

---

## API

### `ExecutionContext.__init__` — `parent` parameter

Pass a parent context to link a child to it:

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

parent_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)
child_cfg  = ExecutionConfig(max_cost_usd=0.5, max_steps=20, max_retries_total=5)

with ExecutionContext(parent_cfg) as parent_ctx:
    with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
        child_ctx.wrap_llm_call(agent_b_fn)
        # costs from wrap_llm_call propagate to parent_ctx automatically
```

### `ExecutionContext.spawn_child()` — convenience factory

```python
with ExecutionContext(parent_cfg) as parent_ctx:
    # Child gets explicit ceiling; other limits inherited from parent
    with parent_ctx.spawn_child(max_cost_usd=0.5) as child_ctx:
        child_ctx.wrap_llm_call(agent_b_fn)

    # Child inherits parent's remaining budget automatically
    with parent_ctx.spawn_child() as child_ctx:
        child_ctx.wrap_llm_call(agent_b_fn)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_cost_usd` | `float \| None` | remaining parent budget | Child cost ceiling |
| `max_steps` | `int \| None` | parent's `max_steps` | Child step limit |
| `max_retries_total` | `int \| None` | parent's `max_retries_total` | Child retry budget |
| `timeout_ms` | `int` | `0` (no timeout) | Child wall-clock timeout |
| `pipeline` | `ShieldPipeline \| None` | `None` | Per-call shield hooks for child |

### `ContextSnapshot.parent_chain_id`

Every snapshot from a child context includes the parent's `chain_id`:

```python
snap = child_ctx.get_snapshot()
print(snap.parent_chain_id)  # UUID of parent chain, or None if no parent
```

---

## Cost Propagation Rules

1. **Direct wrap calls**: Any cost accrued via `wrap_llm_call` / `wrap_tool_call` inside
   a child context is automatically forwarded to the parent after successful completion.

2. **Manual propagation**: Call `child_ctx._propagate_child_cost(amount)` to forward
   cost without going through `wrap_*` (useful for testing or external billing hooks).

3. **Budget ceiling**: If propagated cost pushes the parent's total
   `>= parent_cfg.max_cost_usd`, the parent is marked `aborted=True` and all subsequent
   `wrap_*` calls return `Decision.HALT` without invoking the callable.

4. **Chain continues up**: Propagation is recursive. If A → B → C, a cost in C
   propagates to B, then B propagates to A.

---

## Three-level Agent Chain Example

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

# Orchestrator: $1.00 total budget
orch_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=100, max_retries_total=20)

with ExecutionContext(orch_cfg) as orch:

    # Sub-agent A: up to $0.60 (60% of orchestrator)
    with orch.spawn_child(max_cost_usd=0.60) as agent_a:

        # Sub-agent B: up to $0.30 (50% of agent A)
        with agent_a.spawn_child(max_cost_usd=0.30) as agent_b:
            agent_b.wrap_llm_call(fn=expensive_llm_call)
            # Suppose expensive_llm_call costs $0.20:
            #   agent_b._cost_usd_accumulated → $0.20
            #   agent_a._cost_usd_accumulated → $0.20 (propagated)
            #   orch._cost_usd_accumulated    → $0.20 (propagated)

    final = orch.get_snapshot()
    print(f"Total chain spend: ${final.cost_usd_accumulated:.4f}")
    print(f"Aborted: {final.aborted}")
```

---

## Backward Compatibility

All existing code that constructs `ExecutionContext(config, pipeline=..., metadata=..., circuit_breaker=...)`
continues to work unchanged. The `parent` parameter defaults to `None`, preserving all
prior behavior. The new `parent_chain_id` field in `ContextSnapshot` defaults to `None`
for standalone contexts.

---

## Thread Safety

`_propagate_child_cost` acquires `self._lock` before modifying `_cost_usd_accumulated`
and releases it before recursing to the grandparent. This prevents deadlocks in
multi-threaded agent trees where costs may propagate concurrently from multiple children.
