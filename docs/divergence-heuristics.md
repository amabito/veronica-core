# Divergence Heuristics — Repeated Pattern Detection

## 1. Purpose

Runtime Containment enforces hard limits on cost, step count, retries, and
time.  These limits catch runaway chains that keep growing, but they fire only
*after* a threshold is crossed and after real resources have been consumed.

Divergence detection aims to fire *earlier*: it looks at the *pattern* of
calls rather than aggregate totals and raises a warning when the same operation
appears to be stuck in a loop.  The signal is advisory — the default action is
`warn`, not `halt` — but it gives observers (dashboards, alerting systems,
orchestrators) a chance to intervene before the cost or step limit is reached.

A diverging chain typically looks like:

```
tool/call_api -> tool/call_api -> tool/call_api -> ...
```

or

```
llm/generate -> llm/generate -> llm/generate -> llm/generate -> llm/generate -> ...
```

Both patterns indicate that the agent is making no forward progress: it is
calling the same operation repeatedly without a different call interleaved.

---

## 2. Heuristic: Repeated Pattern Detection

### 2.1 Signature

Each node is identified by its **signature**: a `(kind, name)` pair where
`kind` is one of `"tool"`, `"llm"`, or `"system"`, and `name` is the
operation label supplied to `begin_node`.

```python
NodeSignature = tuple[str, str]   # (kind, name)

# Examples
("tool", "call_api")
("llm",  "generate")
("system", "agent_run")
```

### 2.2 Ring Buffer

`ExecutionGraph` maintains a **ring buffer** of the last `K = 8` signatures
seen, in `mark_running` order.  Only the K most recent entries are kept; older
entries are discarded.  Memory cost is O(K) = O(1).

The buffer is updated unconditionally on every `mark_running` call, even if the
node's status does not change (idempotent call).

### 2.3 Consecutive-Repeat Check

After appending the new signature, the heuristic counts how many entries at
the **tail** of the buffer equal the new signature — i.e., how many consecutive
trailing entries match.  This is strictly a suffix count, not a total-frequency
count.

```
window = [A, B, A, C, C, C]   # C just appended
consecutive(C) = 3             # tail is C, C, C -> 3

window = [A, B, C, A, B, A]   # A just appended
consecutive(A) = 1             # tail is A, B, A -> only the last entry matches
```

This means `tool/a, tool/b, tool/a, tool/b` does **not** trigger the
heuristic even though `tool/a` and `tool/b` each appear twice in the window,
because neither has two or more *consecutive* trailing repeats.

### 2.4 Thresholds

| Kind | Default Threshold |
|------|-------------------|
| `tool` | 3 |
| `llm` | 5 |
| `system` | 999 (effectively disabled) |

`system` nodes represent chain-level housekeeping (e.g., the root node) and
are never expected to trigger divergence.  The threshold of 999 exceeds the
ring buffer size of 8 so it is structurally impossible to fire.

Unknown kinds default to 999.

### 2.5 Emission

When `consecutive_count >= threshold`, a `divergence_suspected` event is
staged:

```json
{
  "event_type": "divergence_suspected",
  "severity": "warn",
  "signature": ["tool", "call_api"],
  "repeat_count": 3,
  "chain_id": "chain-abc-123"
}
```

`severity` is `"warn"`.  The default action is **observation only** — no halt
is triggered automatically.  `ExecutionContext` (or the caller) decides whether
to escalate.

---

## 3. Deduplication Rule

Once a `divergence_suspected` event has been emitted for a given `(kind, name)`
signature within a chain, it is **not emitted again** for the same signature in
that chain.

The set of emitted signatures (`_emitted_divergences`) is per-`ExecutionGraph`
instance and is cleared only when a new graph is created (i.e., at chain
start).

**Rationale:** Without deduplication, a chain that calls `tool/call_api` 100
times would emit 98 events (calls 3 through 100).  This creates event spam and
can overwhelm downstream consumers.  One event per (chain, signature) pair is
sufficient to trigger a human review or automated policy check.

---

## 4. Update Point

Divergence detection runs inside `mark_running`, not `mark_success`.

**Rationale:**

- `mark_running` is called just before the underlying operation is dispatched.
  At this point the *intent* to call is already established.  Detecting the
  pattern here catches divergence as early as possible.
- If a node is halted or fails, `mark_running` may still have been called
  (e.g., the budget check fires after `mark_running`).  Counting these calls
  is correct: a halt triggered by the 3rd consecutive `tool/call_api` *is* a
  diverging loop, even though the third call never completed.
