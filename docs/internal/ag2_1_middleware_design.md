# AG2.1 Beta Middleware Adapter Design

> **WARNING: INTERNAL -- NOT FOR EXTERNAL SHARING**

Status: DRAFT (hypotheses -- pending validation against #2439 codebase)
Date: 2026-03-12
Target: AG2.1 beta (#2439, Lancetnik)

---

## 1. AG2.1 Middleware Chain Overview

AG2.1 beta (#2439) likely introduces a middleware chain built on function
composition via `partial()`. Based on reading the PR diff, the architecture
appears to live in `autogen/beta/middleware/`.

### Core Types (`middleware/base.py`)

Based on the current #2439 draft:

```python
# Factory -- likely creates a new middleware instance per turn
class MiddlewareFactory(Protocol):
    def __call__(self, event: BaseEvent, context: Context) -> BaseMiddleware: ...

# Type aliases for call_next signatures (pending confirmation of exact types)
AgentTurn     = Callable[[BaseEvent, Context], Awaitable[ModelResponse]]
ToolExecution = Callable[[ToolCall, Context], Awaitable[ToolResult | ToolError | ClientToolCall]]
LLMCall       = Callable[[Sequence[BaseEvent], Context], Awaitable[ModelResponse]]

# Base class -- override only the hooks you need
class BaseMiddleware:
    async def on_turn(self, call_next: AgentTurn, event, context) -> ModelResponse: ...
    async def on_tool_execution(self, call_next: ToolExecution, event, context) -> ...: ...
    async def on_llm_call(self, call_next: LLMCall, events, context) -> ModelResponse: ...
```

Default implementation appears to be `return await call_next(...)` (pass-through).

### Chain Construction (`agent.py: _execute()`)

The chain construction likely works as follows (read from PR diff, pending
confirmation that this is the final form):

```python
for m in reversed(list(chain(self._middleware, additional_middleware))):
    mw = m(event, context)                         # factory call
    agent_turn = partial(mw.on_turn, agent_turn)   # wrap
    llm_call   = partial(mw.on_llm_call, llm_call) # wrap
```

Tool execution chains appear to be built separately per `FunctionTool.register()`.

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

veronica-core components mapped to AG2.1 middleware hooks. These mappings
are hypothetical -- each needs validation against the actual #2439 API.

| veronica-core Component | AG2.1 Hook | before/after | Implementation |
|---|---|---|---|
| `SafeModeHook` | `on_turn` | before call_next | If SAFE_MODE active, block without calling call_next. **Open: return error ModelResponse or raise exception?** (see Section 3 note on error signaling) |
| `CircuitBreaker` | `on_turn` | before + after | Before: `check()` -- if OPEN, skip call_next. After: `record_success()` or `record_failure()` based on result. HALF_OPEN single-request gate. |
| `BudgetEnforcer` | `on_llm_call` | before + after | Before: `check()` with estimated cost (**cost estimation approach TBD** -- model name + input tokens or external cost map; the 0.01 hardcode is a placeholder). After: `spend()` with actual cost (**extraction path from ModelResponse unconfirmed**). |
| `AgentStepGuard` | `on_turn` | before | `step()` before call_next. Exceeded -> return partial result. |
| `SemanticLoopGuard` | `on_turn` | after | Feed response text to `record()`. **Requires text extraction from ModelResponse (field structure unconfirmed).** If loop detected, signal halt. |
| `PolicyEngine` (shell/network) | `on_tool_execution` | before | Evaluate tool_name + arguments against shell/network/file rules. DENY -> skip call_next. **ToolCall attribute API unconfirmed -- likely `.name` but arguments extraction pending.** |
| `ShieldPipeline` | `on_llm_call` | before | `before_llm_call()` with ToolCallContext. HALT -> skip call_next. |
| `AdaptiveBudget` | `on_llm_call` | after | Burn rate anomaly detection post-call. Adjusts remaining budget dynamically. |
| `MemoryGovernor` | `on_tool_execution` | before | For memory-related tools, evaluate `MemoryOperation` against governance rules. DENY/QUARANTINE -> block. |
| `ExecutionContext` | `on_turn` | before + after | Chain-level wrapper: timeout enforcement, cost accumulation, execution graph node tracking. |
| `SecretMasker` | `on_llm_call` | before + after | Before: mask secrets in prompt content. After: mask secrets in response content. **Requires BaseEvent content access API (unconfirmed).** |

### Hook Selection Rationale

- **`on_turn`** for cross-cutting concerns (safe mode, circuit breaker, step limit, loop detection) -- these apply to the entire agent turn regardless of how many LLM calls or tool executions occur within it.
- **`on_llm_call`** for cost-aware controls (budget, adaptive budget, shield pipeline, secret masking) -- these need access to the LLM request/response.
- **`on_tool_execution`** for tool-specific policy (shell commands, network access, memory operations) -- these inspect tool name and arguments.

---

## 3. Interface Design

### VeronicaMiddleware (MiddlewareFactory)

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from veronica_core import (
    BudgetEnforcer,
    CircuitBreaker,
    ExecutionContext,
    PolicyEngine,
    SemanticLoopGuard,
)
from veronica_core.agent_guard import AgentStepGuard
from veronica_core.runtime_policy import PolicyContext
from veronica_core.security.masking import SecretMasker
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
    masker: Optional[SecretMasker] = None


class VeronicaMiddleware:
    """AG2.1 MiddlewareFactory for veronica-core governance.

    Implements the MiddlewareFactory protocol (pending confirmation
    that the protocol shape in #2439 is stable).
    """

    def __init__(self, config: VeronicaGovernanceConfig) -> None:
        self._config = config

    def __call__(self, event: Any, context: Any) -> "_VeronicaBaseMiddleware":
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
            # Open question: raise VeronicaHalt or return error ModelResponse?
            # Option A: raise VeronicaHalt("SAFE_MODE active")
            #   Pro: simple, caller sees exception
            #   Con: may not compose well with other middleware expecting a return value
            # Option B: return ModelResponse(content="[GOVERNANCE HALT] SAFE_MODE active")
            #   Pro: composable, other middleware sees a response
            #   Con: requires ModelResponse constructor knowledge (unconfirmed)
            # PoC will test both approaches to determine which AG2.1 handles better.
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
            # Assumes ModelResponse has a .content attribute (unconfirmed)
            text = _extract_text(result)
            loop_decision = cfg.semantic_guard.record(text)
            if not loop_decision.allowed:
                raise VeronicaHalt(f"Loop detected: {loop_decision.reason}")

        return result

    async def on_llm_call(self, call_next, events, context):
        cfg = self._config

        # 1. Secret masking (before)
        if cfg.masker:
            # Requires BaseEvent content access -- structure unconfirmed.
            # Assuming events have a text representation that can be masked.
            # This may need to be event-type-specific once the API stabilizes.
            pass  # TODO: implement after BaseEvent API confirmed

        # 2. Budget check (before)
        if cfg.budget:
            # Cost estimation: model name + input token count -> USD estimate.
            # Placeholder approach below. Production implementation should use
            # LiteLLM's model_cost_map or a similar lookup when available.
            # The 0.01 placeholder is intentionally conservative -- overestimates
            # to avoid budget overrun at the cost of early termination.
            estimated_cost = _estimate_cost(events, context)
            decision = cfg.budget.check(PolicyContext(cost_usd=estimated_cost))
            if not decision.allowed:
                raise VeronicaHalt(f"Budget exceeded: {decision.reason}")

        # 3. Execute LLM call
        result = await call_next(events, context)

        # 4. Budget spend (after -- actual cost)
        if cfg.budget:
            # Cost extraction path from ModelResponse is unconfirmed.
            # Possible sources: result.usage.total_tokens, context metadata,
            # or a post-hoc callback. PoC will test available paths.
            cost = _extract_cost(result, context)
            if cost > 0:
                cfg.budget.spend(cost)

        # 5. Secret masking (after)
        if cfg.masker:
            # Same concern as before -- requires ModelResponse content access.
            pass  # TODO: implement after ModelResponse API confirmed

        return result

    async def on_tool_execution(self, call_next, event, context):
        cfg = self._config

        # 1. Policy engine check
        if cfg.policy_engine:
            # ToolCall likely has .name attribute (seen in #2439 diff)
            # Argument extraction API is unconfirmed
            tool_name = event.name if hasattr(event, "name") else str(event)
            decision = cfg.policy_engine.evaluate_shell([tool_name])
            if not decision.allowed:
                raise VeronicaHalt(f"Tool blocked: {decision.reason}")

        return await call_next(event, context)
```

### Helper Functions

```python
def _extract_text(result: Any) -> str:
    """Extract text content from ModelResponse for loop detection.

    Assumes ModelResponse has a .content attribute (unconfirmed).
    Fallback to str() if attribute is missing.
    """
    if hasattr(result, "content"):
        return str(result.content)
    return str(result)


def _estimate_cost(events: Any, context: Any) -> float:
    """Estimate LLM call cost before execution.

    Production implementation should use model name + input token count
    with a cost-per-token lookup (e.g. LiteLLM model_cost_map).
    This placeholder returns a conservative fixed estimate.
    """
    # TODO: extract model name from context, look up cost-per-token
    return 0.001  # conservative placeholder


def _extract_cost(result: Any, context: Any) -> float:
    """Extract actual cost from LLM result after execution.

    Possible extraction paths (all unconfirmed):
    1. result.usage.total_tokens * cost_per_token
    2. Context metadata set by AG2.1 after LLM call
    3. External callback / event

    PoC will test which path is available.
    """
    if hasattr(result, "usage") and result.usage:
        total = getattr(result.usage, "total_tokens", 0)
        # Rough estimate -- production should use model-specific rates
        return total * 0.000003
    return 0.0
```

---

## 4. GroupChat Integration

> Note: GroupChat integration is out of scope for the initial PoC.
> These are design hypotheses for a later phase.

### select_speaker Governance

AG2.1 beta likely replaces GroupChat's `select_speaker` with the middleware
chain. If so, governance could hook into speaker selection via `on_turn`:

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
        eligible = []
        for name, budget in self._budgets.items():
            if budget.is_exceeded:
                continue
            breaker = self._breakers.get(name)
            if breaker and breaker.state.name == "OPEN":
                continue
            eligible.append(name)

        # Approach 1: inject into context.variables
        # Caveat: Context may be immutable. If so, an alternative is to
        # filter the event itself (e.g. remove ineligible agents from a
        # candidate list field) before passing to call_next.
        # Both approaches need validation against #2439's Context contract.
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
GroupChat candidate filtering. In AG2.1, this would likely integrate as:

1. `AgentEligibilityPolicy` evaluates eligibility (existing PR #2459 logic)
2. `SpeakerGovernanceMiddleware` enforces budget/circuit constraints (new)
3. Both filter the candidate set before speaker selection

These should compose naturally: eligibility is a policy decision, budget/circuit
are governance decisions. Both reduce the candidate set independently.

---

## 5. Minimal PoC Scope

Three scenarios for the initial validation (see external PoC README for
the streamlined version):

### Scenario 1: Budget Exhaustion Stops Agent

```python
from autogen.beta import Agent
from veronica_core import BudgetEnforcer
from veronica_core.adapters.ag2_beta import VeronicaMiddleware, VeronicaGovernanceConfig

budget = BudgetEnforcer(limit_usd=0.05)
governance = VeronicaMiddleware(VeronicaGovernanceConfig(budget=budget))

agent = Agent(name="worker", middleware=[governance])

for i in range(100):
    try:
        reply = await agent.ask(f"Task {i}")
    except VeronicaHalt:
        print(f"Stopped at task {i}: ${budget.spent_usd:.4f} spent")
        break
```

### Scenario 2: Tool Policy Denial

```python
from veronica_core.security.policy_engine import PolicyEngine

policy = PolicyEngine()
governance = VeronicaMiddleware(VeronicaGovernanceConfig(policy_engine=policy))

agent = Agent(name="coder", middleware=[governance])
reply = await agent.ask("Delete all files in /tmp")
# PolicyEngine blocks: shell command 'rm' is denied
```

### Scenario 3: Secret Redaction

```python
from veronica_core.security.masking import SecretMasker

masker = SecretMasker()
governance = VeronicaMiddleware(VeronicaGovernanceConfig(masker=masker))

agent = Agent(name="analyst", middleware=[governance])
reply = await agent.ask("Analyze config with API_KEY=sk-abc123")
# SecretMasker redacts sk-abc123 before it reaches the LLM
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

1. **ModelResponse constructor and fields** -- needed to determine error
   response format when governance blocks a call.

2. **ToolCall.name and argument extraction API** -- needed for policy engine
   tool evaluation.

3. **Context.variables API** -- needed for GroupChat speaker filtering.
   Context may be immutable; if so, filtering the event before call_next
   is an alternative.

4. **Cost/token extraction path** -- how does AG2.1 expose LLM usage after
   a call? Possible: `result.usage`, context metadata, or post-hoc event.

5. **Error signaling: exception vs return value** -- VeronicaHalt (exception)
   is simpler but may not compose with other middleware. Returning an error
   ModelResponse is more composable but requires constructor knowledge.
   PoC will test both.

6. **BaseEvent content access** -- how to read/modify prompt text for secret
   redaction. Depends on event type hierarchy in #2439.

7. **Context mutability** -- if Context is immutable, the speaker governance
   approach of writing to `context.variables` will not work. Alternative:
   filter the event or wrap call_next to modify its inputs.
