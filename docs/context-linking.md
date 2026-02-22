# Multi-agent Context Linking

## Why context linking?

When Agent A spawns Agent B with its own `ExecutionContext`, costs in B don't count against
A's budget by default. A could have `max_cost_usd=1.0` while B spends `$5.00` and A never
knows.

```
Agent A (budget: $1.00)
  └── Agent B (budget: $5.00)  ← independent context, no propagation
        └── LLM call: $3.00   ← A never sees this
```

## How it works

A child context holds a reference to its parent, and costs automatically propagate up the
chain as they accumulate.

```
Agent A (budget: $1.00)
  └── Agent B (child of A, budget: $0.50)
        └── LLM call: $0.30
              → B: $0.30 accumulated
              → propagates to A: $0.30
```

If the propagated total hits A's ceiling, A marks itself `aborted=True` and all subsequent
`wrap_llm_call` / `wrap_tool_call` calls in A return `Decision.HALT`.

---

## API

### `ExecutionContext.__init__` — `parent` parameter

Pass a parent context to link a child:

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

parent_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)
child_cfg  = ExecutionConfig(max_cost_usd=0.5, max_steps=20, max_retries_total=5)

with ExecutionContext(parent_cfg) as parent_ctx:
    with ExecutionContext(child_cfg, parent=parent_ctx) as child_ctx:
        child_ctx.wrap_llm_call(agent_b_fn)
        # costs propagate to parent_ctx automatically
```

### `ExecutionContext.spawn_child()` — convenience factory

```python
with ExecutionContext(parent_cfg) as parent_ctx:
    # explicit ceiling
    with parent_ctx.spawn_child(max_cost_usd=0.5) as child_ctx:
        child_ctx.wrap_llm_call(agent_b_fn)

    # inherits parent's remaining budget at spawn time
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

Snapshots from child contexts include the parent's `chain_id`:

```python
snap = child_ctx.get_snapshot()
print(snap.parent_chain_id)  # UUID of parent chain, or None for root contexts
```

---

## Cost propagation

Any cost accrued via `wrap_llm_call` / `wrap_tool_call` is forwarded to the parent after
the call completes. If the propagated total pushes the parent's accumulated cost to or past
its ceiling, the parent aborts and blocks all further `wrap_*` calls.

Propagation is recursive: in an A → B → C chain, a cost in C propagates to B, then B
propagates to A.

---

## Three-level chain example

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

orch_cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=100, max_retries_total=20)

with ExecutionContext(orch_cfg) as orch:

    with orch.spawn_child(max_cost_usd=0.60) as agent_a:

        with agent_a.spawn_child(max_cost_usd=0.30) as agent_b:
            agent_b.wrap_llm_call(fn=expensive_llm_call)
            # if expensive_llm_call costs $0.20:
            #   agent_b._cost_usd_accumulated → $0.20
            #   agent_a._cost_usd_accumulated → $0.20 (propagated)
            #   orch._cost_usd_accumulated    → $0.20 (propagated)

    final = orch.get_snapshot()
    print(f"Total chain spend: ${final.cost_usd_accumulated:.4f}")
    print(f"Aborted: {final.aborted}")
```

---

## Backward compatibility

All existing `ExecutionContext(config, pipeline=..., metadata=..., circuit_breaker=...)` call
sites continue to work. The `parent` parameter defaults to `None`. The new
`parent_chain_id` field in `ContextSnapshot` is `None` for standalone contexts.

---

## Thread safety

Cost propagation is thread-safe. Multiple child agents propagating costs concurrently will
not corrupt parent state.
