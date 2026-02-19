# VERONICA Cookbook

Copy-paste recipes for common enforcement patterns.
All features are opt-in and disabled by default. No prompt content is stored.

---

## Recipe 1: Weekend / off-hour tightening

Reduce budget ceiling outside business hours. Multiplier is applied by the caller
(VERONICA reports the multiplier; your orchestrator uses it).

```python
from veronica_core import TimeAwarePolicy

policy = TimeAwarePolicy(
    weekend_multiplier=0.85,     # 15% lower on weekends
    offhour_multiplier=0.90,     # 10% lower outside 09:00-18:00 UTC
)

result = policy.evaluate(ctx)
effective_ceiling = int(base_ceiling * result.multiplier)
# result.classification: "business_hours" | "offhour" | "weekend" | "weekend_offhour"
```

ShieldConfig equivalent:

```python
config.time_aware_policy.enabled = True
config.time_aware_policy.weekend_multiplier = 0.85
config.time_aware_policy.offhour_multiplier = 0.90
config.time_aware_policy.work_start_hour = 9
config.time_aware_policy.work_end_hour = 18
```

---

## Recipe 2: Degrade before halt (model fallback)

DEGRADE zone starts at 80% of `max_calls`. The hook returns `Decision.DEGRADE`;
your code does the actual model switch.

```python
from veronica_core import BudgetWindowHook
from veronica_core.shield import ShieldPipeline, ToolCallContext, Decision

hook = BudgetWindowHook(
    max_calls=100,
    window_seconds=600.0,        # 10-minute rolling window
    degrade_threshold=0.8,       # DEGRADE at 80 calls, HALT at 100
    degrade_map={"gpt-4o": "gpt-4o-mini"},  # informational only
)

pipe = ShieldPipeline(pre_dispatch=hook)

ctx = ToolCallContext(request_id="req-1", tool_name="llm", model="gpt-4o")
decision = pipe.before_llm_call(ctx)

if decision == Decision.DEGRADE:
    ctx = ToolCallContext(request_id="req-1", tool_name="llm", model="gpt-4o-mini")
elif decision == Decision.HALT:
    raise RuntimeError("Budget exhausted")
```

---

## Recipe 3: Hard call ceiling (BudgetWindow)

Standalone rolling-window limiter without a pipeline.

```python
from veronica_core import BudgetWindowHook
from veronica_core.shield import Decision, ToolCallContext

hook = BudgetWindowHook(max_calls=50, window_seconds=300.0)

i = 0
while work_remains():
    i += 1
    ctx = ToolCallContext(request_id=f"req-{i}", tool_name="llm")
    decision = hook.before_llm_call(ctx)
    if decision == Decision.HALT:
        break
    do_work()  # ALLOW and DEGRADE calls are auto-recorded
```

`before_llm_call` returns `None` (ALLOW), `Decision.DEGRADE`, or `Decision.HALT`.
ALLOW and DEGRADE calls are automatically counted.

---

## Recipe 4: Token ceiling (TokenBudget)

Cumulative token tracking with DEGRADE zone.

```python
from veronica_core import TokenBudgetHook
from veronica_core.shield import Decision, ToolCallContext

hook = TokenBudgetHook(
    max_output_tokens=50_000,
    degrade_threshold=0.8,       # DEGRADE at 40K, HALT at 50K
)

# Before each LLM call: check budget
ctx = ToolCallContext(request_id="req-1", tool_name="llm")
decision = hook.before_llm_call(ctx)
if decision == Decision.HALT:
    raise RuntimeError("Token budget exhausted")

# After each LLM response: record actual usage
hook.record_usage(output_tokens=1200)
```

Track total tokens (input + output) by setting `max_total_tokens`:

```python
hook = TokenBudgetHook(
    max_output_tokens=50_000,
    max_total_tokens=200_000,    # total ceiling (0 = output-only)
)
```

---

## Recipe 5: Input compression (safe)

Compress oversized input before sending to the model. Raw text is never stored;
only a SHA-256 hash appears in evidence.

```python
from veronica_core import InputCompressionHook
from veronica_core.shield import Decision

hook = InputCompressionHook(
    compression_threshold_tokens=4000,   # compress above 4K tokens
    halt_threshold_tokens=8000,          # reject above 8K tokens
)

text, decision = hook.compress_if_needed(user_input, ctx)
if decision == Decision.HALT:
    raise ValueError("Input too large")

# text is now compressed (or unchanged if below threshold)
response = call_llm(text)
```

Disable compression at runtime (detection-only mode):

```bash
VERONICA_DISABLE_COMPRESSION=1 python your_agent.py
```

Evidence from the last check:

```python
evidence = hook.last_evidence
# {"estimated_tokens": 5200, "input_sha256": "a3f2...", "decision": "DEGRADE"}
```

---

## Recipe 6: Adaptive ceiling tuning (bounded)

Auto-tighten ceiling when HALT events repeat, loosen when events clear.
Bounded to +/-20% of base ceiling by default.

