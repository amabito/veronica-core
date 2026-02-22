# Degradation Ladder (v0.10.0)

Multi-tier graceful degradation before HALT. Instead of failing immediately when cost approaches the ceiling, VERONICA steps down through cheaper alternatives.

## Tiers (lowest to highest activation)

| Cost Fraction | Tier | Action |
|---|---|---|
| < 80% | ALLOW | Normal operation |
| >= 80% | MODEL_DOWNGRADE | Switch to cheaper model |
| >= 85% | CONTEXT_TRIM | Trim context messages |
| >= 90% | RATE_LIMIT | Add delay between calls |

## Quick Start

```python
from veronica_core.shield.degradation import DegradationLadder, DegradationConfig

ladder = DegradationLadder(DegradationConfig(
    model_map={"gpt-4o": "gpt-4o-mini"},
    rate_limit_ms=2000,
))

decision = ladder.evaluate(
    cost_accumulated=0.85,
    max_cost_usd=1.0,
    current_model="gpt-4o",
)

if decision and decision.degradation_action == "MODEL_DOWNGRADE":
    use_model = decision.fallback_model
elif decision and decision.degradation_action == "RATE_LIMIT":
    ladder.apply_rate_limit(decision)
elif decision and decision.degradation_action == "CONTEXT_TRIM":
    messages = ladder.apply_context_trim(messages)
```

## Custom Trimmer

```python
from veronica_core.shield.degradation import Trimmer

class LastNTrimmer:
    def __init__(self, n: int) -> None:
        self._n = n

    def trim(self, messages: list) -> list:
        return messages[-self._n:]

ladder = DegradationLadder(DegradationConfig(trimmer=LastNTrimmer(10)))
```

## PolicyDecision Helpers

```python
from veronica_core.runtime_policy import allow, deny, model_downgrade, rate_limit_decision

ok = allow()
blocked = deny("budget", reason="over $10 limit")
downgrade = model_downgrade("gpt-4o", "gpt-4o-mini")
throttle = rate_limit_decision(delay_ms=1000)
```
