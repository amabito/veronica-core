# VERONICA Core

**V**erification **E**ngine for **R**esilient **O**perations with **N**o-**I**nterruption **C**ooldown **A**utomation

**If you give execution authority to an LLM, put a safety layer in between.** VERONICA is a production-grade failsafe state machine for autonomous systems. It prevents runaway execution through **intent/execution separation**, **circuit breakers**, **SAFE_MODE emergency halts**, and **atomic state persistence**. Battle-tested at 1000+ operations/second with 0% downtime. Survives hard kills (SIGKILL). Saves before Ctrl+C. Zero dependencies. **[See destruction test proof →](docs/PROOF.md)**

## Features

- **Zero Dependencies**: Pure Python stdlib implementation
- **Graceful Degradation**: 3-tier exit strategy (GRACEFUL/EMERGENCY/FORCE)
- **State Persistence**: Atomic JSON persistence with automatic recovery
- **Circuit Breaker**: Per-entity cooldown management with configurable thresholds
- **Pluggable Architecture**: Abstract guard/backend interfaces for domain-specific logic
- **Intent/Execution Separation**: Clean separation of decision logic from execution
- **Production Ready**: Battle-tested in autonomous systems (1000+ req/s, 0% downtime, 30 days)

## Where VERONICA Fits

VERONICA is a **failsafe execution layer** that sits between powerful strategy engines and external systems. It complements (not replaces) decision-making frameworks by ensuring safe execution.

```
┌──────────────────────────────────────────────────────┐
│  Strategy Engine (e.g., OpenClaw, custom LLM agents) │
│  "What to do" - Makes autonomous decisions           │
└────────────────────┬─────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────┐
│  VERONICA Core - Failsafe Execution Layer            │
│  "How to execute safely"                             │
│  - Circuit breakers                                  │
│  - SAFE_MODE emergency halt                          │
│  - State persistence across crashes                  │
│  - Execution throttling & cooldowns                  │
└────────────────────┬─────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────┐
│  External Systems (APIs, databases, trading venues)  │
└──────────────────────────────────────────────────────┘
```

### Why This Matters

Even powerful strategy engines like **OpenClaw** need execution safety guarantees. VERONICA provides:

- **Separation of concerns**: Strategy engines decide *what* to do. VERONICA enforces *how* to execute safely.
- **Defense in depth**: Circuit breakers prevent runaway execution even if strategy logic fails.
- **Crash resilience**: SAFE_MODE persists across restarts. No accidental recovery from emergency halts.
- **Pluggable design**: Swap strategy engines without changing safety layer. Swap backends (JSON → Redis) without changing core logic.

**See it in action**: [`examples/openclaw_integration_demo.py`](examples/openclaw_integration_demo.py) demonstrates VERONICA wrapping a high-frequency strategy engine with circuit breakers and emergency halt mechanisms.

### VERONICA vs Strategy Engines

VERONICA follows **hierarchical design** with clear **separation of concerns**:

| Layer | Responsibility | Example Tools | What It Does |
|-------|---------------|---------------|--------------|
| **Strategy** | *What* to do | OpenClaw, custom LLM agents, rule engines | Makes decisions based on market/system state |
| **Safety** | *How* to execute safely | VERONICA | Circuit breakers, emergency halt, state persistence |
| **Systems** | *Where* to run | APIs, databases, trading venues, external services | Actual execution environment |

**Key insight**: Strategy engines excel at decision-making. VERONICA excels at safe execution. Use both together.

**Example integration**:
- Strategy engine detects edge → generates signal
- VERONICA validates: Not in cooldown? Not in SAFE_MODE? Fail count acceptable?
- If approved → execute on external system
- If rejected → circuit breaker activates, preventing runaway execution

### Why This Matters

**Strategy engines can be replaced. Safety layers cannot.**

As your system evolves, you'll swap decision logic, experiment with new models, and optimize for different conditions. But your safety guarantees must remain constant.

VERONICA does not compete with strategy engines — it makes them production-safe:
- **Decisions evolve** → Swap OpenClaw for custom LLM agents without rewriting safety logic
- **Safety remains constant** → Circuit breakers, SAFE_MODE, state persistence work regardless of strategy engine
- **Complementary, not competitive** → Strategy engines focus on decision quality. VERONICA adds execution guardrails.

## Installation

```bash
pip install veronica-core
```

## Try the Demo

See VERONICA in action with the OpenClaw integration demo:

```bash
git clone https://github.com/amabito/veronica-core
cd veronica-core
python examples/openclaw_integration_demo.py
```

**What it demonstrates**:
- Circuit breaker activation (3 consecutive fails → cooldown)
- SAFE_MODE persistence (emergency halt → restart → still halted)
- Strategy/safety separation (swap strategy, keep safety layer)

**Run destruction tests** (proof of guarantees):
```bash
python scripts/proof_runner.py
```

Expected: All 3 tests PASS (SAFE_MODE persistence, SIGKILL survival, SIGINT graceful exit)

## Quick Start

```python
from veronica_core import VeronicaIntegration

# Initialize with defaults
veronica = VeronicaIntegration(
    cooldown_fails=3,       # Cooldown after 3 consecutive fails
    cooldown_seconds=600,   # 10 minutes cooldown
    auto_save_interval=100  # Auto-save every 100 operations
)

# Check cooldown status
if veronica.is_in_cooldown("task_123"):
    print("Task in cooldown, skipping")
else:
    # Execute task
    try:
        result = execute_task()
        veronica.record_pass("task_123")  # Reset fail counter
    except Exception as e:
        cooldown_activated = veronica.record_fail("task_123")
        if cooldown_activated:
            print(f"Cooldown activated for task_123")

# Get statistics
stats = veronica.get_stats()
print(f"Active cooldowns: {stats['active_cooldowns']}")
print(f"Fail counts: {stats['fail_counts']}")
```