```python
from veronica_core.shield.adaptive_budget import AdaptiveBudgetHook
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision

hook = AdaptiveBudgetHook(
    base_ceiling=100,
    tighten_trigger=3,           # tighten after 3 HALT events in window
    tighten_pct=0.10,            # reduce by 10% per tighten
    loosen_pct=0.05,             # increase by 5% per loosen
    cooldown_seconds=900.0,      # 15 min between adjustments
    max_step_pct=0.05,           # max 5% change per step
    min_multiplier=0.6,          # hard floor: 60% of base
    max_multiplier=1.2,          # hard ceiling: 120% of base
    direction_lock=True,         # block loosen after tighten until events clear
)

# Feed events from your pipeline
hook.feed_event(SafetyEvent(
    event_type="BUDGET_WINDOW_EXCEEDED",
    decision=Decision.HALT,
    reason="ceiling reached",
    hook="BudgetWindowHook",
))

result = hook.adjust()
# result.action: "tighten" | "loosen" | "hold" | "cooldown_blocked" | "direction_locked"
# result.adjusted_ceiling: new ceiling (int)
# result.ceiling_multiplier: current multiplier (float)
```

Enable anomaly spike detection:

```python
hook = AdaptiveBudgetHook(
    base_ceiling=100,
    anomaly_enabled=True,
    anomaly_spike_factor=3.0,    # recent > 3x average triggers anomaly
    anomaly_tighten_pct=0.15,    # temporary 15% reduction
    anomaly_window_seconds=600,  # auto-recover after 10 min
    anomaly_recent_seconds=300,  # "recent" = last 5 min
)
```

ShieldConfig equivalent:

```python
config.adaptive_budget.enabled = True
config.adaptive_budget.cooldown_minutes = 15.0
config.adaptive_budget.direction_lock = True
config.adaptive_budget.anomaly_enabled = True
```

---

## Recipe 7: Deterministic replay (export / import)

Export full control state for observability dashboards or deterministic replay.

```python
# Export
state = hook.export_control_state(
    time_multiplier=time_policy.evaluate(ctx).multiplier,
)
# state is a JSON-serializable dict:
# {
#   "adaptive_multiplier": 0.9,
#   "time_multiplier": 0.85,
#   "anomaly_factor": 1.0,
#   "effective_multiplier": 0.765,
#   "base_ceiling": 100,
#   "adjusted_ceiling": 76,
#   "cooldown_active": false,
#   "anomaly_active": false,
#   "direction_lock_active": true,
#   "last_action": "tighten",
#   ...
# }

import json
print(json.dumps(state, indent=2))

# Import into a fresh hook (restores multiplier + anomaly state)
hook2 = AdaptiveBudgetHook(base_ceiling=100)
hook2.import_control_state(state)
assert hook2.ceiling_multiplier == state["adaptive_multiplier"]
```

Note: the event buffer is not restored. Replay events separately via
`feed_event()` if needed.

---

## Troubleshooting

### "Why did it HALT?"

Check `SafetyEvent` objects from the hook or pipeline:

```python
for ev in pipe.get_events():
    print(ev.event_type, ev.decision.value, ev.reason)
```

Common event types: `BUDGET_WINDOW_EXCEEDED`, `TOKEN_BUDGET_EXCEEDED`,
`INPUT_TOO_LARGE`, `SAFE_MODE_ACTIVE`.

### "Why no adjustment?"

Possible causes:
- **Cooldown active**: check `result.action == "cooldown_blocked"`
- **No events in window**: events older than `window_seconds` are ignored
- **Direction lock**: after tighten, loosen is blocked until exceeded events clear

### "How to disable everything?"

All features are disabled by default. If you explicitly enabled features and want
to disable them at runtime:

```python
config = ShieldConfig()  # everything disabled
# or
config.budget_window.enabled = False
```

For input compression specifically:

```bash
VERONICA_DISABLE_COMPRESSION=1 python your_agent.py
```

For emergency safe mode (blocks all dispatch):

```bash
VERONICA_SAFE_MODE=1 python your_agent.py
```

---

## Configuration formats

### Python (direct)

```python
config = ShieldConfig()
config.budget_window.enabled = True
config.budget_window.max_calls = 100
```

### JSON file

```json
{
  "budget_window": {
    "enabled": true,
    "max_calls": 100,
    "window_seconds": 600.0
  },
  "adaptive_budget": {
    "enabled": true,
    "cooldown_minutes": 15.0
  }
}
```

```python
config = ShieldConfig.from_yaml("shield.json")  # accepts .json natively
```

### YAML file (requires `pip install pyyaml`)

```yaml
budget_window:
  enabled: true
  max_calls: 100
  window_seconds: 600.0
adaptive_budget:
  enabled: true
  cooldown_minutes: 15.0
```

```python
config = ShieldConfig.from_yaml("shield.yaml")
```

### Environment variable

```bash
VERONICA_SAFE_MODE=1 python your_agent.py
```

```python
config = ShieldConfig.from_env()
# config.safe_mode.enabled == True
```

Unknown keys in config files are silently ignored (forward-compatible with newer versions).
