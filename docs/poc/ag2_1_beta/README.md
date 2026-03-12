# AG2.1 Beta Middleware -- Governance PoC

## What This Demonstrates

Testing whether AG2.1 beta's middleware chain (#2439) works for plugging
in runtime governance from outside. Three scenarios using
[veronica-core](https://github.com/amabito/veronica-core) to poke at
the hook contract.

## Quick Start

```python
from autogen.beta import Agent
from veronica_core import BudgetEnforcer
from veronica_core.adapters.ag2_beta import VeronicaMiddleware, VeronicaGovernanceConfig

budget = BudgetEnforcer(limit_usd=0.50)

agent = Agent(
    name="worker",
    middleware=[VeronicaMiddleware(VeronicaGovernanceConfig(budget=budget))],
)
reply = await agent.ask("Summarize the report")
```

## Middleware Hooks Used

Based on the current #2439 draft, `BaseMiddleware` exposes three hooks:

| Hook | Signature | Scope |
|------|-----------|-------|
| `on_turn` | `(call_next, event, context) -> ModelResponse` | Full agent turn including tool loop |
| `on_llm_call` | `(call_next, events, context) -> ModelResponse` | Single LLM API call |
| `on_tool_execution` | `(call_next, event, context) -> ToolResult` | Single tool invocation |

Each hook wraps `call_next` -- the middleware can inspect inputs before the
call, skip it entirely, or inspect the result after. Chains are built via
`partial()` composition in `Agent._execute()`.

## PoC Scenarios

### 1. Shared Budget Enforcement

`on_llm_call` checks remaining budget before the LLM call and records
actual cost after. When cumulative spend exceeds the limit, subsequent
calls are blocked without invoking `call_next`.

```python
from veronica_core import BudgetEnforcer

budget = BudgetEnforcer(limit_usd=1.0)
# on_llm_call before: budget.check(PolicyContext(cost_usd=estimated))
# on_llm_call after:  budget.spend(actual_cost)
# budget.is_exceeded -> skip call_next on next invocation
```

Cost extraction from the LLM response depends on how #2439 exposes
token usage (pending confirmation -- see Open Questions).

### 2. Tool Policy Denial

`on_tool_execution` evaluates the tool name against policy rules before
calling `call_next`. Denied tools return an error without execution.

```python
from veronica_core.security.policy_engine import PolicyEngine

policy = PolicyEngine()  # example policy configuration
# on_tool_execution before: policy.evaluate(PolicyContext(action=tool_name))
# if verdict == DENY -> return error, skip call_next
```

Argument extraction from `ToolCall` likely uses a `.name` attribute,
pending confirmation of the #2439 API surface.

### 3. Secret Redaction

`on_llm_call` masks secrets in prompts before the LLM call and in
responses after. Uses pattern-based detection (API keys, tokens, credentials).

```python
from veronica_core.security.masking import SecretMasker

masker = SecretMasker()
# on_llm_call before: mask prompt content via masker.mask(text)
# on_llm_call after:  mask response content via masker.mask(text)
```

Event content extraction depends on the `BaseEvent` structure in #2439
(pending confirmation).

## Intentionally Out of Scope (First Cut)

The following governance behaviors are excluded from the initial PoC:

- Circuit breaker
- Semantic loop detection
- Safe mode / kill-switch
- GroupChat speaker selection
- Fan-out budget allocation
- Distributed backend (Redis)
- #2459 integration

These are implementable on the same middleware chain, but the first cut
prioritizes validating the hook contract with the three simplest scenarios
above.

## Confirmed from #2439 Draft

- **ModelResponse**: `ModelResponse(message=ModelMessage(content="..."))`
  for governance blocks. `usage: dict[str, float]` for cost tracking.
- **ToolCall API**: `event.name` + `event.serialized_arguments` (dict).
- **Cost extraction**: `response.usage` exposes token counts.
- **Error signaling**: `ToolError(parent_id=..., name=..., error=...)`
  composes with the chain without exceptions.
- **BaseEvent content**: `ModelRequest.content` for prompt access.

These are based on the current #2439 draft and may change before merge.

## Links

- [AG2.1 beta #2439](https://github.com/ag2ai/ag2/pull/2439) -- middleware chain design
- [AgentEligibilityPolicy #2459](https://github.com/ag2ai/ag2/pull/2459)
- [CircuitBreakerCapability #2430](https://github.com/ag2ai/ag2/pull/2430) -- merged
- [veronica-core](https://github.com/amabito/veronica-core)
