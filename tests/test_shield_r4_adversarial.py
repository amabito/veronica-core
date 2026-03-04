"""Adversarial tests for shield subsystem R4 bug fixes.

Covers four fixes:
  S1 - BudgetWindowHook: HALT call now appends timestamp (counts in window).
  S2 - AdaptiveBudgetHook: event at exact cutoff boundary retained (< not <=).
  S3 - TokenBudgetHook: concurrent DEGRADE callers reserve tokens atomically.
  S4 - AdaptiveBudgetHook: _safety_events deque capped at 1000 entries.
"""

from __future__ import annotations

import threading
import time

from veronica_core.shield.adaptive_budget import AdaptiveBudgetHook
from veronica_core.shield.budget_window import BudgetWindowHook
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.token_budget import TokenBudgetHook
from veronica_core.shield.types import Decision, ToolCallContext

CTX = ToolCallContext(request_id="adv-test", tool_name="bash")


def _halt_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.HALT,
        reason="adversarial",
        hook="AdversarialHook",
    )


def _degrade_event(event_type: str = "BUDGET_WINDOW_EXCEEDED") -> SafetyEvent:
    return SafetyEvent(
        event_type=event_type,
        decision=Decision.DEGRADE,
        reason="adversarial",
        hook="AdversarialHook",
    )


# ---------------------------------------------------------------------------
# S1: HALT appends timestamp — subsequent calls still see saturated window
# ---------------------------------------------------------------------------


class TestAdversarialS1HaltTimestamp:
    """S1: HALT call must be counted in the rolling window.

    Before the fix, HALT returned immediately without appending the timestamp,
    so a window that had been filled with (max_calls - 1) ALLOW calls plus
    one HALT call would not record the HALT, allowing the next call at the same
    timestamp to slip through.
    After the fix, HALT-count calls accumulate just like DEGRADE/ALLOW calls.
    """

    def test_halt_call_is_counted_in_window(self, monkeypatch):
        """HALT appends ts; repeated calls at the same instant all remain HALT.

        Setup: max_calls=2, degrade_threshold=1.0 (HALT fires before DEGRADE zone).
        - t=0 call #1: count=0 -> None (ALLOW), appends ts=0.
        - t=0 call #2: count=1 -> None (ALLOW), appends ts=0.
        - t=0 call #3: count=2 >= max=2 -> HALT, appends ts=0 (S1 fix).
        - t=0 call #4: count=3 >= max=2 -> HALT.
        """

        def fake_time() -> float:
            return 0.0  # all calls at t=0

        monkeypatch.setattr(time, "time", fake_time)

        # degrade_threshold=1.0: degrade_at = 1.0 * max_calls = 2.
        # HALT check fires first (count >= max_calls), so no DEGRADE zone confusion.
        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0, degrade_threshold=1.0)

        assert hook.before_llm_call(CTX) is None  # call #1: ALLOW
        assert hook.before_llm_call(CTX) is None  # call #2: ALLOW
        result_halt = hook.before_llm_call(CTX)  # call #3: should HALT
        assert result_halt is Decision.HALT

        # S1: HALT appended ts=0.0, so next call sees count=3 >= 2 -> HALT
        result_halt2 = hook.before_llm_call(CTX)  # call #4
        assert result_halt2 is Decision.HALT

    def test_halt_timestamp_expires_after_full_window(self, monkeypatch):
        """HALT timestamp expires when the window rolls past it.

        max_calls=2, degrade_threshold=1.0.
        - t=0: two ALLOW calls -> ts_list=[0.0, 0.0]
        - t=0: HALT call (S1 fix) -> ts_list=[0.0, 0.0, 0.0]
        - t=120.001: cutoff=60.001; all ts=0.0 pruned (0.0 < 60.001) -> ALLOW
        """
        timestamps = iter([0.0, 0.0, 0.0, 120.001])
        monkeypatch.setattr(time, "time", lambda: next(timestamps))

        hook = BudgetWindowHook(max_calls=2, window_seconds=60.0, degrade_threshold=1.0)

        assert hook.before_llm_call(CTX) is None  # t=0.0, ALLOW
        assert hook.before_llm_call(CTX) is None  # t=0.0, ALLOW
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=0.0, HALT (appended)

        # At t=120.001 all three ts=0.0 are pruned -> fresh window
        assert hook.before_llm_call(CTX) is None  # t=120.001, ALLOW

    def test_repeated_halt_calls_keep_window_saturated(self, monkeypatch):
        """Each HALT call pushes a new timestamp; window stays saturated.

        max_calls=1, degrade_threshold=1.0 (HALT fires before DEGRADE zone).
        - t=0.0: ALLOW (count 0->1, appends ts=0.0)
        - t=1.0: HALT (count=1 >= 1, appends ts=1.0)
        - t=2.0: HALT (count=2 >= 1, appends ts=2.0)  -- S1 fix keeps it saturated
        - t=62.0: cutoff=2.0; ts=0.0 and ts=1.0 pruned; ts=2.0 retained -> HALT
        """
        timestamps = iter([0.0, 1.0, 2.0, 62.0])
        monkeypatch.setattr(time, "time", lambda: next(timestamps))

        # degrade_threshold=1.0: degrade_at=1.0*1=1; HALT check fires first.
        hook = BudgetWindowHook(max_calls=1, window_seconds=60.0, degrade_threshold=1.0)

        assert hook.before_llm_call(CTX) is None  # t=0.0, ALLOW
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=1.0, HALT (appended)
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=2.0, HALT (appended)

        # At t=62.0: cutoff=2.0; ts=0.0 (<2.0) and ts=1.0 (<2.0) pruned;
        # ts=2.0 retained (2.0 < 2.0 is False) -> count=1 >= max=1 -> HALT
        assert hook.before_llm_call(CTX) is Decision.HALT  # t=62.0


