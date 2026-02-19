# VERONICA v0.9.0 — Runtime Containment Layer

This release repositions VERONICA as a Runtime Containment Layer for LLM Systems
and introduces a structural execution model to support that definition.

## What changed

- **Execution graph**: every LLM and tool call is now a typed node in an explicit
  DAG tracked within `ExecutionContext`. HALT reasons, costs, and token counts are
  recorded per node.
- **Amplification metrics**: `llm_calls_per_root`, `tool_calls_per_root`, and
  `retries_per_root` expose chain-level amplification directly from graph counters.
- **Divergence detection**: a lightweight ring-buffer heuristic emits a warn-severity
  `SafetyEvent` when repeated call patterns suggest a no-progress loop.
- **No breaking changes**: existing client code works without modification.
  `ExecutionGraph` is additive and automatic.

## Why v0.9.0

The prior versions established the enforcement primitives (budget, retries, circuit
breaker, step guard). This release makes the structural model explicit. Runtime
Containment is now a first-class architectural concept with supporting data structures,
not a collection of independent hooks.
