# Architecture: Executor / Planner Separation

## Overview

veronica-core is the **Executor** layer in the VERONICA stack.

```
VERONICA Planner (external, pluggable)
  - Budget allocation across competing agents
  - Cost prediction before LLM calls
  - Arbitration under resource contention
       |
       | submits PolicyConfig
       v
veronica-core Executor (this library)
  - Deterministic enforcement
  - Auditable decision trail
  - Dependency-light, stable guarantees
       |
       | enforce / halt
       v
LLM calls
```

## Design Rationale

### Why separate layers?

The Executor must be deterministic and auditable. Its behavior cannot
depend on probabilistic components. If the thing that stops runaway
agents is itself unpredictable, the safety guarantee collapses.

The Planner, by contrast, benefits from being adaptive. Optimal budget
allocation across competing agents, cost prediction from prompt
characteristics, and arbitration under contention are all problems where
AI or statistical models genuinely add value.

Separating the layers preserves both properties:

- The Planner can be as sophisticated as needed (AI, ML, rules, human).
- The Executor's guarantees are unchanged regardless of Planner strategy.

### Why not a single system?

Introducing ML dependencies into veronica-core would:

1. Break the "dependency-light" guarantee (current: stdlib + optional extras only)
2. Couple a fast-evolving experimental layer to a stable, auditable one
3. Make the Executor itself probabilistic — undermining its core value

The analogy is the Linux kernel scheduler (`kube-scheduler`) vs cgroup
enforcement (`kubelet`): the scheduler is heuristic and swappable; the
enforcement is deterministic and kernel-level.

## PlannerProtocol (planned, v1.0)

veronica-core will expose a minimal `PlannerProtocol` (Python `typing.Protocol`)
that defines the contract between Planner and Executor.

The Executor accepts a `PolicyConfig` produced by the Planner and enforces it.
The Planner has no visibility into Executor internals.

Design goals:

- Minimal surface area (submit config, receive SafetyEvents)
- No callbacks into Planner during enforcement
- Planner is stateless from the Executor's perspective

## Feedback Loop

The Executor feeds `SafetyEvent` history back to the Planner as
observability data. The Planner uses this to update future policy
submissions. The Executor never acts on Planner feedback directly.

```
Executor --[SafetyEvents]--> Planner
Planner  --[PolicyConfig]--> Executor
```

This is a clean feedback loop with no bidirectional coupling during
enforcement.

## Current State

veronica-core (v0.10.x) implements the Executor layer fully.

`AdaptiveBudgetHook` is an in-process approximation of the Planner
function: it observes SafetyEvents and adjusts ceilings. This is
appropriate for single-process deployments.

For multi-agent, cross-process, or AI-driven allocation, the external
Planner architecture is the intended path.
