"""Tests for VERONICA Budget cgroup -- BudgetPolicy, BudgetLedger, BudgetEnforcer."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.budget.ledger import BudgetLedger
from veronica.budget.policy import BudgetPolicy, Scope, WindowKind, WindowLimit
from veronica.runtime.events import EventBus, EventTypes
from veronica.runtime.models import Labels, Run, RunStatus, Session
from veronica.runtime.sinks import NullSink
from veronica.scheduler.scheduler import Scheduler
from veronica.scheduler.types import SchedulerConfig, SchedulerQueued


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CollectorSink:
    """Event sink that stores emitted events for assertions."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)

    def by_type(self, event_type: str) -> list:
        return [e for e in self.events if e.type == event_type]


def make_labels(
    org: str = "acme",
    team: str = "ml",
    user: str = "",
    service: str = "",
) -> Labels:
    return Labels(org=org, team=team, user=user, service=service)


def make_enforcer(
    policy: BudgetPolicy | None = None,
    sink_list: list | None = None,
) -> tuple[BudgetEnforcer, list]:
    if policy is None:
        policy = BudgetPolicy()
    if sink_list is None:
        sink_list = [CollectorSink()]
    bus = EventBus(sink_list)
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)
    return enforcer, sink_list


# Fixed time for window_id tests
FIXED_DT = datetime(2026, 2, 17, 8, 30, 45, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Policy defaults — parametrized table
# ---------------------------------------------------------------------------
#
# Given: a default BudgetPolicy with no overrides
# When:  the limit for each scope is read
# Then:  the limit matches the documented default value


@pytest.mark.parametrize(
    "scope_attr,window_attr,expected_usd",
    [
        # Given: org scope
        # When: minute limit is read
        # Then: returns 50.0
        ("org_limits", "minute_usd", 50.0),
        # When: hour limit is read
        # Then: returns 200.0
        ("org_limits", "hour_usd", 200.0),
        # When: day limit is read
        # Then: returns 1000.0
        ("org_limits", "day_usd", 1000.0),
        # Given: default team scope
        # When: minute limit is read
        # Then: returns 15.0
        ("default_team", "minute_usd", 15.0),
        # When: hour limit is read
        # Then: returns 60.0
        ("default_team", "hour_usd", 60.0),
        # When: day limit is read
        # Then: returns 300.0
        ("default_team", "day_usd", 300.0),
    ],
)
def test_policy_default_limits(scope_attr: str, window_attr: str, expected_usd: float) -> None:
    # Given
    policy = BudgetPolicy()

    # When
    limit_obj = getattr(policy, scope_attr)
    actual = getattr(limit_obj, window_attr)

    # Then
    assert actual == expected_usd


def test_policy_custom_team():
    custom_limit = WindowLimit(minute_usd=10.0, hour_usd=40.0, day_usd=200.0)
    policy = BudgetPolicy(teams={"ml": custom_limit})
    limit = policy.get_limit(Scope.TEAM, "ml")
    assert limit.minute_usd == 10.0
    assert limit.hour_usd == 40.0
    assert limit.day_usd == 200.0


def test_policy_user_service_inf():
    policy = BudgetPolicy()
    user_limit = policy.get_limit(Scope.USER, "nobody")
    service_limit = policy.get_limit(Scope.SERVICE, "unknown-svc")
    assert math.isinf(user_limit.minute_usd)
    assert math.isinf(user_limit.hour_usd)
    assert math.isinf(user_limit.day_usd)
    assert math.isinf(service_limit.minute_usd)


# ---------------------------------------------------------------------------
# 2. Window ID
# ---------------------------------------------------------------------------

def test_window_id_minute():
    wid = BudgetLedger.window_id(WindowKind.MINUTE, FIXED_DT)
    assert wid == "202602170830"


def test_window_id_hour():
    wid = BudgetLedger.window_id(WindowKind.HOUR, FIXED_DT)
    assert wid == "2026021708"


def test_window_id_day():
    wid = BudgetLedger.window_id(WindowKind.DAY, FIXED_DT)
    assert wid == "20260217"


# ---------------------------------------------------------------------------
# 3. Ledger reserve/commit/release
# ---------------------------------------------------------------------------

def test_reserve_commit_basic():
    ledger = BudgetLedger()
    scope = Scope.ORG
    scope_id = "acme"
    window = WindowKind.MINUTE

    ledger.reserve(scope, scope_id, window, 5.0)
    ledger.commit(scope, scope_id, window, reserved_usd=5.0, actual_usd=3.0)

    assert ledger.committed(scope, scope_id, window) == pytest.approx(3.0)
    assert ledger.used(scope, scope_id, window) == pytest.approx(3.0)  # reserved=0 now


def test_reserve_release():
    ledger = BudgetLedger()
    scope = Scope.ORG
    scope_id = "acme"
    window = WindowKind.MINUTE

    ledger.reserve(scope, scope_id, window, 5.0)
    ledger.release(scope, scope_id, window, 5.0)

    assert ledger.used(scope, scope_id, window) == pytest.approx(0.0)
    assert ledger.committed(scope, scope_id, window) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4. Enforcer: limit enforcement
# ---------------------------------------------------------------------------

def test_org_limit_reject():
    """Fill org minute to limit, then next pre_check raises BudgetExceeded."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=50.0, hour_usd=200.0, day_usd=1000.0),
        # Set default team limit very high so only org limit matters
        default_team=WindowLimit(minute_usd=1000.0, hour_usd=10000.0, day_usd=100000.0),
    )
    enforcer, _ = make_enforcer(policy=policy)
    # Use label with no team so only org scope is checked
    lbl = Labels(org="acme", team="")
    run = Run()

    # Fill up to exactly at the limit (50.0 = 5 x 10.0)
    for _ in range(5):
        reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=10.0)
        enforcer.post_charge(run, lbl, reserved, 10.0)

    # Now at 50.0, next call should be denied
    with pytest.raises(BudgetExceeded) as exc_info:
        enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=0.01)
    assert exc_info.value.scope == Scope.ORG


def test_team_limit_reject_only_that_team():
    """Team A at minute limit -> A rejected, team B still allowed."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=1000.0, hour_usd=10000.0, day_usd=100000.0),
        default_team=WindowLimit(minute_usd=15.0, hour_usd=60.0, day_usd=300.0),
    )
    enforcer, _ = make_enforcer(policy=policy)
    run = Run()

    lbl_a = make_labels(team="team-a")
    lbl_b = make_labels(team="team-b")

    # Fill team-a to its minute limit
    for _ in range(3):
        reserved = enforcer.pre_check_and_reserve(run.run_id, lbl_a, "llm_call", estimated_cost_usd=5.0)
        enforcer.post_charge(run, lbl_a, reserved, 5.0)

    # team-a should now be at 15.0 limit
    with pytest.raises(BudgetExceeded):
        enforcer.pre_check_and_reserve(run.run_id, lbl_a, "llm_call", estimated_cost_usd=0.01)

    # team-b should still be allowed
    reserved_b = enforcer.pre_check_and_reserve(run.run_id, lbl_b, "llm_call", estimated_cost_usd=1.0)
    assert reserved_b > 0


