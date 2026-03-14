# RFC: Runtime Containment Extension Points for AG2

## Problem

AG2 has observability (OTel tracing, `gather_usage_summary`) and message-level safety (`SafeguardEnforcer`, `MessageTokenLimiter`), but no runtime enforcement hooks that let external libraries intercept agent execution at the right granularity.

Concretely, these are hard to do today without monkey-patching:

| Need | Current AG2 answer | Gap |
|------|-------------------|-----|
| Stop an agent after N consecutive failures | None | No post-reply observation hook |
| Block an LLM call when budget is exhausted | `safeguard_llm_inputs` (message filter) | No cost-aware pre-call gate |
| Exclude a broken agent from GroupChat selection | None | No eligibility filter on speaker selection |
| Hard cost ceiling with automatic stop | `gather_usage_summary` (post-hoc) | No pre-call enforcement |
| Graceful degradation (reduce model tier) | None | No decision-point middleware |

`register_reply` covers the "before" side but has no "after" -- you can't observe the result of `generate_reply` to drive state transitions (circuit breaker, cost tracking, retry counting).

## Proposed Extension Points

Three protocols, each targeting a different layer of the agent lifecycle.

### 1. ReplyInterceptor -- agent reply lifecycle

Wraps `generate_reply()` with before/after hooks. This is where circuit breakers, step counters, and retry limiters live.

```python
from typing import Any, Optional, Protocol

class ReplyInterceptor(Protocol):
    def before_reply(
        self,
        agent: ConversableAgent,
        messages: list[dict],
        sender: Optional[Agent],
    ) -> Optional[Any]:
        """Return a value to short-circuit generate_reply, or None to proceed."""
        ...

    def after_reply(
        self,
        agent: ConversableAgent,
        reply: Any,
        messages: list[dict],
        sender: Optional[Agent],
    ) -> None:
        """Observe the reply. Record success/failure, update counters, emit events."""
        ...
```

**Use cases:**
- Circuit breaker: `before_reply` checks state (OPEN → short-circuit), `after_reply` records success/failure
- Step counter: `after_reply` increments step count, `before_reply` denies when limit reached
- Retry limiter: `after_reply` tracks consecutive failures, `before_reply` enforces cooldown
- OTel instrumentation: `after_reply` emits span events with cost/token metadata

**Why not `register_reply`:** It only fires before the reply. Circuit breaker state transitions require the result -- `record_success()` vs `record_failure()` depends on whether the reply was `None`.

### 2. LLMCallMiddleware -- LLM client layer

Intercepts the actual LLM API call (below `generate_reply`, above the HTTP request). This is where token budgets and cost enforcement live.

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class LLMCallContext:
    model: str
    messages: list[dict]
    agent_name: str
    request_id: str

class LLMCallMiddleware(Protocol):
    def before_llm_call(self, ctx: LLMCallContext) -> Decision:
        """ALLOW, DEGRADE, or HALT before the LLM request is sent."""
        ...

    def after_llm_call(self, ctx: LLMCallContext) -> None:
        """Record token usage, cost, latency after the response arrives."""
        ...
```

`Decision` is an enum: `ALLOW` (proceed), `DEGRADE` (proceed with cheaper model/fewer tokens), `HALT` (block the call).

**Use cases:**
- Token budget: `before_llm_call` checks remaining budget, `after_llm_call` records actual usage
- Cost ceiling: `before_llm_call` compares estimated cost against remaining budget
- Degradation ladder: `before_llm_call` returns `DEGRADE` with fallback model when budget is low
- Rate limiting: `before_llm_call` enforces per-agent or global call rate

**Where this hooks in:** Ideally inside the LLM client wrapper that AG2 uses to call OpenAI/Anthropic/etc. The middleware sees the actual model and message payload, not the agent-level abstraction.

### 3. AgentEligibilityPolicy -- speaker selection layer

Filters candidates before the GroupChatManager selects the next speaker. This is where health-aware orchestration lives.

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class SelectionContext:
    round: int
    last_speaker: str | None
    participants: list[str]

class AgentEligibilityPolicy(Protocol):
    def is_eligible(
        self, agent: ConversableAgent, ctx: SelectionContext
    ) -> bool:
        """Return False to remove agent from the candidate set."""
        ...
```

