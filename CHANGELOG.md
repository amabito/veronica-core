# Changelog

All notable changes to this project will be documented in this file.

---

## [0.9.4] — 2026-02-21

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
- Neither `openai` nor `anthropic` is added as a dependency — both remain optional.
- Patches are NOT applied on import. Explicit opt-in required (`patch_openai()` / `patch_anthropic()`).

---

## [0.9.3] — 2026-02-21

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

## [0.9.2] — 2026-02-21

### Fixed
- `release.yml`: replaced `secrets != ''` if-condition (invalid at workflow parse-time)
  with shell guard `[ -n "${VAR}" ]`; signing step now skips cleanly when key is absent.
- `__version__` in `__init__.py` and `version` in `pyproject.toml` now match on every
  commit that reaches PyPI (release_check gate enforces consistency).

### Notes
- No API changes. All v0.9.1 code is unchanged; this is a CI infrastructure fix only.

---

## [0.9.1] — 2026-02-21

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

## v0.9.0 — Runtime Containment Edition

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
  `retries_per_root` — derived from graph counters. Exposes how many calls a single
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

1. **Cost** — total spend per chain is capped and verified at dispatch time
2. **Retries** — retry budget is finite and tracked; runaway retry loops are bounded
3. **Recursion** — step count limits prevent infinite agent loops
4. **Wait states** — timeout enforcement prevents indefinite blocking
5. **Failure domains** — circuit breakers isolate failure propagation across chains

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
