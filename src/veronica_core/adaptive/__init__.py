"""veronica_core.adaptive — Predictive adaptive policy mechanisms.

Public API:
  BurnRateEstimator       — Sliding-window cost burn rate (burn_rate.py)
  AdaptiveThresholdPolicy — RuntimePolicy: ALLOW/WARN/DEGRADE/HALT based on TTE
  AdaptiveConfig          — Configuration dataclass for AdaptiveThresholdPolicy
  AnomalyDetector         — Per-metric Z-score anomaly detection (Welford's alg)
"""

from veronica_core.adaptive.burn_rate import BurnRateEstimator
from veronica_core.adaptive.threshold import AdaptiveConfig, AdaptiveThresholdPolicy
from veronica_core.adaptive.anomaly import AnomalyDetector

__all__ = [
    "BurnRateEstimator",
    "AdaptiveThresholdPolicy",
    "AdaptiveConfig",
    "AnomalyDetector",
]
