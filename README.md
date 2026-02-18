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
from veronica_core import VeronicaIntegration, get_veronica_integration
from veronica_core import ShieldConfig, BudgetWindowHook, TokenBudgetHook
from veronica_core.shield import SafetyEvent, Decision

# Configure shield with budget window (10 calls per minute)
config = ShieldConfig()
config.budget_window.enabled = True
config.budget_window.max_calls = 10
config.budget_window.window_seconds = 60.0

# Every LLM call passes through VERONICA
integration = get_veronica_integration(shield=config)
```

If the agent spirals, the hook returns `Decision.HALT` **before** the call reaches the provider.

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

## Runaway Loop Demo (veronica_core)

```bash
pip install -e .
python examples/budget_degrade_demo.py
```

```python
from veronica_core import ShieldConfig
from veronica_core.shield import BudgetWindowHook, SafetyEvent, Decision

# 5 calls per minute hard limit
hook = BudgetWindowHook(max_calls=5, window_seconds=60.0)

call_count = 0
while True:
    decision = hook.check_call()
    if decision == Decision.HALT:
        print(f"HALTED after {call_count} calls")
        break
    call_count += 1
    print(f"Call {call_count}: {decision.name}")
    hook.record_call()
```

```
Call 1: ALLOW
Call 2: ALLOW
Call 3: ALLOW
Call 4: ALLOW (DEGRADE zone)
Call 5: ALLOW
HALTED after 5 calls
```

Without VERONICA: infinite retries, $12,000 bill.
With VERONICA: 5 calls, hard stop, zero damage.

---

## Full Demo (Adaptive Budget)

```bash
python examples/adaptive_demo.py
```

| Demo | What happens |
|------|-------------|
| Basic tighten/loosen | Budget exceeded events reduce ceiling; no events loosen it back |
| Cooldown window | Rapid adjustments are rate-limited |
| Direction lock | Prevents premature loosening after tighten |
| Anomaly spike | Sudden event burst triggers temporary ceiling reduction |
| Export/import state | Full control state round-trip for observability |
| Event audit trail | All adjustment decisions recorded as SafetyEvents |

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

VERONICA sits between your agent and the model. Hook-based pipeline.

```python
from veronica_core import ShieldConfig, VeronicaIntegration
from veronica_core.shield import (
    BudgetWindowHook,
    TokenBudgetHook,
    AdaptiveBudgetHook,
)

# Configure all shields declaratively
config = ShieldConfig()
config.budget_window.enabled = True
config.budget_window.max_calls = 100
config.token_budget.enabled = True
config.token_budget.max_output_tokens = 50_000

# Or load from YAML/JSON
config = ShieldConfig.from_yaml("shield.yaml")

# Wire into your agent
integration = VeronicaIntegration(shield=config)
```

Drop-in enforcement layer. All features opt-in and disabled by default.

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

**v0.8.x**
- OpenTelemetry export (opt-in SafetyEvent export)
- Multi-agent coordination (shared budget pools)
- Webhook notifications on HALT/DEGRADE

**v0.9.x**
- Redis-backed distributed budget enforcement
- Middleware mode (ASGI/WSGI)
- Dashboard for real-time shield status

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