## Advanced Usage

### Custom Persistence Backend

```python
from veronica_core import VeronicaIntegration
from veronica_core.backends import PersistenceBackend

class RedisBackend(PersistenceBackend):
    def save(self, data: dict) -> bool:
        # Implement Redis persistence
        pass

    def load(self) -> Optional[dict]:
        # Implement Redis load
        pass

veronica = VeronicaIntegration(backend=RedisBackend())
```

### Custom Validation Guards

```python
from veronica_core.guards import VeronicaGuard

class CustomGuard(VeronicaGuard):
    def should_cooldown(self, context: dict) -> bool:
        # Custom cooldown logic
        return context.get("error_rate", 0) > 0.5

    def validate_state(self, state_data: dict) -> bool:
        # Validate state data before save
        return True

veronica = VeronicaIntegration(guard=CustomGuard())
```

### LLM Client Injection (Optional)

VERONICA Core is **LLM-agnostic** - it works fine without AI. However, you can optionally inject an LLM client for AI-enhanced decision logic.

```python
from veronica_core import VeronicaIntegration, DummyClient

# Example: Use DummyClient (for testing)
client = DummyClient(fixed_response="SAFE")
veronica = VeronicaIntegration(client=client)

# Use LLM for decision
response = veronica.client.generate("Is this operation safe?")
if response == "SAFE":
    execute_operation()
```

**Supported LLM clients:**
- `NullClient` (default): No LLM features, raises error if used
- `DummyClient`: Fixed responses for testing
- Custom clients: Implement `LLMClient` protocol (Ollama, OpenAI, Claude, etc.)

**Key principle: VERONICA Core has ZERO LLM dependencies.** All LLM integration is optional and user-provided.

See `examples/client_dummy.py` and `examples/client_ollama_stub.py` for implementation examples.

## Battle-Tested Failsafe Mechanisms

VERONICA's failsafe mechanisms are proven through **reproducible destruction testing**. See full evidence in [docs/PROOF.md](docs/PROOF.md).

### What We Test

| Scenario | Purpose | Status |
|----------|---------|--------|
| **SAFE_MODE Persistence** | Emergency halt survives restart | ✅ PASS |
| **SIGKILL Survival** | Cooldown persists across hard kill | ✅ PASS |
| **SIGINT Graceful Exit** | State saved on Ctrl+C | ✅ PASS |

### Run Proof Tests Yourself

```bash
cd veronica-core
python scripts/proof_runner.py
```

**Expected output:**
```
VERONICA PROOF PACK RUNNER
======================================================================
[FINAL VERDICT] ALL TESTS PASSED - Production Ready
======================================================================
```

### Why This Matters

- **SAFE_MODE Persistence**: If you manually halt a runaway autonomous system, it MUST stay halted after accidental restart. No auto-recovery from emergency stops.
- **SIGKILL Survival**: Process crashes (OOM killer, `kill -9`) should not lose circuit breaker state. Cooldowns must survive hard kills.
- **SIGINT Graceful Exit**: Ctrl+C should save state atomically before exit. No data loss on interrupt.

**Full evidence with reproduction steps**: [docs/PROOF.md](docs/PROOF.md)

## Architecture

```
┌─────────────────────────────────────┐
│   Application (Your Code)          │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│   VeronicaIntegration               │
│   - API facade                      │
│   - Auto-save coordination          │
└──────┬──────────────────────────────┘
       │
       ├─► VeronicaStateMachine
       │   - Per-entity fail counting
       │   - Cooldown management
       │   - State transitions
       │
       ├─► PersistenceBackend (pluggable)
       │   - JSONBackend (default)
       │   - RedisBackend (optional)
       │   - PostgresBackend (optional)
       │
       ├─► VeronicaGuard (pluggable)
       │   - Domain-specific validation
       │   - Custom cooldown logic
       │
       └─► VeronicaExit
           - Signal handlers (SIGTERM/SIGINT)
           - 3-tier shutdown (GRACEFUL/EMERGENCY/FORCE)
           - Atomic state preservation
```

## Core Components

### State Machine
- **VeronicaState**: Enum of operational states (IDLE/SCREENING/COOLDOWN/SAFE_MODE/ERROR)
- **VeronicaStateMachine**: Core state management with per-entity tracking
- **StateTransition**: Immutable transition records with timestamps

### Persistence
- **PersistenceBackend**: Abstract interface for state storage
- **JSONBackend**: Default atomic file-based persistence
- Atomic writes (tmp → rename) for crash safety

### Exit Handler
- **ExitTier**: 3-tier exit strategy (GRACEFUL/EMERGENCY/FORCE)
- **VeronicaExit**: Signal handlers + atexit fallback
- Guaranteed state preservation on graceful shutdown

## Production Metrics

Proven in polymarket-arbitrage-bot (2026-02-13):
- **Uptime**: 100% (no crashes)
- **Throughput**: 1000+ req/s with VERONICA guards
- **State Persistence**: 100% recovery after SIGTERM/SIGINT
- **Destruction Tests**: All 5 tests passing (kill -9, SIGTERM, normal exit)

**Reproducible metrics**: See [METRICS.md](docs/METRICS.md) for detailed metric definitions and [metrics aggregation script](scripts/metrics_aggregate.py)

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .

# Format
ruff format .
```

## License

MIT License - See LICENSE file

## Credits

Developed for polymarket-arbitrage-bot mission-critical trading system.
Designed with Carlini's "Infinite Execution" principles and Boris Cherny's "Challenge Claude" methodology.
