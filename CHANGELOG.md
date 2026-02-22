# Changelog

All notable changes to this project will be documented in this file.

---

## [0.10.0] - 2026-02-22

### Added

- **P1-1: Auto Cost Calculation (`veronica_core.pricing`)**
  - `Pricing` dataclass: `input_per_1k`, `output_per_1k` (USD)
  - `PRICING_TABLE`: built-in pricing for OpenAI (gpt-4o, gpt-4o-mini, o1, o1-mini, o3-mini),
    Anthropic (claude-3-5-sonnet, claude-3-5-haiku, claude-3-opus), and Google (gemini-1.5-pro, gemini-1.5-flash)
  - `resolve_model_pricing(model)`: case-insensitive prefix lookup; falls back to a conservative
    `$0.030/$0.060` per 1k sentinel when the model is unknown
  - `estimate_cost_usd(model, tokens_in, tokens_out)`: single-call cost estimate
  - `extract_usage_from_response(response)`: extracts `(tokens_in, tokens_out)` from OpenAI,
    Anthropic, and Google SDK response objects (duck-typed, no hard dependency)
  - `ExecutionContext.wrap_llm_call()` / `wrap_tool_call()`: new `model` and `response_hint`
    fields on `WrapOptions`; cost is auto-estimated when `cost_estimate_hint` is 0 and a
    response is available; emits `SafetyEvent("cost_estimated", severity="info")` on success,
    `SafetyEvent("cost_estimation_unavailable", severity="warn")` on fallback
  - Exported from top-level `veronica_core` namespace
  - 17 new tests (test_pricing.py); total: 1185 passing

- **P1-2: Distributed Budget Backend (`veronica_core.distributed`)**
  - `BudgetBackend` Protocol: `add(amount) -> float`, `get() -> float`, `reset()`, `close()`
  - `LocalBudgetBackend`: thread-safe in-process backend (drop-in for existing behavior)
  - `RedisBudgetBackend`: cross-process budget coordination via `INCRBYFLOAT` + atomic pipeline.
    Key format: `veronica:budget:{chain_id}`. Configurable TTL (default: 3600s).
    `fallback_on_error=True` falls back to local accumulation on Redis failure.
  - `get_default_backend(redis_url, chain_id, ttl_seconds)`: convenience factory; returns
    `LocalBudgetBackend` when `redis_url` is `None`
  - `ExecutionConfig`: new optional `redis_url: str | None` field (default `None`)
  - `AIcontainer`: passes `redis_url` through to `ExecutionConfig` when provided
  - Optional extra: `pip install veronica-core[redis]` (requires `redis>=5.0`)
  - Dev extra: `fakeredis>=2.0` for unit testing without a real Redis instance
  - Exported from top-level `veronica_core` namespace
  - 12 new tests (test_distributed.py)

- **P2-1: OpenTelemetry Export (`veronica_core.otel`)**
  - `enable_otel(service_name, exporter=None, endpoint=None)`: configures a global OTel tracer.
    Uses `OTLPSpanExporter` when endpoint is provided; falls back to `ConsoleSpanExporter`.
  - `enable_otel_with_tracer(tracer)`: inject a pre-built tracer (used in tests)
  - `disable_otel()` / `is_otel_enabled()`: runtime toggle
  - `emit_safety_event(event: SafetyEvent)`: records `SafetyEvent` fields as OTel span event
    attributes. **Privacy guarantee**: prompt/response content is never included; only
    `event_type`, `decision`, `reason` (truncated to 200 chars), `hook`, `chain_id`.
  - `emit_containment_decision(decision_name, reason, cost_usd, chain_id)`: low-level helper
  - `ShieldPipeline._record()`: automatically emits OTel after recording each `SafetyEvent`
  - Optional extra: `pip install veronica-core[otel]` (requires `opentelemetry-api>=1.20`)
  - Dev extra: `opentelemetry-sdk>=1.20` for `InMemorySpanExporter`-based tests
  - Exported: `enable_otel`, `disable_otel`, `is_otel_enabled`
  - 10 new tests (test_otel.py)

