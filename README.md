# veronica-core

[![CI](https://github.com/amabito/veronica-core/actions/workflows/ci.yml/badge.svg)](https://github.com/amabito/veronica-core/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/veronica-core)](https://pypi.org/project/veronica-core/)
[![Python](https://img.shields.io/pypi/pyversions/veronica-core)](https://pypi.org/project/veronica-core/)

Enforcement hooks for LLM agent runs. Budget limits, concurrency gating, and degradation control. Pure Python, zero dependencies.

```
pip install veronica-core
```

```python
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.models import Budget

ctx = RuntimeContext()
run = ctx.create_run(budget=Budget(limit_usd=5.0))
session = ctx.create_session(run, agent_name="my-agent")

with ctx.llm_call(session, model="gpt-4o") as step:
    step.cost_usd = 0.03

run.budget.used_usd += step.cost_usd  # propagate cost
exceeded = ctx.check_budget(run)       # True if limit exceeded; run transitions to HALTED
```

---

## Architecture

```
                        +------------------+
  Agent code     -----> |                  | -----> LLM APIs
  Tool calls     -----> |    VERONICA      | -----> Tool executors
  Loop iteration -----> |                  | -----> External services
                        +------------------+
                               |
                        What happens here:
                        - Budget checked on every llm_call() when BudgetEnforcer is configured
                        - Concurrency gate evaluated on admission when Scheduler is configured
                        - Degradation level evaluated per call when DegradeController is configured
                        - Step counter incremented per call
                        - State machine transition recorded
                        - Structured event emitted to sinks
```

---

## What it enforces

- **Budget limits** — `Budget(limit_usd=X)` halts the run when cumulative cost exceeds the ceiling. With BudgetEnforcer, calls are blocked before they execute. With manual `check_budget()`, overage is bounded to one step.
- **Admission control** — Scheduler evaluates ALLOW / QUEUE / REJECT on each call based on org and team concurrency limits.
- **Weighted Fair Queue** — Runs are scheduled across orgs and teams with configurable weights. P0 priority is never starved.
- **Degradation control** — Progressive response to failure signals: model downgrade, token cap reduction, tool blocking, and LLM rejection across four severity levels (NORMAL / SOFT / HARD / EMERGENCY).
- **Loop detection hooks** — `record_loop_detected()` transitions the session to HALTED. The caller is responsible for detecting loop patterns and invoking this method.

---

## What it does NOT do

- Content safety filtering (prompt injection detection, topic blocking) — use a dedicated guardrails layer.
- Authentication or authorization — bring your own identity layer.
- Log storage or trace replay — use Langfuse, LangSmith, or any OpenTelemetry-compatible sink.
- Model selection or routing — out of scope.

VERONICA enforces runtime constraints. It does not inspect content.

---

## Failure scenarios

How VERONICA behaves under each failure mode is documented in [`docs/failure-scenarios/`](https://github.com/amabito/veronica-core/tree/main/docs/failure-scenarios).

Starting with: budget exhaustion mid-run ([runaway-cost.md](https://github.com/amabito/veronica-core/blob/main/docs/failure-scenarios/runaway-cost.md)). Additional scenarios are in development.

---

## Threat model

[`docs/THREAT_MODEL.md`](https://github.com/amabito/veronica-core/blob/main/docs/THREAT_MODEL.md) covers the attacker model, trust boundaries, and what VERONICA explicitly does and does not defend against.

---

## API reference

| Method | Description |
|---|---|
| `ctx.create_run(budget=...)` | Creates a Run in RUNNING state with an optional Budget ceiling. |
| `ctx.create_session(run, agent_name=...)` | Creates a Session scoped to a Run. Increments session counter. |
| `ctx.llm_call(session, model=...)` | Context manager. Records a Step. Attach cost and token counts before exit. |
| `ctx.tool_call(session, tool_name=...)` | Context manager. Records a tool Step. |
| `ctx.check_budget(run)` | Evaluates cumulative cost against Budget. Transitions run to HALTED if exceeded. Returns True if halted. |

Labels (`org`, `team`, `service`, `user`, `env`) can be passed to `create_run()` for scheduler routing and event filtering.

---

## State machine

```
RUNNING     -> DEGRADED, HALTED, QUARANTINED, SUCCEEDED, FAILED, CANCELED
DEGRADED    -> RUNNING (recovery), HALTED, QUARANTINED, SUCCEEDED, FAILED, CANCELED
HALTED      -> FAILED, CANCELED
QUARANTINED -> HALTED, FAILED, CANCELED
SUCCEEDED   -> (terminal)
FAILED      -> (terminal)
CANCELED    -> (terminal)
```

The DegradeController drives RUNNING -> DEGRADED. Budget exhaustion drives -> HALTED. Recovery from DEGRADED -> RUNNING is permitted when failure signals clear.

---

## Contributing

See [CONTRIBUTING.md](https://github.com/amabito/veronica-core/blob/main/CONTRIBUTING.md).

Pull requests for new event sinks, scheduler policies, and state backends are welcome.
Bug reports with a minimal reproducible case are always prioritized.

---

## License

MIT