- Updating on `mark_success` instead would delay detection by one full round
  trip and would miss halted calls entirely.

The event is staged in `self._pending_divergence_events` inside the lock.
`ExecutionContext` must call `drain_divergence_events()` immediately after
`mark_running` returns to retrieve and forward the events.

---

## 5. Limitations and Future Heuristics

The current heuristic detects only **exact consecutive repeats** of a single
signature.  It does not detect:

- **Cycling patterns**: `A, B, A, B, A, B` with period 2.  The ring buffer
  contains all entries, but the suffix-count algorithm sees only one trailing
  `B` (or `A`), so it never fires.
- **Near-duplicates**: two different tool names that effectively do the same
  thing (e.g., `call_api_v1` and `call_api_v2`).
- **Cross-chain divergence**: loops that span multiple chained calls each
  below the threshold.

Future heuristics could address these gaps.  Candidate approaches:

- **Sliding-window n-gram frequency**: count how often any k-gram repeats in
  the window, regardless of position.  More expensive (O(K^2)) but catches
  cycling.
- **Entropy-based**: compute Shannon entropy over the window; a very low
  entropy indicates a monotonous call pattern.
- **Semantic clustering**: map operation names to canonical groups
  (e.g., all `search_*` tools into one group) before hashing the signature.

The cycling, near-duplicate, and cross-chain patterns are not implemented.
The current pattern heuristics are intentionally minimal: they cover the most
common stuck-loop case with O(K) cost and zero external dependencies.

Two time-based heuristics were added in v0.11 and are documented in
Section 6 below.

---

## 6. Time-Based Rate Heuristics (v0.11)

### 6.1 Motivation

The consecutive-repeat heuristic catches stuck loops in terms of call *pattern*
but is blind to spend velocity. An agent that calls different operations on each
step can accumulate cost at an unsafe rate while never triggering the pattern
detector.

Two additional heuristics check *rate* rather than pattern:

- `COST_RATE_EXCEEDED` — cost per second is too high.
- `TOKEN_VELOCITY_EXCEEDED` — output tokens per second is too high.

Both are advisory (`severity = "warn"`); neither halts the chain automatically.

### 6.2 COST_RATE_EXCEEDED

**Trigger:** cumulative chain cost / elapsed seconds since `ExecutionGraph`
creation exceeds `cost_rate_threshold_usd_per_sec`.

**Check point:** `mark_success` — cost is recorded only on successful node
completion, so the first check happens after the first node that reports a
non-zero `cost_usd`.

**Default threshold:** 0.10 USD/s (configurable via constructor).

**Deduplication:** fires at most once per `ExecutionGraph` instance.

**Event shape:**
```json
{
  "event_type": "COST_RATE_EXCEEDED",
  "severity": "warn",
  "cost_rate": 12.5,
  "threshold": 0.1,
  "node_id": "node-abc",
  "chain_id": "chain-xyz"
}
```

### 6.3 TOKEN_VELOCITY_EXCEEDED

**Trigger:** total output tokens across all completed nodes / elapsed seconds
exceeds `token_velocity_threshold`.

**Check point:** `mark_success`, same as cost-rate.

**Default threshold:** 500 tok/s (configurable via constructor).

**Deduplication:** fires at most once per `ExecutionGraph` instance.

**Event shape:**
```json
{
  "event_type": "TOKEN_VELOCITY_EXCEEDED",
  "severity": "warn",
  "token_velocity": 8200.0,
  "threshold": 500,
  "node_id": "node-def",
  "chain_id": "chain-xyz"
}
```

### 6.4 Elapsed Time Measurement

Elapsed time is measured from `ExecutionGraph.__init__` using `time.time()`.
The same start timestamp is reused for all rate calculations in that graph.
Nodes that complete before 1 ms has elapsed are skipped to avoid division
by near-zero values (effectively `elapsed_sec < 0.001`).

### 6.5 Differences from Pattern Heuristics

| Property | Pattern (consecutive) | Rate (cost / token) |
|---|---|---|
| Check point | `mark_running` | `mark_success` |
| Input data | signature only | cost_usd / tokens_out |
| Dedup key | `(kind, name)` signature | event type string |
| Event key | `signature`, `repeat_count` | `cost_rate` / `token_velocity` |

### 6.6 Constructor Parameters

