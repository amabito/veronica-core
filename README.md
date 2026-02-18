# VERONICA

## LLM agents don't fail because of prompts. They fail because nothing stops them.

You don't lose money because your model hallucinated.
You lose money because it retried itself 3,000 times.

---

## The $12K Weekend Problem

It's Monday morning.

Your agent:
- hit a transient API failure
- retried with exponential backoff
- spawned subcalls
- looped on tool failures
- ignored budget signals

Observability tells you what happened.

**VERONICA makes sure it never happens.**

---

## What VERONICA Actually Does

VERONICA sits between your agent and the model.

It enforces execution safety.

- **Hard budget enforcement** (org / team / user / service)
- **Circuit breaker** on model instability
- **Retry containment**
- **Loop termination**
- **Tool timeout enforcement**
- **Degrade levels** (NORMAL / SOFT / HARD / EMERGENCY)

Not logging.
Not tracing.
**Stopping.**

---

## Quickstart

```python
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.events import EventBus
from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.budget.policy import BudgetPolicy, WindowLimit
from veronica.budget.ledger import BudgetLedger
from veronica.runtime.models import Labels, Budget

# Set a $5 per-minute hard limit
policy = BudgetPolicy(org_limits=WindowLimit(minute_usd=5.0))
enforcer = BudgetEnforcer(policy=policy, ledger=BudgetLedger(), bus=EventBus())
ctx = RuntimeContext(enforcer=enforcer)

run = ctx.create_run(labels=Labels(org="acme"), budget=Budget(limit_usd=5.0))
session = ctx.create_session(run)

# Every LLM call passes through VERONICA
with ctx.llm_call(session, model="gpt-4", labels=Labels(org="acme"), run=run) as step:
    response = call_your_llm(prompt)
    step.cost_usd = response.cost
```

If the agent spirals, `BudgetExceeded` is raised **before** the call reaches the provider.

---

## Ship Readiness (v0.7.0)

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
- [x] PyPI auto-publish on GitHub Release
- [x] Everything is opt-in & non-breaking (default behavior unchanged)

580 tests passing. Minimum production use-case: runaway containment + graceful degrade + auditable events + token budgets + input compression + adaptive ceiling + time-aware scheduling + anomaly detection.

---

## Token Budget + Minimal Response Demo (30 seconds)

```bash
pip install -e .
python examples/token_budget_minimal_demo.py
```

```
--- TokenBudgetHook demo ---
  Tokens used:    0 / 100  -> ALLOW
  Tokens used:   70 / 100  -> ALLOW
  Tokens used:   80 / 100  -> DEGRADE  (80% threshold reached)
  Tokens used:   95 / 100  -> DEGRADE
  Tokens used:  100 / 100  -> HALT  (ceiling reached)

  SafetyEvent: TOKEN_BUDGET_EXCEEDED / DEGRADE / TokenBudgetHook
  SafetyEvent: TOKEN_BUDGET_EXCEEDED / DEGRADE / TokenBudgetHook
  SafetyEvent: TOKEN_BUDGET_EXCEEDED / HALT    / TokenBudgetHook

--- MinimalResponsePolicy demo ---
  [disabled] system message unchanged: You are a helpful assistant.
  [enabled]  system message with constraints injected
```

---

## Input Compression Skeleton Demo (30 seconds)

```bash
pip install -e .
python examples/input_compression_skeleton_demo.py
```

```
--- InputCompressionHook demo ---
  Short input (22 tokens)  -> ALLOW
  Medium input (750 tokens) -> DEGRADE  (compression suggested)
  Large input (1250 tokens)  -> HALT  (input too large)

  Evidence (HALT):
    estimated_tokens: 1250
    input_sha256: c59d3c04...  (raw text NOT stored)
    decision: HALT
```

---

## Budget + Degrade Demo (30 seconds)

```bash
pip install -e .
python examples/budget_degrade_demo.py
```

```
Call  1 / model=gpt-4        -> ALLOW
Call  2 / model=gpt-4        -> ALLOW
Call  3 / model=gpt-4        -> ALLOW
Call  4 / model=gpt-4        -> ALLOW
Call  5 / model=gpt-4        -> DEGRADE (fallback to gpt-3.5-turbo)
Call  6 / model=gpt-3.5-turbo -> HALT
SafetyEvent: BUDGET_WINDOW_EXCEEDED / DEGRADE / BudgetWindowHook
SafetyEvent: BUDGET_WINDOW_EXCEEDED / HALT   / BudgetWindowHook
```

---

## Runaway Loop Demo

```bash
pip install -e .
python examples/runaway_loop_demo.py
```

