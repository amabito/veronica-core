# Amplification Factor

## 1. Purpose

Runtime Containment limits what an agent chain is *allowed* to do.
Amplification metrics measure what a chain *actually did* — how many LLM calls,
tool calls, and retries it generated relative to the single root invocation that
started it.

A chain that makes 3 LLM calls from one root prompt has an LLM amplification
factor of 3. A chain that makes 50 LLM calls from one root prompt is amplifying
that single invocation fifty-fold in cost, latency, and downstream risk.

Tracking amplification lets you:

- Detect runaway agent loops before they exceed cost ceilings.
- Tune step limits and retry budgets to match observed usage patterns.
- Compare chains across experiments to understand structural complexity.
- Audit whether containment policies fired at the right amplification level.

---

## 2. Definitions

### Call Amplification (`llm_calls_per_root`)

```
llm_calls_per_root = total_llm_calls / root_count
```

The total number of LLM calls (in any terminal status: success, fail, or halt)
divided by the number of root nodes in the graph. Because every
`ExecutionGraph` has exactly one root node (`root_count = 1`), this value
equals `total_llm_calls` for all current chains. The division is retained
so the field remains semantically correct if multi-root aggregation is
introduced later without a breaking API change.

**Example values:**

| Scenario | llm_calls_per_root |
|---|---|
| Single classification call | 1.0 |
| Classification + generation + refinement | 3.0 |
| Agent loop, step_limit=10, all succeed | 10.0 |
| Agent loop, step_limit=50, halted at step 7 | 7.0 |

### Tool Amplification (`tool_calls_per_root`)

```
tool_calls_per_root = total_tool_calls / root_count
```

The total number of tool calls (same counting rules as LLM calls) divided
by the root count. Tool calls include every node with `kind="tool"` that
reached a terminal status other than `created`.

A chain with one LLM call that in turn makes five web-search tool calls has
`tool_calls_per_root = 5.0`.

### Retry Amplification (`retries_per_root`)

```
retries_per_root = total_retries / root_count
```

The sum of `retries_used` across all terminal nodes, divided by the root
count. Each retry represents an additional attempt beyond the first, so a
node that succeeded on its third try contributes `retries_used = 2`.

High `retries_per_root` values indicate that the chain encountered
transient failures (rate limits, timeouts) and may signal infrastructure
pressure or overly aggressive retry budgets.

---

## 3. Why Chain-Level Metrics

### Single root per ExecutionGraph

`ExecutionGraph` enforces exactly one root node per instance. The root is
created by `create_root()` and has `kind="system"` and `parent_id=None`. All
LLM and tool nodes are descendants of this root.

Because there is always exactly one root, `llm_calls_per_root` equals
`total_llm_calls` in all current chains. The per-root naming is intentional:

1. It communicates the *amplification semantics* — how much work was generated
   from one entry point — rather than a raw total.
2. It prepares the API for future multi-root scenarios (e.g., a batch context
   that aggregates multiple independent sub-chains) without renaming the field.

### What is and is not counted

| Node status | Counted toward llm/tool totals? | Reason |
|---|---|---|
| `success` | Yes | Completed call, cost was incurred. |
| `fail` | Yes | Attempted call; error occurred after dispatch. |
| `halt` | Yes | Attempted call; containment policy fired. |
| `running` | Yes | In-flight call; aggregates updated on terminal transition. |
| `created` | No | Never dispatched; no amplification occurred. |

The root `system` node is never counted toward `total_llm_calls` or
`total_tool_calls` regardless of its kind or status. It is the structural
entry point, not an LLM or tool invocation.

### Why halt nodes count

A halted node represents a call that was dispatched (or was about to be)
when the containment policy fired. The policy stopped the call *because*
amplification was occurring — counting that node reflects the actual
amplification pressure that triggered containment. Excluding halted nodes
would under-count amplification and make it harder to understand why a
policy activated at a particular threshold.

---

## 4. Examples

### Normal chain (classification + generation + refinement)

```
root (system) "agent_run"
  +-- n000001 (llm) "classify"       [success]
  +-- n000002 (llm) "generate"       [success]
  +-- n000003 (llm) "refine"         [success]
```

```
aggregates:
  total_llm_calls:    3
  total_tool_calls:   0
  total_retries:      0
  llm_calls_per_root: 3.0
  tool_calls_per_root: 0.0
  retries_per_root:   0.0
```

### Agent loop (step_limit=50)

An agent that calls an LLM at each step and invokes one tool per step,
running to completion:

```
root (system) "agent_run"
  +-- n000001 (llm)  "step_1"    [success]
  |   +-- n000002 (tool) "search" [success]
  +-- n000003 (llm)  "step_2"    [success]
  |   +-- n000004 (tool) "search" [success]
  ... (50 steps total)
```

```
aggregates:
  total_llm_calls:    50
  total_tool_calls:   50
  llm_calls_per_root: 50.0
  tool_calls_per_root: 50.0
```

### Halted chain (cost ceiling hit at step 8)

The same agent loop, but a cost ceiling fires at step 8. Steps 1-7 succeed;
step 8's LLM call is halted:

```
root (system) "agent_run"
  +-- n000001 (llm) "step_1" [success]
  ...
  +-- n000015 (llm) "step_8" [halt, stop_reason="cost_ceiling_exceeded"]
```

```
aggregates:
  total_llm_calls:    8     # steps 1-7 (success) + step 8 (halt)
  total_tool_calls:   7     # only steps 1-7 dispatched their tool calls
  llm_calls_per_root: 8.0
  tool_calls_per_root: 7.0
```

The halted LLM call at step 8 is counted because containment fired *due to*
that call attempting to execute. Excluding it would make the amplification
factor appear lower than the pressure that triggered the halt.

---

## 5. Reading Amplification Fields from a Snapshot

`ExecutionGraph.snapshot()` returns a dict. The amplification fields live
under the `"aggregates"` key:

```python
graph = ExecutionGraph(chain_id="chain-abc-123")

root_id = graph.create_root(name="agent_run")
step_id = graph.begin_node(parent_id=root_id, kind="llm", name="step_1")
graph.mark_running(step_id)
graph.mark_success(step_id, cost_usd=0.002, tokens_in=100, tokens_out=50)

tool_id = graph.begin_node(parent_id=step_id, kind="tool", name="web_search")
graph.mark_running(tool_id)
graph.mark_success(tool_id, cost_usd=0.0)

snap = graph.snapshot()
agg = snap["aggregates"]

print(agg["llm_calls_per_root"])   # 1.0
print(agg["tool_calls_per_root"])  # 1.0
print(agg["retries_per_root"])     # 0.0
```

All three fields are `float` values. They are always present in the snapshot
dict even when no LLM or tool calls have been made yet (they default to `0.0`).

---

## 6. Related Documentation

- [ExecutionGraph reference](execution-graph.md) — full API, node lifecycle, invariants
- [ExecutionContext reference](execution-context.md) — chain-level budget enforcement
- [Adaptive Control](adaptive-control.md) — how amplification feeds back into policy tuning
- [Metrics](METRICS.md) — observability exports including amplification counters
