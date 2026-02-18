"""Shield configuration models for VERONICA Execution Shield.

Uses stdlib dataclasses only (zero external dependencies).
All features opt-in: enabled=False by default.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List


@dataclass
class SafeModeConfig:
    """Emergency global safe mode (F5). Blocks all tool dispatch when enabled."""

    enabled: bool = False


@dataclass
class BudgetConfig:
    """Time-window budget controller (F1). Limits tokens/calls/cost per window."""

    enabled: bool = False
    max_tokens: int = 100_000
    max_calls: int = 1_000
    max_cost_usd: float = 10.0
    window_seconds: int = 3600


@dataclass
class CircuitBreakerConfig:
    """Deterministic failure circuit breaker (F2). Trips on repeated identical errors."""

    enabled: bool = False
    failure_threshold: int = 5
    recovery_timeout_seconds: int = 60


@dataclass
class EgressConfig:
    """Egress allowlist guard (F3). Default-deny outbound HTTP."""

    enabled: bool = False
    allowed_hosts: List[str] = field(default_factory=list)


@dataclass
class SecretGuardConfig:
    """Secret-aware outbound guard (F4). Scans outbound payloads for credentials."""

    enabled: bool = False
    patterns: List[str] = field(default_factory=list)


@dataclass
class BudgetWindowConfig:
    """Rolling time-window call-count limiter (opt-in).

    Disabled by default -- zero behavioral impact until explicitly enabled.
    """

    enabled: bool = False
    max_calls: int = 100
    window_seconds: float = 60.0


@dataclass
class ShieldConfig:
    """Top-level shield configuration.

    All features disabled by default -- zero behavioral impact.
    Wire into VeronicaIntegration via the ``shield`` parameter.
    """

    safe_mode: SafeModeConfig = field(default_factory=SafeModeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    secret_guard: SecretGuardConfig = field(default_factory=SecretGuardConfig)
    budget_window: BudgetWindowConfig = field(default_factory=BudgetWindowConfig)

    @property
    def is_any_enabled(self) -> bool:
        """Return True if at least one shield feature is enabled."""
        return any([
            self.safe_mode.enabled,
            self.budget.enabled,
            self.circuit_breaker.enabled,
            self.egress.enabled,
            self.secret_guard.enabled,
            self.budget_window.enabled,
        ])

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary (JSON-safe)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ShieldConfig:
        """Deserialize from a plain dictionary."""
        return cls(
            safe_mode=SafeModeConfig(**data.get("safe_mode", {})),
            budget=BudgetConfig(**data.get("budget", {})),
            circuit_breaker=CircuitBreakerConfig(**data.get("circuit_breaker", {})),
            egress=EgressConfig(**data.get("egress", {})),
            secret_guard=SecretGuardConfig(**data.get("secret_guard", {})),
            budget_window=BudgetWindowConfig(**data.get("budget_window", {})),
        )

    @classmethod
    def from_yaml(cls, path: str) -> ShieldConfig:
        """Load configuration from a YAML or JSON file.

        Accepts ``.json`` files natively.  For ``.yaml`` / ``.yml`` files,
        PyYAML must be installed (optional dependency).
        """
        file_path = Path(path)

        if file_path.suffix == ".json":
            with open(file_path) as fh:
                data = json.load(fh)
            return cls.from_dict(data)

        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                f"PyYAML is required to load '{file_path.name}'. "
                "Install with: pip install pyyaml"
            ) from None

        with open(file_path) as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)

    @classmethod
    def from_env(cls) -> ShieldConfig:
        """Build a ShieldConfig from environment variables.

        Recognised variables:
            VERONICA_SAFE_MODE=1  ->  safe_mode.enabled = True
        """
        config = cls()
        if os.environ.get("VERONICA_SAFE_MODE") == "1":
            config.safe_mode.enabled = True
        return config
