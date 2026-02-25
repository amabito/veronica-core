# VERONICA Core + CrewAI

Wrap any CrewAI `crew.kickoff()` call with VERONICA circuit-breaker and
safe-mode protection -- no call-site changes beyond the guard function.

## What it does

`VeronicaIntegration` + `CircuitBreaker` form a two-layer guard around
`crew.kickoff()`:

| Layer | Primitive | Blocks when |
|-------|-----------|-------------|
| Global halt | `VeronicaState.SAFE_MODE` | Operator activates emergency stop |
| Circuit breaker | `CircuitBreaker` | Consecutive failures reach threshold |
| Per-entity cooldown | `VeronicaIntegration` | Entity-specific fail count reached |

## Quick start

```python
from veronica_core import CircuitBreaker, MemoryBackend, VeronicaIntegration, VeronicaState
from veronica_core.runtime_policy import PolicyContext

veronica = VeronicaIntegration(
    cooldown_fails=3,
    cooldown_seconds=60,
    backend=MemoryBackend(),   # swap for JSONBackend() to persist state
)
breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)

def run_crew(crew, inputs):
    # Guard 1: global safe mode
    if veronica.state.current_state == VeronicaState.SAFE_MODE:
        raise RuntimeError("System halted -- reset required")

    # Guard 2: circuit breaker
    decision = breaker.check(PolicyContext())
    if not decision.allowed:
        raise RuntimeError(f"Circuit open: {decision.reason}")

    # Execute
    try:
        result = crew.kickoff(inputs)
        veronica.record_pass("my_crew")
        breaker.record_success()
        return result
    except Exception:
        veronica.record_fail("my_crew")
        breaker.record_failure()
        raise
```

## Run the demo

```bash
# From the project root
uv run python examples/integrations/crewai/example.py
```

The demo uses a stub Crew -- no API key required.

## Demos included

1. **Circuit breaker** -- threshold=3; 4th call is blocked without starting the crew
2. **SAFE_MODE halt** -- operator activates emergency stop; all kickoffs blocked
3. **Manual reset** -- `breaker.reset()` resumes execution after an open circuit

## Requirements

No extra packages needed. The demo provides a `_StubCrew` class that
simulates `crewai.Crew` without an API key or internet access.

To use with a real CrewAI crew, replace `_StubCrew` with your `Crew` instance:

```python
from crewai import Crew, Agent, Task

crew = Crew(agents=[...], tasks=[...])
result = run_crew(crew, inputs={"topic": "AI safety"})
```
