# Changelog

All notable changes to this project will be documented in this file.

Each release entry includes a **Breaking changes** line. Entries marked `none` are safe to upgrade.

---

## [3.6.0] -- 2026-03-11 -- Governed Memory Mediation

**Breaking changes:** none

### Added

- **MemoryView enum** (`memory/types.py`): 7 memory namespace views -- agent_private, local_working, team_shared, session_state, verified_archive, provisional_archive, quarantined.
- **ExecutionMode enum** (`memory/types.py`): 5 runtime modes -- live_execution, replay, simulation, consolidation, audit_review.
- **DegradeDirective** (`memory/types.py`): Frozen dataclass for structured DEGRADE parameters -- mode, max_packet_tokens, verified_only, summary_required, raw_replay_blocked, namespace_downscoped_to, redacted_fields, max_content_size_bytes.
- **CompactnessConstraints** (`memory/types.py`): Frozen dataclass for packet size and content policy -- max_packet_tokens, max_raw_replay_ratio, require_compaction_if_over_budget, prefer_verified_summary, max_attributes_per_packet, max_payload_bytes.
- **MessageContext** (`memory/types.py`): Frozen dataclass for message governance context with metadata freezing and content_size_bytes validation.
- **BridgePolicy** (`memory/types.py`): Frozen dataclass for message-to-memory promotion rules -- allow_archive, require_signature, quarantine_untrusted.
- **ThreatContext** (`memory/types.py`): Frozen dataclass for threat-aware audit metadata -- threat_hypothesis, mitigation_applied, effective_scope, effective_view, compactness_enforced, source_trust, source_provenance.
- **CompactnessEvaluator** (`memory/compactness.py`): MemoryGovernanceHook implementation. Hard limit (max_payload_bytes -> DENY), soft limits (packet_tokens, attributes, raw_replay_ratio -> DEGRADE with merged DegradeDirective), prefer_verified_summary.
- **ViewPolicyEvaluator** (`memory/view_policy.py`): MemoryGovernanceHook implementation. Trust-ranked access matrix (untrusted < provisional < trusted < privileged) across all 7 views and 5 execution modes. AGENT_PRIVATE owner-only, CONSOLIDATION exceptions, AUDIT_REVIEW quarantined read.
- **MessageGovernanceHook protocol** (`memory/message_governance.py`): Runtime-checkable protocol for message-level governance (before_message / after_message).
- **DefaultMessageGovernanceHook** (`memory/message_governance.py`): Fail-open default.
- **DenyOversizedMessageHook** (`memory/message_governance.py`): Size-based DENY/DEGRADE with configurable threshold and DegradeDirective.
- **MessageBridgeHook** (`memory/message_governance.py`): BridgePolicy evaluation -- archive eligibility, signature requirement, type filtering, quarantine routing.
- **Governor DegradeDirective merging** (`memory/governor.py`): `_merge_directives()` with per-type merge rules (bool=OR, int=max, str=last-non-empty, tuple=union). Accumulated directive attached only when final verdict is DEGRADE. ThreatContext propagated from worst-verdict hook.
- **6 new ReasonCode members** (`kernel/decision.py`): MEMORY_GOVERNANCE_DEGRADE, MEMORY_VIEW_DENIED, EXECUTION_MODE_DENIED, COMPACTNESS_EXCEEDED, MESSAGE_GOVERNANCE_DENIED, BRIDGE_PROMOTION_DENIED.
- **to_audit_dict() extensions** (`memory/types.py`): DegradeDirective and ThreatContext serialized to flat audit dict.
- **docs/GOVERNED_MEMORY_MEDIATION.md**: Responsibility boundary, mediation pipeline, DEGRADE semantics, compactness policy, memory views, execution modes, message governance, bridge policy, threat-aware audit, data flow diagram.
- **339 new tests** (175 functional + 164 adversarial): Concurrent access (10-thread), corrupted inputs (NaN/Inf/garbage), boundary abuse, privilege escalation, type immutability, governor error handling, evaluation order verification.

---

## [3.5.0] -- 2026-03-11 -- Kernel Hardening + Stable Contract

**Breaking changes:** none

### Added

- **DecisionEnvelope** (`kernel/decision.py`): Frozen attestation wrapper carrying decision, policy_hash, reason_code, audit_id (UUID4), timestamp, policy_epoch, issuer, and metadata. 7 decision values (ALLOW, DENY, HALT, DEGRADE, QUARANTINE, RETRY, QUEUE). `make_envelope()` factory with auto-generated audit fields.
- **ReasonCode enum** (`kernel/decision.py`): 17 machine-readable reason codes (BUDGET_EXCEEDED, STEP_LIMIT, CIRCUIT_OPEN, SHELL_BLOCKED, etc.).
- **Signed policy bundles** (`security/policy_signing.py`): HMAC-SHA256 bundle signing with canonical form. Newline injection defense on policy_id/issuer/version fields. Ed25519 support via optional `cryptography` package.
- **Policy verification** (`policy/verifier.py`): 6-check verification pipeline -- content hash, epoch, rule types, duplicate IDs, signature requirement, signature verification. Fail-closed: signed bundle without signer is rejected. `verify_or_halt()` production entry point.
- **Tamper-evident audit chain** (`audit/log.py`): SHA-256 hash chain with optional per-entry HMAC-SHA256 signing. Chain verification rejects entries with missing HMAC when signer is provided. Internal consistency only -- not publicly verifiable.
- **HA-ready ABI types** (`kernel/ha.py`): `ReservationState`, `Reservation`, `HeartbeatSnapshot`, `BreakerReflection` -- passive observation types for future HA integration. No business logic, no consensus.
- **Budget denial envelope wiring**: `BudgetEnforcer.check()` attaches `DecisionEnvelope` on all 4 DENY paths (zero-budget, exceeded, invalid cost, would-exceed). ALLOW path unchanged (envelope=None). First production use of `make_envelope()`.
- **Kernel contract doc** (`docs/kernel-contract.md`): Boundary definition (kernel vs control plane), hook inventory, decision vocabulary, envelope status, audit signing status, HA ABI status, sample audit payload.

### Fixed

- **Overstated docs** (`kernel/decision.py`, `kernel/__init__.py`): "all governance decisions" corrected to "governance decisions"; "is wrapped" corrected to "can be wrapped". Envelope is opt-in per path, not mandatory.
- **Hex validation** (`audit/log.py`): `_load_last_hash` now validates 64-char hex format via `int(h, 16)` to reject non-hex strings.
- **Newline injection** (`security/policy_signing.py`): `sign_bundle` rejects `\n` and `\r` in metadata string fields to prevent canonical form injection.
- **Fail-closed verification** (`policy/verifier.py`): Signed bundle with no signer now produces an error (was silently passing).
- **README accuracy**: Security audit rounds corrected from 5 to 4 (130+ findings). Test count updated to 4992.

### Tests

- 7 envelope wiring tests in `TestBudgetEnvelopeWiring` (4 DENY paths, ALLOW-no-envelope, audit_id uniqueness, reason consistency).
- 4 newline injection adversarial tests in `test_signed_policy_bundle.py`.
- Updated `test_adversarial_policy.py` for fail-closed signed-bundle-no-signer behavior.
- F.R.I.D.A.Y. 3-unit review-fix loop: 2 consecutive clean rounds achieved on each change set.

---

## [3.4.3] -- 2026-03-10 -- Security Hardening

**Breaking changes:** none

### Fixed

- **NaN/Inf bypass guards**: `circuit_breaker`, `degradation`, `adaptive_budget`, `budget_allocator`, `execution_graph` -- `math.isfinite()` validation on numeric thresholds and cost inputs to prevent silent policy bypass via `float('nan')`.
- **Bool-as-int coercion guard**: `circuit_breaker.failure_threshold` -- explicit `isinstance(v, bool)` check before `isinstance(v, int)` to prevent `True`/`False` being accepted as valid thresholds.
- **Info leakage prevention**: `retry`, `governor`, `execution_context`, `key_providers`, `verifier` -- replaced `str(exc)` with `type(exc).__name__` in error messages and logs to avoid exposing internal URLs, credentials, or stack details.
- **Log injection prevention**: `distributed_circuit_breaker` -- 4 log paths now use `_redact_exc()` to sanitize Redis URLs from exception messages.
- **Thread safety**: `runtime_policy.__len__()` and `retry` property reads now acquire lock for nogil readiness.
- **Agent-id coercion**: `memory_boundary` -- `None`-safe `str()` conversion prevents `str(None)` producing literal `"None"` as agent ID.
- **Resource exhaustion**: `adaptive_budget` event buffer capped at 1M entries; MCP stats dict hard-capped at 10K distinct tool names (was warn-only).
- **Vault key parsing**: `VaultKeyProvider` filters non-numeric version keys to prevent `int()` crash on malformed Vault responses.

### Tests

- 2 test updates: `test_adversarial_memory_gov` assertion aligned with info-leakage fix; `test_budget_allocator` NaN weight test updated from "no crash" to "rejected with ValueError". Total: 4837 (net -7 from test consolidation).

---

## [3.4.2] -- 2026-03-08 -- Review-Fix Hardening + AG2 Merge

**Breaking changes:** none

### Fixed

- **Version comparison tuple-length mismatch**: `_compare_versions()` with zero-padding prevents `(0, 4) < (0, 4, 0)` false inequality when comparing 2-segment vs 3-segment version strings in `is_version_compatible()`.
- **MemoryBoundaryHook docstring accuracy** (Round 1-4): Corrected trust_router vs trust_tracker references, rule priority order documentation to match `_rule_specificity` scoring (agent_id=+2, namespace=+1), PROVISIONAL trust label, and `default_trust` impact on unknown agent access.
- **Info leak prevention**: Internal exception details removed from `PermissionError` message in trust resolution failure path -- logged only.
- **Adapter scaffold regex strictness**: `_VALID_NAME_RE` restricted to lowercase-only (`^[a-z][a-z0-9_-]*$`) to prevent silent PascalCase mismatch on uppercase input.
- **CI ruff-check skip**: Tests requiring ruff now guarded by `_ruff_available()` check with `@_SKIP_NO_RUFF` marker for environments without ruff installed.
- **Fixture skip safety**: `_build_fixtures` replaced module-level `pytest.skip()` with `warnings.warn()` to prevent silent test module exclusion.

### Tests

- 4 new tests: 2 version tuple-length regression tests in `test_adapter_harness.py`, 1 tracker-without-router trust check in `test_trust_memory_isolation.py`, 1 uppercase name rejection in `test_adapter_scaffold.py`. Total: 4844.

---

## [3.4.1] -- 2026-03-08 -- Post-Release Hardening

**Breaking changes:** none

### Fixed