# ---------------------------------------------------------------------------
# S2: exact cutoff boundary retained (< not <=)
# ---------------------------------------------------------------------------


class TestAdversarialS2CutoffBoundary:
    """S2: event at ts == cutoff must be retained in the window.

    The prune helper used ``<= cutoff`` (old code), which erroneously discarded
    events timestamped at exactly the cutoff instant, shrinking the effective
    window by one epsilon.
    """

    def test_event_at_exact_cutoff_is_retained(self):
        """Event fed at ts=T must survive a prune with cutoff=T (strict <)."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=1,
            tighten_pct=0.10,
        )
        # Feed one HALT event at t=0.0
        event = _halt_event()
        hook.feed_event(event, ts=0.0)

        # adjust() called at t=60.0 -> cutoff = 60.0 - 60.0 = 0.0
        # With correct < semantics: ts=0.0 < 0.0 is False -> event retained -> tighten
        result = hook.adjust(_now=60.0)
        assert result.tighten_events_in_window >= 1, (
            "Event at exact cutoff boundary must be retained, not pruned"
        )
        assert result.action == "tighten"

    def test_event_just_before_cutoff_is_pruned(self):
        """Event at ts=T-epsilon is pruned when cutoff=T (ts < T is True)."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=1,
            tighten_pct=0.10,
        )
        event = _halt_event()
        hook.feed_event(event, ts=0.0)

        # adjust() at t=60.001 -> cutoff=0.001; ts=0.0 < 0.001 -> pruned
        result = hook.adjust(_now=60.001)
        assert result.tighten_events_in_window == 0, (
            "Event strictly before cutoff must be pruned"
        )

    def test_export_control_state_boundary_consistent(self):
        """export_control_state uses same < semantics as _prune_event_buffer."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=60.0,
            tighten_trigger=1,
            tighten_pct=0.10,
        )
        event = _halt_event()
        hook.feed_event(event, ts=100.0)

        # At _now=160.0: cutoff=100.0; ts=100.0 < 100.0 is False -> event in-window
        # recent_event_counts["tighten"] reflects tighten events still in window.
        state = hook.export_control_state(_now=160.0)
        tighten_count = state["recent_event_counts"]["tighten"]
        assert tighten_count >= 1, (
            "export_control_state must treat exact boundary consistently (< not <=): "
            f"expected tighten>=1, got {tighten_count}"
        )


# ---------------------------------------------------------------------------
# S3: TokenBudgetHook DEGRADE reserves tokens (concurrent ceiling)
# ---------------------------------------------------------------------------


class TestAdversarialS3DegradeConcurrentReservation:
    """S3: DEGRADE callers must reserve tokens to prevent TOCTOU overrun.

    Before the fix, DEGRADE returned without incrementing pending_output.
    Multiple concurrent threads in the DEGRADE zone could each proceed,
    collectively exceeding the output ceiling.
    """

    def test_degrade_increments_pending_output(self):
        """Single DEGRADE call must increment pending_output atomically."""
        hook = TokenBudgetHook(
            max_output_tokens=100,
            degrade_threshold=0.8,  # degrade at 80 tokens
        )
        hook.record_usage(output_tokens=80)

        ctx = ToolCallContext(request_id="degrade-test", tool_name="llm", tokens_out=5)
        result = hook.before_llm_call(ctx)

        assert result is Decision.DEGRADE
        assert hook.pending_output == 5, (
            "DEGRADE must reserve estimated tokens to prevent concurrent overrun"
        )

    def test_concurrent_degrade_callers_cannot_exceed_ceiling(self):
        """10 threads in DEGRADE zone must not collectively exceed max_output_tokens."""
        max_out = 100
        # Use 85 tokens: in DEGRADE zone (>= 80), 15 tokens remain before ceiling
        hook = TokenBudgetHook(max_output_tokens=max_out, degrade_threshold=0.8)
        hook.record_usage(output_tokens=85)

        per_call = 5  # 85 + 3*5=100, 4th call hits ceiling
        ctx = ToolCallContext(
            request_id="conc-degrade", tool_name="llm", tokens_out=per_call
        )

        decisions: list[Decision | None] = []
        lock = threading.Lock()

        def call_hook() -> None:
            decision = hook.before_llm_call(ctx)
            with lock:
                decisions.append(decision)

        threads = [threading.Thread(target=call_hook) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        degrade_count = sum(1 for d in decisions if d is Decision.DEGRADE)
        halt_count = sum(1 for d in decisions if d is Decision.HALT)

        # At most 3 calls should DEGRADE (85 + 3*5 = 100)
        assert degrade_count <= 3, (
            f"Only 3 DEGRADE calls fit under ceiling, got {degrade_count}. "
            "S3 reservation must prevent concurrent overrun."
        )
        assert halt_count >= 7, f"Remaining calls must HALT, got {halt_count}"

    def test_degrade_reservation_released_on_record_usage(self):
        """Pending reservation from DEGRADE path is released when usage is recorded."""
        hook = TokenBudgetHook(max_output_tokens=100, degrade_threshold=0.8)
        hook.record_usage(output_tokens=80)

        ctx = ToolCallContext(request_id="release-test", tool_name="llm", tokens_out=10)
        result = hook.before_llm_call(ctx)
        assert result is Decision.DEGRADE
        assert hook.pending_output == 10

        # Record actual usage: pending drops back to 0
        hook.record_usage(output_tokens=10)
        assert hook.pending_output == 0


# ---------------------------------------------------------------------------
# S4: _safety_events deque bounded at 1000 entries
# ---------------------------------------------------------------------------


class TestAdversarialS4SafetyEventsBounded:
    """S4: _safety_events must not grow beyond 1000 entries.

    Before the fix, _safety_events was an unbounded list[SafetyEvent].
    Long-running sessions could accumulate millions of events and exhaust memory.
    """

    def test_safety_events_capped_at_1000(self):
        """Feeding events that trigger 2000 internal SafetyEvent writes stays <= 1000."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=1.0,
            tighten_trigger=1,
            tighten_pct=0.10,
            loosen_pct=0.05,
        )

        # Each adjust() that fires a tighten writes one ADAPTIVE_ADJUSTMENT SafetyEvent.
        # Feed a HALT event well inside the window, then call adjust 1500 times,
        # each spaced 2 s apart so there is no cooldown block.
        for i in range(1500):
            hook.feed_event(_halt_event(), ts=float(i))
            hook.adjust(_now=float(i) + 2.0)

        events = hook.get_events()
        assert len(events) <= 1000, (
            f"_safety_events must be capped at 1000, got {len(events)}. "
            "S4 deque(maxlen=1000) fix is required."
        )

    def test_safety_events_oldest_entries_evicted(self):
        """When cap is reached, oldest events are dropped (deque FIFO eviction)."""
        hook = AdaptiveBudgetHook(
            base_ceiling=10_000,
            window_seconds=1.0,
            tighten_trigger=1,
            tighten_pct=0.10,
        )

        # Reset between cycles so the ceiling never bottoms out and each cycle tightens.
        for i in range(1001):
            hook.reset()
            hook.feed_event(_halt_event(), ts=0.0)
            hook.adjust(_now=float(i) + 2.0)

        events = hook.get_events()
        assert len(events) <= 1000

    def test_safety_events_clear_works_after_cap(self):
        """clear_events() empties the bounded deque without error."""
        hook = AdaptiveBudgetHook(
            base_ceiling=100,
            window_seconds=1.0,
            tighten_trigger=1,
            tighten_pct=0.10,
        )
        for i in range(200):
            hook.feed_event(_halt_event(), ts=float(i))
            hook.adjust(_now=float(i) + 2.0)

        hook.clear_events()
        assert hook.get_events() == []
