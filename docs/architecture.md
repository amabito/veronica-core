# Architecture: Runtime Containment Kernel + Execution OS

## Overview

veronica-core is the **Runtime Containment Kernel**.
[VERONICA](https://github.com/amabito/veronica) is the **Execution OS** built around it.

## Stack

```
Application
     |
veronica-core --------[policy config]-------- VERONICA (Control Plane)
     |                                              |
LLM Providers                              Org Policy / Dashboard /
                                           Shared State / Audit / Alerts
```

**veronica-core is on the critical path. VERONICA is on the management path.**

The Control Plane manages policy and state asynchronously.
It does not sit between the application and LLM calls.
This preserves veronica-core's latency guarantees regardless of VERONICA availability.

---

## Layer Responsibilities

### veronica-core (Kernel)

Enforces bounded execution. Runs local. No cloud required.

| Primitive | Role |
|---|---|
| `ExecutionContext` | Bounded execution scope |
| `ExecutionGraph` | Multi-chain containment |
| `ShieldPipeline` | Pre-call enforcement hooks |
| `BudgetEnforcer` | Cost ceiling per chain |
| `CircuitBreaker` | Failure isolation |
| `AdaptiveBudgetHook` | Feedback-based ceiling adjustment |
| Divergence heuristics | Loop and anomaly detection |

OS analogy: scheduler, memory quota, process table, kill signal.

### VERONICA (Control Plane)

Manages policy across agents, services, and organizations. Planned.

| Component | Role |
|---|---|
| Planner | Execution strategy -- decides what limits to set |
| Budget allocation | Distributes budget across competing agents |
| Org policy engine | Organization-wide containment rules |
| Shared circuit state | Cross-service breaker coordination |
| Dashboard | Visibility into execution health |
| Audit / Compliance | Policy enforcement at scale |

OS analogy: control plane, governance plane, management console.

---

## Design Principles

**The kernel enforces. The OS decides.**

veronica-core's guarantees are unconditional. They hold whether VERONICA is present or not.
VERONICA extends those guarantees across agents, services, and organizations.

**Separation of concerns.**

A probabilistic or adaptive component must not sit inside the enforcement boundary.
The Planner proposes policy. The kernel enforces it. The Planner has no visibility into enforcement internals.

**Planner scope boundary.**

The Planner decides *what limits to set* -- ceiling, timeout, escalation policy.
The Planner does not decide *what the agent does* -- routing, model selection, prompt construction.
Crossing this boundary turns the Planner into an orchestrator. That is a different product.

---

## PlannerProtocol (planned, v1.0)

veronica-core will expose a minimal `PlannerProtocol` (Python `typing.Protocol`)
defining the contract between Planner and kernel.

Design goals:
- Minimal surface area: submit `PolicyConfig`, receive `SafetyEvent` stream
- No callbacks into Planner during enforcement
- Planner is stateless from the kernel's perspective

## Feedback Loop

```
veronica-core --[SafetyEvents]--> VERONICA Planner --[PolicyConfig]--> veronica-core
```

The kernel never modifies its behavior based on SafetyEvents mid-execution.
Adaptation always flows via a new PolicyConfig from the Planner.

---

## Current State

veronica-core (v0.11.0) implements the kernel layer fully.

`AdaptiveBudgetHook` is an in-process approximation of the Planner function:
it observes SafetyEvents and adjusts ceilings. Appropriate for single-process deployments.

For multi-agent, cross-process, or AI-driven allocation, the VERONICA Control Plane is the intended path.