- **P2-2: Multi-agent Context Linking (`veronica_core.containment`)**
  - `ExecutionContext.__init__`: new optional `parent: ExecutionContext | None` parameter
  - `ExecutionContext.spawn_child(**overrides)`: creates a child context linked to `self`;
    child budget defaults to `parent.remaining_budget_usd` when not overridden
  - Child cost propagation: every `_accumulate_cost()` call walks up to the root and calls
    `_propagate_child_cost()` on each ancestor; parent aborts if its own ceiling is exceeded
  - `ContextSnapshot.parent_chain_id`: captures the parent's `chain_id` (or `None` for roots)
  - All existing constructor signatures and context-manager usage unchanged
  - 12 new tests (test_context_linking.py)

- **P2-3: Degradation Ladder (`veronica_core.shield.degradation`)**
  - `Trimmer` Protocol: `trim(messages: list) -> list`
  - `NoOpTrimmer`: pass-through trimmer (default)
  - `DegradationConfig`: `model_map`, `rate_limit_ms`, `cost_thresholds`
    (keys: `model_downgrade`, `context_trim`, `rate_limit`; values: fraction of `max_cost_usd`)
  - `DegradationLadder.evaluate(cost_accumulated, max_cost_usd, current_model) -> PolicyDecision | None`:
    returns `None` below all thresholds; returns a graded `PolicyDecision` with
    `degradation_action` ∈ `{model_downgrade, context_trim, rate_limit, halt}` above each tier
  - `apply_rate_limit(decision)`: `time.sleep(rate_limit_ms / 1000)`
  - `apply_context_trim(messages)`: delegates to the configured `Trimmer`
  - `PolicyDecision` extended with `degradation_action: str | None`, `fallback_model: str | None`,
    `rate_limit_ms: int` (all default to backward-compatible values — no existing code changes)
  - Helper factories: `allow(policy_type)`, `deny(reason)`, `model_downgrade(current, target)`,
    `rate_limit_decision(ms)` — exported from `veronica_core.runtime_policy` and top-level
  - 16 new tests (test_degradation.py)

### Notes

- No breaking API changes. All v0.9.7 public interfaces are fully preserved.
- New optional extras: `[redis]`, `[otel]` — neither is installed by default.
- Test suite: 1185 passing, 0 failures, 4 xfailed.
- Assumptions made during implementation (documented in code):
  - Unknown models in `PRICING_TABLE` fall back to `$0.030/$0.060` per 1k (conservative sentinel)
  - `RedisBudgetBackend` uses string keys with float-compatible `INCRBYFLOAT`; TTL is reset on each write
  - OTel reason strings are truncated at 200 characters to limit span attribute size
  - `spawn_child` without explicit `max_cost_usd` inherits `parent.remaining_budget_usd` at spawn time (not dynamically)

---

## [0.9.7] - 2026-02-22

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

### Added
- `SemanticLoopGuard` — pure-Python semantic loop detection using word-level
  Jaccard similarity; no heavy dependencies required
- `AIcontainer` now accepts `semantic_guard: Optional[SemanticLoopGuard]`
  parameter for automatic loop enforcement
- `SemanticLoopGuard.feed(text)` — convenience method combining `record()` + `check()`
- `SemanticLoopGuard.reset()` — clears the rolling output buffer

### Details
- Rolling window of recent LLM outputs (default: 3)
- Configurable Jaccard threshold (default: 0.92)
- Exact-match shortcut for O(1) detection of identical outputs
- `min_chars` guard to avoid false positives on short outputs (default: 80)
- Implements `RuntimePolicy` protocol (`check`, `policy_type`, `reset`)
- Exported from top-level `veronica_core` namespace
- 15 new tests; total: 1120 passing

---

## [0.9.5] — 2026-02-21

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
