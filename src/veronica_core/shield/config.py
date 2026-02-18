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

    ``degrade_threshold`` controls the DEGRADE zone as a fraction of
    ``max_calls`` (default 0.8 = 80 %).  Set to 1.0 to disable DEGRADE.
    ``degrade_map`` is an optional mapping of model names for fallback routing
    (consumed by the caller; the hook itself only returns the Decision).
    """

    enabled: bool = False
    max_calls: int = 100
    window_seconds: float = 60.0
    degrade_threshold: float = 0.8
    degrade_map: dict = field(default_factory=dict)


@dataclass
class InputCompressionConfig:
    """Input compression gate (opt-in).

    Disabled by default.  When enabled, ``compress_if_needed()`` compresses
    input above ``compression_threshold_tokens`` and HALTs at
    ``halt_threshold_tokens``.

    ``fallback_to_original``: if True, compression failure returns DEGRADE
    with original text instead of HALT.
    """

    enabled: bool = False
    compression_threshold_tokens: int = 4000
    halt_threshold_tokens: int = 8000
    fallback_to_original: bool = False


@dataclass
class TokenBudgetConfig:
    """Token-based budget limiter (opt-in).

    Disabled by default. When enabled, enforces cumulative token limits
    with optional DEGRADE zone at degrade_threshold.
    Set max_total_tokens=0 to track output tokens only.
    """

    enabled: bool = False
    max_output_tokens: int = 100_000
    max_total_tokens: int = 0  # 0 = output-only tracking
    degrade_threshold: float = 0.8


@dataclass
class AdaptiveBudgetConfig:
    """Adaptive budget auto-adjustment (opt-in).

    When enabled, monitors SafetyEvents and auto-adjusts budget ceiling
    within +/- ``max_adjustment_pct`` of the base value.

    v0.7.0 stabilization:
      - ``cooldown_minutes``: minimum interval between adjustments
      - ``max_step_pct``: per-adjustment cap on multiplier change
      - ``min_multiplier`` / ``max_multiplier``: absolute hard bounds

    v0.7.0 anomaly tightening:
      - ``anomaly_enabled``: enable spike detection
      - ``anomaly_spike_factor``: recent events > factor * avg triggers anomaly
      - ``anomaly_tighten_pct``: temporary ceiling reduction (orthogonal to multiplier)
      - ``anomaly_window_minutes``: auto-recovery after N minutes
      - ``anomaly_recent_minutes``: "recent" period for spike detection

    Rules:
      - >= ``tighten_trigger`` HALT events in window -> ceiling * (1 - tighten_pct)
      - Zero DEGRADE events in window -> ceiling * (1 + loosen_pct)
    """

    enabled: bool = False
    window_seconds: float = 1800.0
    tighten_trigger: int = 3
    tighten_pct: float = 0.10
    loosen_pct: float = 0.05
    max_adjustment_pct: float = 0.20
    # v0.7.0 stabilization
    cooldown_minutes: float = 15.0
    max_step_pct: float = 0.05
    min_multiplier: float = 0.6
    max_multiplier: float = 1.2
    direction_lock: bool = True
    # v0.7.0 anomaly tightening
    anomaly_enabled: bool = False
    anomaly_spike_factor: float = 3.0
    anomaly_tighten_pct: float = 0.15
    anomaly_window_minutes: float = 10.0
    anomaly_recent_minutes: float = 5.0


@dataclass
class TimeAwarePolicyConfig:
    """Time-aware budget multiplier (opt-in).

    When enabled, reduces budget ceilings during weekends and off-hours.
    ``weekend_multiplier`` and ``offhour_multiplier`` are applied as
    fractions of the base ceiling.  Work hours default to 09:00-18:00 UTC.
    """

    enabled: bool = False
    weekend_multiplier: float = 0.85
    offhour_multiplier: float = 0.90
    work_start_hour: int = 9
    work_start_minute: int = 0
    work_end_hour: int = 18
    work_end_minute: int = 0


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
    token_budget: TokenBudgetConfig = field(default_factory=TokenBudgetConfig)
    input_compression: InputCompressionConfig = field(default_factory=InputCompressionConfig)
    adaptive_budget: AdaptiveBudgetConfig = field(default_factory=AdaptiveBudgetConfig)
    time_aware_policy: TimeAwarePolicyConfig = field(default_factory=TimeAwarePolicyConfig)

    @property
    def is_any_enabled(self) -> bool:
        """Return True if at least one shield feature is enabled."""
        return any((
            self.safe_mode.enabled,
            self.budget.enabled,
            self.circuit_breaker.enabled,
            self.egress.enabled,
            self.secret_guard.enabled,
            self.budget_window.enabled,
            self.token_budget.enabled,
            self.input_compression.enabled,
            self.adaptive_budget.enabled,
            self.time_aware_policy.enabled,
        ))

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary (JSON-safe)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ShieldConfig:
        """Deserialize from a plain dictionary.

        Unknown keys in sub-dicts are silently ignored for forward
        compatibility (e.g. config saved by a newer version).
        """
        import dataclasses

        def _safe_init(klass, raw: dict | None):  # type: ignore[type-arg]
            if not raw:
                return klass()
            valid = {f.name for f in dataclasses.fields(klass)}
            return klass(**{k: v for k, v in raw.items() if k in valid})

        return cls(
            safe_mode=_safe_init(SafeModeConfig, data.get("safe_mode", {})),
            budget=_safe_init(BudgetConfig, data.get("budget", {})),
            circuit_breaker=_safe_init(CircuitBreakerConfig, data.get("circuit_breaker", {})),
            egress=_safe_init(EgressConfig, data.get("egress", {})),
            secret_guard=_safe_init(SecretGuardConfig, data.get("secret_guard", {})),
            budget_window=_safe_init(BudgetWindowConfig, data.get("budget_window", {})),
            token_budget=_safe_init(TokenBudgetConfig, data.get("token_budget", {})),
            input_compression=_safe_init(InputCompressionConfig, data.get("input_compression", {})),
            adaptive_budget=_safe_init(AdaptiveBudgetConfig, data.get("adaptive_budget", {})),
            time_aware_policy=_safe_init(TimeAwarePolicyConfig, data.get("time_aware_policy", {})),
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
