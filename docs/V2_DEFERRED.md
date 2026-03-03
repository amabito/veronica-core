# v2.0 Deferred Items

Tracked issues deferred from the v1.8.x bug hunt rounds. These require
breaking changes, large refactors, or address edge cases too narrow for
a patch release.

---

## Architectural

| ID | Severity | Description | File(s) |
|----|----------|-------------|---------|
| D-1 | LOW | `adapter/` vs `adapters/` directory naming unification | `src/veronica_core/adapters/` |
| D-3 | LOW | `__init__.py` eager-loads 117 symbols; migrate to lazy imports | `src/veronica_core/__init__.py` |
| D-4 | LOW | `execution_context.py` is a 1290-line god class; split into sub-modules | `src/veronica_core/containment/execution_context.py` |
| D-5 | LOW | `distributed.py` is 1224 lines; split into circuit breaker + budget backend | `src/veronica_core/distributed.py` |
| D-6 | LOW | `policy_engine.py` is 1089 lines; split into shell/file/net evaluators | `src/veronica_core/security/policy_engine.py` |
| D-7 | LOW | `distributed.py` `_last_reconnect_attempt` not initialised in `__init__` (uses getattr fallback) | `src/veronica_core/distributed.py` |
| D-8 | LOW | `quickstart.py` uses `type: ignore` for dynamic attribute monkey-patching | `src/veronica_core/quickstart.py` |

## Design Tradeoffs (Documented, Not Bugs)

| ID | Description | File | Notes |
|----|-------------|------|-------|
| T-1 | `budget_backend.add()` outside lock (H3) allows one-call overrun in high concurrency | `execution_context.py:947` | Documented trade-off; H4 distributed check catches on next call |
| T-2 | `max_steps=0` causes immediate HALT on first wrap call | `execution_context.py:1042` | Semantically correct: "zero steps allowed = no calls" |

## Latent Edge Cases

| ID | Severity | Description | File | Notes |
|----|----------|-------------|------|-------|
| L-1 | LOW | `attach_partial_buffer()` always raises for new buffer attachment | `containment/` | Design ambiguity |
| L-2 | LOW | `_propagate_child_cost` epsilon inconsistency (1e-9 USD) | `execution_context.py` | Negligible impact |
| L-3 | LOW | `direction_locked` does not update `_last_action` | `shield/adaptive_budget.py` | Misleading to observers but functionally correct |
| L-4 | LOW | No explicit inline-exec guard for node/ruby/perl | `security/policy_engine.py` | Latent; python/bash/sh covered |
| L-5 | LOW | PolicySignerV2 false tamper when `cryptography` missing | `security/policy_signing.py` | Unreachable in strict mode |
| L-6 | LOW | ComplianceExporter `_attached` thread safety under nogil Python (PEP 703) | `compliance/exporter.py` | Safe under CPython GIL; revisit for 3.13+ nogil |
| L-7 | LOW | Event dedup key excludes timestamp; events with `request_id=None` may be incorrectly merged | `containment/execution_context.py` | Normal usage has unique request_id per call |
| L-8 | LOW | JSONBackend orphans `.tmp` file on failed `replace()` (Windows file locking) | `backends.py` | Negligible disk impact |
| L-9 | LOW | ComplianceExporter `_enqueue` drops payload silently on Full->Empty->Full race | `compliance/exporter.py` | Rare under normal load |
| L-10 | LOW | WSGI middleware context-var reset / `__exit__` exception ordering suboptimal | `middleware.py` | Both ops expected safe |

## Resolved in v1.8.8 (Previously Deferred)

| ID | Description | Resolution |
|----|-------------|------------|
| R-6 | `ShieldPipeline.on_error()` no-hook path missing SafetyEvent | Added symmetric event recording for fallback policy |
| R-7 | Divergence event dedup used O(n) `not in` (missed in v1.8.7) | Applied `_event_dedup_keys` O(1) pattern to 4th site |
| R-8 | `MinimalResponsePolicy` grammar: singular "question" when `max_questions > 1` | Added plural handling |
| R-9 | `policy_engine.py` `args.index("-m")` searched full list vs `args[1:]` check | Fixed to `args[1:].index("-m") + 1` |
| R-10 | `DynamicAllocator` min_share floor could exceed total budget | Clamp min_floor when `reserved > total_budget` |
| R-11 | `patch.py` cost estimation failure logged at debug level | Upgraded to `logger.warning` |

## Resolved in v1.8.7 (Previously Deferred)

| ID | Description | Resolution |
|----|-------------|------------|
| R-1 | Pipeline event O(n) dedup | Applied `_event_dedup_keys` O(1) pattern to all 3 intake sites |
| R-2 | ComplianceExporter `attach()`/`_drain_attached()` missing lock | Added `self._lock` protection |
| R-3 | `_atexit_registered` bound to first instance only | Changed to `WeakSet` + class-level `_save_all_instances()` handler |
| R-4 | semantic.py dead code (`if not union: return 0.0`) | Removed unreachable branch |
| R-5 | retry.py dead code (post-loop fallback) | Simplified to `pragma: no cover` guard |

---

Last updated: 2026-03-03 (v1.8.8)
