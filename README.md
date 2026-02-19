# VERONICA

## VERONICA is a Runtime Containment Layer for LLM Systems.

*Turning unbounded model behavior into bounded system behavior.*

```bash
pip install veronica-core
```

Jump to [Quickstart (5 minutes)](#quickstart-5-minutes) or browse [docs/cookbook.md](docs/cookbook.md).

---

## 1. The Missing Layer in LLM Stacks

Modern LLM stacks are built around three well-understood components:

- **Prompting** — instruction construction, context management, few-shot formatting
- **Orchestration** — agent routing, tool dispatch, workflow sequencing
- **Observability** — tracing, logging, cost dashboards, latency metrics

What they lack is a fourth component: **runtime containment**.

Observability != Containment.

An observability stack tells you that an agent spent $12,000 over a weekend. It records the retry loops, the token volumes, the timestamp of each failed call. It produces a precise audit trail of a runaway execution.

What it does not do is stop it.

Runtime containment is the component that stops it. It operates before the damage occurs, not after. It enforces structural limits on what an LLM-integrated system is permitted to do at runtime — independent of prompt design, orchestration logic, or model behavior.

---

## 2. Why LLM Calls Are Not APIs

LLM calls are frequently treated as ordinary API calls: send a request, receive a response. This framing is incorrect, and the gap between the two creates reliability problems at scale.

Standard API calls exhibit predictable properties:
- Deterministic behavior for identical inputs
- Fixed or bounded response cost
- Safe retry semantics (idempotent by construction)
- No recursive invocation patterns

LLM calls exhibit none of these:

**Stochastic behavior.** The same prompt produces different outputs across invocations. There is no stable function to test against. Every call is a sample from a distribution, not a deterministic computation.

**Variable token cost.** Output length is model-determined, not caller-determined. A single call can consume 4 tokens or 4,000. Budget projections based on typical behavior fail under adversarial or unusual inputs.

**Recursive invocation.** Agents invoke tools; tools invoke agents; agents invoke agents. Recursion depth is not bounded by the model itself. A single top-level call can spawn hundreds of descendant calls with no inherent termination condition.

**Retry amplification.** When a component fails under load, exponential backoff retries compound across nested call chains. A failure rate of 5% per layer, across three layers, does not produce a 15% aggregate failure rate — it produces amplified retry storms that collapse throughput.

**Non-idempotent retries.** Retrying an LLM call is not guaranteed to be safe. Downstream state mutations, external tool calls, and partial execution all make naive retry semantics dangerous.

LLM calls are probabilistic, cost-generating components. They require structural bounding. They cannot be treated as deterministic, cost-stable services.

---

## 3. What Runtime Containment Means

Runtime containment is a constraint layer that enforces bounded behavior on LLM-integrated systems.

It does not modify prompts. It does not filter content. It does not evaluate output quality. It enforces operational limits on the execution environment itself — evaluated at call time, before the model is invoked.

A runtime containment layer enforces:

1. **Bounded cost** — maximum token spend and call volume per window, per entity, per system
2. **Bounded retries** — rate limits and amplification controls that prevent retry storms from escalating
3. **Bounded recursion** — per-entity circuit-breaking that terminates runaway loops regardless of orchestration logic
4. **Bounded wait states** — isolation of stalled or degraded components from the rest of the system
5. **Failure domain isolation** — structural separation between a failing component and adjacent components, with auditable evidence

VERONICA implements these five properties as composable, opt-in primitives.

---

## 4. Containment Layers in VERONICA

### Layer 1 — Cost Bounding

In distributed systems, resource quotas enforce hard limits on consumption per tenant, per service, per time window. Without them, a single runaway process exhausts shared resources.

LLM systems face the same problem at the token and call level. Without cost bounding, a single agent session can consume unbounded token volume with no mechanism to stop it.

VERONICA components:

- **BudgetWindowHook** — enforces a call-count ceiling within a sliding time window; emits DEGRADE before the ceiling is reached, then HALT at the ceiling
- **TokenBudgetHook** — enforces a cumulative token ceiling (output tokens or total tokens) with a configurable DEGRADE zone approaching the limit
- **TimeAwarePolicy** — applies time-based multipliers (off-hours, weekends) to reduce active ceilings during periods of lower oversight
- **AdaptiveBudgetHook** — adjusts ceilings dynamically based on observed SafetyEvent history; stabilized with cooldown windows, per-step smoothing, hard floor and ceiling bounds, and direction lock

---

### Layer 2 — Amplification Control

In distributed systems, retry amplification is a well-documented failure mode: a component under pressure receives more retries than it can handle, which increases pressure, which triggers more retries. Circuit breakers and rate limiters exist to interrupt this dynamic.

LLM systems exhibit the same failure mode. A transient model error triggers orchestration retries. Each retry may invoke tools, which invoke the model again. The amplification is geometric.

VERONICA components:

- **BudgetWindowHook** — the primary amplification control; a ceiling breach halts further calls regardless of upstream retry logic or backoff strategy
- **DEGRADE decision** — signals fallback behavior before hard stop, allowing graceful degradation (e.g., model downgrade) rather than binary failure
- **Anomaly tightening** (AdaptiveBudgetHook) — detects spike patterns in SafetyEvent history and temporarily reduces the effective ceiling during burst activity, with automatic recovery when the burst subsides

---

### Layer 3 — Recursive Containment

In distributed systems, recursive or cyclic call graphs require depth bounds or visited-node tracking to prevent infinite traversal. Without them, any recursive structure is a potential infinite loop.

LLM agents are recursive by construction: tool calls invoke the model; the model invokes tools. The recursion is implicit in the orchestration design, not explicit in any single call.

VERONICA components:

- **VeronicaStateMachine** — tracks per-entity fail counts; activates COOLDOWN state after a configurable number of consecutive failures; transitions to SAFE_MODE for system-wide halt
- **Per-entity cooldown isolation** — an entity in COOLDOWN is blocked from further invocations for a configurable duration; this prevents tight loops on failing components without affecting other entities
- **ShieldPipeline** — composable pre-dispatch hook chain; all registered hooks are evaluated in order before each LLM call; any hook may emit DEGRADE or HALT

---

### Layer 4 — Stall Isolation

In distributed systems, a stalled downstream service causes upstream callers to block on connection pools, exhaust timeouts, and degrade responsiveness across unrelated request paths. Bulkhead patterns and timeouts exist to contain stall propagation.

LLM systems stall when a model enters a state of repeated low-quality, excessively verbose, or non-terminating responses. Without isolation, a stalled model session propagates degradation upstream.

VERONICA components:

- **VeronicaGuard** — abstract interface for domain-specific stall detection; implementations inspect latency, error rate, response quality, or any domain signal to trigger immediate cooldown activation, bypassing the default fail-count threshold
- **Per-entity cooldown** (VeronicaStateMachine) — stall isolation is per entity; a stalled tool or agent does not trigger cooldown for entities with clean histories
- **MinimalResponsePolicy** — opt-in system-message injection that enforces output conciseness constraints, reducing the probability of runaway token generation from verbose model states

---

### Layer 5 — Failure Domain Isolation

In distributed systems, failure domain isolation ensures that a fault in one component does not propagate to adjacent components. Structured error events, circuit-state export, and tiered shutdown protocols are standard mechanisms for this.

LLM systems require the same. A component failure should produce structured evidence, enable state inspection, and permit controlled shutdown without corrupting adjacent execution state.

VERONICA components:

- **SafetyEvent** — structured evidence record for every non-ALLOW decision; contains event type, decision, hook identity, and SHA-256 hashed context; raw prompt content is never stored
- **Deterministic replay** — control state (ceiling, multipliers, adjustment history) can be exported and re-imported; enables observability dashboard integration and post-incident reproduction
- **InputCompressionHook** — gates oversized inputs before they reach the model; HALT on inputs exceeding the ceiling, DEGRADE with compression recommendation in the intermediate zone
- **VeronicaExit** — three-tier shutdown protocol (GRACEFUL, EMERGENCY, FORCE) with SIGTERM and SIGINT signal handling and atexit fallback; state is preserved where possible at each tier

---

## 5. Architecture Overview

VERONICA operates as a middleware constraint layer between the orchestration layer and the LLM provider. It does not modify orchestration logic. It enforces constraints on what the orchestration layer is permitted to dispatch downstream.

```
App
  |
  v
Orchestrator
  |
  v
Runtime Containment (VERONICA)
  |
  v
LLM Provider
```

Each call from the orchestrator passes through the ShieldPipeline before reaching the provider. The pipeline evaluates registered hooks in order. Any hook may emit DEGRADE or HALT. A HALT decision terminates the call and emits a SafetyEvent. The orchestrator receives the decision and handles it according to its own logic.

VERONICA does not prescribe how the orchestrator responds to DEGRADE or HALT. It enforces that the constraint evaluation occurs, that the decision is recorded as a structured event, and that the call does not proceed past a HALT decision.

---

## 6. OSS and Cloud Boundary

**veronica-core** is the local containment primitive library. It contains all enforcement logic: ShieldPipeline, BudgetWindowHook, TokenBudgetHook, AdaptiveBudgetHook, TimeAwarePolicy, InputCompressionHook, MinimalResponsePolicy, VeronicaStateMachine, SafetyEvent, VeronicaExit, and associated state management.

veronica-core operates without network connectivity, external services, or vendor dependencies. All containment decisions are local and synchronous.

**veronica-cloud** (forthcoming) provides coordination primitives for multi-agent and multi-tenant deployments: shared budget pools, distributed policy enforcement, and real-time dashboard integration for SafetyEvent streams.

The boundary is functional: cloud enhances visibility and coordination across distributed deployments. It does not enhance safety. Safety properties are enforced by veronica-core at the local layer. An agent running without cloud connectivity is still bounded. An agent running without veronica-core is not.

---

## 7. Design Philosophy

VERONICA is not:

- **Observability** — it does not trace, log, or visualize execution after the fact
- **Content guardrails** — it does not inspect, classify, or filter prompt or completion content
- **Evaluation tooling** — it does not assess output quality, factual accuracy, or alignment properties

VERONICA is:

- **Runtime constraint enforcement** — hard and soft limits on call volume, token spend, input size, and execution state, evaluated before each LLM call
- **Systems-level bounding layer** — structural containment at the orchestration boundary, treating LLM calls as probabilistic, cost-generating components that require bounding

The design is deliberately narrow. A component that attempts to solve observability, guardrails, containment, and evaluation simultaneously solves none of them well. VERONICA solves containment.

---

## Quickstart (5 minutes)

### Install

```bash
pip install veronica-core
```

### Minimal runtime containment example

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig, WrapOptions

def simulated_llm_call(prompt: str) -> str:
    return f"response to: {prompt}"

config = ExecutionConfig(
    max_cost_usd=1.00,    # hard cost ceiling per chain
    max_steps=50,         # hard step ceiling
    max_retries_total=10,
    timeout_ms=0,
)

with ExecutionContext(config=config) as ctx:
    for i in range(3):
        decision = ctx.wrap_llm_call(
            fn=lambda: simulated_llm_call(f"prompt {i}"),
            options=WrapOptions(
                operation_name=f"generate_{i}",
                cost_estimate_hint=0.04,
            ),
        )
        if decision.name == "HALT":
            break

snap = ctx.get_graph_snapshot()
print(snap["aggregates"])
```

### Expected output

```python
{
    "total_cost_usd": 0.12,
    "total_llm_calls": 3,
    "total_tool_calls": 0,
    "total_retries": 0,
    "max_depth": 1,
    "llm_calls_per_root": 3.0,
    "tool_calls_per_root": 0.0,
    "retries_per_root": 0.0,
    "divergence_emitted_count": 0
}
```

This demonstrates runtime containment as a structural property: every call is recorded
into an execution graph, amplification is measurable at the chain level, and HALT
semantics are deterministic and auditable per node.

### What each part does

- `ExecutionConfig` — declares hard limits for the chain (cost, steps, retries, timeout)
- `ExecutionContext` — scopes one agent run or request chain; enforces limits at dispatch time
- `wrap_llm_call()` — records the call as a typed node; evaluates all containment conditions before dispatch
- `get_graph_snapshot()` — returns an immutable, JSON-serializable view of the execution graph

### Enforce a step ceiling

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig, WrapOptions
from veronica_core.shield.types import Decision

config = ExecutionConfig(max_cost_usd=10.0, max_steps=5, max_retries_total=20, timeout_ms=0)

with ExecutionContext(config=config) as ctx:
    for i in range(10):
        decision = ctx.wrap_llm_call(
            fn=lambda: "result",
            options=WrapOptions(operation_name=f"step_{i}"),
        )
        if decision == Decision.HALT:
            print(f"Halted at step {i}")
            break
```

### What to read next

- [docs/execution-context.md](docs/execution-context.md) -- ExecutionContext API spec
- [docs/execution-graph.md](docs/execution-graph.md) -- execution graph model and invariants
- [docs/amplification-factor.md](docs/amplification-factor.md) -- amplification metrics
- [docs/divergence-heuristics.md](docs/divergence-heuristics.md) -- divergence detection
- [docs/cookbook.md](docs/cookbook.md) -- copy-paste recipes for common patterns
- [examples/execution_context_demo.py](examples/execution_context_demo.py) -- runnable scenarios (step limit, budget, abort, circuit, divergence)
- [CHANGELOG.md](CHANGELOG.md) -- version history

---

**Records:** every LLM and tool call as a typed node in an execution graph.
**Never stores:** prompt contents. Evidence uses SHA-256 hashes by default.

---

## Ship Readiness (v0.9.0)

- [x] BudgetWindow stops runaway execution (ceiling enforced)
- [x] SafetyEvent records structured evidence for non-ALLOW decisions
- [x] DEGRADE supported (fallback at threshold, HALT at ceiling)
- [x] TokenBudgetHook: cumulative output/total token ceiling with DEGRADE zone
- [x] MinimalResponsePolicy: opt-in conciseness constraints for system messages
- [x] InputCompressionHook: real compression with Compressor protocol + safety guarantees (v0.5.1)
- [x] AdaptiveBudgetHook: auto-adjusts ceiling based on SafetyEvent history (v0.6.0)
- [x] TimeAwarePolicy: weekend/off-hours budget multipliers (v0.6.0)
- [x] Adaptive stabilization: cooldown, smoothing, floor/ceiling, direction lock (v0.7.0)
- [x] Anomaly tightening: spike detection with temporary ceiling reduction (v0.7.0)
- [x] Deterministic replay: export/import control state for observability (v0.7.0)
- [x] ExecutionGraph: first-class runtime execution graph with typed node lifecycle (v0.9.0)
- [x] Amplification metrics: llm_calls_per_root, tool_calls_per_root, retries_per_root (v0.9.0)
- [x] Divergence heuristic: repeated-signature detection, warn-only, deduped (v0.9.0)
- [x] PyPI auto-publish on GitHub Release
- [x] Everything is opt-in & non-breaking (default behavior unchanged)

616 tests passing. Minimum production use-case: runaway containment + graceful degrade + auditable events + token budgets + input compression + adaptive ceiling + time-aware scheduling + anomaly detection + execution graph + divergence detection.

---

## Roadmap

### v0.9.x

- OpenTelemetry export (opt-in SafetyEvent export to standard spans)
- Middleware mode (ASGI/WSGI integration)
- Distributed budget coordination (Redis-backed shared pools)
- Improved divergence heuristics (cost-rate, token-velocity)

### v1.0

- Stable `ExecutionContext` API with formal deprecation policy
- Formal containment guarantee documentation
- `ExecutionGraph` extensibility hooks for external integrations
- Multi-agent containment primitives (shared budget, cross-chain circuit breaker)

---

## Install

```bash
pip install -e .

# With dev tools
pip install -e ".[dev]"
pytest
```

![CI](https://img.shields.io/badge/tests-616%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

---

## Version History

See [CHANGELOG.md](CHANGELOG.md) for version history.

---

## License

MIT

---

*Runtime Containment is the missing layer in LLM infrastructure.*
