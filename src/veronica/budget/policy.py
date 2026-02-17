"""VERONICA Budget policy definitions."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class Scope(str, Enum):
    ORG = "org"
    TEAM = "team"
    USER = "user"
    SERVICE = "service"


# Hierarchy from broadest to narrowest
SCOPE_HIERARCHY: list[Scope] = [Scope.ORG, Scope.TEAM, Scope.USER, Scope.SERVICE]


class WindowKind(str, Enum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


@dataclass
class WindowLimit:
    """Per-window spend limits in USD. Default is unlimited (inf)."""

    minute_usd: float = math.inf
    hour_usd: float = math.inf
    day_usd: float = math.inf

    def limit_for(self, window: WindowKind) -> float:
        """Return the limit for the given window kind."""
        if window is WindowKind.MINUTE:
            return self.minute_usd
        if window is WindowKind.HOUR:
            return self.hour_usd
        return self.day_usd


@dataclass
class BudgetPolicy:
    """Global budget policy defining limits per scope and thresholds."""

    # Organization-level limits
    org_limits: WindowLimit = field(
        default_factory=lambda: WindowLimit(
            minute_usd=50.0,
            hour_usd=200.0,
            day_usd=1000.0,
        )
    )

    # Default limits applied to any team not explicitly listed
    default_team: WindowLimit = field(
        default_factory=lambda: WindowLimit(
            minute_usd=15.0,
            hour_usd=60.0,
            day_usd=300.0,
        )
    )

    # Per-team limits keyed by team name
    teams: dict[str, WindowLimit] = field(default_factory=dict)

    # Per-user limits keyed by user name
    users: dict[str, WindowLimit] = field(default_factory=dict)

    # Per-service limits keyed by service name
    services: dict[str, WindowLimit] = field(default_factory=dict)

    # Warning thresholds as fractions of limit (e.g. 0.8 = 80%)
    thresholds: list[float] = field(default_factory=lambda: [0.8, 0.9, 1.0])

    def get_limit(self, scope: Scope, scope_id: str) -> WindowLimit:
        """Return the WindowLimit for the given scope and scope_id.

        Falls back to org_limits for ORG, default_team for TEAM with no
        explicit entry, and unlimited for USER/SERVICE with no explicit entry.
        """
        if scope is Scope.ORG:
            return self.org_limits
        if scope is Scope.TEAM:
            return self.teams.get(scope_id, self.default_team)
        if scope is Scope.USER:
            return self.users.get(scope_id, WindowLimit())
        # Scope.SERVICE
        return self.services.get(scope_id, WindowLimit())
