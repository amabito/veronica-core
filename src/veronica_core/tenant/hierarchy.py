"""Tenant hierarchy -- Organisation/Project/Team/Agent budget tree.

Each :class:`Tenant` has an optional *parent_id* which links it to a parent
in the same :class:`TenantRegistry`.  Policies and budget pools are resolved
by walking up the tree until a non-``None`` value is found.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generator

if TYPE_CHECKING:
    from veronica_core.shield.pipeline import ShieldPipeline
    from veronica_core.tenant.pool import BudgetPool

__all__ = [
    "TenantNotFoundError",
    "Tenant",
    "TenantRegistry",
]


class TenantNotFoundError(KeyError):
    """Raised when a requested tenant ID is not found in the registry."""


@dataclass
class Tenant:
    """A single node in the tenant hierarchy.

    Attributes:
        id: Unique identifier for this tenant.
        parent_id: ID of the parent tenant.  ``None`` for a root tenant.
        budget_pool: Pool owned by this tenant.  ``None`` means "use parent's pool".
        policy: Shield pipeline for this tenant.  ``None`` means "inherit from parent".
        metadata: Arbitrary key/value pairs stored alongside the tenant.
    """

    id: str
    parent_id: str | None = None
    budget_pool: "BudgetPool | None" = None
    policy: "ShieldPipeline | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TenantRegistry:
    """Thread-safe registry of tenants with parent/child relationships.

    Tenants are stored in a flat dictionary keyed by ``tenant_id``.  The
    hierarchy is encoded via :attr:`Tenant.parent_id` references.  All
    traversal helpers (``resolve_policy``, ``get_effective_budget``) walk the
    tree iteratively via :meth:`_walk_ancestors` to avoid Python's default
    recursion limit.

    A ``_children`` index (parent_id -> set of child IDs) is maintained for
    O(1) child lookups in :meth:`get_children` and :meth:`remove`.
    """

    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        # O(1) child lookup: parent_id -> set of direct child IDs
        self._children: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _walk_ancestors(self, tenant_id: str) -> Generator[Tenant, None, None]:
        """Yield tenants walking upward from *tenant_id* (inclusive).

        Must be called with ``self._lock`` already held.

        Raises:
            TenantNotFoundError: If *tenant_id* is not registered.
            ValueError: If a cycle is detected in the hierarchy.
        """
        if tenant_id not in self._tenants:
            raise TenantNotFoundError(f"Tenant {tenant_id!r} not found.")
        current_id: str | None = tenant_id
        visited: set[str] = set()
        while current_id is not None:
            if current_id in visited:
                raise ValueError(
                    f"Cycle detected in tenant hierarchy at {current_id!r}."
                )
            if current_id not in self._tenants:
                break
            visited.add(current_id)
            tenant = self._tenants[current_id]
            yield tenant
            current_id = tenant.parent_id

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, tenant: Tenant) -> None:
        """Register a tenant.

        Raises:
            ValueError: If a tenant with the same *id* already exists.
            TenantNotFoundError: If *parent_id* is set but the parent is not
                registered.
        """
        with self._lock:
            if tenant.id in self._tenants:
                raise ValueError(f"Tenant {tenant.id!r} already registered.")
            if tenant.parent_id is not None and tenant.parent_id not in self._tenants:
                raise TenantNotFoundError(
                    f"Parent tenant {tenant.parent_id!r} not found."
                )
            self._tenants[tenant.id] = tenant
            # Update children index.
            if tenant.parent_id is not None:
                self._children.setdefault(tenant.parent_id, set()).add(tenant.id)
            # Ensure entry exists for this tenant (even if it has no children yet).
            self._children.setdefault(tenant.id, set())

    def remove(self, tenant_id: str) -> None:
        """Remove a leaf tenant from the registry.

        Raises:
            TenantNotFoundError: If *tenant_id* is not registered.
            ValueError: If the tenant has children (must remove children first).
        """
        with self._lock:
            if tenant_id not in self._tenants:
                raise TenantNotFoundError(f"Tenant {tenant_id!r} not found.")
            # O(1) child check via index.
            child_ids = self._children.get(tenant_id, set())
            if child_ids:
                raise ValueError(
                    f"Cannot remove tenant {tenant_id!r}: has {len(child_ids)} child(ren)."
                )
            tenant = self._tenants.pop(tenant_id)
            # Remove from parent's children set.
            if tenant.parent_id is not None:
                self._children.get(tenant.parent_id, set()).discard(tenant_id)
            # Drop the now-empty children entry.
            self._children.pop(tenant_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, tenant_id: str) -> Tenant:
        """Return the tenant with *tenant_id*.

        Raises:
            TenantNotFoundError: If not found.
        """
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                raise TenantNotFoundError(f"Tenant {tenant_id!r} not found.")
            return tenant

    def get_children(self, tenant_id: str) -> list[Tenant]:
        """Return direct children of *tenant_id* (order is unspecified).

        O(k) where k = number of direct children (index lookup).
        """
        with self._lock:
            if tenant_id not in self._tenants:
                raise TenantNotFoundError(f"Tenant {tenant_id!r} not found.")
            child_ids = self._children.get(tenant_id, set())
            return [self._tenants[cid] for cid in child_ids]

    def resolve_policy(self, tenant_id: str) -> "ShieldPipeline | None":
        """Walk the hierarchy upward until a non-``None`` policy is found.

        Returns ``None`` if no tenant in the chain has a policy configured.

        Raises:
            TenantNotFoundError: If *tenant_id* is not registered.
        """
        with self._lock:
            for tenant in self._walk_ancestors(tenant_id):
                if tenant.policy is not None:
                    return tenant.policy
            return None

    def get_effective_budget(self, tenant_id: str) -> float:
        """Return remaining budget considering the hierarchy.

        Walks upward to find the nearest ancestor (inclusive) that owns a
        :class:`~veronica_core.tenant.pool.BudgetPool` and returns its
        :meth:`~veronica_core.tenant.pool.BudgetPool.remaining` value.

        Returns ``float("inf")`` if no ancestor has a pool (unconstrained).

        Note: The pool reference is extracted inside the lock, but
        ``remaining()`` is called outside to avoid nested lock acquisition
        (TenantRegistry._lock -> BudgetPool._lock) which could deadlock
        if the inverse order is ever taken elsewhere.

        Raises:
            TenantNotFoundError: If *tenant_id* is not registered.
        """
        pool: "BudgetPool | None" = None
        with self._lock:
            for tenant in self._walk_ancestors(tenant_id):
                if tenant.budget_pool is not None:
                    pool = tenant.budget_pool
                    break
        return pool.remaining() if pool is not None else float("inf")
