# Changelog

All notable changes to this project will be documented in this file.

## v0.7.1

- Verified PyPI publish pipeline (CI release gate + dry-run install)
- No functional changes

## v0.7.0

- Adaptive budget stabilization: cooldown window, adjustment smoothing, hard floor/ceiling, direction lock
- Anomaly tightening: spike detection with temporary ceiling reduction + auto-recovery
- Deterministic replay API: export/import control state for observability dashboards
- `docs/adaptive-control.md` engineering reference
- `examples/adaptive_demo.py` full demo

## v0.6.0

- `AdaptiveBudgetHook`: auto-adjusts ceiling based on SafetyEvent history
- `TimeAwarePolicy`: weekend / off-hour budget multipliers
- `InputCompressionHook` skeleton with `Compressor` protocol

## v0.5.x

- `TokenBudgetHook`: cumulative output/total token ceiling with DEGRADE zone
- `MinimalResponsePolicy`: opt-in conciseness constraints for system messages
- `InputCompressionHook`: real compression with safety guarantees (SHA-256 evidence, no raw text stored)

## v0.4.x

- `BudgetWindowHook`: rolling-window call ceiling with DEGRADE threshold
- `SafetyEvent`: structured evidence for non-ALLOW decisions
- DEGRADE support: model fallback before hard stop
- `ShieldPipeline`: hook-based pre-dispatch pipeline
- Risk audit MVP
