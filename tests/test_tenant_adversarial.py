"""Adversarial tests for multi-tenant budget management.

Attacker mindset: race conditions, edge values, abuse patterns.
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.tenant import (
    Tenant,
    TenantRegistry,
    BudgetPool,
)
from veronica_core.shield.pipeline import ShieldPipeline


# ---------------------------------------------------------------------------
# Concurrent allocate() -- no overcommit
# ---------------------------------------------------------------------------


def test_concurrent_allocate_no_overcommit() -> None:
    """10 threads each try to allocate 20 from a 100-unit pool.

    At most 5 can succeed (5 * 20 = 100).  The sum of successful allocations
    must never exceed the pool total.
    """
    pool = BudgetPool(total=100.0, pool_id="race-pool")
    results: list[bool] = []
    lock = threading.Lock()

    def try_allocate(idx: int) -> None:
        ok = pool.allocate(f"child-{idx}", 20.0)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=try_allocate, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    successes = sum(1 for r in results if r)
    # Remaining should be >= 0.
    assert pool.remaining() >= -1e-9, (
        f"Overcommit detected: remaining={pool.remaining()}"
    )
    # At most 5 can succeed.
    assert successes <= 5, f"Too many successful allocations: {successes}"
    # Sum of allocations <= total.
    total_alloc = sum(pool._allocations.values())  # internal check
    assert total_alloc <= pool.total + 1e-9, (
        f"Internal allocations {total_alloc} exceed total {pool.total}"
    )


# ---------------------------------------------------------------------------
# Negative allocation amount -- rejected
# ---------------------------------------------------------------------------


def test_negative_allocation_rejected() -> None:
    pool = BudgetPool(total=50.0)
    assert pool.allocate("child-a", -10.0) is False
    assert pool.allocate("child-b", 0.0) is False


# ---------------------------------------------------------------------------
# Double-release -- idempotent (no double-return)
# ---------------------------------------------------------------------------


def test_double_release_idempotent() -> None:
    """Releasing the same child twice must not return budget twice."""
    pool = BudgetPool(total=100.0)
    pool.allocate("child-a", 60.0)

    first = pool.release("child-a")
    second = pool.release("child-a")

    assert first == pytest.approx(60.0)
    assert second == pytest.approx(0.0)
    # Pool remaining must not exceed total.
    assert pool.remaining() <= 100.0 + 1e-9


# ---------------------------------------------------------------------------
# Deeply nested hierarchy -- resolve_policy does not stack overflow
# ---------------------------------------------------------------------------


def test_deeply_nested_hierarchy_no_stack_overflow() -> None:
    """100-level hierarchy: resolve_policy walks iteratively."""
    registry = TenantRegistry()
    root_policy = ShieldPipeline()
    registry.register(Tenant(id="root", policy=root_policy))

    prev_id = "root"
    for depth in range(1, 101):
        tenant_id = f"depth-{depth}"
        registry.register(Tenant(id=tenant_id, parent_id=prev_id))
        prev_id = tenant_id

    # The leaf at depth 100 should resolve to root's policy.
    resolved = registry.resolve_policy("depth-100")
    assert resolved is root_policy


# ---------------------------------------------------------------------------
# Tenant ID collision -- clear error
# ---------------------------------------------------------------------------


def test_tenant_id_collision_raises_clearly() -> None:
    registry = TenantRegistry()
    registry.register(Tenant(id="org-1"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Tenant(id="org-1"))


# ---------------------------------------------------------------------------
# Zero-budget pool -- all allocations fail gracefully
# ---------------------------------------------------------------------------


def test_zero_budget_pool_all_allocations_fail() -> None:
    pool = BudgetPool(total=0.0)
    assert pool.allocate("child-a", 0.01) is False
    assert pool.allocate("child-b", 1.0) is False
    assert pool.remaining() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Negative total pool -- rejected at construction
# ---------------------------------------------------------------------------


def test_negative_total_pool_raises() -> None:
    with pytest.raises(ValueError):
        BudgetPool(total=-1.0)


# ---------------------------------------------------------------------------
# Concurrent spend from multiple threads -- no over-spend
# ---------------------------------------------------------------------------


def test_concurrent_spend_no_overspend() -> None:
    """20 threads each try to spend 10 from a single 100-unit child allocation."""
    pool = BudgetPool(total=200.0)
    pool.allocate("child-a", 100.0)

    successes: list[bool] = []
    lock = threading.Lock()

    def try_spend() -> None:
        ok = pool.spend("child-a", 10.0)
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=try_spend) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    success_count = sum(1 for s in successes if s)
    # Exactly 10 threads can spend 10 each (allocation = 100).
    assert success_count <= 10, f"Over-spend: {success_count} succeeded"
    # Remaining for child must be >= 0.
    assert pool.remaining_for("child-a") >= -1e-9


# ---------------------------------------------------------------------------
# NEW: 20 threads on near-exhausted pool -- sum NEVER exceeds total
# ---------------------------------------------------------------------------


def test_concurrent_allocate_near_exhausted_pool_no_overcommit() -> None:
    """20 threads each allocate 1.0 from a pool with only 5.0 total.

    At most 5 succeed.  Internal allocations must never exceed 5.0.
    """
    pool = BudgetPool(total=5.0, pool_id="near-exhausted")

    results: list[bool] = []
    lock = threading.Lock()

    def try_alloc(idx: int) -> None:
        ok = pool.allocate(f"child-{idx}", 1.0)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=try_alloc, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Hard invariant: sum(allocations) <= total
    with pool._lock:
        total_alloc = sum(pool._allocations.values())
    assert total_alloc <= 5.0 + 1e-9, f"Overcommit: {total_alloc} > 5.0"
    assert pool.remaining() >= -1e-9, f"Negative remaining: {pool.remaining()}"


# ---------------------------------------------------------------------------
# NEW: Concurrent spend() + release() race -- no negative balances
# ---------------------------------------------------------------------------


def test_concurrent_spend_and_release_no_negative_balance() -> None:
    """Interleaved spend() and release() from 10 threads must not corrupt state."""
    pool = BudgetPool(total=100.0)
    pool.allocate("shared", 100.0)

    errors: list[str] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        # Half threads spend, half release then re-allocate.
        if idx % 2 == 0:
            pool.spend("shared", 5.0)
        else:
            pool.release("shared")
            pool.allocate("shared", 10.0)
        rem = pool.remaining_for("shared")
        if rem < -1e-9:
            with lock:
                errors.append(f"Thread {idx}: negative remaining_for={rem}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == [], f"Negative balances detected: {errors}"


# ---------------------------------------------------------------------------
# NEW: NaN / Inf amounts in allocate and spend -- all rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -1.0,
        0.0,
    ],
)
def test_invalid_amount_allocate_rejected(amount: float) -> None:
    pool = BudgetPool(total=100.0)
    assert pool.allocate("child-a", amount) is False


@pytest.mark.parametrize(
    "amount",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -1.0,
        0.0,
    ],
)
def test_invalid_amount_spend_rejected(amount: float) -> None:
    pool = BudgetPool(total=100.0)
    pool.allocate("child-a", 50.0)
    assert pool.spend("child-a", amount) is False


# ---------------------------------------------------------------------------
# NEW: NaN / Inf total at construction -- rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total",
    [float("nan"), float("inf"), float("-inf"), -0.01],
)
def test_invalid_total_raises(total: float) -> None:
    with pytest.raises((ValueError, TypeError)):
        BudgetPool(total=total)


# ---------------------------------------------------------------------------
# NEW: Orphaned children after parent removal attempt -- clear error
# ---------------------------------------------------------------------------


def test_remove_parent_with_children_raises_clear_error() -> None:
    """Removing a tenant that still has children must fail with a clear message."""
    registry = TenantRegistry()
    registry.register(Tenant(id="org"))
    registry.register(Tenant(id="proj-a", parent_id="org"))
    registry.register(Tenant(id="proj-b", parent_id="org"))

    with pytest.raises(ValueError, match="child"):
        registry.remove("org")

    # Both children must still be accessible.
    assert registry.get("proj-a").parent_id == "org"
    assert registry.get("proj-b").parent_id == "org"


# ---------------------------------------------------------------------------
# NEW: Unicode and empty-string tenant IDs -- handled without crash
# ---------------------------------------------------------------------------


def test_unicode_tenant_id_handled() -> None:
    registry = TenantRegistry()
    registry.register(Tenant(id="org-\u6c34\u706b\u571f"))  # 水火土
    t = registry.get("org-\u6c34\u706b\u571f")
    assert t.id == "org-\u6c34\u706b\u571f"


def test_empty_string_tenant_id_handled() -> None:
    """Empty-string IDs are unusual but must not crash the registry."""
    registry = TenantRegistry()
    registry.register(Tenant(id=""))
    t = registry.get("")
    assert t.id == ""


def test_unicode_child_id_in_budget_pool() -> None:
    """Unicode child IDs in BudgetPool must work without encoding errors."""
    pool = BudgetPool(total=100.0)
    assert (
        pool.allocate("\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8-1", 40.0) is True
    )  # エージェント-1
    assert pool.spend("\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8-1", 10.0) is True
    assert pool.remaining_for(
        "\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8-1"
    ) == pytest.approx(30.0)


def test_null_bytes_tenant_id_handled() -> None:
    """Null-byte IDs must not crash the registry."""
    registry = TenantRegistry()
    registry.register(Tenant(id="\x00\x00"))
    t = registry.get("\x00\x00")
    assert t.id == "\x00\x00"
