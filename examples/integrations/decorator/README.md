# VERONICA Core + `@veronica_guard` Decorator

The fastest integration path: one decorator, zero framework dependencies.

## Quick start

```python
from veronica_core import veronica_guard
from veronica_core.inject import VeronicaHalt

@veronica_guard(max_cost_usd=1.0, max_steps=20)
def run_agent(prompt: str) -> str:
    # existing LLM call — unchanged
    return llm.complete(prompt)

try:
    result = run_agent("What is VERONICA?")
except VeronicaHalt as e:
    print(f"Session halted: {e}")
```

## What it enforces

| Limit | Parameter | Default |
|-------|-----------|---------|
| Cost ceiling | `max_cost_usd` | `1.0` |
| Step count | `max_steps` | `25` |
| Retry count | `max_retries_total` | `3` |

Limits are evaluated **before** each invocation. Once a limit is reached,
every subsequent call raises `VeronicaHalt` (or returns a `PolicyDecision`
if `return_decision=True`).

## Live introspection

```python
@veronica_guard(max_cost_usd=5.0, max_steps=50)
def agent(prompt: str) -> str: ...

agent("hello")

# _container is attached to the wrapper
c = agent._container
print(c.step_guard.current_step)  # steps consumed
print(c.budget.spent_usd)     # dollars spent
```

## Graceful denial (no exceptions)

```python
@veronica_guard(max_cost_usd=1.0, max_steps=5, return_decision=True)
def agent(prompt: str):
    ...

outcome = agent("hello")
if hasattr(outcome, "allowed") and not outcome.allowed:
    # PolicyDecision returned instead of exception
    print(f"Denied: {outcome.reason}")
```

## Nested guards

Each decorated function gets its **own independent** `AIcontainer`.
Limits on `summarize()` do not affect `classify()`:

```python
@veronica_guard(max_cost_usd=0.50, max_steps=5)
def summarize(text: str) -> str: ...

@veronica_guard(max_cost_usd=2.00, max_steps=20)
def classify(text: str) -> str: ...
```

## Run the demo

```bash
uv run python examples/integrations/decorator/example.py
```

No API key or external dependency required.

## Demos included

1. **Basic guard** — 3-step cap; 4th call raises `VeronicaHalt`
2. **Nested guards** — independent limits per function
3. **Introspection** — read live metrics via `_container`
4. **Graceful denial** — `return_decision=True` avoids exception-based flow