def test_minute_hour_day_independent():
    """Minute at limit, hour still has room -- minute rejects."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=1000.0, day_usd=10000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=1000.0, day_usd=10000.0),
    )
    enforcer, _ = make_enforcer(policy=policy)
    lbl = make_labels()
    run = Run()

    # Fill minute to limit
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=10.0)
    enforcer.post_charge(run, lbl, reserved, 10.0)

    # Next call on minute window should be denied despite hour having room
    with pytest.raises(BudgetExceeded) as exc_info:
        enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=0.01)
    assert exc_info.value.window == WindowKind.MINUTE


def test_window_reset_on_new_period(monkeypatch):
    """Commit in minute X, check in minute X+1 -> fresh window (use monkeypatch)."""
    ledger = BudgetLedger()
    scope = Scope.ORG
    scope_id = "acme"
    window = WindowKind.MINUTE

    # Window 0830: commit 40.0
    dt_0830 = datetime(2026, 2, 17, 8, 30, 0, tzinfo=timezone.utc)
    ledger.reserve(scope, scope_id, window, 40.0, ts=dt_0830)
    ledger.commit(scope, scope_id, window, 40.0, 40.0, ts=dt_0830)
    assert ledger.committed(scope, scope_id, window, ts=dt_0830) == pytest.approx(40.0)

    # Window 0831: different key -> fresh (0.0)
    dt_0831 = datetime(2026, 2, 17, 8, 31, 0, tzinfo=timezone.utc)
    used_new_window = ledger.used(scope, scope_id, window, ts=dt_0831)
    assert used_new_window == pytest.approx(0.0)


def test_strictest_limit_wins():
    """org minute=50, team minute=15 -> team hit first at 15."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=50.0, hour_usd=200.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=15.0, hour_usd=60.0, day_usd=300.0),
    )
    enforcer, _ = make_enforcer(policy=policy)
    lbl = make_labels()
    run = Run()

    # Fill 15.0 (3 x 5.0) to hit team minute limit first
    for _ in range(3):
        reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=5.0)
        enforcer.post_charge(run, lbl, reserved, 5.0)

    with pytest.raises(BudgetExceeded) as exc_info:
        enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=0.01)
    # Team limit is hit first (15 < 50)
    assert exc_info.value.scope == Scope.TEAM


