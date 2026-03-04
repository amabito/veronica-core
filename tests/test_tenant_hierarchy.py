"""Tests for TenantRegistry hierarchy behaviour."""

from __future__ import annotations

import threading
import time

import pytest

from veronica_core.tenant import (
    Tenant,
    TenantNotFoundError,
    TenantRegistry,
    BudgetPool,
)
from veronica_core.shield.pipeline import ShieldPipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> TenantRegistry:
    return TenantRegistry()


@pytest.fixture()
def root_policy() -> ShieldPipeline:
    return ShieldPipeline()


@pytest.fixture()
def child_policy() -> ShieldPipeline:
    return ShieldPipeline()


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


def test_register_root_tenant_succeeds(registry: TenantRegistry) -> None:
    tenant = Tenant(id="org-1")
    registry.register(tenant)
    assert registry.get("org-1").id == "org-1"


def test_register_child_with_valid_parent_succeeds(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    assert registry.get("proj-1").parent_id == "org-1"


def test_register_child_with_nonexistent_parent_raises(
    registry: TenantRegistry,
) -> None:
    with pytest.raises(TenantNotFoundError):
        registry.register(Tenant(id="proj-1", parent_id="ghost-org"))


def test_register_duplicate_id_raises(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Tenant(id="org-1"))


# ---------------------------------------------------------------------------
# resolve_policy
# ---------------------------------------------------------------------------


def test_resolve_policy_child_has_none_returns_parent_policy(
    registry: TenantRegistry,
    root_policy: ShieldPipeline,
) -> None:
    """Child with policy=None should inherit parent's policy."""
    registry.register(Tenant(id="org-1", policy=root_policy))
    registry.register(Tenant(id="proj-1", parent_id="org-1", policy=None))
    resolved = registry.resolve_policy("proj-1")
    assert resolved is root_policy


def test_resolve_policy_child_has_own_policy_returns_child_policy(
    registry: TenantRegistry,
    root_policy: ShieldPipeline,
    child_policy: ShieldPipeline,
) -> None:
    """Child with its own policy should NOT inherit parent's policy."""
    registry.register(Tenant(id="org-1", policy=root_policy))
    registry.register(Tenant(id="proj-1", parent_id="org-1", policy=child_policy))
    resolved = registry.resolve_policy("proj-1")
    assert resolved is child_policy


def test_resolve_policy_no_policy_in_chain_returns_none(
    registry: TenantRegistry,
) -> None:
    registry.register(Tenant(id="org-1"))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    assert registry.resolve_policy("proj-1") is None


def test_resolve_policy_unknown_tenant_raises(registry: TenantRegistry) -> None:
    with pytest.raises(TenantNotFoundError):
        registry.resolve_policy("ghost")


# ---------------------------------------------------------------------------
# get_effective_budget
# ---------------------------------------------------------------------------


def test_get_effective_budget_three_level_hierarchy(
    registry: TenantRegistry,
) -> None:
    """org -> project -> agent; only org has a pool."""
    org_pool = BudgetPool(total=100.0, pool_id="org-pool")
    registry.register(Tenant(id="org-1", budget_pool=org_pool))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    registry.register(Tenant(id="agent-1", parent_id="proj-1"))

    # All three should resolve to the org pool's remaining budget.
    assert registry.get_effective_budget("org-1") == 100.0
    assert registry.get_effective_budget("proj-1") == 100.0
    assert registry.get_effective_budget("agent-1") == 100.0


def test_get_effective_budget_no_pool_returns_inf(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    assert registry.get_effective_budget("org-1") == float("inf")


def test_get_effective_budget_unknown_tenant_raises(registry: TenantRegistry) -> None:
    with pytest.raises(TenantNotFoundError):
        registry.get_effective_budget("ghost")


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_tenant_with_children_raises(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    with pytest.raises(ValueError, match="child"):
        registry.remove("org-1")


def test_remove_leaf_tenant_succeeds(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    registry.remove("proj-1")
    with pytest.raises(TenantNotFoundError):
        registry.get("proj-1")
    # Parent still exists.
    assert registry.get("org-1").id == "org-1"


def test_remove_nonexistent_tenant_raises(registry: TenantRegistry) -> None:
    with pytest.raises(TenantNotFoundError):
        registry.remove("ghost")


# ---------------------------------------------------------------------------
# get_children
# ---------------------------------------------------------------------------


def test_get_children_returns_direct_children(registry: TenantRegistry) -> None:
    registry.register(Tenant(id="org-1"))
    registry.register(Tenant(id="proj-1", parent_id="org-1"))
    registry.register(Tenant(id="proj-2", parent_id="org-1"))
    registry.register(Tenant(id="team-1", parent_id="proj-1"))
    children = registry.get_children("org-1")
    assert {c.id for c in children} == {"proj-1", "proj-2"}


# ---------------------------------------------------------------------------
# Thread-safety
# ---------------------------------------------------------------------------


def test_concurrent_register_and_get(registry: TenantRegistry) -> None:
    """Concurrent register/get must not raise or corrupt state."""
    errors: list[Exception] = []

    def register_and_get(idx: int) -> None:
        try:
            t = Tenant(id=f"tenant-{idx}")
            registry.register(t)
            _ = registry.get(f"tenant-{idx}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=register_and_get, args=(i,)) for i in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == [], f"Thread errors: {errors}"
