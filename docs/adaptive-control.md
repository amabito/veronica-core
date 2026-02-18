# Adaptive Budget Control (v0.7.0)

## Overview

`AdaptiveBudgetHook` auto-adjusts the budget ceiling based on recent `SafetyEvent` history.
It observes events in a rolling time window and applies ceiling adjustments through a multiplier.

v0.7.0 adds stabilization features that prevent oscillation and improve convergence.

---

## Core Mechanism

| Condition | Action | Effect |
|-----------|--------|--------|
| >= `tighten_trigger` HALT events | Tighten | `multiplier -= tighten_pct` |
| 0 DEGRADE events | Loosen | `multiplier += loosen_pct` |
| Otherwise | Hold | No change |

Effective ceiling = `base_ceiling * multiplier * anomaly_factor`

The multiplier is clamped between `min_multiplier` and `max_multiplier`.

---

## Stabilization Features (v0.7.0)

### Cooldown Window

Prevents rapid oscillation by enforcing a minimum interval between adjustments.

```
cooldown_seconds=900  # 15 minutes between adjustments
```

When an adjustment is attempted during cooldown, a `ADAPTIVE_COOLDOWN_BLOCKED`
SafetyEvent is recorded and the multiplier is unchanged.

### Adjustment Smoothing

Limits the per-step change in multiplier, preventing large jumps.

```
max_step_pct=0.05  # Max 5% change per adjustment
```

If `tighten_pct=0.10` and `max_step_pct=0.05`, the effective step is 0.05.
This forces gradual convergence over multiple adjustment cycles.

### Hard Floor/Ceiling

Absolute bounds on the multiplier, independent of `max_adjustment`.

```
min_multiplier=0.6  # Never reduce below 60% of base
max_multiplier=1.2  # Never exceed 120% of base
```

### Direction Lock

Prevents loosen after tighten until all exceeded events clear from the window.

```
direction_lock=True
```

Flow: Tighten -> (events still in window) -> loosen blocked -> (events expire) -> loosen allowed.

Records `ADAPTIVE_DIRECTION_LOCKED` SafetyEvent when blocking.

---

## Anomaly Tightening (v0.7.0)

Detects sudden spikes in HALT events and applies a temporary ceiling reduction.

### Spike Detection

Compares recent event rate against the rolling average:

```
periods = window_seconds / anomaly_recent_seconds
avg_per_period = tighten_count / periods
spike = recent_tighten_count >= tighten_trigger
        AND recent_tighten_count > spike_factor * avg_per_period
```

### Anomaly Factor

When a spike is detected:
- `anomaly_factor = 1.0 - anomaly_tighten_pct` (e.g., 0.85 for 15% reduction)
- Orthogonal to the normal ceiling multiplier (they compound)
- Auto-recovers after `anomaly_window_seconds`

### Configuration

```python
AdaptiveBudgetHook(
    base_ceiling=100,
    anomaly_enabled=True,
    anomaly_spike_factor=3.0,       # Recent > 3x average triggers spike
    anomaly_tighten_pct=0.15,       # 15% temporary reduction
    anomaly_window_seconds=600.0,   # Auto-recover after 10 minutes
    anomaly_recent_seconds=300.0,   # "Recent" = last 5 minutes
)
```

### Events

| Event Type | Decision | When |
|-----------|----------|------|
| `ANOMALY_TIGHTENING_APPLIED` | DEGRADE | Spike detected |
| `ANOMALY_RECOVERED` | ALLOW | Auto-recovery after window |

---

## Deterministic Replay API (v0.7.0)

### export_control_state()

Returns a JSON-serializable dict with the full control state:

```python
state = hook.export_control_state(time_multiplier=0.9)
# {
#     "adaptive_multiplier": 0.9,
#     "time_multiplier": 0.9,
#     "anomaly_factor": 1.0,
#     "effective_multiplier": 0.81,
#     "base_ceiling": 100,
#     "adjusted_ceiling": 81,
#     "hard_floor": 0.6,
#     "hard_ceiling": 1.2,
#     "last_adjustment_ts": 1700000000.0,
#     "last_action": "tighten",
#     "cooldown_active": true,
#     "cooldown_remaining_seconds": 120.5,
#     "anomaly_active": false,
#     "anomaly_activated_ts": null,
#     "direction_lock_active": true,
#     "recent_event_counts": {"tighten": 3, "degrade": 1}
# }
```

### import_control_state()

Restores core state from a previously exported dict.
Event buffer is NOT restored (replay events via `feed_event()` if needed).

```python
hook.import_control_state(state)
```

---

## Configuration via ShieldConfig

```python
from veronica_core.shield.config import ShieldConfig, AdaptiveBudgetConfig

config = ShieldConfig(
    adaptive_budget=AdaptiveBudgetConfig(
        enabled=True,
        window_seconds=1800.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        loosen_pct=0.05,
        max_adjustment_pct=0.20,
        cooldown_minutes=15.0,
        max_step_pct=0.05,
        min_multiplier=0.6,
        max_multiplier=1.2,
        direction_lock=True,
        anomaly_enabled=True,
        anomaly_spike_factor=3.0,
        anomaly_tighten_pct=0.15,
        anomaly_window_minutes=10.0,
        anomaly_recent_minutes=5.0,
    )
)
```

All features are opt-in and disabled by default. Enabling adaptive budget
has zero impact on other shield features.

---

## Thread Safety

All state is behind a `threading.Lock`. `feed_event()`, `adjust()`,
`export_control_state()`, and `import_control_state()` are thread-safe.

## Deterministic Testing

All time-dependent methods accept a `_now` parameter for injected timestamps.
This allows deterministic unit testing without `time.sleep()` or mocking.
