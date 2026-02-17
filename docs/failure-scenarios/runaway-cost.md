# Failure Scenario: Runaway LLM Cost

## Scenario

A development team deploys an AI agent that processes customer support tickets. The agent uses GPT-4o for analysis and response generation. On Friday at 6 PM, a bug in the retry logic causes the agent to enter an infinite loop, repeatedly calling the LLM API with the same failed request.

## Timeline

| Time | Event |
|------|-------|
| Friday 18:00 | Agent encounters malformed ticket. LLM call fails. |
| Friday 18:00 | Retry logic triggers. No backoff. No max attempts. |
| Friday 18:01 | Agent is calling GPT-4o at ~2 requests/second. |
| Friday 18:01 | Each call: ~4K tokens in, ~2K tokens out = ~$0.04/call. |
| Friday 23:59 | 43,200 calls. $1,728 spent. No alerts triggered. |
| Saturday-Sunday | Agent continues. 172,800 more calls over 48 hours. |
| Monday 09:00 | Engineer checks dashboard. $8,640 total. Weekend bill: ~$12,000 with other agents. |

## Financial Impact

| Metric | Value |
|--------|-------|
| Cost per call | ~$0.04 (GPT-4o, 6K tokens) |
| Calls per hour | 7,200 (2/sec) |
| Hours unattended | 63 (Friday 6PM to Monday 9AM) |
| Total calls | 453,600 |
| Total cost | $18,144 |
| Typical weekend budget | $200 |
| Overspend | 90x budget |

## How VERONICA Prevents This

### Budget Enforcement

```python
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.models import Budget

ctx = RuntimeContext()
run = ctx.create_run(budget=Budget(limit_usd=200.0))  # Weekend budget
session = ctx.create_session(run, agent_name="support-agent")

for ticket in tickets:
    with ctx.llm_call(session, model="gpt-4o") as step:
        response = call_openai(ticket)
        step.cost_usd = response.usage.total_cost
        step.tokens_in = response.usage.prompt_tokens
        step.tokens_out = response.usage.completion_tokens

    # Propagate cost and check after every call
    run.budget.used_usd += step.cost_usd
    if ctx.check_budget(run):
        # Run is now HALTED. No more calls possible.
        alert_oncall("Budget exceeded", run)
        break
```

### What Happens at $200

1. `check_budget(run)` detects `used_usd > limit_usd`.
2. Emits `BUDGET_EXCEEDED` event (severity: CRITICAL).
3. Transitions run to `HALTED` state.
4. Returns `True` — caller breaks the loop.
5. Total cost: $200 instead of $18,144.

### Additional Enforcement Layers

Even without the explicit budget check in the loop, VERONICA provides additional enforcement hooks:

- **Loop detection hooks**: The caller can detect repeated failure patterns and invoke `record_loop_detected()` to transition the session to `HALTED`.
- **Degradation control**: The `DegradeController` monitors failure signals (error rate, consecutive failures, budget utilization) and progressively restricts execution: downgrading models, capping tokens, blocking tools, and rejecting non-critical LLM calls.

No single mechanism is sufficient on its own. A retry bug that catches exceptions or a call that always returns a success response can defeat one layer. Multiple independent layers reduce the probability that all are bypassed simultaneously.

## Residual Risk

- If the caller ignores the `HALTED` run state and continues calling the LLM API directly, bypassing `llm_call()` and `check_budget()`, VERONICA has no visibility and cannot enforce. Enforcement requires instrumentation at the call site.
- Cost reporting depends on caller accuracy. If `step.cost_usd` is set to `0.0` or omitted, the budget counter does not advance and enforcement is blind to actual spend. See [THREAT_MODEL.md](../THREAT_MODEL.md#1-budget-bypass-via-false-cost-reporting).
- Between the last `check_budget()` call and the enforcement threshold being crossed, one additional LLM call may execute. The overage is bounded to a single step's cost.

## Lessons

1. **Enforce, don't just alert.** Logging the runaway at $200 is necessary but not sufficient. Logs do not halt execution. Budget enforcement does. Use both.
2. **Budget limits should be per-run, not only monthly.** A monthly hard limit of $10,000 does not prevent a $12,000 weekend. Granular per-run budgets match the actual risk boundary.
3. **Defense in depth.** Budget enforcement, loop detection hooks, and degradation control each address different failure modes. Any single mechanism can be circumvented by a sufficiently unusual failure pattern.
4. **Fail closed.** When VERONICA halts a run, execution stops. The caller must make an explicit decision to create a new run and continue. There is no automatic resumption. This is intentional: a human or a supervisory system should evaluate why the limit was reached before authorizing further spend.
