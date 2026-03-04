"""Tests for BudgetPool in-memory behaviour."""

from __future__ import annotations

import pytest

from veronica_core.tenant import BudgetPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pool() -> BudgetPool:
    return BudgetPool(total=100.0, pool_id="test-pool")


# ---------------------------------------------------------------------------
# allocate
# ---------------------------------------------------------------------------


def test_allocate_within_budget_returns_true(pool: BudgetPool) -> None:
    assert pool.allocate("child-a", 50.0) is True


def test_allocate_exceeding_remaining_returns_false(pool: BudgetPool) -> None:
    pool.allocate("child-a", 80.0)
    assert pool.allocate("child-b", 30.0) is False


def test_allocate_exactly_remaining_returns_true(pool: BudgetPool) -> None:
    pool.allocate("child-a", 60.0)
    assert pool.allocate("child-b", 40.0) is True


def test_allocate_zero_returns_false(pool: BudgetPool) -> None:
    assert pool.allocate("child-a", 0.0) is False


def test_allocate_negative_returns_false(pool: BudgetPool) -> None:
    assert pool.allocate("child-a", -10.0) is False


# ---------------------------------------------------------------------------
# spend
# ---------------------------------------------------------------------------


def test_spend_within_allocation_returns_true(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    assert pool.spend("child-a", 20.0) is True


def test_spend_exceeding_allocation_returns_false(pool: BudgetPool) -> None:
    pool.allocate("child-a", 10.0)
    assert pool.spend("child-a", 15.0) is False


def test_spend_without_allocation_returns_false(pool: BudgetPool) -> None:
    assert pool.spend("child-a", 5.0) is False


def test_spend_zero_returns_false(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    assert pool.spend("child-a", 0.0) is False


def test_spend_negative_returns_false(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    assert pool.spend("child-a", -1.0) is False


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_returns_remaining_to_pool(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    pool.spend("child-a", 20.0)
    returned = pool.release("child-a")
    assert returned == pytest.approx(30.0)
    # Pool should now have 100 - 20 = 80 remaining (only spent is gone).
    # But "child-a" is deleted, so remaining = total - allocations.
    # At this point there are no allocations, so remaining = 100.
    # Wait: the spent is gone but the pool total doesn't recover the spent.
    # Actually: allocations track the ceiling, not the spent.  After release,
    # the allocation is removed entirely, so remaining = 100 - 0 = 100.
    assert pool.remaining() == pytest.approx(100.0)


def test_release_nonexistent_child_returns_zero(pool: BudgetPool) -> None:
    assert pool.release("ghost") == pytest.approx(0.0)


def test_release_is_idempotent(pool: BudgetPool) -> None:
    pool.allocate("child-a", 30.0)
    first = pool.release("child-a")
    second = pool.release("child-a")
    assert first == pytest.approx(30.0)
    assert second == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_usage_returns_correct_per_child_spending(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    pool.allocate("child-b", 30.0)
    pool.spend("child-a", 10.0)
    pool.spend("child-b", 5.0)
    u = pool.usage()
    assert u["child-a"] == pytest.approx(10.0)
    assert u["child-b"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# remaining
# ---------------------------------------------------------------------------


def test_remaining_is_total_minus_sum_of_allocations(pool: BudgetPool) -> None:
    pool.allocate("child-a", 40.0)
    pool.allocate("child-b", 20.0)
    assert pool.remaining() == pytest.approx(40.0)


def test_remaining_after_all_allocated_is_zero(pool: BudgetPool) -> None:
    pool.allocate("child-a", 100.0)
    assert pool.remaining() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# remaining_for
# ---------------------------------------------------------------------------


def test_remaining_for_tracks_spend(pool: BudgetPool) -> None:
    pool.allocate("child-a", 50.0)
    pool.spend("child-a", 15.0)
    assert pool.remaining_for("child-a") == pytest.approx(35.0)


def test_remaining_for_unknown_child_is_zero(pool: BudgetPool) -> None:
    assert pool.remaining_for("ghost") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Multiple children
# ---------------------------------------------------------------------------


def test_multiple_children_share_pool(pool: BudgetPool) -> None:
    assert pool.allocate("child-a", 40.0) is True
    assert pool.allocate("child-b", 40.0) is True
    assert pool.allocate("child-c", 20.0) is True
    assert pool.remaining() == pytest.approx(0.0)
    pool.spend("child-a", 10.0)
    pool.spend("child-b", 5.0)
    u = pool.usage()
    assert u["child-a"] == pytest.approx(10.0)
    assert u["child-b"] == pytest.approx(5.0)
    assert u.get("child-c", 0.0) == pytest.approx(0.0)
