# AG2.1 Beta Middleware Adapter Design

Status: DRAFT
Date: 2026-03-12
Target: AG2.1 beta (#2439, Lancetnik)

---

## 1. AG2.1 Middleware Chain Overview

AG2.1 beta (#2439) introduces a middleware chain built on function composition
via `partial()`. The architecture lives in `autogen/beta/middleware/`.

### Core Types (`middleware/base.py`)

```python
# Factory -- creates a new middleware instance per turn
class MiddlewareFactory(Protocol):
    def __call__(self, event: BaseEvent, context: Context) -> BaseMiddleware: ...

# Type aliases for call_next signatures
AgentTurn     = Callable[[BaseEvent, Context], Awaitable[ModelResponse]]
ToolExecution = Callable[[ToolCall, Context], Awaitable[ToolResult | ToolError | ClientToolCall]]
LLMCall       = Callable[[Sequence[BaseEvent], Context], Awaitable[ModelResponse]]

# Base class -- override only the hooks you need
class BaseMiddleware:
    async def on_turn(self, call_next: AgentTurn, event, context) -> ModelResponse: ...
    async def on_tool_execution(self, call_next: ToolExecution, event, context) -> ...: ...
    async def on_llm_call(self, call_next: LLMCall, events, context) -> ModelResponse: ...
```

Default implementation: `return await call_next(...)` (pass-through).

### Chain Construction (`agent.py: _execute()`)

```python
for m in reversed(list(chain(self._middleware, additional_middleware))):
    mw = m(event, context)                         # factory call
    agent_turn = partial(mw.on_turn, agent_turn)   # wrap
    llm_call   = partial(mw.on_llm_call, llm_call) # wrap
```

Tool execution chains are built separately per `FunctionTool.register()`.

### Three Independent Chains

| Chain | Terminal | Scope |
|-------|---------|-------|
| `agent_turn` | `_execute_turn()` | Full turn (includes tool loop) |
| `llm_call` | `client(tools=...)` | Single LLM API call |
| `tool_execution` | `FunctionTool.__call__()` | Single tool invocation |

### Registration API

```python
# Agent-level (all turns)
agent = Agent(name="a", middleware=[MyMiddleware()])

# Per-ask (single turn only)
reply = await agent.ask("msg", middleware=[ExtraMiddleware()])
```

### Built-in Middleware

| Class | Hook | Behavior |
|-------|------|----------|
| `HistoryLimiter(max_events)` | `on_llm_call` | Truncate event history |
| `TokenLimiter(max_tokens)` | `on_llm_call` | Estimate-based history trim |
| `RetryMiddleware(max_retries)` | `on_llm_call` | Retry on LLM failure |
| `LoggingMiddleware(logger)` | all three | Timing + structured logging |

---

## 2. Component Mapping

veronica-core components mapped to AG2.1 middleware hooks:

| veronica-core Component | AG2.1 Hook | before/after | Implementation |
|---|---|---|---|
| `SafeModeHook` | `on_turn` | before call_next | If SAFE_MODE active, return error ModelResponse without calling call_next. Checked first (outermost middleware). |
| `CircuitBreaker` | `on_turn` | before + after | Before: `check()` -- if OPEN, skip call_next. After: `record_success()` or `record_failure()` based on result. HALF_OPEN single-request gate. |
| `BudgetEnforcer` | `on_llm_call` | before + after | Before: `check()` with estimated cost. After: `spend()` with actual `response_cost`. Exceeds limit -> skip call_next. |
| `AgentStepGuard` | `on_turn` | before | `step()` before call_next. Exceeded -> return partial result. |
| `SemanticLoopGuard` | `on_turn` | after | Feed response text to `record()`. If loop detected, raise `VeronicaHalt` or return degrade response. |
| `PolicyEngine` (shell/network) | `on_tool_execution` | before | Evaluate tool_name + arguments against shell/network/file rules. DENY -> return `ToolError` without calling call_next. |
| `ShieldPipeline` | `on_llm_call` | before | `before_llm_call()` with ToolCallContext. HALT -> skip call_next. |
| `AdaptiveBudget` | `on_llm_call` | after | Burn rate anomaly detection post-call. Adjusts remaining budget dynamically. |
| `MemoryGovernor` | `on_tool_execution` | before | For memory-related tools, evaluate `MemoryOperation` against governance rules. DENY/QUARANTINE -> block. |
| `ExecutionContext` | `on_turn` | before + after | Chain-level wrapper: timeout enforcement, cost accumulation, execution graph node tracking. |

### Hook Selection Rationale

- **`on_turn`** for cross-cutting concerns (safe mode, circuit breaker, step limit, loop detection) -- these apply to the entire agent turn regardless of how many LLM calls or tool executions occur within it.
- **`on_llm_call`** for cost-aware controls (budget, adaptive budget, shield pipeline) -- these need access to the LLM request/response for cost tracking.
- **`on_tool_execution`** for tool-specific policy (shell commands, network access, memory operations) -- these inspect tool name and arguments.

---

## 3. Interface Design

### VeronicaMiddleware (MiddlewareFactory)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from veronica_core import (
    BudgetEnforcer,
    CircuitBreaker,
    ExecutionContext,
    PolicyEngine,
    SemanticLoopGuard,
)
from veronica_core.agent_guard import AgentStepGuard
from veronica_core.runtime_policy import PolicyContext
from veronica_core.shield.safe_mode import SafeModeHook


@dataclass
class VeronicaGovernanceConfig:
    """Configuration for veronica-core governance middleware."""

    execution_context: Optional[ExecutionContext] = None
    budget: Optional[BudgetEnforcer] = None
    circuit_breaker: Optional[CircuitBreaker] = None
    step_guard: Optional[AgentStepGuard] = None
    semantic_guard: Optional[SemanticLoopGuard] = None
    policy_engine: Optional[PolicyEngine] = None
    safe_mode: Optional[SafeModeHook] = None


class VeronicaMiddleware:
    """AG2.1 MiddlewareFactory for veronica-core governance.

    Usage::

        from veronica_core.adapters.ag2_beta import VeronicaMiddleware

        governance = VeronicaMiddleware(
            VeronicaGovernanceConfig(
                budget=BudgetEnforcer(limit_usd=1.0),
                circuit_breaker=CircuitBreaker(failure_threshold=3),
                semantic_guard=SemanticLoopGuard(window=5, threshold=0.92),
            )
        )

        agent = Agent(name="worker", middleware=[governance])
    """

    def __init__(self, config: VeronicaGovernanceConfig) -> None:
        self._config = config

    def __call__(self, event: Any, context: Any) -> "_VeronicaBaseMiddleware":
        # MiddlewareFactory protocol: return a new middleware instance per turn
        return _VeronicaBaseMiddleware(self._config)
```

### _VeronicaBaseMiddleware (BaseMiddleware)

```python
class _VeronicaBaseMiddleware:
    """Per-turn middleware instance. Wraps all three AG2.1 hooks."""

    def __init__(self, config: VeronicaGovernanceConfig) -> None:
        self._config = config

    async def on_turn(self, call_next, event, context):
        cfg = self._config

        # 1. Safe mode check (outermost gate)
        if cfg.safe_mode and cfg.safe_mode.enabled:
            # Return a ModelResponse indicating halt
            # <!-- TODO: confirm ModelResponse constructor from #2439 -->
            raise VeronicaHalt("SAFE_MODE active")

        # 2. Step guard
        if cfg.step_guard:
            if not cfg.step_guard.step():
                raise VeronicaHalt("Step limit exceeded")

        # 3. Circuit breaker (before)
        if cfg.circuit_breaker:
            decision = cfg.circuit_breaker.check(PolicyContext())
            if not decision.allowed:
                raise VeronicaHalt(f"Circuit open: {decision.reason}")

        # 4. Execute turn
        try:
            result = await call_next(event, context)
        except Exception as exc:
            if cfg.circuit_breaker:
                cfg.circuit_breaker.record_failure(error=exc)
            raise

        # 5. Circuit breaker (after -- record outcome)
        if cfg.circuit_breaker:
            if result is None:
                cfg.circuit_breaker.record_failure()
            else:
                cfg.circuit_breaker.record_success()

        # 6. Semantic loop detection (after)
        if cfg.semantic_guard and result is not None:
            text = _extract_text(result)
            loop_decision = cfg.semantic_guard.record(text)
            if not loop_decision.allowed:
                raise VeronicaHalt(f"Loop detected: {loop_decision.reason}")

        return result

    async def on_llm_call(self, call_next, events, context):
        cfg = self._config

        # 1. Budget check (before)
        if cfg.budget:
            decision = cfg.budget.check(PolicyContext(cost_usd=0.01))
            if not decision.allowed:
                raise VeronicaHalt(f"Budget exceeded: {decision.reason}")

        # 2. Execute LLM call
        result = await call_next(events, context)

        # 3. Budget spend (after -- actual cost)
        if cfg.budget:
            cost = _extract_cost(result, context)
            if cost > 0:
                cfg.budget.spend(cost)

        return result

    async def on_tool_execution(self, call_next, event, context):
        cfg = self._config

        # 1. Policy engine check
        if cfg.policy_engine:
            tool_name = event.name if hasattr(event, "name") else str(event)
            # <!-- TODO: extract arguments from ToolCall after #2439 API stabilizes -->
            decision = cfg.policy_engine.evaluate_tool(tool_name, {})
            if not decision.allowed:
                # Return ToolError without executing
                # <!-- TODO: confirm ToolError constructor from #2439 -->
                raise VeronicaHalt(f"Tool blocked: {decision.reason}")

        return await call_next(event, context)
```

### Helper Functions

```python
def _extract_text(result: Any) -> str:
    """Extract text content from ModelResponse for loop detection."""
    # <!-- TODO: confirm ModelResponse structure from #2439 -->
    if hasattr(result, "content"):
        return str(result.content)
    return str(result)


def _extract_cost(result: Any, context: Any) -> float:
    """Extract response cost from LLM result or context."""
    # AG2.1 may expose cost via context or result metadata
    # <!-- TODO: confirm cost extraction path from #2439 -->
    if hasattr(result, "usage") and result.usage:
        total = getattr(result.usage, "total_tokens", 0)
        return total * 0.000003  # fallback estimate
    return 0.0
```

---

## 4. GroupChat Integration

### select_speaker Governance

AG2.1 beta replaces GroupChat's `select_speaker` with the middleware chain.
Governance hooks into speaker selection via `on_turn`:

```python
class SpeakerGovernanceMiddleware:
    """Filter eligible speakers before GroupChat selection."""

    def __init__(
        self,
        budget_allocator: BudgetAllocator,
        agent_budgets: dict[str, BudgetEnforcer],
        circuit_breakers: dict[str, CircuitBreaker],
    ) -> None:
        self._allocator = budget_allocator
        self._budgets = agent_budgets
        self._breakers = circuit_breakers

    def __call__(self, event, context):
        return _SpeakerGovernance(self._allocator, self._budgets, self._breakers)


class _SpeakerGovernance:
    async def on_turn(self, call_next, event, context):
        # Filter out agents with exhausted budgets or open circuits
        # <!-- TODO: confirm how AG2.1 GroupChat exposes candidate list -->
        # Approach: modify context.variables to exclude ineligible agents
        # before call_next invokes the speaker selection logic.

        eligible = []
        for name, budget in self._budgets.items():
            if budget.is_exceeded:
                continue
            breaker = self._breakers.get(name)
            if breaker and breaker.state.name == "OPEN":
                continue
            eligible.append(name)

        # Inject eligible list into context for speaker selection
        # <!-- TODO: confirm Context.variables API from #2439 -->
        context.variables["veronica_eligible_agents"] = eligible

        return await call_next(event, context)
```

### Fan-Out Budget Allocation

When GroupChat dispatches to multiple agents, `BudgetAllocator` distributes
the remaining budget:

```python
from veronica_core.containment.budget_allocator import DynamicAllocator

allocator = DynamicAllocator(min_share=0.05)
result = allocator.allocate(
    total_budget=remaining_budget,
    agent_names=["planner", "coder", "reviewer"],
    current_usage={"planner": 0.12, "coder": 0.35, "reviewer": 0.08},
)
# result.allocations = {"planner": 0.25, "coder": 0.10, "reviewer": 0.30}
```

Three allocation strategies are available:

| Strategy | Class | Behavior |
|----------|-------|----------|
| Equal split | `FairShareAllocator` | `remaining / N` per agent |
| Weighted | `WeightedAllocator(weights)` | Proportional to weights |
| Usage-adaptive | `DynamicAllocator(min_share)` | Inverse of current usage, floor at min_share |

### AgentEligibilityPolicy Connection

PR #2459 (AgentEligibilityPolicy) defines runtime eligibility rules for
GroupChat candidate filtering. In AG2.1, this integrates as:

1. `AgentEligibilityPolicy` evaluates eligibility (existing PR #2459 logic)
2. `SpeakerGovernanceMiddleware` enforces budget/circuit constraints (new)
3. Both filter the candidate list before speaker selection

These compose naturally: eligibility is a policy decision, budget/circuit
are governance decisions. Both reduce the candidate set independently.

---

## 5. Minimal PoC Scope

Three scenarios for the first demo to show Mark:

### Scenario 1: Budget Exhaustion Stops Agent

```python
from autogen.beta import Agent
from veronica_core import BudgetEnforcer
from veronica_core.adapters.ag2_beta import VeronicaMiddleware, VeronicaGovernanceConfig

budget = BudgetEnforcer(limit_usd=0.05)
governance = VeronicaMiddleware(VeronicaGovernanceConfig(budget=budget))

agent = Agent(name="worker", middleware=[governance])

# Agent runs until budget exhausted, then VeronicaHalt is raised
for i in range(100):
    try:
        reply = await agent.ask(f"Task {i}")
    except VeronicaHalt:
        print(f"Stopped at task {i}: ${budget.spent_usd:.4f} spent")
        break
```

### Scenario 2: Semantic Loop Detection

```python
from veronica_core import SemanticLoopGuard

loop_guard = SemanticLoopGuard(window=5, threshold=0.92)
governance = VeronicaMiddleware(VeronicaGovernanceConfig(semantic_guard=loop_guard))

agent = Agent(name="writer", middleware=[governance])

# If the agent repeats similar output 5 times, VeronicaHalt is raised
try:
    reply = await agent.ask("Write a poem about cats")
except VeronicaHalt as e:
    print(f"Loop detected: {e}")
```

### Scenario 3: Tool Policy Enforcement

```python
from veronica_core.security.policy_engine import PolicyEngine

policy = PolicyEngine()  # default: deny rm, curl, powershell, etc.
governance = VeronicaMiddleware(VeronicaGovernanceConfig(policy_engine=policy))

agent = Agent(name="coder", middleware=[governance])

# Tool calls to shell commands are evaluated against policy rules
# Denied tools return ToolError without execution
reply = await agent.ask("Delete all files in /tmp")
# PolicyEngine blocks: shell command 'rm' is denied
```

---

## 6. Coexistence with Existing Adapters

### Module Layout

```
veronica_core/adapters/
├── ag2.py              # Current AG2 (0.2.x -- 0.6.x) adapter
├── ag2_capability.py   # Current CircuitBreakerCapability
└── ag2_beta.py         # NEW: AG2.1 beta middleware adapter
```

### Import Path Separation

```python
# Current AG2 (unchanged)
from veronica_core.adapters.ag2 import VeronicaConversableAgent
from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

# AG2.1 beta (new)
from veronica_core.adapters.ag2_beta import VeronicaMiddleware, VeronicaGovernanceConfig
```

### Version Detection (Optional)

```python
def _detect_ag2_version() -> str:
    """Detect installed AG2 version for adapter selection guidance."""
    try:
        import autogen.beta  # AG2.1 beta
        return "2.1-beta"
    except ImportError:
        pass
    try:
        import autogen
        return getattr(autogen, "__version__", "0.2.x")
    except ImportError:
        return "not-installed"
```

No automatic adapter switching. Users explicitly choose the import path
based on their AG2 version. This avoids magic and keeps the dependency
on AG2.1 beta optional.

### Shared Infrastructure

Both adapters reuse the same veronica-core primitives:

```
ag2.py          -> AIContainer -> BudgetEnforcer, CircuitBreaker, ...
ag2_beta.py     -> VeronicaGovernanceConfig -> BudgetEnforcer, CircuitBreaker, ...
```

The governance logic is identical. Only the integration surface differs:
- `ag2.py`: wraps `generate_reply()` via monkey-patch or subclass
- `ag2_beta.py`: implements `BaseMiddleware` protocol with `on_turn/on_llm_call/on_tool_execution`

---

## Open Questions

<!-- TODO: Confirm ModelResponse constructor and fields from #2439 -->
<!-- TODO: Confirm ToolCall.name and argument extraction API from #2439 -->
<!-- TODO: Confirm Context.variables API for injecting eligible agent list -->
<!-- TODO: Confirm how GroupChat exposes speaker candidate list in AG2.1 -->
<!-- TODO: Confirm cost/token extraction path from LLM response in AG2.1 -->
<!-- TODO: Determine if VeronicaHalt should be caught by AG2.1 or propagate to user -->
