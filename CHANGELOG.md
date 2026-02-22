# Changelog

All notable changes to this project will be documented in this file.

---

## [0.10.3] - 2026-02-22

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
  No flag-level check can close this structural gap. `"make"` has been removed from
  `SHELL_ALLOW_COMMANDS` so all `make` invocations now return `DENY`.

  **Breaking change**: code that previously relied on `make` being allowlisted must explicitly
  grant `make` access through a custom `PolicyEngine` subclass or YAML policy override.

- **MEDIUM: Policy YAML load fail-open (R-5)** (`security/policy_engine.py`):
  `_load_policy()` caught all exceptions and silently returned `{}`, meaning a corrupt or
  attacker-tampered YAML file would silently disable all YAML-defined rules (rollback checks,
  custom allow/deny lists). `_load_policy()` is now fail-closed: if the policy file *exists*
  but cannot be parsed, a `RuntimeError` is raised immediately. The file-absent path retains
  backward-compatible behavior (warn and return `{}`).

---

## [0.10.2] - 2026-02-22

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

### Security

- **`_load_key()` warning on dev key fallback** (`security/policy_signing.py`): emit
  `logger.warning` when `VERONICA_POLICY_KEY` is unset so operators are alerted before
  deploying with the publicly-known development key. Docstring now documents the
  recommended `secrets.token_hex(32)` generation command and points to AWS Secrets
  Manager / Azure Key Vault / HashiCorp Vault as preferred secret stores.

- **Sandbox ignores credential files** (`runner/sandbox_windows.py`): `shutil.copytree`
  ignore patterns extended to cover `.env`, `.env.*`, `*.env`, `*.key`, `*.pem`, `*.pfx`,
  `*.p12`, and `*.secret` — preventing private keys and credentials from being copied into
  the ephemeral sandbox directory.

- **`NonceRegistry` TTL-based eviction** (`approval/approver.py`): replaced size-based
  eviction (`maxsize`) with time-to-live eviction (default 5 minutes). Nonces are now
  guaranteed to be checked for the full TTL window regardless of registry size, eliminating
  the replay-attack window that existed when the registry was full and old nonces were
  evicted before expiry.

- **Exception narrowing in `policy_signing.py`**: broad `except Exception` blocks replaced
  with `except ValueError` (malformed base64) and a dedicated `except Exception` that logs
  only `type(exc).__name__` to avoid leaking key material in error messages.

- **`scheduler.py` — remove `__import__("time")` anti-pattern**: replaced dynamic import
  in hot event-emission paths with a standard top-level `import time`, removing an
  unintentional obfuscation of the module dependency.

- **Silent log-corruption skip replaced with warnings** (`audit/log.py`): `get_last_policy_version`
  and `_load_last_hash` now emit `logger.warning` (including file path and exception) instead
  of silently continuing past `json.JSONDecodeError` / `OSError`, making audit-log corruption
  observable.

---

## [0.10.0] - 2026-02-22

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