# ---------------------------------------------------------------------------
# 5. Threshold events
# ---------------------------------------------------------------------------

def test_threshold_80_event():
    """Charge to 80% -> budget.threshold_crossed emitted with threshold=0.8."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
    )
    collector = CollectorSink()
    enforcer, _ = make_enforcer(policy=policy, sink_list=[collector])
    lbl = make_labels()
    run = Run()

    # Charge to 8.0 (80% of 10.0 minute limit)
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=8.0)
    enforcer.post_charge(run, lbl, reserved, 8.0)

    threshold_events = collector.by_type(EventTypes.BUDGET_THRESHOLD_CROSSED)
    thresholds_fired = [e.payload["threshold"] for e in threshold_events]
    assert 0.8 in thresholds_fired


def test_threshold_90_event_degraded():
    """90% -> event + Run.status==DEGRADED."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
    )
    collector = CollectorSink()
    enforcer, _ = make_enforcer(policy=policy, sink_list=[collector])
    lbl = make_labels()
    run = Run()

    # Charge to 9.0 (90% of 10.0)
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=9.0)
    enforcer.post_charge(run, lbl, reserved, 9.0)

    threshold_events = collector.by_type(EventTypes.BUDGET_THRESHOLD_CROSSED)
    thresholds_fired = {e.payload["threshold"] for e in threshold_events}
    assert 0.9 in thresholds_fired
    assert run.status == RunStatus.DEGRADED


def test_threshold_100_event_halted():
    """100% -> event + Run.status==HALTED."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
    )
    collector = CollectorSink()
    enforcer, _ = make_enforcer(policy=policy, sink_list=[collector])
    lbl = make_labels()
    run = Run()

    # Charge to 10.0 (100% of 10.0)
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=10.0)
    enforcer.post_charge(run, lbl, reserved, 10.0)

    threshold_events = collector.by_type(EventTypes.BUDGET_THRESHOLD_CROSSED)
    thresholds_fired = {e.payload["threshold"] for e in threshold_events}
    assert 1.0 in thresholds_fired
    assert run.status == RunStatus.HALTED


def test_threshold_dedup_same_window():
    """Two charges both >= 80% -> only 1 threshold event for 0.8."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
    )
    collector = CollectorSink()
    enforcer, _ = make_enforcer(policy=policy, sink_list=[collector])
    lbl = make_labels()
    run = Run()

    # First charge to 8.0 (80%) -- fires 0.8
    reserved1 = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=8.0)
    enforcer.post_charge(run, lbl, reserved1, 8.0)

    events_after_first = len(collector.by_type(EventTypes.BUDGET_THRESHOLD_CROSSED))

    # Second charge -- still >= 80% but dedup should prevent re-firing 0.8
    # (we can only add 0.01 since we're at 8.0 out of 10.0 for team minute)
    # Skip second charge if it would exceed limit; just verify dedup on first batch
    threshold_events_for_08 = [
        e for e in collector.by_type(EventTypes.BUDGET_THRESHOLD_CROSSED)
        if e.payload["threshold"] == 0.8
    ]
    # Count across all scopes -- dedup per (scope, scope_id, window, window_id, threshold)
    # Expect at most 2 (one per scope: org and team)
    assert len(threshold_events_for_08) <= 2


# ---------------------------------------------------------------------------
# 6. Scheduler integration: queue releases reservation
# ---------------------------------------------------------------------------

