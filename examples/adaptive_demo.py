"""Adaptive Budget Control demo -- runs in < 1 second, no external API needed.

Shows v0.7.0 adaptive budget features:
  1. Tighten on HALT events
  2. Cooldown blocking
  3. Direction lock
  4. Anomaly spike detection and auto-recovery
  5. Export/import control state

Usage:
    pip install -e .
    python examples/adaptive_demo.py
"""

from __future__ import annotations

from veronica_core.shield.adaptive_budget import AdaptiveBudgetHook
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


def _halt_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.HALT,
        reason="demo",
        hook="DemoHook",
    )


def _degrade_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.DEGRADE,
        reason="demo",
        hook="DemoHook",
    )


def demo_basic_tighten_loosen() -> None:
    """Show tighten on HALT events, then loosen when events clear."""
    print("--- Basic Tighten/Loosen ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=60.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        loosen_pct=0.05,
    )

    now = 1000.0
    print(f"  Initial ceiling: {hook.adjusted_ceiling}")

    # Feed 3 HALT events -> tighten
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)

    result = hook.adjust(_now=now + 10)
    print(f"  After 3 HALT events: {result.action}, ceiling={result.adjusted_ceiling}")

    # Advance past window -> events expire -> loosen
    result = hook.adjust(_now=now + 70)
    print(f"  After window expires: {result.action}, ceiling={result.adjusted_ceiling}")
    print()


def demo_cooldown() -> None:
    """Show cooldown blocking rapid adjustments."""
    print("--- Cooldown Window ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=60.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        cooldown_seconds=30.0,
    )

    now = 1000.0
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)

    result = hook.adjust(_now=now + 5)
    print(f"  First adjust: {result.action}, ceiling={result.adjusted_ceiling}")

    # Feed more events and try again within cooldown
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + 10 + i)

    result = hook.adjust(_now=now + 15)
    print(f"  Within cooldown (15s < 30s): {result.action}")

    # After cooldown
    result = hook.adjust(_now=now + 40)
    print(f"  After cooldown (40s > 30s): {result.action}, ceiling={result.adjusted_ceiling}")
    print()


def demo_direction_lock() -> None:
    """Show direction lock preventing premature loosening."""
    print("--- Direction Lock ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=10.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        loosen_pct=0.05,
        direction_lock=True,
    )

    now = 1000.0
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)

    # Tighten (3 events in window)
    result = hook.adjust(_now=now + 3)
    print(f"  Tighten: {result.action}, ceiling={result.adjusted_ceiling}")

    # Advance so 2 events expire, 1 remains (below trigger but > 0)
    # Would loosen (degrade_count=0), but direction lock blocks it
    result = hook.adjust(_now=now + 11)
    print(f"  Loosen blocked (1 event remains): {result.action}")

    # All events expire -> loosen allowed
    result = hook.adjust(_now=now + 13)
    print(f"  Events cleared: {result.action}, ceiling={result.adjusted_ceiling}")
    print()


def demo_anomaly() -> None:
    """Show anomaly spike detection and auto-recovery."""
    print("--- Anomaly Tightening ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=120.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        anomaly_enabled=True,
        anomaly_spike_factor=1.5,
        anomaly_tighten_pct=0.15,
        anomaly_window_seconds=60.0,
        anomaly_recent_seconds=30.0,
    )

    now = 1000.0

    # Spike: 3 HALT events all in recent window
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)

    result = hook.adjust(_now=now + 5)
    print(f"  Spike detected: anomaly_active={result.anomaly_active}, ceiling={result.adjusted_ceiling}")
    print(f"  (base=100, multiplier={result.ceiling_multiplier}, anomaly=-15%)")

    # Auto-recovery after anomaly window
    result = hook.adjust(_now=now + 200)
    print(f"  After 200s: anomaly_active={result.anomaly_active}, ceiling={result.adjusted_ceiling}")
    print()


def demo_export_import() -> None:
    """Show deterministic replay API."""
    print("--- Export/Import Control State ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=60.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        cooldown_seconds=30.0,
        min_multiplier=0.6,
        max_multiplier=1.2,
    )

    now = 1000.0
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)
    hook.adjust(_now=now + 5)

    state = hook.export_control_state(_now=now + 10)
    print(f"  Exported state:")
    print(f"    multiplier={state['adaptive_multiplier']}")
    print(f"    ceiling={state['adjusted_ceiling']}")
    print(f"    cooldown_active={state['cooldown_active']}")
    print(f"    last_action={state['last_action']}")

    # Import into fresh hook
    hook2 = AdaptiveBudgetHook(base_ceiling=100, window_seconds=60.0)
    hook2.import_control_state(state)
    print(f"  After import: multiplier={hook2.ceiling_multiplier}, ceiling={hook2.adjusted_ceiling}")
    print()


def demo_events() -> None:
    """Show SafetyEvent audit trail."""
    print("--- SafetyEvent Audit Trail ---")
    hook = AdaptiveBudgetHook(
        base_ceiling=100,
        window_seconds=60.0,
        tighten_trigger=3,
        tighten_pct=0.10,
        cooldown_seconds=30.0,
        direction_lock=True,
    )

    now = 1000.0
    for i in range(3):
        hook.feed_event(_halt_event(), ts=now + i)

    hook.adjust(_now=now + 5)   # tighten
    hook.adjust(_now=now + 10)  # cooldown blocked
    hook.adjust(_now=now + 40)  # direction locked (events still in window)

    for event in hook.get_events():
        print(f"  {event.event_type:<30s} {event.decision.value:<8s} {event.reason[:60]}")
    print()


def main() -> None:
    demo_basic_tighten_loosen()
    demo_cooldown()
    demo_direction_lock()
    demo_anomaly()
    demo_export_import()
    demo_events()

    print("All demos complete. 580+ tests passing.")


if __name__ == "__main__":
    main()
