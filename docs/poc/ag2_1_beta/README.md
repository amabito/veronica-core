# veronica-core x AG2.1 Beta -- Governance Middleware PoC

This directory contains a proof-of-concept integration of veronica-core's
runtime governance into AG2.1 beta's middleware chain (#2439).

## Overview

veronica-core provides runtime governance primitives -- budget enforcement,
circuit breaking, semantic loop detection, tool policy evaluation -- as
AG2.1 middleware. The governance logic runs inside AG2.1's `on_turn`,
`on_llm_call`, and `on_tool_execution` hooks without modifying AG2 internals.

```
Agent.ask()
  -> VeronicaMiddleware.on_turn     (safe mode, circuit breaker, step guard)
    -> VeronicaMiddleware.on_llm_call  (budget check/spend)
      -> LLM provider
    -> VeronicaMiddleware.on_tool_execution  (policy engine)
      -> tool function
```

## Quick Start

```python
from autogen.beta import Agent
from veronica_core import BudgetEnforcer, CircuitBreaker, SemanticLoopGuard
from veronica_core.adapters.ag2_beta import VeronicaMiddleware, VeronicaGovernanceConfig

config = VeronicaGovernanceConfig(
    budget=BudgetEnforcer(limit_usd=1.0),
    circuit_breaker=CircuitBreaker(failure_threshold=3, recovery_timeout=60),
    semantic_guard=SemanticLoopGuard(window=5, threshold=0.92),
)

agent = Agent(
    name="governed_agent",
    middleware=[VeronicaMiddleware(config)],
)

reply = await agent.ask("Summarize the quarterly report")
```

## PoC Scenarios

1. **Budget exhaustion** -- agent stops when cumulative LLM cost exceeds limit
2. **Loop detection** -- agent halts when output similarity exceeds threshold
3. **Tool policy** -- dangerous shell/network commands blocked before execution

See `docs/design/ag2_1_middleware_adapter.md` for the full design document.

---

## Why External Governance?

### The Gap in Agent Frameworks

Existing agent frameworks, including those written in systems languages,
implement permission control as static policy -- allow, ask, or reject at
configuration time. Runtime governance that reacts to live execution state
is absent: no circuit breaking when an LLM provider degrades, no detection
of semantic loops where the agent repeats itself, no adaptive budget
enforcement that adjusts to burn-rate anomalies. These are runtime concerns
that require continuous observation and state-machine transitions during
execution, not static policy declarations before it.

### Runtime Governance as a Separate Concern

Governance requires domain knowledge distinct from orchestration.
Budget enforcement involves distributed state machines with atomic
two-phase commit across processes. Circuit breaking requires failure
counting with configurable predicates, recovery timeouts, and
half-open single-request gates. Semantic loop detection uses
rolling-window Jaccard similarity with tunable thresholds.
Building these into a framework's core increases coupling and slows
iteration on the framework's primary job -- agent orchestration.
The middleware pattern separates these concerns cleanly.

### AG2.1's Middleware Architecture Enables This

AG2.1 beta's middleware chain provides three independent wrapping points
(`on_turn`, `on_llm_call`, `on_tool_execution`) where external logic
intercepts the execution flow via `call_next`. As Mark (msze) noted in
the design discussion: "we explicitly considered your use-case" and
"we made it extensible so that something like this could work."
This architecture makes governance a pluggable layer rather than a
fork-and-patch exercise.

### veronica-core as a Reference Implementation

veronica-core is a 33,000-line governance-only library with zero required
dependencies. It provides adapters for 7 frameworks (AG2, CrewAI, LangChain,
LangGraph, LlamaIndex, MCP, ROS2), a distributed circuit breaker backed by
Redis with Lua-scripted atomic operations, adaptive budget enforcement with
burn-rate anomaly detection, and semantic loop detection. The AG2.1 middleware
adapter maps these primitives onto AG2.1's `BaseMiddleware` protocol, adding
runtime governance to any AG2.1 agent with a single `middleware=[...]`
parameter.

---

## Related

- [AG2.1 beta PR #2439](https://github.com/ag2ai/ag2/pull/2439) -- middleware chain design
- [AgentEligibilityPolicy PR #2459](https://github.com/ag2ai/ag2/pull/2459) -- runtime GroupChat filtering
- [CircuitBreakerCapability PR #2430](https://github.com/ag2ai/ag2/pull/2430) -- merged, current AG2 adapter
- [Design document](../design/ag2_1_middleware_adapter.md) -- full middleware mapping