- **Trust-router condition** (#73): Simplified trust check from `trust_router or trust_tracker` to `trust_tracker is not None` -- prevents false DENY when only trust_router is provided without tracker.
- **UNCONSTRAINED_VERSIONS constant**: Replaced magic tuple `("0.0.0", "99.99.99")` with named constant in `adapter_capabilities.py` and `test_adapter_harness.py`.
- **Adapter scaffold ruff check**: `_ruff_check` helper now uses `shutil.which("ruff")` fallback for standalone ruff binaries.
- **Test consolidation**: `TestGeneratedContent` consolidated from 6 separate tests to 1 parametrized test (21 tests, was 26).
- **Ship Readiness CI gate**: Updated version reference for Publish to PyPI quality gate.

### Tests

- `tests/test_memory_boundary_hook.py` -- 5 new adversarial boundary abuse tests (same-specificity ordering, 100-rule performance, wildcard specificity scoring, deny_count non-negative, 10-thread mixed allow/deny). Total: 35 tests.

---

## [3.4.0] -- 2026-03-08 -- Memory Boundary + Trust Isolation + Adapter Tooling

**Breaking changes:** none

### Added

- **MemoryBoundaryHook** (#71): Dual-protocol hook (PostDispatchHook + MemoryGovernanceHook) for trust-based memory access control. Declarative `MemoryAccessRule` with agent_id/namespace wildcard matching and specificity scoring. Intercepts `memory_read`/`memory_write` calls and enforces per-agent namespace access rules.
- **Trust-based memory isolation** (#73): Integration with `TrustEscalationTracker` for namespace protection. UNTRUSTED agents denied all access to trusted namespaces, PROVISIONAL agents get read-only access, TRUSTED/PRIVILEGED agents get full access. Fail-closed for unknown trust levels.
- **Adapter test harness** (#68): Parameterized test suite across all 5 framework adapters (LangChain, LangGraph, AG2, CrewAI, LlamaIndex) with stub injection for optional dependencies. 98 tests covering instantiation, ALLOW/HALT paths, metrics emission, capabilities, concurrency, and error handling.
- **Adapter version declaration** (#69): `AdapterCapabilities.supported_versions` field with `is_version_compatible()` method for semver-style range checking. All 5 adapters declare real version ranges.
- **Adapter scaffold generator** (#70): `generate_adapter()` API and `python -m veronica_core.cli new-adapter` CLI for generating adapter + test boilerplate. Validates framework names, prevents overwrites, generates ruff-clean code.

### Tests

- `tests/test_memory_boundary_hook.py` -- 30 tests (rules, wildcards, governor integration, PostDispatchHook, concurrent, adversarial)
- `tests/test_trust_memory_isolation.py` -- 15 tests (trust levels, transitions, concurrent, adversarial)
- `tests/test_adapter_harness.py` -- 98 parameterized tests across 5 adapters
- `tests/test_adapter_scaffold.py` -- 26 tests (generation, content, naming, edge cases)

---

## [3.3.0] -- 2026-03-08 -- Memory Governance + Policy Audit Wiring

**Breaking changes:** none

### Added

- **Memory Governance integration**: `MemoryGovernor` wired into `ExecutionContext._wrap()` pre-dispatch and `AIContainer.check()` post-pipeline. `MemoryGovernor.evaluate()` enforces fail-closed DENY on errors, None returns, and unknown verdicts. QUARANTINE/DEGRADE treated as "allow with annotation".
- **Policy audit wiring**: All chain events (`abort`, `circuit_open`, `budget_exceeded`, `budget_exceeded_by_child`, `memory_governance_denied`, and limit-check callbacks) automatically enriched with `FrozenPolicyView.to_audit_dict()` metadata via unified `_emit_chain_event()` path.
- **`_build_memory_op()` shared helper**: Eliminates duplication between `_check_memory_governance()` and `_notify_memory_governance_after()`.
- **`_notify_memory_governance_after()`**: Post-dispatch notification to governance hooks. Catches `BaseException` to honour "never raises" contract and prevent successful call corruption.
- **`_get_policy_audit_metadata()`**: Extracts JSON-serializable policy metadata from `PolicyViewHolder`. Never raises -- swallows errors to avoid disrupting containment control flow.
- **`_make_emit_chain_event_cb` policy enrichment**: Limit-exceeded callbacks now include active policy metadata in emitted events.
- **`ContextSnapshot.policy_metadata`**: New optional field on snapshot for policy audit trail.
- **`_ChainEventLog.emit_chain_event` `policy_metadata` parameter**: Stores policy dict under `"policy"` key in `SafetyEvent.metadata`.
- **`_STOP_REASON_EVENT_TYPE["memory_governance_denied"]`**: Maps to `CHAIN_MEMORY_GOVERNANCE_DENIED`.
- **Child context propagation**: `create_child()` and `spawn_child()` propagate `memory_governor` and `policy_view_holder` to child contexts.
- **Memory subsystem**: `memory.governor`, `memory.hooks`, `memory.types` -- governance hook framework with verdict aggregation (DENY short-circuits, QUARANTINE > DEGRADE > ALLOW).
- **Policy subsystem**: `policy.bundle`, `policy.frozen_view`, `policy.verifier`, `policy.audit_helpers` -- immutable policy bundles with content-hash verification and frozen audit views.
- **Tracker decomposition**: `_budget_tracker`, `_step_tracker`, `_retry_tracker`, `_timeout_manager` extracted from `_limit_checker`.

### Fixed

- **Emit path unification**: 3 direct `_event_log.emit_chain_event()` calls (abort, circuit_breaker, budget_exceeded_by_child) now route through `_emit_chain_event()` for consistent policy metadata enrichment.
- **`_make_emit_chain_event_cb` missing policy metadata**: Limit-exceeded events (budget, step, retry, timeout) now include policy metadata via closure-captured `_get_policy_audit_metadata`.
- **`AIContainer._check_memory_governor` silent error swallowing**: Added `logger.error()` for governor evaluation failures.
- **`_notify_memory_governance_after` BaseException leak**: Changed from `except Exception` to `except BaseException` to prevent SystemExit/KeyboardInterrupt from corrupting successful call node status.

### Tests

- `tests/test_adversarial_v33_integration.py` -- 64 adversarial tests across 14 categories (hook poisoning, concurrent access, policy view corruption, state manipulation, notify_after failure, chain event flooding, TOCTOU, boundary abuse, AIContainer, emit path unification, limit-exceeded metadata, concurrent _build_memory_op, child propagation, BaseException swallowing).
- `tests/test_policy_audit_wiring.py` -- 14 integration tests for policy metadata -> audit event wiring.
- `tests/test_memory_gov_integration.py` -- happy-path tests for ExecutionContext with MemoryGovernor.
- Plus unit tests for memory governor, hooks, types, policy bundle, frozen view, verifier, and individual trackers.
- 4611 tests total, ruff clean.

---

## [3.2.0] -- 2026-03-08 -- Context Decomposition + nogil Readiness

**Breaking changes:** none

### Changed

- **ExecutionContext decomposition**: Extracted `_LimitChecker` (step/cost/retry counters, limit enforcement) and `_ChainEventLog` (SafetyEvent dedup, append, snapshot) into focused internal helpers. Reduces `execution_context.py` from 1536 to ~1430 lines.
- **Atomic operations**: `commit_success()` atomically increments step count and adds cost in a single lock acquisition. `add_cost_and_get_total()` eliminates TOCTOU in `_propagate_child_cost`.
- **Snapshot-then-emit**: `check_limits()` snapshots counters under the lock, then emits events outside the lock to avoid holding the lock during potentially slow I/O.
- **Setter encapsulation**: Compatibility shims (`_cost_usd_accumulated`, `_step_count` setters) now delegate to `set_cost()` / `set_step_count()` instead of accessing internal `_lock` directly.
- **Lambda elimination**: Pre-built emit callback cached in `__init__` instead of allocating a lambda on every `_check_limits_delegate` call.

### Added

- **nogil (PEP 703) audit**: 9 modules audited for GIL-dependent patterns. 2 real races fixed:
  - `distributed_circuit_breaker.py`: `state`, `failure_count`, `success_count`, `reset()` now snapshot `_using_fallback` and `client` atomically under `self._lock` before I/O.
  - `integration.py`: Added `_class_lock` protecting `_atexit_registered` and `_live_instances` in `__init__` and `_save_all_instances`.
- 7 modules confirmed GIL-safe with explicit lock annotations: `circuit_breaker.py`, `distributed.py`, `execution_graph.py`, `budget.py`, `adaptive_budget.py`, `ingester.py`, `_shared.py`.
- `tests/test_limit_checker.py` -- 43 tests across 7 classes.
- `tests/test_chain_event_log.py` -- 27 tests across 6 classes.
- `tests/test_nogil_safety.py` -- 12 tests across 3 classes (adversarial threading).
- 4147 tests total, ruff clean.

---

## [3.1.0] -- 2026-03-08 -- Kernel Stabilization

**Breaking changes:** none

### Changed

- **PEP 562 lazy imports**: Converted 136 eager imports to on-demand loading via `__getattr__` + `_LAZY_IMPORTS` registry. 7 core symbols remain eager (`VeronicaState`, `StateTransition`, `VeronicaStateMachine`, `ExecutionConfig`, `ExecutionContext`, `WrapOptions`). Import time reduced for consumers that use only core types.
- **time.monotonic migration**: `CircuitBreaker._last_failure_time`, `BudgetWindowHook`, and `ExecutionGraph._init_time` now use `time.monotonic()` for local timers. Wall-clock `time.time()` preserved for cross-process timestamps (Redis, persist, distributed circuit breaker).
- **adapter/ to adapters/ unification**: `SecureExecutor` and related types moved from `veronica_core.adapter` to `veronica_core.adapters.exec`. Old import paths emit `DeprecationWarning` and re-export transparently.

### Fixed

- **BudgetEnforcer zero-budget**: `limit_usd=0.0` now correctly blocks all calls (was allowing due to missing early return). `utilization` property returns `1.0` instead of `float('inf')` for zero budgets.
- **persist.py encoding**: Added explicit `encoding="utf-8"` to save/load operations for cross-platform consistency.
- **middleware.py**: Fixed ruff E402 import ordering violation.
- **Test deduplication**: Removed duplicate `test_utilization_zero_limit_returns_one` from two test files.

### Added

- `tests/conftest.py` -- shared fixtures (`default_config`, `ctx`, `strict_config`, `strict_ctx`, `wrap_options`) for 157 test files.
- `tests/test_persist.py` -- 18 tests for deprecated-but-used `VeronicaPersistence` (roundtrip, corrupted JSON, binary garbage, concurrent writes, backup).
- 3 new `TestBudgetZeroLimit` tests for zero-budget edge cases.

---

## [3.0.4] -- 2026-03-08 -- Full Security Audit (24 Rounds)

### Fixed

- **HIGH**: Symlink check after `Path.resolve()` always returned False -- now checks unresolved path before resolution, and evaluates policy on both original and resolved paths (`adapter/exec.py`).
- **HIGH**: NonceRegistry evicted live nonces on capacity -- switched to fail-closed (reject new approvals when full) to prevent replay attacks (`approval/approver.py`).
- **HIGH**: `uv run` inner command bypassed full shell policy pipeline -- now checks deny commands, operators, credentials, exec flags, package install, and allowlist (`security/policy_rules.py`).
- **HIGH**: npm/pnpm option-parsing bypass hid dangerous subcommands -- switched to position-independent scanning of all non-option tokens (`security/policy_rules.py`).
- **MEDIUM**: Policy evaluator exceptions silently returned None (default DENY) -- now explicitly catches and returns DENY with `EVALUATOR_ERROR` rule_id (`security/policy_engine.py`).
- **MEDIUM**: HTTPS-only scheme enforcement missing in network policy -- non-HTTPS URLs now denied with `NET_DENY_SCHEME` (`security/policy_rules.py`).
- **MEDIUM**: HTTP proxy bypass via system proxy settings -- disabled proxy with `ProxyHandler({})` (`adapter/exec.py`).
- **MEDIUM**: HTTP redirect loop not detected -- added visited-URL tracking and max-redirect limit with no-auto-redirect opener (`adapter/exec.py`).
- **MEDIUM**: VaultKeyProvider accepted HTTP URLs, exposing VAULT_TOKEN -- added HTTPS enforcement (`security/key_providers.py`).
- **MEDIUM**: Windows sandbox `--key=value` arguments bypassed path check -- now splits on `=` and checks value part (`runner/sandbox_windows.py`).
- **MEDIUM**: Drive-less rooted paths (`/Users/Alice`) bypassed sandbox block -- now prepends current drive on Windows (`runner/sandbox_windows.py`).
- **MEDIUM**: UNC paths and extended-length prefix (`\\?\`) bypassed sandbox block -- added normalization (`runner/sandbox_windows.py`).
- **MEDIUM**: `python -c<code>` attached form and combined short flags (`-Ic`) bypassed inline-exec deny (`security/policy_rules.py`).
- **MEDIUM**: npm/pnpm `exec`/`dlx`/`x` subcommands not denied -- added `SHELL_DENY_PKG_EXEC` rule (`security/policy_rules.py`).
- **MEDIUM**: `uv --color always run` bypassed subcommand detection -- added `--color` to `_UV_OPTS_WITH_VALUE` (`security/policy_rules.py`).
- **MEDIUM**: Credential check only inspected argv1 -- now scans all non-option tokens (`security/policy_rules.py`).
- **MEDIUM**: git `--exec-path` not in options-with-value set -- added to `_GIT_OPTS_WITH_VALUE` and scans all non-option tokens for deny subcommands (`security/policy_rules.py`).
- **LOW**: Key pin check failed when public key file absent but key_provider available -- added fallback chain (`security/policy_engine.py`).
- **LOW**: Shell `cwd` not resolved to canonical path -- now uses `_resolve_path` before passing to subprocess (`adapter/exec.py`).
- **LOW**: Atomic file writes using `mkstemp` for crash safety (`backends.py`, `distributed.py`, `persist.py`, `runner/attestation.py`).
- **LOW**: Sandbox `_temp_dir` not cleared on non-owned teardown (`runner/sandbox.py`).
- **LOW**: Case-insensitive prefix check in `_sandbox_ignore` (`runner/sandbox.py`).
- **LOW**: Cross-platform drive-letter path detection in sandbox_windows -- Linux `os.path.isabs` does not recognise `C:/` paths (`runner/sandbox_windows.py`).

**Breaking changes**: none.

---

## [3.0.3] -- 2026-03-06 -- Parallel Audit Fix

### Fixed

- **HIGH**: NTFS junction sandbox escape -- `os.path.islink()` returns False for junctions; added `_is_junction()` helper with `Path.is_junction()` (3.12+) and ctypes `GetFileAttributesW` fallback (3.11).
- **MEDIUM**: Case-insensitive suffix bypass -- `_sandbox_ignore()` now uses `name.lower().endswith()` to catch uppercase extensions (e.g. `secret.PEM`) on all platforms.

### Added

- `_is_junction()` helper in `runner/sandbox.py` -- cross-version NTFS junction detection (Python 3.11+).
- 27 new tests for `_sandbox_ignore()`: symlink rejection, NTFS junction rejection, credential denylist (all names), suffix matching, case-insensitive suffix, prefix matching, safe file passthrough.
- 23 new audit coverage tests: URL exfil (semicolon query, matrix params, %2F per-segment, backslash userinfo), ComplianceExporter HTTPS enforcement (7 tests), AsyncMCPContainmentAdapter sync/async budget dispatch (3 tests), WSGI iterable close + exception logging (3 tests).

**Breaking changes**: none.

---

## [3.0.2] -- 2026-03-06 -- Independent Audit Fix

### Fixed

- **HIGH**: URL percent-encoding bypass in `_check_data_exfil()` -- path segments and fragments now URL-decoded before exfiltration checks.
- **HIGH**: POSIX sandbox `copytree()` now excludes secret files (`.env`, `*.key`, `*.pem`, `*.p12`, `*.pfx`, `credentials.json`) matching Windows sandbox behavior.
- **HIGH**: `AsyncMCPContainmentAdapter` now correctly `await`s budget backend `reserve()`/`commit()`/`rollback()` with sync/async dual dispatch.
- **HIGH**: `AdaptiveBudgetHook._event_buffer` deque now bounded with `maxlen` to prevent memory exhaustion under sustained load.
- **HIGH**: `anomaly_recent_seconds > window_seconds` now clamped with warning (was silently disabling spike detection).
- **MEDIUM**: `ComplianceExporter` endpoint HTTPS enforcement via `urlparse` hostname check (blocks `http://localhostevil.com` prefix bypass).
- **MEDIUM**: `PartialResultBuffer` fully thread-safe -- all properties, `set_metadata()`, `to_dict()` now hold lock.
- **MEDIUM**: `JSONBackend.save()` protected with `threading.Lock` against concurrent writes.
- **MEDIUM**: `VeronicaExit` signal handler registration guarded against non-main-thread `ValueError`.
- **MEDIUM**: `BudgetWindowHook` and `TokenBudgetHook` validate `degrade_threshold` in (0.0, 1.0].
- **MEDIUM**: WSGI middleware now handles app exceptions with proper 429-on-halt (matching ASGI behavior).
- `PolicySignerV2.sign()` accepts `policy_bytes` parameter for TOCTOU prevention (matching `verify()`).
- `_load_key()` enforces max key length (1024 bytes) before hex decode to prevent memory allocation attacks.
- Flaky CI test `test_step_and_budget_checked_atomically_no_toctou` fixed -- step_count is post-increment by design; test now validates correct invariant (step_count == ALLOW count).

### Added

- `AsyncBudgetBackendProtocol` and `ReconciliationCallback` now exported from `veronica_core.__init__`.

**Breaking changes**: none. `BudgetWindowHook(degrade_threshold=0.0)` now raises `ValueError` (was silently degrading every call).

---

## [3.0.1] -- 2026-03-06 -- Full Codebase Security Audit Fix

### Fixed

- **CRITICAL**: Policy engine TOCTOU race -- verify and load now use the same bytes (no re-read from disk).
- **CRITICAL**: Data exfiltration via URL path segments, query keys, userinfo, and fragments now detected by `_check_data_exfil()`.
- **CRITICAL**: HMAC key minimum length enforced (32 bytes / 256-bit) in `PolicySigner._load_key()`.
- **CRITICAL**: `FrameworkAdapterProtocol` narrowed to `capabilities()` only; new `ExtendedAdapterProtocol` for optional methods. `isinstance()` now returns True for all adapters.
- **HIGH**: Git subcommand bypass via global options (`git -c key=val push`) closed with option-value-aware parsing.
- **HIGH**: `PASSWORD_KV` masking pattern now detects quoted YAML values (`password: "secret"`).
- **HIGH**: `SemanticLoopGuard` and `PolicyPipeline` now thread-safe (threading.Lock).
- **HIGH**: `BudgetWindowHook` deque maxlen increased (max_calls * 10) to prevent burst undercount.
- **HIGH**: `_BudgetProxy.spend()` returns False (fail-safe) when `_get_fn` is None.
- **HIGH**: `audit_chain.py` hash comparison now constant-time (`hmac.compare_digest`).
- **HIGH**: `sandbox_windows.py` blocks 2-char drive paths; `dirs_exist_ok=False`.
- 46 medium/low fixes: documentation, validation, thread safety, API exports.

### Added

- `ExtendedAdapterProtocol` -- optional protocol for adapters supporting cost/token extraction and halt/degrade signals.
- `Capability` and `enable_otel_with_tracer` now exported from `veronica_core.__init__`.
- `_utils.py` -- shared `redact_exc()` (eliminates duplication between `distributed.py` and `distributed_circuit_breaker.py`).
- `SafeModeHook.disable()` method and `enabled` setter.
- `__all__` added to 9 core modules.

**Breaking changes**: none. All fixes are backward-compatible.

---

## [3.0.0] -- 2026-03-05 -- God Class Split, Adapter Capabilities, Audit Chain

### Added

- `AdapterCapabilities` frozen dataclass: static capability declaration for framework adapters (`supports_streaming`, `supports_cost_extraction`, `supports_token_extraction`, `supports_async`, `supports_reserve_commit`, `supports_agent_identity`, `framework_name`, `framework_version_constraint`, `extra`).
- `FrameworkAdapterProtocol.capabilities()` method: adapters now declare their features at runtime.
- All 9 adapters (LangChain, LangGraph, CrewAI, LlamaIndex, AG2, MCP sync/async, ROS2, AG2Capability) implement `capabilities()`.
- `AuditChain`: tamper-proof hash chain for safety events using SHA-256. Append-only, thread-safe, with `verify()`, `export_json()`, `from_json()`.
- `AuditEntry` frozen dataclass: sequence, timestamp, prev_hash, data, entry_hash.
- 38 new tests (15 adapter capabilities + 23 audit chain, including adversarial: concurrent appends, replay attacks, forged entries, corrupted imports).

### Changed

- **God Class Split** (no API changes, backward-compatible re-exports):
  - `distributed.py` (1596 lines) split into `distributed.py` (709) + `distributed_circuit_breaker.py` (914).
  - `security/policy_engine.py` (1156 lines) split into `policy_engine.py` (435) + `policy_rules.py` (677).
  - `containment/execution_context.py` (1704 lines) split into `execution_context.py` (1531) + `types.py` (204).
- All original import paths preserved via re-exports.

### Breaking changes

- `FrameworkAdapterProtocol` now requires `capabilities()` method. Existing adapters that use `isinstance()` checks must add this method.

---

## v2.0 -- v2.7 Release Series Summary

Eight releases in three days. 1311 new tests (2563 to 3874). Zero breaking changes from v2.1.0 onward.

**v2.0.0 -- Reserve/Commit/Rollback.** Two-phase budget protocol (`reserve` / `commit` / `rollback`) on local and Redis backends. Async budget backends (`AsyncLocalBudgetBackend`, `AsyncRedisBudgetBackend`). WebSocket containment via ASGI middleware (`close(1008)` on step exhaustion). `CancellationToken` parent/child hierarchy with upward cost propagation. `SharedTimeoutPool` singleton for process-wide deadline scheduling. 627 new tests.

**v2.1.0 -- Declarative Policy, Adaptive Threshold, Multi-tenant Budget.** YAML/JSON policy loader with hot-reload watcher and 7 builtin rule types. `AdaptiveThresholdPolicy` with burn-rate estimation and spike detection. `AnomalyDetector` using Welford's online Z-score. `TenantRegistry` with Organisation/Project/Team/Agent hierarchy and ancestor-walk resolution. `BudgetPool` with distributed reserve/commit/rollback integration. 231 new tests (107 adversarial).

**v2.2.0 -- OTel Feedback Loop.** `OTelMetricsIngester` parses AG2, veronica-core, and OpenLLMetry spans into per-agent metrics. `MetricsDrivenPolicy` evaluates declarative `MetricRule` thresholds (gt/lt/gte/lte/eq) with severity ordering. Sliding-window cost tracking. Agent cardinality cap (10K). NaN/Inf threshold rejection. 229 new tests.

**v2.3.x -- ExecutionGraph Hooks + Safety Hardening.** Dynamic observer/subscriber registration on `ExecutionGraph`. `NodeEvent` frozen dataclass with 13 fields. Copy-on-write lock-free iteration. v2.3.1: deprecated API cleanup (`AIcontainer`, `VeronicaPersistence`, `GuardConfig.timeout_ms`), `ExecutionContext.close()`. 42 tests.

**v2.4.0 -- Code Quality.** `Decision` enum migration across MCP adapters. `build_adapter_container()` shared factory. CrewAI `execution_context=` kwarg. Snapshot reuse in `ExecutionContextContainerAdapter.check()`. 23 adversarial tests.

**v2.5.0 -- HALT Unification.** Shared `check_and_halt()` across all 5 framework adapters (AG2, CrewAI, LangChain, LangGraph, LlamaIndex). `emit_metrics_decision()` / `emit_metrics_tokens()` helpers. `metrics=` and `agent_id=` kwargs on all adapters. `docs/API.md` rewritten (739 lines). 37 new tests.

**v2.6.0 -- Policy Simulation.** `PolicySimulator` replays execution logs against `ShieldPipeline` configs. `ExecutionLog` with JSON file, string, and OTel span import. `SimulationReport` with per-agent breakdown, `savings_percentage`, `summary()`, `to_dict()`. NaN-safe cost accumulation via `math.isfinite()`. 51 new tests (22 adversarial).

**v2.7.0 -- A2A Trust Boundary.** Cross-agent trust classification with 4 tiers (`TrustLevel`: UNTRUSTED, PROVISIONAL, TRUSTED, PRIVILEGED). `AgentIdentity` frozen dataclass with origin validation. `TrustBasedPolicyRouter` maps trust levels to `ShieldPipeline` configs. `TrustEscalationTracker` with per-agent locking, cardinality cap (10K), O(1) index-based promotion, automatic demotion on failure. 59 new tests (31 adversarial).

---

## [2.7.0] -- 2026-03-05 -- A2A Trust Boundary

### Added

- `TrustLevel(str, Enum)` with 4 tiers: UNTRUSTED, PROVISIONAL, TRUSTED, PRIVILEGED.
- `AgentIdentity` frozen dataclass with origin validation (`local`, `a2a`, `mcp`).
- `TrustPolicy` frozen dataclass: promotion threshold, ceiling, default trust.
- `TrustBasedPolicyRouter`: maps `TrustLevel` to `ShieldPipeline`, read-only after init.
- `TrustEscalationTracker`: per-agent locking, double-checked locking for creation, cardinality cap (10K), automatic promotion/demotion.
- 59 tests: 28 happy-path + 31 adversarial (concurrent access, cardinality race, rapid promote/demote, string comparison regression).

### Breaking changes

none

## [2.6.0] -- 2026-03-05 -- Policy Simulation

**Breaking changes:** none.

### Added

- **`veronica_core.simulation` package** -- replay historical execution logs against policy configurations for what-if analysis.
- **`ExecutionLogEntry`** -- frozen dataclass recording a single action (llm_call, tool_call, reply) with cost, tokens, latency, and success status.
- **`ExecutionLog`** -- collection with factory methods: `from_file()` (JSON), `from_string()`, `from_otel_export()` (OTel span dicts).
- **`PolicySimulator`** -- replays log entries against a `ShieldPipeline`, evaluating `before_llm_call`, `before_tool_call`, `before_charge`, and `on_error` hooks per entry.
- **`SimulationReport`** -- aggregate statistics (allowed/halted/degraded/warned counts, cost_saved_estimate, savings_percentage, per-agent breakdown) with `summary()` and `to_dict()`.
- **`SimulationEvent`** -- frozen dataclass recording each policy decision during replay.
- **NaN-safe cost accumulation** -- `math.isfinite()` guards prevent NaN/Inf costs from corrupting report totals.
- **51 new tests** -- 29 happy-path + 22 adversarial (corrupted input, concurrent access, state accumulation, boundary conditions, pipeline edge cases).

---

## [2.5.0] -- 2026-03-05 -- HALT Unification & Metrics Wiring

**Breaking changes:** none.

### Added

- **`check_and_halt()`** (`_shared.py`) -- shared helper centralizing `container.check() -> VeronicaHalt` pattern. All 5 framework adapters now use it.
- **`emit_metrics_decision()`**, **`emit_metrics_tokens()`**, **`safe_emit()`** (`_shared.py`) -- ContainmentMetricsProtocol emission helpers with exception swallowing.
- **`metrics=` and `agent_id=` kwargs** on all framework adapters (ag2, crewai, langchain, langgraph, llamaindex). Emits `record_decision` on ALLOW/HALT and `record_tokens` on LLM completion.
- **37 new tests** -- 12 for HALT unification (including adversarial: empty reason, exception propagation), 25 for metrics wiring.

### Changed

- **All framework adapters** migrated from inline `check() -> VeronicaHalt` to shared `check_and_halt()`. Reduces per-adapter HALT logic from 3 lines to 1 call.
- **docs/API.md** rewritten from v1.0.0 to v2.5.0 (739 lines). Covers ExecutionContext, Decision enum, MCP adapters, middleware, metrics, protocols.

---

## [2.4.0] -- 2026-03-05 -- Code Quality & HALT Hardening

**Breaking changes:** none. `MCPToolResult.decision` field type changed from `str` to `Decision` enum, but `Decision` inherits from `str` so `== "ALLOW"` / `== "HALT"` comparisons still work.

### Added

- **`ExecutionContext.close()`** -- explicit resource cleanup extracted from `__exit__`. Idempotent, thread-safe (10-thread concurrent close calls exactly-once backend cleanup).
- **CrewAI `execution_context=` kwarg** -- chain-level `ExecutionContext` enforcement for CrewAI adapter via `build_adapter_container()`.
- **`build_adapter_container()`** (`_shared.py`) -- routes to `ExecutionContextContainerAdapter` when `execution_context` is provided, else standalone `AIContainer`.
- **23 adversarial tests** (`test_v24_adversarial.py`) -- 6 categories: corrupted input, concurrent access, state corruption, boundary abuse, Decision enum backward compat, wrap+close race.

### Changed

- **`MCPToolResult.decision`** default changed from `str "ALLOW"` to `Decision.ALLOW` enum. All internal string literals migrated to enum references.
- **`_try_rollback()`** refactored from `@staticmethod(backend, reservation_id)` to instance method `(self, reservation_id)` using `self._budget_backend`. Eliminates 5 duplicated `self._budget_backend` arguments at call sites.
- **`AsyncMCPContainmentAdapter._ensure_stats()`** -- fast-path added: skips lock acquisition for existing tool names (matching sync adapter behavior).
- **`AsyncMCPContainmentAdapter.__init__()`** -- `_backend_supports_reserve` cached at init time instead of per-call `hasattr` check.
- **`ExecutionContextContainerAdapter.check()`** -- reuses already-fetched snapshot instead of triggering a second `get_snapshot()` call.

### Fixed

- **`_BudgetProxy.spent_usd`** -- `float(None)` crash when `_cost_usd_accumulated` is `None`. Now returns `0.0` safely.

---

## [2.3.1] -- 2026-03-05 -- Safety Hardening & Phase 3 Cleanup

**Breaking changes:** deprecated aliases removed (`AIcontainer`, `VeronicaPersistence`, `GuardConfig.timeout_ms`). Callers must use `AIContainer`, `JSONBackend`/`MemoryBackend`, and `ExecutionContext` timeout respectively.

### Removed (Phase 3 deprecated API cleanup)

- **`AIcontainer` alias** (`veronica_core.container`, `veronica_core`): removed. Use `AIContainer`.
- **`VeronicaPersistence`** (`veronica_core.persist`): removed. Use `JSONBackend` or `MemoryBackend`.
- **`GuardConfig.timeout_ms`**: field removed. Use `ExecutionContext(config=ExecutionConfig(timeout_ms=...))`.
- **`_wrap_legacy_persistence`** (`exit.py`): internal adapter removed with `VeronicaPersistence`.
- **`VeronicaIntegration` legacy mode**: `backend=None` now defaults to `JSONBackend` instead of `VeronicaPersistence`.

### Added

- **`ExecutionContext.close()`**: explicit resource release method for deterministic cleanup.
- **`ContainmentMetrics` wiring**: metrics emission connected to containment decision paths.

### Fixed

- **Silent `except` blocks** converted to `logger.warning` for improved observability.

### Tests

- 18+ adversarial tests added covering removed API boundaries and new methods.
- Test imports migrated from internal paths to public API where applicable.

---

## [2.3.0] -- 2026-03-05 -- ExecutionGraph Extensibility Hooks

**Breaking changes:** none

### Added

- **Dynamic observer registration** (`ExecutionGraph.add_observer` / `remove_observer`):
  - Register `ExecutionGraphObserver` instances after construction.
  - Identity-based dedup prevents duplicate registration.
  - Copy-on-write list replacement for lock-free iteration during notification.
  - `on_decision` callback fires on every `mark_halt` (unconditional, with default reason `"halt"`).
- **`NodeEvent` frozen dataclass** (`veronica_core.containment.NodeEvent`):
  - Lightweight immutable event emitted on terminal transitions (success / fail / halt).
  - 13 fields: `node_id`, `status`, `kind`, `name`, `cost_usd`, `tokens_in`, `tokens_out`, `depth`, `elapsed_ms`, `chain_id`, `model`, `error_class`, `stop_reason`.
  - Built under lock via `_build_node_event` factory (TOCTOU prevention).
- **Subscriber API** (`ExecutionGraph.add_subscriber` / `remove_subscriber`):
  - `Callable[[NodeEvent], None]` callbacks for lightweight event consumption.
  - Identity-based dedup and `is not` removal semantics.
  - Exception isolation: subscriber failures never propagate to the graph caller.

### Hardened (5 audit rounds -- R6-R8 fixes + R9-R10 clean)

- TOCTOU in `NodeEvent` construction: fields now snapshot under lock.
- `mark_halt` stores `halt_reason` (not raw `stop_reason`) for observer/subscriber/snapshot consistency.
- Timestamp comparison uses `is not None` (not truthy check) to handle epoch-zero correctly.

### Tests

- 24 hook-specific tests (happy path + adversarial + concurrent).
- 3697 total tests passing.

---

## [2.2.0] -- 2026-03-05 -- OTel Feedback Loop

**Breaking changes:** none

### Added

- **OTel Feedback Loop** (`veronica_core.otel_feedback`, `veronica_core.policy.metrics_policy`):
  - `OTelMetricsIngester`: Thread-safe span parser accumulating per-agent metrics (tokens, cost, latency, errors). Supports AG2, veronica-core, and OpenLLMetry span formats.
  - `AgentMetrics` dataclass with public `error_count` property and sliding-window cost tracking.
  - `MetricsDrivenPolicy`: Implements `RuntimePolicy` protocol. Evaluates declarative `MetricRule` thresholds with severity ordering (halt > degrade > warn).
  - `MetricRule` dataclass with operator evaluation (`gt`, `lt`, `gte`, `lte`, `eq`), per-agent filtering, and strict validation (rejects NaN/inf thresholds).
  - `PolicyRegistry` integration: `metric_rule` builtin factory with explicit null/empty field rejection.
  - Module-level default ingester via `set_default_ingester()` / `get_default_ingester()`.

### Hardened (5 audit rounds -- R1-R4 adversarial + R5 independent)

- NaN/inf threshold rejection in `MetricRule.__post_init__` (silent bypass / DoS prevention).
- Agent cardinality cap (`max_agents=10,000`) to prevent unbounded state growth.
- Cost window bounded via `deque(maxlen=100,000)` to prevent memory DoS.
- `reset()` lock ordering fix: release global lock before per-agent locks (deadlock prevention).
- `_get_metrics()` upgraded to `logger.warning` on failure (was silent `logger.debug`).
- Non-finite observed metric values filtered via `math.isfinite()`.
- Registry factory rejects null/empty `metric`, `operator`, `action` fields (was silent default).
- `_error_count` exposed via public `AgentMetrics.error_count` property.
- `ingest_span` failure logging added (was bare `except: pass`).

### Tests

- 229 Phase D tests (unit + adversarial Categories 13-17).
- 3684 total tests passing.

---

## [2.1.0] -- 2026-03-05 -- Declarative Policy, Adaptive Threshold, Multi-tenant Budget

**Breaking changes:** none

### Added

- **Declarative Policy Layer** (`veronica_core.policy`):
  - `PolicySchema` / `RuleSchema` dataclasses with validation and contradictory-rule detection.
  - `PolicyRegistry` with 6 builtin rule types (`token_budget`, `cost_ceiling`, `rate_limit`, `circuit_breaker`, `step_limit`, `time_limit`). Thread-safe, singleton default instance.
  - `PolicyLoader`: load from YAML/JSON files or strings, `validate()` without building, `watch()` for hot-reload with cancellable `WatchHandle`.
  - `LoadedPolicy` wrapper delegating to `ShieldPipeline` with introspectable `hooks` list.
  - Optional `pyyaml` dependency via `pip install veronica-core[yaml]`.

- **Adaptive Budget Policy** (`veronica_core.adaptive`):
  - `BurnRateEstimator`: sliding-window cost burn rate with EMA trend weighting. Atomic multi-window snapshot via `current_rates()`.
  - `AdaptiveThresholdPolicy`: implements `RuntimePolicy` protocol. Escalates ALLOW / WARN / DEGRADE / HALT based on time-to-exhaustion. Spike detection via instantaneous vs baseline rate comparison.
  - `AdaptiveConfig` dataclass with threshold ordering validation (`0 < halt <= degrade <= warn`).
  - `AnomalyDetector`: per-metric Z-score anomaly detection using Welford's online algorithm. Per-metric locking, configurable warmup period.

- **Multi-tenant Budget Management** (`veronica_core.tenant`):
  - `Tenant` dataclass with optional `budget_pool` and `policy` fields.
  - `TenantRegistry`: thread-safe hierarchy (Organisation / Project / Team / Agent) with O(1) child index. Ancestor-walk policy and budget resolution.
  - `BudgetPool`: allocate / spend / release with optional `BudgetBackend` integration (reserve/commit/rollback for distributed safety). TOCTOU-safe rollback on backend failure.

- 231 new tests (3224 -> 3455 total), including 107 adversarial tests covering concurrency, NaN/Inf/negative inputs, deep hierarchy, Unicode tenant IDs, and TOCTOU race conditions.
- `docs/EVOLUTION_ROADMAP.md`: v2.0 -> v4.0 evolution plan.

---

## [2.0.0] -- 2026-03-04 -- Reserve/Commit/Rollback, Async Budget, Adapter Unification

**Breaking changes:** `BudgetBackend` protocol now requires `reserve(amount, ceiling)`, `commit(rid)`, `rollback(rid)` methods. Old `spend()`-based backends must migrate.

### Added

- **Two-phase budget protocol**: `reserve(amount, ceiling)` / `commit(rid)` / `rollback(rid)` on `LocalBudgetBackend` and `RedisBudgetBackend`. Prevents double-spending via escrow-based accounting with 60-second reservation timeout.
- **Async budget backends**: `AsyncLocalBudgetBackend` and `AsyncRedisBudgetBackend` with native `asyncio.Lock` coordination.
- **WebSocket containment**: `VeronicaASGIMiddleware` enforces step limits on WebSocket connections with `close(1008)` on exhaustion.
- **CancellationToken**: Parent/child propagation (upward only) with `_propagate_child_cost` for hierarchical cost enforcement.
- **SharedTimeoutPool**: Module-level singleton daemon thread for timeout scheduling (replaces per-context threads).
- **ReconciliationCallback protocol**: Hook for estimated vs actual cost drift detection.
- **ContextVar migration**: `ExecutionContext` stored in `contextvars.ContextVar` for async-safe access.
- **`_MCPAdapterBase`**: Shared base class for sync and async MCP adapters, eliminating code duplication.
- **Quickstart examples**: `langchain_minimal.py`, `langgraph_minimal.py`, `ag2_minimal.py` with working stub LLMs.
- **Failure mode tests**: 8 targeted failure scenarios (double commit, rollback after commit, Redis disconnect, Lua atomicity, reserve/rollback, WebSocket step limit, CancellationToken cascade, SharedTimeoutPool exhaustion).
- **Distributed consistency documentation**: `docs/DISTRIBUTED_CONSISTENCY.md` covering protocol, atomicity guarantees, failure recovery, and consistency model.
- 627 new tests (2563 -> 3190 total).

### Fixed

- **Constructor validation**: `ExecutionConfig` rejects negative `timeout_ms`, `max_cost_usd`, `max_steps`.
- **Retry counter**: Off-by-one fix in retry tracking.
- **Signal propagation**: Parent abort correctly propagates cost overflow from child contexts.
- **NaN/Inf budget validation**: `LocalBudgetBackend.spend()` rejects NaN, Inf, and negative amounts.
- **HALF_OPEN slot leak**: `DistributedCircuitBreaker` properly releases slots on timeout.

### Changed

- Adapter unification: LangChain, LangGraph, AG2 adapters share common `build_adapter_container` factory.
- `_wrap()` cleanup path uses `finally` block for guaranteed rollback.

---

## [1.8.11] -- 2026-03-03 -- Round 5 Deep Audit

**Breaking changes:** none

### Fixed

- **Serializers**: `serialize_snapshot` no-nodes fallback now uses timezone-aware `datetime.min.replace(tzinfo=timezone.utc)` instead of naive `datetime.min`. Prevents `TypeError` when mixed with timezone-aware timestamps.
- **DistributedCircuitBreaker**: Added `close()` method for Redis connection cleanup. Called automatically from `ExecutionContext.__exit__`.
- **TokenBudgetHook**: `before_llm_call` now clamps negative `tokens_out`/`tokens_in` estimates to 0 via `max(0, ...)`. Prevents negative estimates from bypassing budget enforcement.
- **AG2 CircuitBreakerCapability**: `_guarded_generate_reply` now catches exceptions raised by `original_generate_reply()` and records them as circuit breaker failures before re-raising. Previously, exceptions bypassed the circuit breaker entirely.

### Added

- 8 new tests: token budget negative estimate clamping (3), AG2 exception recording (3), distributed CB close (2).
- Deferred items L-18 (unbounded ExecutionGraph._nodes), L-19 (ASGI 429 suppression), L-20 (AG2 name collision).

---

## [1.8.10] -- 2026-03-03 -- Round 4 Hardening

**Breaking changes:** `ExecutionConfig(timeout_ms=-1)` now raises `ValueError` (previously accepted silently).

### Fixed

- **ExecutionContext**: Metrics recording failure now logs at DEBUG level instead of silently swallowing the exception.
- **ExecutionContext**: Added `logging` import and module-level `logger` for instrumentation.
- **ExecutionConfig**: `timeout_ms` now validated as non-negative in `__post_init__()` (consistent with other fields).
- **VeronicaExit**: `_graceful_exit()` and `_emergency_exit()` now wrap each step in try/except to prevent unhandled exceptions during shutdown.

### Changed

- `docs/V2_DEFERRED.md`: Added 3 new deferred items (T-5, L-16, L-17), 5 resolved items (R-19 to R-22), updated test gap notes (D-9).

---

## [1.8.9] -- 2026-03-03 -- Round 3 Deep Audit

**Breaking changes:** `PartialResultBuffer.append()` now raises `ValueError` after `mark_complete()` (previously silent).

### Fixed

- **distributed.py**: `RedisBudgetBackend.is_using_fallback` and `DistributedCircuitBreaker.is_using_fallback` now hold `_lock` for thread safety.
- **ExecutionContext**: `get_partial_result()` now holds `_lock` for thread-safe dict access.
- **ExecutionContext**: `__exit__` now sets `_aborted = True` to prevent post-exit `wrap_llm_call` execution.
- **PartialResultBuffer**: `append()` now raises `ValueError` if the buffer has been `mark_complete()`d.
- **PolicyEngine**: Added `/proc/self/environ`, `/proc/self/cmdline`, `/proc/*/environ`, `/proc/*/cmdline` to `FILE_READ_DENY_PATTERNS`.
- **SecretMasker**: `_mask_value()` at `MAX_DEPTH` now applies shallow masking to strings, bytes, and immediate container children instead of returning unmasked.

### Changed

- `docs/V2_DEFERRED.md`: Added 7 resolved items (R-12 to R-18), 7 new deferred items (T-3, T-4, L-11 to L-15).

---

## [1.8.8] -- 2026-03-03 -- Round 2 LOW Sweep

**Breaking changes:** none

### Fixed

- **ShieldPipeline**: `on_error()` no-hook fallback path now records SafetyEvent (was asymmetric with other pipeline methods).
- **ExecutionContext**: Divergence event dedup now uses O(1) `_event_dedup_keys` (4th site missed in v1.8.7).
- **MinimalResponsePolicy**: Fix grammar -- "question" now pluralised correctly when `max_questions > 1`.
- **PolicyEngine**: `_check_python_inline_exec` `-m` index search now uses `args[1:]` slice consistently.
- **DynamicAllocator**: Clamp `min_floor` when `min_share * n` exceeds total budget (was violating allocation invariant).
- **patch.py**: Cost estimation failure log level upgraded from `debug` to `warning`.

### Changed

- `docs/V2_DEFERRED.md`: Added 6 resolved items (R-6 to R-11), 6 new deferred items (D-7, D-8, L-7 to L-10).

---

## [1.8.7] -- 2026-03-03 -- LOW Bug Sweep & CI Fix

**Breaking changes:** none

### Fixed

- **ExecutionContext**: Pipeline event intake now uses O(1) dedup keys (was O(n) list scan at 3 call sites).
- **ComplianceExporter**: `attach()` and `_drain_attached()` now hold `_lock` for thread safety.
- **VeronicaIntegration**: `_save_all_instances()` class method saves ALL live instances on exit (was first-only).
- **SemanticLoopGuard**: Remove dead code branch in `_jaccard()` (`if not union` unreachable after early return).
- **RetryContainer**: Remove dead post-loop fallback code (unreachable with `max_retries >= 0`).
- **HMAC oracle tests**: Add `VERONICA_POLICY_KEY` fixture for CI (non-DEV) environments.

### Added

- `docs/V2_DEFERRED.md`: Comprehensive list of v2.0 deferred architectural items.

---

## [1.8.6] -- 2026-03-03 -- Shield & Security Hardening

**Breaking changes:** none

### Fixed

- **BudgetWindowHook**: HALT now records timestamp in window (off-by-one metrics fix).
- **BudgetWindowHook**: Add `deque(maxlen)` to prevent unbounded growth under sustained HALT.
- **AdaptiveBudgetHook**: Fix `<= cutoff` to `< cutoff` boundary pruning (off-by-one).
- **AdaptiveBudgetHook**: Cap `_safety_events` with `deque(maxlen=1000)` (OOM prevention).
- **AdaptiveBudgetHook**: Clamp `ceiling_multiplier` on `import_control_state` (bounds validation).
- **TokenBudgetHook**: DEGRADE path now reserves pending tokens (TOCTOU race fix).
- **TimeAwarePolicy**: Cap `_safety_events` with `deque(maxlen=1000)`.
- **InputCompressionHook**: Cap `_safety_events` with `deque(maxlen=1000)`.
- **PolicyEngine**: Add null-byte (`\x00`) to `SHELL_DENY_OPERATORS`.
- **PolicyEngine**: Redact expected HMAC in tamper audit log (oracle prevention).
- **PolicyEngine**: Resolve symlinks via `os.path.realpath()` in file read/write checks.
- **PolicyEngine**: Case-insensitive deny for `go` subcommands and `cmake` flags.
- **SemanticLoopGuard**: `policy_type` is now a read-only `@property`.
- **VeronicaIntegration**: Deduplicate `atexit.register(save)` across instances.
- **_shared.py**: Fix `or`-masking of zero-value tokens (explicit `None` check).
- **llamaindex.py**: Fix same `or`-masking bug (incomplete v1.8.5 fix).
- **mcp.py**: Document `timeout_seconds` as post-hoc measurement.

### Tests

- Add 41 adversarial tests for all fixed bugs (shield, security, core, adapters).
- Fix 7 tautological assertions (`or True`, `>= 0`, conditional vacuity).
- Strengthen 4 weak assertions to exact equality checks.
- Total: 2533 tests passing.

---

## [1.8.5] -- 2026-03-03 -- Simplify & Quality Hardening

**Breaking changes:** none

### Fixed

- **retry.py**: `check()` read `_last_error` without lock (data race). Now acquires lock.
- **retry.py**: Unreachable fallback `raise self._last_error` when `_last_error is None` caused
  TypeError. Now raises `RuntimeError("max retries exceeded")`.
- **mcp_async.py**: `_ensure_stats()` TOCTOU -- check moved inside `async with _stats_lock`.
- **compliance/exporter.py**: Default endpoint hardcoded to external URL. Now requires explicit
  endpoint (raises ValueError if empty).
- **policy_engine.py**: `_shannon_entropy()` called twice per query param. Computed once now.
- **risk_score.py**: `is_safe_mode` computed sum redundantly. Now delegates to `current_score`.
- **distributed.py**: `import re` moved to top-level. Redundant `fallback.get()` consolidated.
- **adaptive_budget.py**: Anomaly factor expression (5 occurrences) extracted to
  `_anomaly_factor_locked()` helper.

### Changed

- **Adapter unification**: `ag2.py`, `langchain.py`, `llamaindex.py` now use `_shared.build_container()`
  and `_shared.record_budget_spend()`. Eliminated 4 copy-paste budget warning blocks.
  `_extract_llm_result_cost()` moved from `langgraph.py` to `_shared.py`.

### Added

- `tests/test_adapter_exec.py` -- 35 tests for `adapter/exec.py` (path traversal, capability
  branches, timeout, mkdir side effects).
- `tests/test_state_machine.py` -- 4 concurrency tests with `threading.Barrier`.
- `tests/test_compliance_exporter.py` -- tautological assertions replaced with meaningful checks.
- Total: 2482 tests passing.

---

## [1.8.4] -- 2026-03-03 -- Distributed Safety & Coverage Hardening

**Breaking changes:** none

### Fixed

- **CRITICAL: Lua tonumber() nil crash** -- Redis `tonumber()` returns nil for empty string
  or corrupted values in all Lua scripts (`_LUA_CHECK`, `_LUA_RECORD_FAILURE`,
  `_LUA_RECORD_SUCCESS`). Added nil guards after every `tonumber()` call with safe
  defaults (0 for counters, current time for timestamps).
- **CRITICAL: Lua ARGV validation** -- Lua scripts now reject nil TTL/threshold arguments
  at the top of each script, preventing silent misconfiguration.
- **CRITICAL: WSGI double start_response** -- `VeronicaWSGIMiddleware` could call
  `start_response` twice (once from app, once for 429 HALT) violating PEP 3333.
  Added `_StartResponseTracker` wrapper to skip post-flight 429 if response already started.
- **distributed.py**: `INCRBYFLOAT` float precision drift -- added `_BUDGET_EPSILON`
  tolerance constant for budget limit comparison after many increments.
- **distributed.py**: TOCTOU race in `get()` -- client reference now captured under lock
  to prevent reconnection between None-check and usage.
- **retry.py**: Lock held during `time.sleep(delay)` blocked `check()` and `reset()` from
  other threads. Lock now released before sleep and re-acquired after.
- **execution_context.py**: Cross-process budget enforcement -- `_check_limits()` now
  queries distributed backend global total (when not LocalBudgetBackend) to detect
  budget exceeded by other processes.
- **state.py**: Cooldown `fail_count` not reset -- `record_fail()` now resets
  `fail_counts[pair] = 0` when cooldown triggers.
- **security/masking.py**: HEX_SECRET regex overly broad -- tightened to require 40+ chars
  AND context prefix (`key=`/`token=`/`secret=`/`password=`) to avoid matching git hashes,
  MD5, and SHA256 digests.

### Added

- 112 new tests: BudgetEnforcer adversarial (NaN/Inf/negative/concurrent), AgentStepGuard
  concurrent step/reset, LLMClient protocol compliance, distributed Lua corruption,
  WSGI double-call, retry lock-during-sleep, state cooldown reset, HEX_SECRET precision.
- Total: 2438 tests passing.

---

## [1.8.3] -- 2026-03-03 -- Thread Safety & Adversarial Hardening

**Breaking changes:** none

### Fixed

- **CRITICAL: ExecutionConfig NaN bypass** -- `float('nan')` in `max_cost_usd` bypassed
  all comparison-based budget checks (`nan >= nan` is always False). Added `__post_init__`
  validation rejecting NaN, Inf, and negative values for all numeric config fields.
- **distributed.py**: 6 remaining Redis URL credential leaks in logger calls now redacted
  via `_redact_exc()`. Fixed TOCTOU race in `add()`/`get()`/`reset()` fallback paths by
  holding `_lock` for the entire fallback-check-and-dispatch sequence.
- **execution_context.py**: `_budget_backend.add()` moved outside `_lock` to prevent
  blocking Redis IO from stalling all threads. `pipeline.get_events()` moved outside lock
  to prevent re-entrant deadlock. `_partial_buffers` dict write now protected by `_lock`.
  Event dedup upgraded from O(n) list scan to O(1) set lookup. `get_graph_snapshot()`
  now acquires `_lock` for read consistency.
- **mcp_async.py**: Added `asyncio.Lock` for stats updates preventing race conditions
  after `await` yield points. `list_tools` failure log level upgraded from debug to warning.
- **mcp.py**: `tool_name` validation now rejects None, empty string, and non-string values.
- **patch.py**: Cost estimation failures now logged at debug level (was silent `return 0.0`).
- **state.py**: `VeronicaStateMachine.from_dict()` now handles corrupted entries gracefully
  with warning instead of crashing.
- **integration.py**: `operation_count` in `maybe_save()` protected by `_lock`.
- **exporter.py**: All silent `except Exception: pass` blocks now log at debug level.

### Added

- 75 adversarial tests covering: TOCTOU proof with `threading.Barrier`, HALF_OPEN slot
  atomicity, step/cost boundary off-by-one, `_finalize_success` idempotency, concurrent
  event dedup, `_budget_backend.add()` failure resilience, async stats lock contention
  under 100 coroutines, sync stats concurrent initialization race.

---

## [1.8.2] -- 2026-03-03 -- Phase 0 Hotfix

**Breaking changes:** none

### Fixed

- **quickstart.py**: `on_halt` parameter now actually dispatches HALT decisions.
  `on_halt="raise"` raises `VeronicaHalt`, `"warn"` logs a warning, `"silent"` is
  a no-op. Previously the parameter was stored but never acted upon.
- **distributed.py**: Redis URL credentials are now redacted in error log messages.
  `_redact_exc()` strips `user:password@` from `redis://` and `rediss://` URLs
  before logging, preventing credential leakage.

### Changed

- **`__init__.py`**: `enable_otel_with_provider` and `OTelExecutionGraphObserver`
  added to `__all__` (were importable but missing from the public API surface).
- **`__init__.py`**: `VeronicaPersistence` removed from `__all__` and direct imports.
  Accessing it now emits `DeprecationWarning` via `__getattr__` (use `JSONBackend`
  or `MemoryBackend` instead, removal scheduled for v2.0).
- **pyproject.toml**: Removed duplicate `[project.optional-dependencies].dev` section.
  All dev dependencies now live in `[dependency-groups].dev`. Added `pytest-cov>=5.0`.
- **pyproject.toml**: MCP optional dependency pinned to `mcp>=1.0,<2` (upper bound
  added to prevent silent breakage on MCP 2.0 protocol changes).

---

## [1.8.1] -- 2026-03-03 -- Full Audit Fixes

**Breaking changes:** none

### Fixed

- **middleware.py**: Pre-flight check replaced `wrap_llm_call(fn=lambda: None)` with
  `get_snapshot()` + budget ceiling check. Previously every HTTP request burned a step
  count even without an LLM call. ASGI and WSGI middleware both updated.
- **circuit_breaker.py**: HALF_OPEN in-flight slot now released when `failure_predicate`
  returns `False`. Previously a filtered failure left the slot permanently occupied,
  making the circuit unrecoverable.
- **budget.py**: `check()` now validates `cost_usd` for NaN, Inf, and negative values.
  NaN bypassed the `>` comparison (always False); negative values reduced projected spend.
  `import math` moved from hot-path to module level.
- **integration.py**: Guard check changed from `if context:` to `if context is not None:`
  so empty dict `{}` (valid context) no longer skips the guard. `get_fail_count()` now
  acquires lock before reading `fail_counts`. `get_cooldown_remaining()` returns `None`
  for expired cooldowns (was returning `0.0`).
- **persist.py**: Constructor coerces `str` path to `Path` to prevent `AttributeError`
  on `.parent.mkdir()`.

### Design Decisions Documented

- **retry.py lock scope**: `fn()` and `time.sleep()` inside `_lock` is intentional
  serialization. `test_execute_is_serialized` validates max concurrent == 1.
  Trade-off: recursive callers will deadlock (known limitation, not a bug).
- **agent_guard.py increment-then-check**: `max_steps=3` allows 2 actions (not 3).
  This is the established test contract, not an off-by-one bug.
- **exit.py cooperative shutdown**: `_signal_handler` sets `exit_requested` but does
  not terminate the process. Designed for poll-based cooperative shutdown via
  `is_exit_requested()`.

### Deferred to v2.0

- Async SDK bypass in `patch.py` (requires `wrap_llm_call_async()` API)
- Two-phase async budget atomicity in `mcp_async.py`
- `threading.local()` → `ContextVar` for node stack in `execution_context.py`
- Distributed budget `reserve/commit/rollback` pattern
- Post-call cost reconciliation API for MCP adapters
- `_MCPAdapterBase` mixin extraction (sync/async deduplication)

### Internal

- Full codebase audit (4 parallel automated audits).
  52 findings triaged, 7 fixes applied, 4 confirmed as design intent, remainder
  deferred to v2.0. Audit reports archived in `.archive/20260303_audit_v180/`.

---

## [1.8.0] -- 2026-03-02 -- MCP Adapter Hardening

**Breaking changes:** none

### Changed

- **Sync adapter: `nonlocal` closure refactoring** (`MCPContainmentAdapter`): replaced
  single-element list closures (`call_result[0]`, `call_error[0]`, `duration_ms[0]`)
  with Python 3 `nonlocal` declarations. No behavioral change; eliminates per-call
  list allocations and improves readability.

### Added

- **Stats memory defense**: both `MCPContainmentAdapter` and `AsyncMCPContainmentAdapter`
  now emit a `WARNING`-level log when the number of distinct tracked tool names exceeds
  10,000. Alerts operators to unbounded tool-name generation (e.g. attacker-controlled
  input) without breaking containment.
- **60 new adversarial tests** covering 14 gap categories for MCP adapters:
  `_extract_token_count` dict keys, steps exhaustion boundary, CB HALF_OPEN transitions,
  cost on exception, `isError` truthy non-bool values, `BaseException` propagation,
  `wrap_mcp_server` edge cases (None/empty/duplicate tools), concurrent `_ensure_stats`
  race, negative `cost_per_token`, async two-phase budget, sync post-call timeout,
  deeply nested results, async concurrent CB trip race, sync fn returning coroutine.

### Internal

- Simplify code review (3-agent parallel): code reuse, quality, efficiency.
  Findings: 10 sync/async duplications catalogued (deferred to v2.0 base class
  extraction), `isError` detection already unified, `asyncio.Lock` already removed.
- AG2 RFC impact analysis: zero impact from v1.7.0 MCP changes. AG2 tests 42/42 pass.

---

## [1.7.0] -- 2026-03-02 -- Async MCP Containment

**Breaking changes:** none

### Added

- **AsyncMCPContainmentAdapter** (`veronica_core.adapters.mcp_async`): async counterpart
  of `MCPContainmentAdapter` for `asyncio`-based MCP tool calls.
  - `timeout_seconds` parameter via `asyncio.wait_for()`.
  - `failure_predicate` for selective circuit breaker tripping.
  - `isError` detection on MCP tool results (does not trip CB).
  - `asyncio.Lock`-based thread-safe stats.
- **`wrap_mcp_server()`** helper: creates a pre-configured `AsyncMCPContainmentAdapter`
  from an MCP `ClientSession`, with optional tool discovery via `list_tools()`.
- **Sync adapter hardening** (`MCPContainmentAdapter`):
  - Async guard: `TypeError` when `call_fn` is a coroutine function.
  - `timeout_seconds` parameter (elapsed-time check).
  - `failure_predicate` parameter for selective CB tripping.
  - `isError` handling on MCP tool results.
- **`mcp` optional dependency**: `pip install veronica-core[mcp]`.

### Tests

- 115 new tests (49 async adapter, 20 sync hardening, 16 wrap_mcp_server, 30 adversarial).
- Total: 2172 tests passing.

---

## [1.6.1] -- 2026-03-02 -- MCP Adapter Docs Fix

**Breaking changes:** none

### Fixed

- Corrected CHANGELOG v1.6.0 MCP adapter description: removed references to
  `wrap_mcp_server()`, `before_tool_call()`, and `after_tool_call()` which were
  not part of the MCP adapter public API (they exist in the Shield pipeline).

---

## [1.6.0] -- 2026-03-02 -- Protocols, Budget Allocator, OTel Bridge

**Breaking changes:** none

### Added

- **Protocol definitions** (`veronica_core.protocols`): 4 `@runtime_checkable` Protocols
  (`ExecutionGraphObserver`, `BudgetBackendProtocol`, `PolicyEvaluator`, `ContainmentReporter`)
  for type-safe plugin contracts.
- **BudgetAllocator** (`veronica_core.budget_allocator`): hierarchical budget distribution
  with `EQUAL`, `WEIGHTED`, `PRIORITY` strategies and automatic child ExecutionContext creation.
- **MCP adapter** (`veronica_core.adapters.mcp`): Model Context Protocol tool-call containment
  with per-server circuit breaker, budget enforcement, and per-tool cost tracking.
- **AG2 OpenTelemetry bridge** (`veronica_core.otel`):
  - `enable_otel_with_provider()`: share an external TracerProvider (e.g. AG2's) so
    containment events appear in the same trace tree.
  - `get_tracer()`: retrieve the veronica-core OTel tracer for child span creation.
  - `OTelExecutionGraphObserver`: ExecutionGraphObserver that emits lifecycle events
    (`node.start`, `node.complete`, `node.failed`, `node.decision`) to OTel spans.
  - AG2 adapter OTel emission: `CircuitBreakerCapability` and `VeronicaConversableAgent`
    now emit containment decision events (ALLOW/HALT) to the active OTel span.
- **Distributed CircuitBreaker docs** (`docs/distributed-circuit-breaker.md`).

### Fixed

- Defensive `str(error or "")[:500]` in `OTelExecutionGraphObserver.on_node_failed()`
  and `on_decision()` to handle None inputs without crashing.

### Tests

- 53 new adversarial tests (protocols, budget allocator, MCP adapter, AG2 OTel).
- Total: 2057 tests passing.

---

## [1.5.0] -- 2026-03-01 -- Enterprise Key Providers & CI Guard

**Breaking changes:** none

### Added

- **KeyProvider Protocol** (`veronica_core.security.key_providers`): pluggable key
  material sourcing for `PolicySignerV2` and `PolicyEngine`.
  - `FileKeyProvider`: load PEM from disk (default, wraps existing behavior).
  - `EnvKeyProvider`: load PEM from environment variable (`VERONICA_PUBLIC_KEY_PEM`).
  - `VaultKeyProvider`: fetch from HashiCorp Vault transit engine (requires `hvac`).
- **`[vault]` extra**: `pip install veronica-core[vault]` for `hvac>=2.0` dependency.
- **`PolicySignerV2`**: accepts `key_provider` parameter; verify() delegates to
  provider instead of reading PEM file directly.
- **`PolicyEngine`**: accepts `key_provider` parameter; passes it through to
  `_verify_policy_signature` and `_validate_jwk_format`.
- **CIGuard** (`veronica_core.security.ci_guard`): CI-specific secret leak detection
  combining SecretMasker's 28 patterns with 7 CI-specific patterns (GitHub Actions,
  GitLab CI, Docker auth, CircleCI, Jenkins, Artifactory, Buildkite).
  - `scan()`: returns deduplicated `Finding` list with line number and severity.
  - `scan_file()`: scan a file for leaked secrets.
  - `protect_output()`: mask all secrets (CI patterns first, then base).
  - `is_ci()`: detect CI/PROD environment via `SecurityLevel`.
- **104 new tests** (1780 -> 1884): 46 KeyProvider tests + 58 CIGuard tests,
  including 40 adversarial tests (ReDoS, concurrent access, corrupted input,
  boundary abuse, exception coverage).

### Fixed

- **`PolicySignerV2.verify()`**: broadened exception handling from
  `except (OSError, RuntimeError)` to `except Exception`. External `KeyProvider`
  implementations may raise `KeyError`, `TypeError`, `AttributeError`, etc.;
  all now safely return `False` instead of crashing.

---

## [1.4.0] -- 2026-02-28 -- Adapter Consolidation & Hardening

**Breaking changes:** none

### Added

- **CrewAI adapter** (`veronica_core.adapters.crewai`): `VeronicaCrewAIListener`
  wraps CrewAI event bus with budget, step, and retry enforcement.
- **LangGraph adapter** (`veronica_core.adapters.langgraph`): `VeronicaLangGraphListener`
  wraps LangGraph event hooks with the same containment guarantees.
- **Shared adapter helpers** (`veronica_core.adapters._shared`): `build_container()`
  and `cost_from_total_tokens()` centralized to eliminate DRY violations across adapters.
- **Quickstart API** (`veronica_core.quickstart`): 2-line setup via `init("$5.00")` /
  `shutdown()` for instant cost containment.
- **Compliance exporter** (`veronica_core.compliance`): `ComplianceExporter` with
  JSON/CSV serialization and optional `httpx` webhook delivery.
- **`[compliance]` extra**: `pip install veronica-core[compliance]` for `httpx` dependency.
- **BudgetEnforcer**: rejects NaN / Inf amounts with `ValueError`.
- **AdaptiveBudgetHook**: validates `tighten_trigger >= 1` and `window_seconds > 0`
  at construction; extracted `_prune_event_buffer`, `_count_tighten_events` helpers.
- **ApprovalRateLimiter**: adversarial tests for concurrent `acquire()` + `reset()` race.
- **PolicyEngine**: refactored shell rule evaluation into focused sub-functions
  (`_check_shell_deny_commands`, `_check_shell_operators`, `_check_credentials_in_args`).
- **DistributedCircuitBreaker**: extracted `_attempt_reconnect_if_on_fallback` and
  `_activate_fallback` helpers; refactored `check()` / `record_failure()` / `record_success()`
  to use shared helper for Redis error handling.
- **ExecutionGraph**: extracted `_update_graph_metrics`, `_check_cost_rate_divergence`,
  `_check_token_velocity_divergence` from monolithic `close_node()`.

### Changed

- **Test suite audit**: 85 duplicate/low-value tests removed (1865 -> 1780). All
  removed tests were same-branch duplicates, tautological mirrors, strict subsets,
  or cross-file duplicates. Zero adversarial, boundary, thread-safety, or regression
  tests deleted.

### Fixed

- **CrewAI adapter**: narrowed bare `except` to specific exception types
  (`AttributeError`, `TypeError`, `ValueError`, `KeyError`, `OverflowError`, `RuntimeError`).

### Stats

- 1780 tests (was 1865), 92% coverage, 0 failures.

---

## [1.3.0] -- 2026-02-28 -- ROS2 Adapter

**Breaking changes:** none

### Added

- **ROS2 adapter** (`veronica_core.adapters.ros2`): `SafetyMonitor` and `OperatingMode`
  for runtime containment in ROS2 callback-based architectures.
- **`OperatingMode` enum**: 4-tier degradation (FULL_AUTO / CAUTIOUS / SLOW / HALT)
  with `speed_scale` hint for actuator throttling.
- **`SafetyMonitor.guard()`** context manager: wraps ROS2 callbacks with automatic
  fault detection, mode degradation, and recovery.
- TurtleBot3 Gazebo demo (`examples/ros2/`): LiDAR fault injection with graceful
  degradation and automatic recovery.
- 46 tests (25 functional + 21 adversarial) covering concurrent access, reentrant
  guard, recursive callbacks, SystemExit propagation, and mode thrashing.

### Fixed

- **Infinite recursion in `SafetyMonitor._check_transition`**: `_last_mode` is now
  updated before invoking `on_mode_change` callback, preventing infinite loops when
  the callback calls `record_fault()` and state corruption when the callback raises.

---

## [1.2.0] -- 2026-02-28 -- Failure Classification

**Breaking changes:** none

### Added

- **`FailurePredicate`** type alias: `Callable[[BaseException], bool]` -- returns
  `True` to count the failure, `False` to ignore it.
- **`failure_predicate`** parameter on `CircuitBreaker`, `DistributedCircuitBreaker`,
  and `get_default_circuit_breaker()` factory.
- **3 built-in predicate factories**:
  - `ignore_exception_types(*types)` -- ignore user-caused errors (e.g. 400s)
  - `count_exception_types(*types)` -- only count provider failures (e.g. 500s, timeouts)
  - `ignore_status_codes(*codes)` -- filter by HTTP `.status_code` or `.response.status_code`
- `record_failure(*, error=None) -> bool` on both `CircuitBreaker` and
  `DistributedCircuitBreaker`. Returns `True` if counted, `False` if filtered.
  `error=None` always counts (backward compatible with AG2 null-reply detection).
- Adapters pass `error=` to `record_failure()`: `ExecutionContext` and
  `VeronicaLlamaIndexHandler.on_llm_error()`.
- Exported from `veronica_core`: `FailurePredicate`, `ignore_exception_types`,
  `count_exception_types`, `ignore_status_codes`.
- 45 tests in `tests/test_failure_classification.py`: predicate factories (10),
  CircuitBreaker integration (8), DistributedCircuitBreaker (4), adapter (2),
  adversarial (20), export (1).
- Total tests: 1501 (was 1456).

### Design

- **Zero Redis overhead**: Predicate evaluated in Python before lock/Lua call.
  Filtered failures skip Redis entirely (0.14us vs 121us).
- **Fail-safe**: If predicate raises `Exception`, the failure is counted (deny > allow).
  `SystemExit`/`KeyboardInterrupt` propagate (not caught).
- **Backward compatible**: `record_failure()` with no args still works -- `error=None`
  bypasses the predicate entirely.

---

## [1.1.2] -- 2026-02-27 -- DCB Lua Bug Fix & Optimization

**Breaking changes:** none

### Fixed

- **Lua in_flight unconditional reset bug**: `_LUA_RECORD_FAILURE` and
  `_LUA_RECORD_SUCCESS` unconditionally reset `half_open_in_flight` to 0,
  even when the circuit was in CLOSED or OPEN state. This could break the
  single-request invariant if a non-slot-holder called `record_failure()`
  while state was CLOSED. Now gated on `state == 'HALF_OPEN'` only.
- **Fail-safe design documented**: Any failure during HALF_OPEN reopens the
  circuit immediately (deny > allow). Per-process slot ownership is
  intentionally not tracked -- if any process reports a failure while testing,
  the service is still unhealthy.

### Changed

- `state` property: HGETALL (all 7+ fields) replaced with HMGET (2 fields:
  `state`, `last_failure_time`). Reduces data transfer per read.
- `to_dict()`: Delegates to `snapshot()` instead of duplicating HGETALL + parse
  logic. 40 lines reduced to 12 lines.
- Extracted `_resolve_state_str()` and `_parse_last_failure_time()` helpers
  to eliminate duplicated state parsing across `state`, `snapshot()`, and
  `to_dict()`.
- Removed redundant `import time as _time` in `RedisBudgetBackend._try_reconnect()`.
- Simplified defensive `getattr(self, "_fallback_seed_base", 0.0)` to direct
  attribute access.

### Added

- `lupa>=2.6` dev dependency: required for fakeredis Lua scripting support.
- 5 adversarial tests in `TestAdversarialInFlightInvariant`: multi-process
  in_flight slot behavior, concurrent failure during HALF_OPEN, slot holder
  record_failure/record_success.
- Performance benchmarks: `tests/bench_distributed_circuit_breaker.py` with
  13 benchmarks covering all operations (check, record, snapshot, full cycle,
  local fallback, reset).
- Total tests: 1456 (was 1434).

### Performance

- ~10% overall latency reduction from code waste removal:
  - `check()` CLOSED: 155 -> 140us (-9.4%)
  - `record_failure()`: 150 -> 128us (-14.5%)
  - `reset()`: 130 -> 105us (-19.4%)
  - Full cycle: 304 -> 272us (-10.5%)

---

## [1.1.1] -- 2026-02-27 -- DCB Performance & Reliability Hardening

**Breaking changes:** none

### Fixed

- **HALF_OPEN slot permanent lock-out**: When a process crashed after claiming the
  HALF_OPEN test slot, the slot remained stuck indefinitely (up to TTL expiry,
  default 3600s). Added `half_open_slot_timeout` parameter (default 120s) with
  Lua-script-level auto-release of stale slots. The `half_open_claimed_at`
  timestamp is recorded atomically when claiming the slot.

### Added

- `CircuitSnapshot` frozen dataclass: immutable snapshot of all circuit state
  (state, failure_count, success_count, last_failure_time, distributed, circuit_id).
- `DistributedCircuitBreaker.snapshot()` method: retrieves all circuit state in a
  single Redis `HGETALL` round-trip. Prevents N+1 Redis reads in monitoring code.
- `redis_client` parameter on `DistributedCircuitBreaker`: inject a pre-created
  `redis.Redis` instance for connection pool sharing across multiple breakers.
  Prevents connection proliferation in multi-breaker deployments.
- `_register_scripts()` internal method: extracted Lua script registration for
  reuse in both `_connect()` and `redis_client` injection paths.
- 17 new tests: `TestSnapshot` (7), `TestRedisClientInjection` (2),
  `TestHalfOpenSlotTimeout` (8) including auto-release verification.
- Total tests: 1434 (was 1417).

---

## [1.1.0] -- 2026-02-27 -- Distributed Circuit Breaker

**Breaking changes:** none

### Added

- `DistributedCircuitBreaker`: Redis-backed circuit breaker for cross-process
  failure isolation. Shares circuit state (CLOSED/OPEN/HALF_OPEN) across multiple
  processes via a Redis hash. Uses Lua scripts for atomic state transitions
  (record_failure, record_success, check) to prevent race conditions. Falls back
  to a local `CircuitBreaker` if Redis is unreachable, following the same
  seed/reconcile/reconnect pattern as `RedisBudgetBackend`.
- `get_default_circuit_breaker()` factory: returns `DistributedCircuitBreaker`
  when `redis_url` is provided, otherwise a local `CircuitBreaker`. Same pattern
  as the existing `get_default_backend()`.
- 67 tests for `DistributedCircuitBreaker` including:
  - Basic state transitions and cross-process state sharing
  - HALF_OPEN concurrency (Lua-atomicity verified with 20 threads)
  - Redis failover, reconnect, and reconciliation
  - Adversarial tests: corrupted Redis data (invalid state strings, non-numeric
    fields, missing fields, stuck half_open_in_flight), boundary values
    (threshold=1, timeout=0, threshold=1M), TOCTOU races (concurrent
    record_failure + check, concurrent record_failure + record_success),
    mid-operation Redis failure for all operations

### Roadmap (upcoming)

- **v1.2.0**: `CircuitBreakerCapability.conservative()` / `.aggressive()` factory
  presets; AG2 `GroupChatManager` per-agent label mapper.
- **v1.3.0**: Native `ReplyInterceptor` and `LLMCallMiddleware` protocols (pending
  AG2 upstream discussion -- see [RFC issue](https://github.com/ag2ai/ag2/issues)).

---

## [1.0.2] -- 2026-02-27 -- Fix runtime import for autogen package

**Breaking changes:** none

### Fixed

- Runtime import in `adapters/ag2.py` now tries `from autogen import` first,
  falling back to `from ag2 import`. The previous `import ag2` failed with
  `ModuleNotFoundError` when the package was installed via `pip install autogen`
  (the module is named `autogen`, not `ag2`).
- Docstrings and examples unified to `from autogen import ConversableAgent` style
  (`ag2.py`, `ag2_capability.py`, `examples/`).
- `trigger=None` in docstrings and examples replaced with
  `trigger=lambda _: True` (AG2 raises `ValueError` on `None`).
- `generate_reply([])` in examples replaced with
  `generate_reply([{"role": "user", "content": "test"}])`.

---

## [1.0.1] -- 2026-02-26 -- AG2 Capability: remove_from_agent

**Breaking changes:** none

### Added

- `CircuitBreakerCapability.remove_from_agent(agent)`: restores the original
  `generate_reply` method and removes the agent's `CircuitBreaker` from the
  capability. Calling on an unregistered agent logs a warning and returns
  without raising. Enables clean teardown without side-effects -- e.g., for
  testing, hot-swapping capabilities, or graceful shutdown flows.

### Changed

- `CircuitBreakerCapability.__init__` now initializes `_originals: Dict[str, Any]`
  alongside `_breakers`. The original `generate_reply` reference is stored on
  `add_to_agent()` and consumed on `remove_from_agent()`.

### Roadmap (upcoming)

- **v1.1.0**: `CircuitBreakerCapability.conservative()` / `.aggressive()` factory
  presets; AG2 `GroupChatManager` ↔ per-agent label mapper for isolated budget
  tracking across group chat participants.
- **v1.2.0**: Native `ReplyInterceptor` and `LLMCallMiddleware` protocols (pending
  AG2 upstream discussion -- see [RFC issue](https://github.com/ag2ai/ag2/issues)).

### Tests

- 6 new tests for `remove_from_agent` (restore, breaker clear, noop on unregistered,
  re-add after remove). All 29 AG2 capability tests passing.

---

## [1.0.0] -- 2026-02-26 -- Production Release & Adversarial Hardening

**Breaking changes:** `on_error()` default changed from ALLOW to HALT. `AIcontainer` renamed to `AIContainer`.

### Security (Adversarial Review Fixes)

- **CRITICAL** (`policy_engine.py`): `go run`/`go generate` shell injection blocked.
  Added `go` to `SHELL_DENY_EXEC_FLAGS` with `frozenset({"run", "generate", "tool", "env"})`.
- **CRITICAL** (`integration.py`): TOCTOU race in `record_fail()` fixed. State mutation
  and guard cooldown activation now wrapped in a single `_op_lock`.
- **CRITICAL** (`execution_context.py`): Redis budget double-spend fixed. Cost
  reconciliation now uses local delta increment instead of overwriting with Redis
  global total.
- **HIGH** (`masking.py`): `SecretMasker` bytes leak fixed. `bytes` values are now
  decoded to `str` before masking, preventing raw secret bytes in output.
- **HIGH** (`circuit_breaker.py`): HALF_OPEN state now enforces single concurrent
  test request via `_half_open_in_flight` counter.
- **HIGH** (`retry.py`): `RetryContainer` thread-safety added with `threading.Lock`
  on `_attempt_count` and `_total_retries`.
- **HIGH** (`policy_engine.py`): Unicode operator bypass blocked via NFKC
  normalization before shell operator checks.
- **MEDIUM** (`execution_context.py`): `attach_partial_buffer()` guard prevents
  overwrite of existing buffer.
- **MEDIUM** (`state.py`): `from_dict()` shallow-copies mutable dicts to prevent
  shared reference mutation.
- **MEDIUM** (`langchain.py`): Zero-token LLM response bypass removed. Responses
  with `total_tokens=0` are now tracked correctly.

### Security (Design Hardening)

- `ShieldPipeline.on_error()` now defaults to HALT (fail-closed). Pass
  `on_error_policy=Decision.ALLOW` to restore the previous fail-open behavior.
- Redis budget reconciliation on reconnect prevents cost bypass: accumulated
  spend is re-read from Redis on successful reconnect and reconciled against the
  in-process counter before new calls are allowed.
- `_load_key()` raises `RuntimeError` in non-DEV environments when
  `VERONICA_POLICY_KEY` is not set, preventing deployment with the publicly-known
  development key.

### Breaking Changes

- `timeout_ms` parameter is deprecated: accepted but ignored, emits
  `DeprecationWarning`. Full enforcement will be added in a future release.
- `on_error()` default changed from ALLOW to HALT. Pass
  `on_error_policy=Decision.ALLOW` to restore old behavior.
- `AIcontainer` renamed to `AIContainer` (PascalCase convention). The old name
  still works but emits `DeprecationWarning`. It will be removed in a future
  release.

### Added

- LlamaIndex adapter (`adapters/llamaindex.py`): `VeronicaLlamaIndexHandler`
  enforces VERONICA policies on every LLM and embedding call in a LlamaIndex
  pipeline. Accepts `GuardConfig` or `ExecutionConfig`.
- LangChain adapter now uses per-model pricing from `pricing.py` for accurate
  cost accounting.
- OpenClaw integration marked as experimental; available via
  `veronica_core.adapters.openclaw` (import guard present).
- AG2 `CircuitBreakerCapability` now uses `breaker.check()` instead of direct
  state access, enforcing HALF_OPEN single-request semantics.

### Fixed

- O(n) `pop(0)` replaced with `deque.popleft()` in 3 modules for O(1)
  queue operations.
- Thread-safe security level management: security level is now read and set
  under a `threading.Lock`.
- Redis fallback reset on successful reconnect: fallback mode is cleared when
  the Redis connection is restored, preventing permanent degraded mode.
- `get_cooldown_remaining()` race condition: `KeyError` on concurrent cooldown
  expiry now caught gracefully.

### Deprecations

- `VeronicaPersistence`: runtime `DeprecationWarning` now emitted on
  construction. Use `PersistenceBackend` (`veronica_core.backends`) instead.
- `AIcontainer`: runtime `DeprecationWarning` emitted on access. Use
  `AIContainer` instead.

### Tests

- 1465 tests passing (was 1415), +46 adversarial regression tests covering
  concurrency, type variations, TOCTOU races, and shell injection vectors.
- 4 xfailed (pre-existing SHA pin, unrelated to this release).

---

## [0.12.0] -- 2026-02-26 -- Middleware, Time-Based Divergence, Streaming Buffers

**Breaking changes:** none

### New

- **ASGI/WSGI middleware** (`veronica_core.middleware`): `VeronicaASGIMiddleware` and
  `VeronicaWSGIMiddleware` wrap each HTTP request in a fresh `ExecutionContext`. The
  context is stored in a `ContextVar` so any code within the same request can call
  `get_current_execution_context()` without passing the object manually. Non-HTTP
  scopes (lifespan, websocket) pass through to the inner app unchanged. Returns 429
  on `Decision.HALT` -- either pre-flight (limit already exceeded before the app is
  called) or post-flight (context was aborted during the call). The 429 path skips
  the inner app entirely on pre-flight; on post-flight it is suppressed if the app
  has already started sending a response (ASGI protocol constraint).

- **Time-based divergence heuristics** (`ExecutionGraph`): Two rate checks added to
  the existing consecutive-pattern detector:
  - `COST_RATE_EXCEEDED` -- fires once when cumulative chain cost / elapsed seconds
    exceeds `cost_rate_threshold_usd_per_sec` (constructor param, default 0.10 USD/s).
  - `TOKEN_VELOCITY_EXCEEDED` -- fires once when total output tokens / elapsed seconds
    exceeds `token_velocity_threshold` (constructor param, default 500 tok/s).
  Both fire on `mark_success` (cost and token counts are unavailable before a node
  completes). Both are deduped per chain -- at most one event of each type per
  `ExecutionGraph` instance. Event shape mirrors existing `divergence_suspected`
  events but carries `cost_rate` or `token_velocity` fields instead of `signature` /
  `repeat_count`. `snapshot()["aggregates"]` now includes `"total_tokens_out"`.

- **PartialResultBuffer integration** (`ExecutionContext`): `WrapOptions` gains an
  optional `partial_buffer: PartialResultBuffer | None` field. When provided,
  `wrap_llm_call` stores the buffer in a `ContextVar` for the duration of `fn()`,
  so streaming callbacks can call `get_current_partial_buffer()` without any
  threading changes. On clean completion, `buf.mark_complete()` is called
  automatically. On halt or exception the buffer is left as-is. The buffer is
  registered under the node's `graph_node_id`; call
  `ctx.get_partial_result(node_id)` to retrieve it later. `NodeRecord.partial_buffer`
  holds the reference.

### Tests

- `tests/test_middleware.py` (8): ASGI lifespan passthrough, normal 200, 429-on-HALT,
  ContextVar injection, ContextVar cleanup after request; WSGI normal, 429-on-HALT,
  `environ["veronica.context"]` injection.
- `tests/test_divergence_v2.py` (9): cost-rate fires / below-threshold / dedup,
  token-velocity fires / below-threshold / dedup, both independent, drain clears,
  `total_tokens_out` aggregate.
- `tests/test_partial_stream.py` (12): ContextVar carries buffer into fn, cleared
  after wrap, None when omitted, get_partial_result by node ID, unknown node → None,
  sequential calls get separate buffers, exception resets ContextVar, existing wrap
  behavior unchanged, mark_complete on success, multiple buffers per context, HALT
  leaves buffer partial, NodeRecord stores reference.

---

## [0.11.1] -- 2026-02-26 -- Bug fixes and code quality improvements

**Breaking changes:** none

### Fixed

- **AG2 adapter -- TokenBudgetHook always zero**: `CircuitBreakerCapability._guarded_generate_reply()` and
  `VeronicaConversableAgent.generate_reply()` now call `token_budget_hook.before_llm_call()` before the
  call and `record_usage()` after, so token budgets are enforced correctly when used with AG2 agents.
- **AG2 adapter -- ToolCallContext not constructed**: `ag2.py` and `patch.py` now build a proper
  `ToolCallContext` (with `request_id`, `model`, `tool_name`) instead of passing placeholder values.
- **integration.py -- from_dict contradictory state**: On deserialization failure, `loaded_from_disk`
  is now always `False` when `state` is `None`, eliminating the contradictory state.
- **inject.py -- VeronicaHalt decision type**: `decision` field is now `Optional[PolicyDecision]`
  to allow `decision=None` callers without a type error.
- **input_compression.py -- ambiguous variable name**: Renamed `l` to `line` (E741).
- **budget.py -- to_dict() thread safety**: Wrapped internal state reads in `with self._lock:`.
- **integration.py -- manual lock management**: Replaced `acquire()/release()` with `with self._lock:`.

### New

- **`PostDispatchHook` protocol** (`veronica_core.shield`): Companion to `PreDispatchHook`.
  Provides `after_llm_call(ctx, response)` for post-call observation and recording.
- **`NoopPostDispatchHook`** (`veronica_core.shield`): No-op implementation of `PostDispatchHook`.
- **`register_veronica_hook()` -- AG2 limitation documented**: Docstring now explicitly notes that
  `register_reply()` provides no after-hook; `CircuitBreakerCapability` is recommended instead.
- **`patch_openai()` / `patch_anthropic()` -- AG2 caveat documented**: Docstring now notes that
  AG2's internal `ModelClient` abstraction may bypass the patch.

### Improved

- **otel.py -- thread safety**: `_otel_enabled` and `_tracer` globals are now protected by a
  `threading.Lock`.
- **Token estimation unified**: All `record_usage()` calls use `len(str(reply)) // 4` consistently.
- **pricing.py -- unknown model warning**: Emits `logging.warning()` when falling back to the
  default price for an unrecognised model name.

---

## [0.11.0] -- 2026-02-25 -- CircuitBreakerCapability for AG2

**Breaking changes:** none

### New

- **CircuitBreakerCapability** (`veronica_core.adapters.ag2_capability`): AG2
  `AgentCapability`-compatible adapter that attaches a per-agent `CircuitBreaker`
  without requiring call-site changes. Call `cap.add_to_agent(agent)` once;
  `generate_reply()` is wrapped transparently. Accepts an optional
  `veronica: VeronicaIntegration` argument for system-wide `SAFE_MODE` propagation
  across all attached agents. Exported from the top-level package as
  `from veronica_core import CircuitBreakerCapability`. Updated AG2 example in
  `examples/integrations/autogen/` extended to 7 demos (demos 5–7 cover
  `CircuitBreakerCapability`; demos 1–4 retain the original wrapper pattern as
  reference).

### Tests

- `tests/test_ag2_capability.py` (21): CircuitBreaker attaches via add_to_agent,
  circuit opens after failure_threshold None replies, open circuit returns None without
  calling generate_reply, HALF_OPEN probe on recovery, successful probe closes circuit,
  independent circuits per agent, SAFE_MODE blocks all agents, SAFE_MODE cleared
  resumes normal operation, no VeronicaIntegration works standalone, failure_count
  resets on success, get_breaker returns correct instance, multiple add_to_agent calls
  idempotent, recovery_timeout respected, non-None reply records pass, mixed agents
  one broken one healthy, circuit state export, failure after partial recovery resets,
  default parameters, keyword arguments accepted, docstring presence.

---

## [0.10.7] -- 2026-02-25 -- PyPI Metadata & Package Housekeeping

**Breaking changes:** none

### Packaging

- **License field**: Changed `license = "MIT"` to `license = {text = "MIT"}` so PyPI
  correctly displays the MIT license (PEP 621 table-format required by hatchling).
- **Development Status**: Classifier updated from `3 - Alpha` to `4 - Beta` reflecting
  stable API surface and 1289-test coverage since v0.10.4.
- **New classifiers**: Added `Operating System :: OS Independent` and
  `Topic :: Scientific/Engineering :: Artificial Intelligence`.
- **Expanded keywords**: Added `safety`, `circuit-breaker`, `token-budget`, `ai-safety`,
  `multi-agent`, `containment`, `rate-limit`.
- **Project URLs**: Added `Homepage` (`https://veronica-core.dev`), `Changelog`, and
  `Documentation` links; homepage URL corrected from PyPI self-reference to project site.

### Housekeeping

- Removed internal ops files from public repo (X posting scripts, marketing assets,
  outreach templates) -- moved to private `veronica-ops` repository.

---

## [0.10.6] -- 2026-02-25 -- Test Suite Quality Overhaul

**Breaking changes:** none

### Tests

- **Classical Testing alignment**: Removed 6 HIGH-priority anti-pattern categories identified
  against [Classical vs London-style testing principles](https://zenn.dev/tko_kasai/articles/3f5863e3407891):
  - **H-1** (`test_aicontainer.py`): Removed object-identity assertion (`is`-check) -- implementation
    detail, not observable behavior.
  - **H-3** (`test_llm_client_injection.py`): Replaced London-style interaction-verification
    (call counts) with observable output assertions.
  - **H-4** (`test_backends.py`, `test_shield_hooks_noop.py`): Deleted trivial CRUD passthrough
    tests and noop-stub assertions that added no behavioral coverage. `test_shield_hooks_noop.py`
    deleted entirely.
  - **H-5** (`test_shield_safe_mode.py`, `test_shield_degrade.py`): Removed ShieldPipeline
    short-circuit assertions duplicated across files (canonical location: `test_shield_pipeline.py`).
    Removed TokenBudgetHook HALT duplication across 4 files.
  - **H-6** (`test_shield_config.py`, `test_shield_types.py`, `test_llm_safety.py`): Removed
    getter-coverage and enum-value tests that verified data structure shape, not behavior.
  - **S-5** (`test_execution_coverage.py`, `test_context_linking.py`,
    `test_v0104_guard_graph.py`): Replaced all private attribute access (`_lock`,
    `_cancellation_token`, `_timeout_thread`, `_cost_usd_accumulated`) with public-API-only
    behavioral assertions. Tests now survive internal renames.

- **New behavioral test files** (37 new tests, all passing):
  - `tests/test_requirements_driven.py` (7): EARS-style requirement-driven tests with
    explicit `REQUIREMENT:` docstrings -- first traceable requirement coverage in the suite.
  - `tests/test_user_journey.py` (6): Primary user journey end-to-end: budget exhaustion
    → reset → resume, and degradation → recovery flows.
  - `tests/test_async_behavior.py` (5): Async observable behavior -- async-wrapped functions
    return correct `Decision`, concurrent `asyncio.gather` calls respect shared budget.
  - `tests/test_timeout_expiry.py` (6): `timeout_ms` watcher E2E verification -- observable
    `Decision.HALT` return before long-running `fn` completes, no private attribute access.
  - `tests/security/test_audit_log_thread_safety.py` (4): 10-thread concurrent AuditLog
    append -- hash chain integrity and zero lost writes under race conditions.
  - `tests/test_fault_injection.py` (9): JSONBackend corrupted-JSON graceful fallback;
    ShieldPipeline hook-exception behaviour (predictable halt, not silent swallow).

- **Refactoring** (no behavior change):
  - **M-1**: Renamed 30+ test functions in `test_shield_degrade.py`, `test_adaptive_budget.py`,
    `test_shield_token_budget.py` from threshold-describing names to business-meaningful names
    (e.g., `system_degrades_service_when_call_rate_approaches_configured_limit`).
  - **M-2**: Converted `test_degradation.py`, `test_budget_cgroup.py`, `test_runtime_policy.py`
    to `@pytest.mark.parametrize` table-driven structure with Given/When/Then comments.
  - **S-4** (`tests/security/test_lint_no_raw_exec.py`, `tools/lint_no_raw_exec.py`): AST
    linter extended to detect aliased subprocess imports (`import subprocess as sp; sp.run(...)`).

### Stats

- Test count: 1262 → 1289 (+27 net; +37 new, -13 removed anti-patterns / deleted file)
- All 1289 tests pass (4 xfailed -- pre-existing SHA pin, unrelated to this release)

---

## [0.10.5] -- 2026-02-23 -- Adversarial Security Hardening

**Breaking changes:** none

### Security

- **HIGH** (`shield/token_budget.py`): `TokenBudgetHook.before_llm_call()` now uses
  pending-reservation accounting to close a TOCTOU window. When `ctx.tokens_out`/`ctx.tokens_in`
  are provided, the estimated tokens are atomically reserved inside the lock after passing all
  checks, so concurrent callers project against `_output_total + _pending_output + estimate`
  rather than a stale snapshot. `record_usage()` releases the pending reservation atomically.
  New `release_reservation()` method lets callers cancel a reservation on LLM call failure.

- **HIGH** (`partial.py`): `PartialResultBuffer.append()` now raises structured
  `PartialBufferOverflow(ValueError)` instead of a plain `ValueError`. The exception carries
  evidence fields (`total_bytes`, `kept_bytes`, `total_chunks`, `kept_chunks`,
  `truncation_point`) enabling upstream callers to emit a `SafetyEvent` with full context.
  `to_dict()` includes `"truncated": True` when an overflow occurred. Backward-compatible:
  existing `except ValueError` handlers continue to work.

- **HIGH** (`shield/budget_window.py`): Off-by-one at window boundary fixed.
  The timestamp pruning loop now uses `< cutoff` instead of `<= cutoff`, preventing a
  single-call gift when an event lands at exactly the window boundary.

- **HIGH** (`containment/execution_graph.py`): Frequency-based divergence detection added
  alongside the existing consecutive-signature check. When any single call signature appears
  `>= freq_threshold` times within the `_K=8` sliding window -- regardless of interleaving --
  a `divergence_suspected` event fires with `detection_mode="frequency"`. Thresholds:
  tool=5, llm=7. This closes the alternating A,B,A,B... bypass confirmed by the adversarial
  review.

- **HIGH** (`retry.py`): `RetryContainer` gains a `jitter: float = 0.25` field (default 25%).
  Backoff delays are multiplied by `1.0 + random.uniform(-jitter, jitter)` and clamped to
  `[0.0, backoff_max]`. Without jitter, simultaneous agents produced perfectly synchronized
  retry bursts (thundering herd).

- **MEDIUM** (`containment/execution_context.py`): Chain event accumulation is now capped at
  `_MAX_CHAIN_EVENTS = 1_000`; events beyond the cap are silently dropped. Duplicate event
  deduplication key now excludes the auto-generated `ts` field (was including it, making every
  event unique and defeating the guard).

- **MEDIUM** (`pricing.py`): Substring-based model name matching removed from
  `resolve_model_pricing()`. Only exact match (step 1) and prefix match (step 2) remain.
  Substring matching allowed a model named `"my-enterprise-gpt-4o-mini"` to resolve to the
  cheaper `"gpt-4o-mini"` pricing, enabling cost underestimation.

- **LOW** (`pricing.py`): `estimate_cost_usd()` now raises `ValueError` for negative
  `tokens_in` / `tokens_out`, closing a vector where adversarial callers could push the
  accumulated cost counter below zero.

- **LOW** (`shield/token_budget.py`): `record_usage()` validates non-negative tokens
  (raises `ValueError`), preventing negative-token injection that would drive
  `_output_total` below zero and permanently disable HALT/DEGRADE.

### Tests

- 21 new regression tests across `test_shield_token_budget.py`, `test_partial.py`,
  `test_execution_graph.py`, and `tests/security/` covering all patched areas.
  Full suite: **1253 passed, 4 xfailed**.

---

## [0.10.4] -- 2026-02-22 -- Concurrency & Isolation Hotfix

**Breaking changes:** none

### Security

- **CRITICAL** (`budget.py`): `BudgetEnforcer.spend()` now uses check-then-add within a single
  lock acquisition. The previous increment-before-check pattern permitted concurrent threads to
  collectively overspend the limit. `spend()` now raises `ValueError` for negative amounts.

- **CRITICAL** (`circuit_breaker.py`): `CircuitBreaker` now tracks an owner context ID.
  Binding the same instance to a second `ExecutionContext` raises `RuntimeError`:
  `"CircuitBreaker instance is being shared across contexts; create a new one per ExecutionContext."`

- **HIGH** (`inject.py`): `veronica_guard` now creates a fresh `AIcontainer` per invocation.
  Previously, all callers of a decorated function shared one `BudgetEnforcer`, `RetryContainer`,
  and `AgentStepGuard`, enabling multi-tenant state leakage.

- **MEDIUM** (`aicontainer.py`): `AIcontainer.reset()` is now protected by a `threading.Lock`
  to prevent TOCTOU races under concurrent `check()` + `reset()` usage.

- **MEDIUM** (`execution_graph.py`): Divergence detection now fires when a node transitions
  `created → success` directly (skipping `mark_running()`), closing the lifecycle-skip bypass.

### Rationale

veronica-core provides chain-scoped containment. Shared mutable enforcement state across chains
or invocations breaks isolation regardless of per-call policy correctness.

---

## [0.10.3] - 2026-02-22

**Breaking changes:** none

### Security

- **CRITICAL: Combined short flag bypass for inline exec (R-1)** (`security/policy_engine.py`):
  `python -Sc "code"` bypassed `SHELL_DENY_INLINE_EXEC` because Python expands `-Sc` into
  `-S -c` at runtime, but the policy engine checked for the exact token `"-c"` using a
  `frozenset` intersection, which returned an empty set for `-Sc`. A new
  `_has_combined_short_flag(token, ch)` helper (`re.match(r"^-[A-Za-z]{2,}$", token)`) now
  detects combined short-option clusters. The `python`/`python3` branch in `_eval_shell` scans
  every token with both an exact check (`token == "-c"`) and a cluster check
  (`_has_combined_short_flag(token, "c")`), closing the bypass for any cluster containing `c`.

- **HIGH: Stdin code execution via `python -` not blocked (R-1)** (`security/policy_engine.py`):
  `python - < evil.py` (stdin execution) was not covered by any deny rule. The `python`/`python3`
  block in `_eval_shell` now explicitly denies `"-"` as an argument with
  `rule_id=SHELL_DENY_INLINE_EXEC`.

- **HIGH: Supply chain bypass via `python -m pip` (R-2)** (`security/policy_engine.py`):
  `python -m pip install evil` received `ALLOW` because `SHELL_PKG_INSTALL` only checked
  `argv0 in ("pip", "pip3")`. The new `_PYTHON_MODULE_PKG_MANAGERS` constant
  (`frozenset({"pip", "pip3", "ensurepip"})`) is checked when `python`/`python3` is invoked
  with `-m`, returning `REQUIRE_APPROVAL` with `rule_id=SHELL_PKG_INSTALL`.

- **HIGH: `make -f evil.mk` sub-shell escape (R-3)** (`security/policy_engine.py`):
  `make` spawns sub-shells to execute recipe lines, which are invisible to `PolicyEngine`.
  No flag-level check can close this structural gap. veronica-core is a policy enforcement
  layer that operates on argv; it is not an OS-level sandbox. Build tools that spawn their
  own sub-shells cannot be safely contained here. `"make"` has been removed from
  `SHELL_ALLOW_COMMANDS`; all `make` invocations now return `DENY`.

  > **BREAKING CHANGE**: Any code that relied on `make` being allowlisted will now receive
  > `DENY`. See the v0.10.3 release notes for migration options.

- **MEDIUM: Policy YAML load fail-open (R-5)** (`security/policy_engine.py`):
  `_load_policy()` caught all exceptions and silently returned `{}`, meaning a corrupt or
  attacker-tampered YAML file would silently disable all YAML-defined rules (rollback checks,
  custom allow/deny lists). `_load_policy()` is now fail-closed: if the policy file *exists*
  but cannot be parsed, a `RuntimeError` is raised immediately. The file-absent path retains
  backward-compatible behavior (warn and return `{}`).

---

## [0.10.2] - 2026-02-22

**Breaking changes:** none

### Security

- **CRITICAL: Inline code execution bypass via SHELL_ALLOW_COMMANDS** (`security/policy_engine.py`):
  Allowlisted commands such as `python`, `python3`, `cmake`, and `make` could bypass the
  containment layer when called with inline code execution flags (`-c`, `-P`, `--eval`).
  An adversary with shell tool access could execute `python -c "import os; os.system(...)"` and
  receive `ALLOW` because `python` is in `SHELL_ALLOW_COMMANDS` while the dangerous flag was
  not inspected. A new `SHELL_DENY_EXEC_FLAGS` table now denies these flag patterns with
  `rule_id=SHELL_DENY_INLINE_EXEC` and `risk_score_delta=9` before the allowlist check.
  The `uv run` wrapper path is also covered: `uv run python -c "..."` is blocked by scanning
  `args[2:]` for inline-exec flags (`_UVR_INLINE_EXEC_FLAGS`).

- **HIGH: Missing command substitution operators in `SHELL_DENY_OPERATORS`** (`security/policy_engine.py`):
  The operator deny list did not include `$(`, backtick (`` ` ``), or newline (`\n`).
  All three enable shell command substitution, allowing payloads such as
  `echo "$(cat /etc/passwd)"` to pass operator checks.
  All three patterns are now blocked with `rule_id=SHELL_DENY_OPERATOR`.

- **HIGH: URL host extraction inconsistency** (`security/policy_engine.py`):
  `_url_host()` used a hand-rolled parser that diverged from `urllib.parse.urlparse()` under
  non-standard URL formats (user-info fields, IPv6 literals, percent-encoding, default ports).
  An attacker could craft a URL parsed differently by `_url_host` (host allowlist) versus
  `_url_path` (path allowlist), potentially allowing a request to a non-allowlisted host.
  `_url_host()` now uses `urllib.parse.urlparse(url).hostname`, matching `_url_path()`.

- **HIGH: `patch.py` thread safety** (`patch.py`):
  The module-level `_patches` registry had no lock. Concurrent calls to `patch_openai()` and
  `unpatch_all()` could corrupt the registry, causing an already-wrapped callable to be wrapped
  again (infinite recursion) or the original to be lost (double-free-equivalent). A
  `threading.Lock` (`_patches_lock`) now guards all reads and writes to `_patches` in
  `patch_openai()`, `patch_anthropic()`, and `unpatch_all()`.

- **MEDIUM: `SandboxRunner` stale data contamination** (`runner/sandbox.py`):
  When `ephemeral_dir` was provided with `read_only=True`, the repo was copied into
  `ephemeral_dir/_repo` with `dirs_exist_ok=True`. If `_repo` existed from a previous run,
  files deleted from the source repo persisted in the sandbox, creating a data contamination
  path where the sandbox environment diverged from the source tree. `_setup()` now removes
  `_repo` before copying and uses `dirs_exist_ok=False` to enforce a clean copy.

- **MEDIUM: `SecretMasker` `HEX_SECRET` upper-bound gap** (`security/masking.py`):
  The `HEX_SECRET` pattern matched `{32,64}` hex characters, silently missing secrets longer
  than 64 characters (e.g. 128-char SHA-512 digests, 256-char token strings). The pattern is
  updated to `{32,}` (no upper bound) while retaining the word-boundary guards that prevent
  false positives on commit hashes embedded in prose.

- **LOW: Silent policy file load failure** (`security/policy_engine.py`):
  `_load_policy()` returned an empty `{}` on any `Exception` without logging, making YAML
  parse errors and missing `pyyaml` installs silently undetectable in production. Failures now
  emit `logger.warning("policy_load_failed: ...")` with file path and exception type.

---

## [0.10.1] - 2026-02-22

**Breaking changes:** none

### Security

- **`_load_key()` warning on dev key fallback** (`security/policy_signing.py`): emit
  `logger.warning` when `VERONICA_POLICY_KEY` is unset so operators are alerted before
  deploying with the publicly-known development key. Docstring now documents the
  recommended `secrets.token_hex(32)` generation command and points to AWS Secrets
  Manager / Azure Key Vault / HashiCorp Vault as preferred secret stores.

- **Sandbox ignores credential files** (`runner/sandbox_windows.py`): `shutil.copytree`
  ignore patterns extended to cover `.env`, `.env.*`, `*.env`, `*.key`, `*.pem`, `*.pfx`,
  `*.p12`, and `*.secret` -- preventing private keys and credentials from being copied into
  the ephemeral sandbox directory.

- **`NonceRegistry` TTL-based eviction** (`approval/approver.py`): replaced size-based
  eviction (`maxsize`) with time-to-live eviction (default 5 minutes). Nonces are now
  guaranteed to be checked for the full TTL window regardless of registry size, eliminating
  the replay-attack window that existed when the registry was full and old nonces were
  evicted before expiry.

- **Exception narrowing in `policy_signing.py`**: broad `except Exception` blocks replaced
  with `except ValueError` (malformed base64) and a dedicated `except Exception` that logs
  only `type(exc).__name__` to avoid leaking key material in error messages.

- **`scheduler.py` -- remove `__import__("time")` anti-pattern**: replaced dynamic import
  in hot event-emission paths with a standard top-level `import time`, removing an
  unintentional obfuscation of the module dependency.

- **Silent log-corruption skip replaced with warnings** (`audit/log.py`): `get_last_policy_version`
  and `_load_last_hash` now emit `logger.warning` (including file path and exception) instead
  of silently continuing past `json.JSONDecodeError` / `OSError`, making audit-log corruption
  observable.

---

## [0.10.0] - 2026-02-22

**Breaking changes:** none

### Added

- **Auto cost estimation** (`veronica_core.pricing`): built-in pricing table for OpenAI, Anthropic,
  and Google models. Pass `model` and `response_hint` to `WrapOptions` and the cost is extracted
  automatically from the SDK response. Falls back to a conservative sentinel ($0.030/$0.060 per 1K)
  for unknown models, with a `COST_ESTIMATION_SKIPPED` SafetyEvent when usage data isn't available.
  Direct API: `estimate_cost_usd()`, `resolve_model_pricing()`, `extract_usage_from_response()`.

- **Distributed budget backend** (`veronica_core.distributed`): `BudgetBackend` protocol with
  `LocalBudgetBackend` (in-process, thread-safe) and `RedisBudgetBackend` (cross-process via
  `INCRBYFLOAT`). Pass `redis_url` to `ExecutionConfig` and the backend wires up automatically.
  Falls back to local accumulation on Redis errors when `fallback_on_error=True`. Optional extra:
  `veronica-core[redis]`.

- **OpenTelemetry export** (`veronica_core.otel`): `enable_otel(service_name)` hooks into
  `ShieldPipeline` and emits each `SafetyEvent` as an OTel span event. Prompt and response content
  is never exported. Optional extra: `veronica-core[otel]`.

- **Parent-child context linking** (`veronica_core.containment`): pass `parent=ctx` to
  `ExecutionContext` or use `ctx.spawn_child()` to create a linked child. Child costs propagate
  up the chain automatically; if the parent ceiling is exceeded, it marks itself aborted.
  `ContextSnapshot.parent_chain_id` records the parent's chain ID.

- **Degradation ladder** (`veronica_core.shield.degradation`): `DegradationLadder` evaluates
  cost fraction against configurable thresholds and returns tiered `PolicyDecision` values:
  model downgrade -> context trim -> rate limit -> halt. `PolicyDecision` gains
  `degradation_action`, `fallback_model`, and `rate_limit_ms` fields (all backward-compatible
  defaults). Helper factories: `allow()`, `deny()`, `model_downgrade()`, `rate_limit_decision()`.

### Notes

- No breaking API changes. All v0.9.7 public interfaces are unchanged.
- New optional extras: `[redis]`, `[otel]`.
- Test suite: 1185 passing, 0 failures.

---

## [0.9.7] - 2026-02-22

**Breaking changes:** none

### Fixed

- **Thread safety (11 issues):**
  - `agent_guard`: add `threading.Lock` to all state-mutating methods
  - `budget`: protect property reads (`spent_usd`, `remaining_usd`, etc.) inside Lock to prevent torn reads
  - `circuit_breaker`: add `threading.Lock`; extract `_maybe_half_open_locked` to avoid side-effects under contention
  - `shield/pipeline`: protect `safety_events` list with `threading.Lock`
  - `security/policy_engine`: protect `PolicyHook.last_decision` with Lock
  - `security/risk_score`: fix TOCTOU in `is_safe_mode` (atomicize score read + compare)
  - `exit`: add `threading.Lock` to `request_exit` (prevents duplicate exit processing on double SIGTERM)
  - `state`: add `threading.Lock` to all methods; fix `cleanup_expired` deadlock risk via lock separation

- **Resource leak (1 issue):**
  - `containment/execution_context`: call `thread.join(timeout=1.0)` in `__exit__` to prevent timeout watcher thread leak

- **Security (1 issue):**
  - `security/key_pin`: replace `==` with `hmac.compare_digest` to prevent timing-attack key-pin bypass

- **Logic bugs (3 issues):**
  - `containment/execution_context`: atomicize cost-estimate check to eliminate TOCTOU race
  - `state`: fix `state_history` trim (was reassignment; now in-place `del`); enforce `VALID_TRANSITIONS` on all transitions
  - `integration`: register `atexit` handler in modern backend mode (was missing)
  - `audit/log`: move `_build_entry()` inside Lock to prevent hash-chain corruption under concurrent writes

### Notes

- No API changes. All v0.9.6 public interfaces are unchanged.
- Test suite: 1120 passing, 0 failures, 92% coverage.

---

## [0.9.6] - 2026-02-21

**Breaking changes:** none

### Added
- `SemanticLoopGuard` -- pure-Python semantic loop detection using word-level
  Jaccard similarity; no heavy dependencies required
- `AIcontainer` now accepts `semantic_guard: Optional[SemanticLoopGuard]`
  parameter for automatic loop enforcement
- `SemanticLoopGuard.feed(text)` -- convenience method combining `record()` + `check()`
- `SemanticLoopGuard.reset()` -- clears the rolling output buffer

### Details
- Rolling window of recent LLM outputs (default: 3)
- Configurable Jaccard threshold (default: 0.92)
- Exact-match shortcut for O(1) detection of identical outputs
- `min_chars` guard to avoid false positives on short outputs (default: 80)
- Implements `RuntimePolicy` protocol (`check`, `policy_type`, `reset`)
- Exported from top-level `veronica_core` namespace
- 15 new tests; total: 1120 passing

---

## [0.9.5] -- 2026-02-21

**Breaking changes:** none

### Added
- `veronica_core.adapters.langchain` module: LangChain callback handler.
  - `VeronicaCallbackHandler(config)`: `BaseCallbackHandler` subclass that enforces
    VERONICA policies on every LLM call in a LangChain pipeline.
  - Accepts `GuardConfig` or `ExecutionConfig` (both expose `max_cost_usd`,
    `max_steps`, `max_retries_total`).
  - `on_llm_start`: pre-call policy check via `AIcontainer.check()`.
    Raises `VeronicaHalt` on denial.
  - `on_llm_end`: increments step counter; records token cost via
    `BudgetEnforcer.spend()`.
  - `on_llm_error`: logs error without charging budget.
  - `langchain-core` (or `langchain`) required separately; not a
    `veronica-core` dependency. Clear `ImportError` if absent.

### Notes
- No deprecations. All existing APIs unchanged.
- Adapters are opt-in: `from veronica_core.adapters.langchain import VeronicaCallbackHandler`.
  The top-level `veronica_core` namespace is not changed.

---

## [0.9.4] -- 2026-02-21

**Breaking changes:** none

### Added
- `veronica_core.patch` module: opt-in SDK monkey-patching for OpenAI and Anthropic clients.
  - `patch_openai()`: patches `openai.resources.chat.completions.Completions.create` (v1.x+)
    and `openai.ChatCompletion.create` (v0.x legacy). Safe if openai is not installed.
  - `patch_anthropic()`: patches `anthropic.resources.messages.Messages.create`.
    Safe if anthropic is not installed.
  - `unpatch_all()`: restores all patched methods to their originals.
  - Context-aware: patches only activate inside `@veronica_guard` boundaries.
    Calls outside a guard pass through unchanged.
  - Pre-call: policy check via active container (`container.check(cost_usd=0.0)`).
  - Post-call: token cost recorded via `BudgetEnforcer.spend()` after response.
- `get_active_container() -> Optional[AIcontainer]`: returns the `AIcontainer` bound
  to the current `@veronica_guard` boundary, or `None` if called outside a guard.
  Exported from `veronica_core.inject` and top-level `veronica_core`.

### Notes
- No deprecations. All existing APIs unchanged.
- Neither `openai` nor `anthropic` is added as a dependency -- both remain optional.
- Patches are NOT applied on import. Explicit opt-in required (`patch_openai()` / `patch_anthropic()`).

---

## [0.9.3] -- 2026-02-21

**Breaking changes:** none

### Added
- `veronica_core.inject` module: decorator-based execution boundary injection.
  - `veronica_guard(max_cost_usd, max_steps, max_retries_total, timeout_ms, return_decision)`:
    wraps any callable in an `AIcontainer` execution boundary. Raises `VeronicaHalt`
    on policy denial; returns `PolicyDecision` when `return_decision=True`.
  - `VeronicaHalt(RuntimeError)`: raised when a guard denies execution. Carries
    `.reason: str` and `.decision: PolicyDecision`.
  - `GuardConfig`: dataclass documenting all `veronica_guard` parameters.
  - `is_guard_active() -> bool`: returns `True` when called inside a guard boundary
    (via `contextvars`). Enables future transparent injection.
- All symbols exported from `veronica_core` top-level (`__init__.py`).

### Notes
- No deprecations. All existing APIs unchanged.
- `timeout_ms` is accepted but not yet enforced (reserved for v1.0).

---

## [0.9.2] -- 2026-02-21

**Breaking changes:** none

### Fixed
- `release.yml`: replaced `secrets != ''` if-condition (invalid at workflow parse-time)
  with shell guard `[ -n "${VAR}" ]`; signing step now skips cleanly when key is absent.
- `__version__` in `__init__.py` and `version` in `pyproject.toml` now match on every
  commit that reaches PyPI (release_check gate enforces consistency).

### Notes
- No API changes. All v0.9.1 code is unchanged; this is a CI infrastructure fix only.

---

## [0.9.1] -- 2026-02-21

**Breaking changes:** none

### Added
- `AIcontainer` (`veronica_core.container`): declarative execution boundary that composes
  `BudgetEnforcer`, `CircuitBreaker`, `RetryContainer`, `AgentStepGuard` into a single
  `check()` / `reset()` API. All existing primitive imports unchanged.

### Security
- `PolicyEngine` with declarative DENY/REQUIRE_APPROVAL/ALLOW rules loaded from
  `policies/default.yaml`; integrates with `ShieldPipeline` via `PolicyHook`.
- `SecretMasker`: redacts 28 credential patterns (AWS, GitHub, OpenAI, Anthropic,
  Stripe, Slack, bitbank, Polymarket, and others) in audit output.
- `SandboxRunner` / `WindowsSandboxRunner`: ephemeral temp-dir isolation with auto-cleanup.
- `CLIApprover` v1: HMAC-SHA256 signed approval tokens with 5-minute TTL.
- `ApprovalToken` v2: single-use nonces with replay, scope, and expiry enforcement.
- `ApprovalBatcher` (SHA-256 dedup) + `ApprovalRateLimiter` (token bucket, 10/60s).
- `AuditLog`: append-only JSONL with SHA-256 hash chain; secret masking on all entries.
- `RiskScoreAccumulator`: thread-safe deny counter that auto-transitions to SAFE_MODE
  at a configurable threshold.
- AST-based CI lint gate (`tools/lint_no_raw_exec.py`) blocking `exec()`, `eval()`,
  `os.system()`, `subprocess` with `shell=True` in source files.
- Network exfiltration guard: URL length cap (2048 chars), Shannon entropy check (>4.5
  bits), base64/hex query-string pattern detection.
- Policy file signing: HMAC-SHA256 (`policies/default.yaml.sig`) and ed25519
  (`policies/default.yaml.sig.v2`); `RuntimeError` on tamper detection.
- Supply chain guard: `pip`, `npm`, `uv`, `cargo install` and lock-file writes
  route to REQUIRE_APPROVAL in the policy engine.
- `EnvironmentFingerprint` + `AttestationChecker`: mid-session anomaly detection
  with `ATTESTATION_ANOMALY` audit event.
- Security levels `DEV` / `CI` / `PROD` (auto-detected via `VERONICA_SECURITY_LEVEL`).
- SHA-256 key pinning (`policies/key_pin.txt`): `RuntimeError` on mismatch in CI/PROD.
- Policy rollback protection via backward scan of the audit log.
- Release tooling: `tools/release_sign_policy.py` (ed25519 signing CLI),
  `tools/verify_release.py` (sig + key-pin + policy-version verification).
- 36 verifiable security claims documented in `docs/SECURITY_CLAIMS.md` with pytest mapping.
- 20 red-team regression scenarios (exfiltration, credential-hunt, workflow-poison,
  persistence) all blocked, tracked in `tests/redteam/test_redteam.py`.

### Fixed
- CRLF normalization in `PolicySigner` for cross-platform ed25519 consistency.
- `cryptography` added to dev dependencies to fix CI test collection.

### Notes
- No deprecation warnings introduced in this release.
- Existing primitives (`BudgetEnforcer`, `CircuitBreaker`, etc.) are fully preserved.

---

## v0.9.0 -- Runtime Containment Edition

### Architectural Shift

VERONICA is no longer positioned primarily as a "cost control" or "LLM safety utility".

It is now explicitly defined as:

> A Runtime Containment Layer for LLM Systems.

This release introduces a structural evolution, not incremental feature additions.
The prior framing emphasized individual enforcement hooks applied at call sites.
The v0.9.0 framing emphasizes what those hooks collectively constitute:
a constraint layer that makes unbounded model behavior bounded at the system level.

The distinction matters because containment is an architectural property, not a feature.
An observability stack tells you that an agent ran away.
A containment layer prevents it from doing so.

---

### Added

- **ExecutionGraph**: a first-class execution graph tracking every LLM and tool call
  within an `ExecutionContext` chain as typed nodes with lifecycle states
  (`created`, `running`, `success`, `fail`, `halt`). Each node records
  `cost_usd`, `tokens_in`, `tokens_out`, `stop_reason`, and `error_class`.

- **Chain-level amplification metrics**: `llm_calls_per_root`, `tool_calls_per_root`,
  `retries_per_root` -- derived from graph counters. Exposes how many calls a single
  root request generates. HALT and FAIL nodes are counted as attempted calls.

- **Divergence heuristic**: repeated-signature detection using a ring buffer of the
  last K=8 `(kind, name)` signatures. Emits `SafetyEvent("divergence_suspected",
  severity="warn")` when a tool repeats 3 times consecutively or an LLM call repeats
  5 times. Does not halt by default. Deduplicated per chain per signature.

- **`ctx.get_graph_snapshot()`**: returns an immutable, JSON-serializable snapshot of
  the full execution graph including all nodes, aggregates, and amplification metrics.

- **`docs/execution-graph.md`**: full specification of the graph model, invariants,
  data model, and API surface.

- **`docs/amplification-factor.md`**: definition and rationale for chain-level
  amplification metrics.

- **`docs/divergence-heuristics.md`**: specification of the divergence detection
  heuristic, thresholds, deduplication rule, and limitations.

---

### Changed

- **`ExecutionContext`** now instantiates an `ExecutionGraph` at init and records
  every `wrap_llm_call()` and `wrap_tool_call()` dispatch as a graph node with
  full lifecycle tracking.

- **HALT semantics** are now recorded as `stop_reason` on graph nodes
  (`budget_exceeded`, `step_limit_exceeded`, `circuit_open`, `aborted`, etc.).
  Previously, HALTs were only visible as `SafetyEvent` entries.

- **`get_snapshot().graph_summary`**: optional field added to `ContextSnapshot`
  containing `{total_cost_usd, total_llm_calls, total_tool_calls, total_retries,
  max_depth, llm_calls_per_root, tool_calls_per_root, retries_per_root}`.

- **`README.md`**: rewritten to lead with distributed systems vocabulary.
  Positions VERONICA as the fourth component of the LLM stack alongside prompting,
  orchestration, and observability.

- **`pyproject.toml` description**: updated to reflect the Runtime Containment framing.

---

### Design Clarifications

Containment is defined as enforcement of bounded properties across five dimensions:

1. **Cost** -- total spend per chain is capped and verified at dispatch time
2. **Retries** -- retry budget is finite and tracked; runaway retry loops are bounded
3. **Recursion** -- step count limits prevent infinite agent loops
4. **Wait states** -- timeout enforcement prevents indefinite blocking
5. **Failure domains** -- circuit breakers isolate failure propagation across chains

**Observability is not Containment.** This distinction is clarified explicitly in
the revised README and in `docs/execution-graph.md`. Tracing and logging record what
happened. Containment prevents specific classes of behavior from happening.

The `ExecutionGraph` is an internal structural model, not a tracing product.
It exists to make containment decisions explicit and auditable within the process.

---

### Backwards Compatibility

- No breaking API changes. All existing client code continues to work unchanged.
- The simple wrapper pattern (`with ExecutionContext(...) as ctx`) is fully preserved.
- `ExecutionGraph` is additive; it is instantiated automatically and does not require
  any changes to call sites.
- `graph_summary` in `ContextSnapshot` is `Optional` and defaults to `None`.
  Callers that do not use it are unaffected.
- `get_graph_snapshot()` is a new method; no existing method signatures changed.

---

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

## v0.3.x

- `CircuitBreaker`: CLOSED/OPEN/HALF_OPEN state machine
- `RetryContainer`: bounded retry with jitter
- `AgentStepGuard`: step count enforcement
- Runtime Policy Control API

## v0.2.x

- `BudgetEnforcer`: hard cost ceiling per chain
- `PolicyContext` / `PolicyDecision` / `PolicyPipeline`

## v0.1.x

- Initial release: state machine core, persistence backends, guard interface
