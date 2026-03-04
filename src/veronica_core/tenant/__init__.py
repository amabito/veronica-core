"""Multi-tenant budget management for veronica-core.

Provides a hierarchical Organisation → Project → Team → Agent model where
each level can own a :class:`BudgetPool` and a :class:`ShieldPipeline` policy.
Unset values (``None``) are resolved by walking up the :class:`TenantRegistry`.
"""

from veronica_core.tenant.hierarchy import (
    Tenant,
    TenantNotFoundError,
    TenantRegistry,
)
from veronica_core.tenant.pool import (
    BudgetExhaustedError,
    BudgetPool,
)

__all__ = [
    "Tenant",
    "TenantNotFoundError",
    "TenantRegistry",
    "BudgetExhaustedError",
    "BudgetPool",
]