```python
# examples/runaway_loop_demo.py
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.events import EventBus
from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.budget.policy import BudgetPolicy, WindowLimit
from veronica.budget.ledger import BudgetLedger
from veronica.runtime.models import Labels, Budget

policy = BudgetPolicy(org_limits=WindowLimit(minute_usd=0.05))
enforcer = BudgetEnforcer(policy=policy, ledger=BudgetLedger(), bus=EventBus())
ctx = RuntimeContext(enforcer=enforcer)

labels = Labels(org="demo-org", team="demo-team")
run = ctx.create_run(labels=labels, budget=Budget(limit_usd=0.10))
session = ctx.create_session(run)

call_count = 0
try:
    while True:
        with ctx.llm_call(session, model="gpt-4", labels=labels, run=run) as step:
            call_count += 1
            step.cost_usd = 0.01
            print(f"Call {call_count}: ${step.cost_usd}")
except BudgetExceeded as e:
    print(f"HALTED after {call_count} calls: {e}")
```

```
Call 1: $0.01
Call 2: $0.01
Call 3: $0.01
Call 4: $0.01
Call 5: $0.01
HALTED after 5 calls: Budget exceeded: org/demo-org window=minute used=0.050000 limit=0.050000
```

Without VERONICA: infinite retries, $12,000 bill.
With VERONICA: 5 calls, hard stop, zero damage.

---

## Full Demo (4 Scenarios)

```bash
python -m veronica.demo
```

| Scenario | What happens |
|----------|-------------|
| `retry_cascade` | Failures escalate degrade level; scheduler rejects overflow |
| `budget_burn` | Spend crosses 80 / 90 / 100%; run goes DEGRADED then HALTED |
| `tool_hang` | Tool timeouts trigger blocking; LLM fallback succeeds |
| `runaway_agent` | Admission control queues then rejects excess calls |

Writes structured events to `veronica-demo-events.jsonl`.

---

## Observability vs Enforcement

|                    | Observability Tools | VERONICA |
|--------------------|---------------------|----------|
| Acts when          | After failure       | **Before damage** |
| Prevents cost loss | No                  | **Yes** |
| Stops runaway loop | No                  | **Yes** |
| Circuit breaker    | No                  | **Yes** |
| Hard budget stop   | No                  | **Yes** |

Observability explains the fire.

**VERONICA pulls the fuse.**

---

## Integration

VERONICA wraps any LLM call pattern. Context manager based.

```python
from veronica.runtime.hooks import RuntimeContext
from veronica.control.controller import DegradeController
from veronica.scheduler.scheduler import Scheduler

# Full stack: budget + degrade + scheduler
ctx = RuntimeContext(
    enforcer=enforcer,
    controller=DegradeController(),
    scheduler=Scheduler(),
)

# Wrap your agent loop
for task in agent_tasks:
    try:
        with ctx.llm_call(session, model="gpt-4", labels=labels, run=run) as step:
            result = your_agent.execute(task)
            step.cost_usd = result.cost
    except (BudgetExceeded, DegradedRejected, SchedulerRejected):
        # VERONICA blocked the call. Handle gracefully.
        break
```

Drop-in enforcement layer. No agent code changes required.

---

## Why This Category Matters

As agents become autonomous, retries compound.

A single transient failure can:
- explode cost
- cascade into recursive calls
- bypass soft limits
- create orphan state
- burn through budget at 3 AM with no one watching

This is not a prompt problem.

It is an **execution control** problem.

VERONICA defines the Enforcement Layer category.

---

## Roadmap

**v0.3.x**
- Agent loop detection improvements
- Multi-model policy enforcement
- CLI wrapper for any Python agent

**v0.4.x**
- Docker image
- Middleware mode (ASGI/WSGI)
- OpenTelemetry export (opt-in)
- Redis-backed distributed scheduler

---

## Install

```bash
pip install -e .

# With dev tools
pip install -e ".[dev]"
pytest
```

![CI](https://img.shields.io/badge/tests-580%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

---

### v0.7.0 — Adaptive Budget Stabilization

Adaptive budget control with production-grade stabilization.
[Full engineering doc](docs/adaptive-control.md)

New features:
- **Cooldown window**: minimum interval between adjustments (prevents oscillation)
- **Adjustment smoothing**: per-step cap on multiplier change (gradual convergence)
- **Hard floor/ceiling**: absolute bounds on multiplier
- **Direction lock**: blocks loosen after tighten until exceeded events clear
- **Anomaly tightening**: spike detection with temporary ceiling reduction + auto-recovery
- **Deterministic replay**: export/import control state for observability dashboards

```bash
python examples/adaptive_demo.py
```

---

### v0.4.0 — Execution Shield Foundation

Design and diagrams:
[docs/v0.4.0-technical-artifacts.md](docs/v0.4.0-technical-artifacts.md)

SafeModeHook is optional and disabled by default.
BudgetWindowHook is optional and disabled by default.
DEGRADE support allows model fallback before hard stop.
[Execution Boundary concept](docs/execution-boundary.md)

---

## License

MIT
