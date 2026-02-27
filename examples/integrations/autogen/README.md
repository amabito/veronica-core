# VERONICA Core + AG2

AG2 (AutoGen) retries failed agents indefinitely -- VERONICA adds a
per-agent circuit breaker and a system-wide SAFE_MODE to stop runaway
loops before they drain your API budget.

## Quick start -- CircuitBreakerCapability (recommended)

`CircuitBreakerCapability` follows AG2's `AgentCapability` pattern.
Call `add_to_agent()` once; your existing `generate_reply()` call sites
need no changes.

```python
from veronica_core import CircuitBreakerCapability

cap = CircuitBreakerCapability(failure_threshold=3, recovery_timeout=60)
cap.add_to_agent(planner)
cap.add_to_agent(executor)

# Call generate_reply exactly as before -- circuit breaker is transparent:
reply = planner.generate_reply(messages)
```

Each agent gets its own independent `CircuitBreaker` (CLOSED / OPEN /
HALF_OPEN).  After `failure_threshold` consecutive `None` replies the
circuit opens and the agent is skipped for `recovery_timeout` seconds.

### With SAFE_MODE (system-wide halt)

```python
from veronica_core import CircuitBreakerCapability, VeronicaIntegration
from veronica_core.backends import MemoryBackend
from veronica_core.state import VeronicaState

veronica = VeronicaIntegration(backend=MemoryBackend())
cap = CircuitBreakerCapability(failure_threshold=3, veronica=veronica)
cap.add_to_agent(planner)
cap.add_to_agent(executor)

# Block all agents instantly from the orchestrator:
veronica.state.transition(VeronicaState.SAFE_MODE, reason="Cost spike detected")

# Clear once resolved (two-step; SAFE_MODE -> IDLE -> SCREENING):
veronica.state.transition(VeronicaState.IDLE, reason="Manual review passed")
veronica.state.transition(VeronicaState.SCREENING, reason="Resuming")
```

### Inspect circuit state

```python
breaker = cap.get_breaker("planner")
print(breaker.state)          # CircuitState.CLOSED / OPEN / HALF_OPEN
print(breaker.failure_count)  # consecutive failures
```

## Run the demos

```bash
uv run python examples/integrations/autogen/example.py
```

No API key required -- all demos use stub agents.

## Demos included

| Demo | Pattern | What it shows |
|------|---------|---------------|
| `demo_capability_circuit_breaker` | CircuitBreakerCapability | add_to_agent(); circuit opens after failures |
| `demo_capability_safe_mode` | CircuitBreakerCapability | System-wide halt via shared VeronicaIntegration |
| `demo_capability_per_agent` | CircuitBreakerCapability | Two agents, one capability, independent circuits |
| `demo_circuit_breaker` | Wrapper (reference) | guarded_reply(); cooldown activates after failures |
| `demo_safe_mode` | Wrapper (reference) | Orchestrator triggers system-wide halt |
| `demo_per_agent_tracking` | Wrapper (reference) | Healthy agent runs while broken agent cools down |
| `demo_token_budget` | VeronicaIntegration | Shared token ceiling across agent calls |

## Persist state across restarts

Swap `MemoryBackend` for `JSONBackend`:

```python
from veronica_core.backends import JSONBackend

veronica = VeronicaIntegration(backend=JSONBackend(path="veronica_state.json"))
```

## Reference: original wrapper pattern

The wrapper pattern (demos 1-3) requires changing call sites from
`agent.generate_reply(messages)` to `guarded_reply(agent, messages)`.
`CircuitBreakerCapability` removes that requirement.

```python
from veronica_core import VeronicaIntegration
from veronica_core.backends import MemoryBackend
from veronica_core.state import VeronicaState

veronica = VeronicaIntegration(
    cooldown_fails=3,
    cooldown_seconds=60,
    backend=MemoryBackend(),
)

def guarded_reply(agent, messages):
    if veronica.state.current_state == VeronicaState.SAFE_MODE:
        return None
    if veronica.is_in_cooldown(agent.name):
        return None
    reply = agent.generate_reply(messages)
    if reply is None:
        veronica.record_fail(agent.name)
    else:
        veronica.record_pass(agent.name)
    return reply
```

## Requirements

```
pip install veronica-core
pip install autogen      # optional -- not required for the demos
```