`SelectionContext` is intentionally minimal -- round index, last speaker, participant names -- rather than the full `GroupChat` object. Keeps the policy's surface area small.

**Use cases:**
- Circuit breaker: breaker OPEN → `is_eligible` returns `False`, agent drops out of candidate set
- Cost-based exclusion: agent's cumulative cost exceeds per-agent budget → not eligible
- Cooldown: agent was selected in the last N rounds → temporarily ineligible
- Description mutation as complement: mark ineligible agents' descriptions (e.g. `"[DO NOT CALL - FAILED]"`) as a soft signal for LLM-based selection, alongside the deterministic eligibility filter

**Related: `None` reply semantics.** Currently a `None` reply in group chat ends the run. A mode like `on_agent_failure="try_another"` would let the manager retry with a different agent, allowing circuit breakers to accumulate failures naturally before tripping.

## How These Layers Interact

```
GroupChatManager.select_speaker()
  │
  ├─ AgentEligibilityPolicy.is_eligible()     ← Layer 3
  │   (filter candidates before selection)
  │
  ▼
agent.generate_reply()
  │
  ├─ ReplyInterceptor.before_reply()           ← Layer 1
  │   (circuit breaker check, step limit)
  │
  ├─ LLM client call
  │   ├─ LLMCallMiddleware.before_llm_call()   ← Layer 2
  │   │   (budget check, degradation)
  │   │
  │   ├─ actual API request
  │   │
  │   └─ LLMCallMiddleware.after_llm_call()    ← Layer 2
  │       (record tokens, cost)
  │
  └─ ReplyInterceptor.after_reply()            ← Layer 1
      (record success/failure, update counters)
```

Each layer is independent and opt-in. An application can use any combination -- just a circuit breaker, just a cost ceiling, or all three together.

## Reference Implementation

[veronica-core](https://github.com/amabito/veronica-core) (v1.8.3, Apache-2.0, 2346 tests, 92% coverage) implements all three patterns today via monkey-patching:

- `CircuitBreakerCapability` -- maps to ReplyInterceptor (wraps `generate_reply` with before/after)
- `TokenBudgetHook` -- maps to LLMCallMiddleware (pre-call budget check, post-call usage recording)
- GroupChat eligibility -- proposed, maps to AgentEligibilityPolicy

PR [#2430](https://github.com/ag2ai/ag2/pull/2430) demonstrates the circuit breaker integration as a notebook.

The monkey-patching works but is fragile across AG2 versions. Native hooks would make this a supported integration surface.

## What This Doesn't Cover

- Message content filtering (already handled by `SafeguardEnforcer`)
- Prompt injection detection (orthogonal concern)
- Authentication/authorization (infrastructure layer)
- Specific policy logic (that's the implementation's job, not the hook's)

## Compatibility

- All three protocols are opt-in with no-op defaults
- Zero breaking changes to existing AG2 APIs
- Existing `register_reply`, `safeguard_llm_inputs`, and `update_agent_state` hooks continue working
- Libraries implement the protocols; AG2 provides the extension points

## Open Questions

1. **Hook registration API** -- should these be registered per-agent (like `register_reply`), per-GroupChat, or globally? Per-agent is most flexible but verbose for large groups.

2. **ReplyInterceptor vs extending `register_reply`** -- would adding an `after_reply` callback to the existing `register_reply` mechanism be preferable to a new protocol? That's a smaller change but mixes two concerns (reply generation vs observation).

3. **LLMCallMiddleware placement** -- AG2's LLM client layer is evolving. Where exactly should the middleware hook in -- `OpenAIWrapper`, the new `LLMConfig`-based client, or a shared abstraction?

4. **AgentEligibilityPolicy scope** -- should this live on `GroupChat`, `GroupChatManager`, or as a standalone filter passed to `run_group_chat`?

5. **`None` reply handling in group chat** -- should `on_agent_failure` be a GroupChat parameter, a pattern-level config, or handled differently?
