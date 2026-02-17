# Changelog

All notable changes to VERONICA Core will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-02-17

### Added
- **Runtime Policy Control** -- composable LLM runtime constraint abstractions
  - `RuntimePolicy` Protocol (`check(context) -> PolicyDecision`, `reset()`, `policy_type`)
  - `PolicyContext` dataclass (cost_usd, step_count, entity_id, chain_id, timestamp, metadata)
  - `PolicyDecision` dataclass (allowed, policy_type, reason, partial_result)
  - `PolicyPipeline` AND-composition (first denial wins, no override)
- `CircuitBreaker` -- automatic failure isolation (CLOSED/OPEN/HALF_OPEN)
  - Configurable failure_threshold and recovery_timeout
  - Implements RuntimePolicy protocol

### Changed
- `BudgetEnforcer` now implements `RuntimePolicy` (additive: `check()`, `policy_type`)
- `AgentStepGuard` now implements `RuntimePolicy` (additive: `check()`, `policy_type`)
- `RetryContainer` now implements `RuntimePolicy` (additive: `check()`, `reset()`, `policy_type`)

### Migration
- **Zero breaking changes.** All v0.1 APIs work unchanged.
- New imports available: `from veronica_core import RuntimePolicy, PolicyContext, PolicyDecision, PolicyPipeline, CircuitBreaker, CircuitState`
- Existing primitives gain `check()` method without affecting `spend()`, `step()`, or `execute()` behavior

## [0.1.0] - 2026-02-16

### Added
- Core state machine (VeronicaStateMachine)
  - Per-entity fail counting and cooldown management
  - State transitions with history tracking (IDLE/SCREENING/COOLDOWN/SAFE_MODE/ERROR)
  - Configurable cooldown thresholds and duration
- Persistence layer
  - Atomic JSON file-based persistence (crash-safe tmp → rename)
  - Pluggable backend interface (PersistenceBackend)
  - Memory backend for testing
- Exit handler (VeronicaExit)
  - 3-tier exit strategy (GRACEFUL/EMERGENCY/FORCE)
  - Signal handlers for SIGTERM/SIGINT
  - atexit fallback for unhandled exits
  - Guaranteed state preservation on graceful shutdown
- Guard interface (VeronicaGuard)
  - Pluggable validation and lifecycle hooks
  - PermissiveGuard (default, no validation)
  - Custom guard support for domain-specific logic
- LLM client integration (optional)
  - LLMClient Protocol for pluggable AI integration
  - NullClient (default, no LLM features)
  - DummyClient for testing
  - Zero LLM dependencies (stdlib only)
- Integration API (VeronicaIntegration)
  - High-level facade for common operations
  - Auto-save with configurable interval
  - Singleton pattern support (get_veronica_integration)
- Critical state preservation
  - SAFE_MODE/ERROR states persist across restarts
  - Cooldown timers survive hard kills (SIGKILL)
  - Emergency handlers save state before exit

### Features
- Zero external dependencies (stdlib only)
- Full type hints for all public APIs
- Comprehensive docstrings (Google style)
- Production-grade error handling
- Human-readable JSON state format

### Testing
- Proof Pack: 3/3 destruction tests passing
  - SAFE_MODE persistence across restart
  - Cooldown survival after SIGKILL
  - Emergency state save on SIGINT
- Example scripts for all scenarios
- Automated proof runner

### Documentation
- README.md with quick start guide
- API.md with detailed interface documentation
- PROOF.md with destruction test evidence
- Examples directory with 3 runnable scripts

### Known Limitations
- File I/O overhead (~1-5ms per save operation)
- Not suitable for <1ms latency requirements
- Requires filesystem access for persistence

### Security
- No sensitive data handling (state file is plain JSON)
- No network operations
- No external process execution
- Safe for untrusted environments

## [0.0.1] - 2026-02-13 (Internal)

### Added
- Initial implementation extracted from polymarket-arbitrage-bot
- Proven in production (1000+ req/s, 100% uptime)
- SAFE_MODE persistence bug fix (veronica_integration.py:66)

---

## Upgrade Guide

### From 0.0.1 (Internal) to 0.1.0

**API Changes:**
- `VeronicaPersistence` → `JSONBackend` (old API deprecated but still works)
- `record_fail()` now returns `bool` (was `None`)

**Migration Example:**
```python
# Old (0.0.1)
from veronica_core import VeronicaPersistence
persistence = VeronicaPersistence()

# New (0.1.0)
from veronica_core import JSONBackend
backend = JSONBackend("veronica_state.json")
```

**State File Format:**
No changes - state files from 0.0.1 are compatible with 0.1.0

---

## Release Notes

### v0.1.0 - Production Ready

First public release of VERONICA Core, extracted and cleaned up from polymarket-arbitrage-bot.

**Highlights:**
- Battle-tested in production (1000+ req/s, 0% downtime)
- Complete Proof Pack with 3 destruction test scenarios
- Zero dependencies, full type hints, comprehensive docs
- Pluggable architecture (backends, guards)

**Breaking Changes:**
None (first public release)

**Deprecations:**
- `VeronicaPersistence` class (use `JSONBackend` instead)

**Security Updates:**
None

**Bug Fixes:**
- SAFE_MODE persistence across restart (inherited fix from 0.0.1)

**Performance:**
- Atomic file writes: ~1-5ms overhead per save
- Auto-save interval configurable (default: 100 operations)
- No performance regressions from 0.0.1

---

## Future Roadmap

### v0.3.0 (Planned)
- [ ] LiteLLM integration (RuntimePolicy as callback)
- [ ] Redis backend for distributed systems
- [ ] Metrics/observability hooks

### v1.0.0 (Planned)
- [ ] Stable API freeze
- [ ] Production hardening (1M+ req/s tested)
- [ ] Multi-language bindings (Rust, Go)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License - See [LICENSE](LICENSE) file for details