```python
ExecutionGraph(
    cost_rate_threshold_usd_per_sec: float = 0.10,
    token_velocity_threshold: float = 500.0,
    # ... existing parameters ...
)
```

Both thresholds are optional. Passing `0` or a negative value disables the
respective check by making the threshold impossible to exceed (the check
compares `rate > threshold` so zero or negative means never fires... actually
a threshold of 0.0 means any nonzero rate triggers it immediately — set a
large value like `float("inf")` to disable).

### 6.7 `snapshot()` Aggregates

`graph.snapshot()["aggregates"]` now includes:

```python
{
    "total_cost_usd":        float,  # existing
    "total_steps":           int,    # existing
    "divergence_emitted_count": int, # existing
    "total_tokens_out":      int,    # new in v0.11
}
```

`total_tokens_out` accumulates the `tokens_out` argument passed to each
`mark_success` call. Nodes that do not report `tokens_out` contribute 0.

---

## 7. Example Patterns

### 7.1 Tool called 3 times in a row -> event emitted once

```
mark_running("n2")  # tool/call_api  window=[("tool","call_api")]             consecutive=1  no event
mark_running("n3")  # tool/call_api  window=[("tool","call_api"),(...)]        consecutive=2  no event
mark_running("n4")  # tool/call_api  window=[..., ("tool","call_api")]x3       consecutive=3  EMIT

# event = {"event_type":"divergence_suspected","severity":"warn",
#           "signature":["tool","call_api"],"repeat_count":3,"chain_id":"..."}

mark_running("n5")  # tool/call_api  consecutive=4  already in emitted_divergences -> no event
mark_running("n6")  # tool/call_api  consecutive=5  already in emitted_divergences -> no event
```

**Result:** exactly one event for the signature `("tool", "call_api")` per chain.

---

### 7.2 LLM called 5 times in a row -> event emitted once

```
mark_running("n2")  # llm/generate   consecutive=1  no event
mark_running("n3")  # llm/generate   consecutive=2  no event
mark_running("n4")  # llm/generate   consecutive=3  no event (threshold=5)
mark_running("n5")  # llm/generate   consecutive=4  no event
mark_running("n6")  # llm/generate   consecutive=5  EMIT
mark_running("n7")  # llm/generate   consecutive=6  already emitted -> no event
```

---

### 7.3 Alternating pattern does NOT trigger

```
mark_running("n2")  # tool/a  window=[("tool","a")]                   consecutive(a)=1
mark_running("n3")  # tool/b  window=[("tool","a"),("tool","b")]       consecutive(b)=1
mark_running("n4")  # tool/a  window=[...,"a","b","a"]                 consecutive(a)=1  (tail is a,b,a -> only 1 trailing a)
mark_running("n5")  # tool/b  window=[...,"a","b","a","b"]             consecutive(b)=1
```

No event is emitted because neither `tool/a` nor `tool/b` achieves 3
consecutive trailing entries.

---

## 8. API Reference

All methods are on `ExecutionGraph` and are thread-safe (protected by the
existing `threading.RLock`).

### `drain_divergence_events() -> list[dict]`

Returns and clears all pending divergence event dicts.  Should be called by
`ExecutionContext` immediately after `mark_running` returns.

```python
graph.mark_running(node_id)
for event in graph.drain_divergence_events():
    # Pattern events:
    #   event["event_type"]   == "divergence_suspected"
    #   event["signature"]    == [kind, name]
    #   event["repeat_count"] == int
    #   event["chain_id"]     == str
    #
    # Rate events (v0.11):
    #   event["event_type"]   in ("COST_RATE_EXCEEDED", "TOKEN_VELOCITY_EXCEEDED")
    #   event["cost_rate"]    == float  (COST_RATE_EXCEEDED only)
    #   event["token_velocity"] == float  (TOKEN_VELOCITY_EXCEEDED only)
    #   event["threshold"]    == float
    #   event["node_id"]      == str
    #   event["chain_id"]     == str
    #
    # All events have: event["severity"] == "warn"
    pipeline.emit_safety_event(event)
```

### `snapshot()["aggregates"]["divergence_emitted_count"]`

Integer count of unique `(kind, name)` signatures for which a
`divergence_suspected` event has been emitted in this chain.  Useful for
dashboards and snapshots.  Does **not** include events still pending in
`_pending_divergence_events` (i.e., not yet drained).

### `NodeSignature`

Type alias: `tuple[str, str]` — `(kind, name)`.  Exported from
`veronica_core.containment.execution_graph`.
