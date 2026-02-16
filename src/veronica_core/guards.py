"""VERONICA Guard Interface - Domain-specific validation logic."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict


class VeronicaGuard(ABC):
    """Abstract guard for domain-specific validation and decision logic.

    Guards enable pluggable domain-specific logic without coupling the
    core state machine to any particular application.

    Example use cases:
    - Trading: Check market conditions before activating cooldown
    - API: Validate rate limit status
    - Batch processing: Check queue health metrics
    """

    @abstractmethod
    def should_cooldown(self, entity: str, context: Dict[str, Any]) -> bool:
        """Determine if cooldown should be activated for entity.

        Args:
            entity: Entity identifier (e.g., trading pair, API endpoint)
            context: Domain-specific context data (e.g., error_rate, latency)

        Returns:
            True if cooldown should activate immediately (overrides fail count)
        """
        pass

    @abstractmethod
    def validate_state(self, state_data: Dict[str, Any]) -> bool:
        """Validate state data before persistence.

        Args:
            state_data: Serialized state dictionary

        Returns:
            True if state data is valid and safe to persist
        """
        pass

    def on_cooldown_activated(self, entity: str, context: Dict[str, Any]) -> None:
        """Hook called when cooldown is activated (optional).

        Args:
            entity: Entity identifier
            context: Domain-specific context
        """
        pass

    def on_cooldown_expired(self, entity: str) -> None:
        """Hook called when cooldown expires (optional).

        Args:
            entity: Entity identifier
        """
        pass


class PermissiveGuard(VeronicaGuard):
    """Permissive guard that allows all operations (default).

    Use this when you don't need custom validation logic.
    """

    def should_cooldown(self, entity: str, context: Dict[str, Any]) -> bool:
        """Never trigger early cooldown."""
        return False

    def validate_state(self, state_data: Dict[str, Any]) -> bool:
        """All states are valid."""
        return True