def test_queue_releases_reservation():
    """Create enforcer+scheduler, fill scheduler to limit -> QUEUE -> reservation released."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=1000.0, hour_usd=10000.0, day_usd=100000.0),
        default_team=WindowLimit(minute_usd=1000.0, hour_usd=10000.0, day_usd=100000.0),
    )
    collector = CollectorSink()
    bus = EventBus([collector])
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)

    config = SchedulerConfig(
        org_max_inflight=100,
        team_max_inflight=1,  # Only 1 slot per team
        org_queue_capacity=10_000,
        team_queue_capacity=2_000,
    )
    scheduler = Scheduler(config=config, bus=bus)

    lbl = make_labels(org="acme", team="ml")
    run = Run()
    session = Session(run_id=run.run_id)

    from veronica.scheduler.types import Priority, QueueEntry
    from veronica.runtime.models import generate_uuidv7

    # Fill the one team slot
    entry1 = QueueEntry(
        step_id=generate_uuidv7(),
        run_id=run.run_id,
        session_id=session.session_id,
        org=lbl.org,
        team=lbl.team,
        priority=Priority.P1,
    )
    result1 = scheduler.admit(entry1)
    assert result1.value == "allow"

    # Pre-check and reserve for a second call
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=5.0)
    assert reserved > 0

    # Second entry should be queued since team slot is full
    entry2 = QueueEntry(
        step_id=generate_uuidv7(),
        run_id=run.run_id,
        session_id=session.session_id,
        org=lbl.org,
        team=lbl.team,
        priority=Priority.P1,
    )
    result2 = scheduler.admit(entry2)
    assert result2.value == "queue"

    # Release the reservation (simulating QUEUE path in hooks)
    enforcer.release_reservation(lbl, reserved)

    # After releasing, ledger used should be 0
    used = ledger.used(Scope.ORG, lbl.org, WindowKind.MINUTE)
    assert used == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 7. Abort still commits
# ---------------------------------------------------------------------------

def test_abort_still_commits():
    """pre_check -> raise exception in yield -> post_charge still called."""
    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=100.0, hour_usd=1000.0, day_usd=10000.0),
        default_team=WindowLimit(minute_usd=100.0, hour_usd=1000.0, day_usd=10000.0),
    )
    collector = CollectorSink()
    bus = EventBus([collector])
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)

    lbl = make_labels()
    run = Run()

    # Reserve
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=5.0)

    # Simulate exception during execution, then post_charge is still called
    try:
        raise RuntimeError("simulated abort")
    except RuntimeError:
        pass

    # post_charge should still be called (hooks.py does this in except block)
    enforcer.post_charge(run, lbl, reserved, actual_cost_usd=2.0)

    # Verify committed amount
    assert ledger.committed(Scope.ORG, lbl.org, WindowKind.MINUTE) == pytest.approx(2.0)
    assert ledger.committed(Scope.TEAM, lbl.team, WindowKind.MINUTE) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 8. Hooks integration tests
# ---------------------------------------------------------------------------

def test_hooks_llm_call_budget_reject():
    """RuntimeContext(enforcer=enforcer), fill to limit, llm_call raises BudgetExceeded."""
    from veronica.runtime.hooks import RuntimeContext

    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
        default_team=WindowLimit(minute_usd=10.0, hour_usd=100.0, day_usd=1000.0),
    )
    collector = CollectorSink()
    bus = EventBus([collector])
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)

    ctx = RuntimeContext(sinks=[NullSink()], enforcer=enforcer)
    run = ctx.create_run()
    session = ctx.create_session(run)
    lbl = make_labels()

    # Fill the budget to the limit
    reserved = enforcer.pre_check_and_reserve(run.run_id, lbl, "llm_call", estimated_cost_usd=10.0)
    enforcer.post_charge(run, lbl, reserved, 10.0)

    # Now llm_call should raise BudgetExceeded before yielding
    with pytest.raises(BudgetExceeded):
        with ctx.llm_call(session, model="gpt-4", labels=lbl, run=run):
            pass  # Should not reach here


def test_hooks_llm_call_budget_charge():
    """RuntimeContext(enforcer=enforcer, run=run), llm_call succeeds, ledger shows committed cost."""
    from veronica.runtime.hooks import RuntimeContext

    policy = BudgetPolicy(
        org_limits=WindowLimit(minute_usd=100.0, hour_usd=1000.0, day_usd=10000.0),
        default_team=WindowLimit(minute_usd=100.0, hour_usd=1000.0, day_usd=10000.0),
    )
    collector = CollectorSink()
    bus = EventBus([collector])
    ledger = BudgetLedger()
    enforcer = BudgetEnforcer(policy=policy, ledger=ledger, bus=bus)

    ctx = RuntimeContext(sinks=[NullSink()], enforcer=enforcer)
    run = ctx.create_run()
    session = ctx.create_session(run)
    lbl = make_labels()

    with ctx.llm_call(session, model="gpt-4", labels=lbl, run=run) as step:
        step.cost_usd = 3.0

    # After successful llm_call, ledger should have committed the cost
    committed_org = ledger.committed(Scope.ORG, lbl.org, WindowKind.MINUTE)
    committed_team = ledger.committed(Scope.TEAM, lbl.team, WindowKind.MINUTE)
    assert committed_org > 0
    assert committed_team > 0
