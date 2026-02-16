# VERONICA Core API Reference

Version: 0.1.0

## Overview

VERONICA Core provides a failsafe state machine for mission-critical applications with pluggable persistence and validation.

## Core Modules

### veronica_core.state

#### VeronicaState (Enum)

Operational states for the state machine.

```python
class VeronicaState(Enum):
    IDLE = "IDLE"           # No active operations
    SCREENING = "SCREENING" # Active processing
    COOLDOWN = "COOLDOWN"   # Entity-specific cooldown active
    SAFE_MODE = "SAFE_MODE" # Emergency safe mode (all ops halted)
    ERROR = "ERROR"         # System error state
```

#### VeronicaStateMachine

Core state machine with per-entity fail tracking and cooldown management.

**Constructor:**
```python
VeronicaStateMachine(
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600
)
```

**Parameters:**
- `cooldown_fails`: Number of consecutive fails to trigger cooldown (default: 3)
- `cooldown_seconds`: Cooldown duration in seconds (default: 600)

**Methods:**

##### `is_in_cooldown(pair: str) -> bool`
Check if entity is currently in cooldown.

**Example:**
```python
sm = VeronicaStateMachine()
sm.record_fail("task_1")
sm.record_fail("task_1")
sm.record_fail("task_1")  # Activates cooldown

if sm.is_in_cooldown("task_1"):
    print("Task in cooldown")
```

##### `record_fail(pair: str) -> bool`
Record failure for entity. Returns True if cooldown was activated.

##### `record_pass(pair: str) -> None`
Record success for entity. Resets fail counter.

##### `cleanup_expired() -> List[str]`
Remove expired cooldowns. Returns list of cleaned entity IDs.

##### `transition(to_state: VeronicaState, reason: str) -> None`
Transition to new state with reason.

##### `get_stats() -> Dict`
Get current state statistics.

**Returns:**
```python
{
    "current_state": str,
    "active_cooldowns": Dict[str, float],  # {entity: remaining_seconds}
    "fail_counts": Dict[str, int],
    "total_transitions": int,
    "last_transition": Optional[Dict],
}
```

##### `to_dict() -> Dict`
Serialize state for persistence.

##### `from_dict(data: Dict) -> VeronicaStateMachine` (classmethod)
Deserialize state from dict.

---

### veronica_core.backends

#### PersistenceBackend (ABC)

Abstract interface for pluggable persistence.

**Methods:**

##### `save(data: Dict) -> bool`
Save state data. Returns True on success.

##### `load() -> Optional[Dict]`
Load state data. Returns None if no state exists.

##### `backup() -> bool`
Create backup (optional). Returns True on success.

#### JSONBackend

File-based persistence with atomic writes.

**Constructor:**
```python
JSONBackend(path: str | Path)
```

**Example:**
```python
from veronica_core import JSONBackend

backend = JSONBackend("data/state.json")
data = {"test": "value"}
backend.save(data)
loaded = backend.load()
backend.backup()  # Creates timestamped backup
```

#### MemoryBackend

In-memory persistence for testing (does NOT persist across restarts).

**Example:**
```python
from veronica_core import MemoryBackend

backend = MemoryBackend()
backend.save({"test": "data"})
loaded = backend.load()
```

---

### veronica_core.guards

#### VeronicaGuard (ABC)

Abstract interface for domain-specific validation.

**Methods:**

##### `should_cooldown(entity: str, context: Dict[str, Any]) -> bool`
Determine if cooldown should activate immediately (overrides fail count).

##### `validate_state(state_data: Dict[str, Any]) -> bool`
Validate state before persistence.

##### `on_cooldown_activated(entity: str, context: Dict[str, Any]) -> None`
Hook called when cooldown activates (optional).

##### `on_cooldown_expired(entity: str) -> None`
Hook called when cooldown expires (optional).

**Example Implementation:**
```python
from veronica_core import VeronicaGuard

class ApiGuard(VeronicaGuard):
    def should_cooldown(self, endpoint: str, context: dict) -> bool:
        # Activate cooldown if rate limit low
        remaining = context.get("x-rate-limit-remaining", 100)
        return remaining < 10

    def validate_state(self, state_data: dict) -> bool:
        # Only accept valid endpoints
        valid_endpoints = {"/api/v1/users", "/api/v1/posts"}
        fail_counts = state_data.get("fail_counts", {})
        return all(e in valid_endpoints for e in fail_counts.keys())
```

#### PermissiveGuard

Default guard that allows all operations.

---

### veronica_core.clients

#### LLMClient (Protocol)

Protocol for pluggable LLM client integration (optional).

**VERONICA Core does NOT require LLM** - this is purely optional for AI-enhanced decision logic.

**Methods:**

##### `generate(prompt: str, *, context: Optional[Dict] = None, **kwargs) -> str`
Generate text response from LLM.

**Example Implementation:**
```python
from veronica_core import LLMClient

class OllamaClient:
    def __init__(self, model: str = "llama3.2:3b"):
        self.model = model
        self.base_url = "http://localhost:11434"

    def generate(self, prompt: str, *, context=None, **kwargs) -> str:
        import requests
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False}
        )
        return response.json()["response"]
```

#### NullClient

Default LLM client that raises error when invoked.

Use this as default to ensure VERONICA Core works without LLM. Raises `RuntimeError` if LLM functionality is used without client injection.

```python
from veronica_core import VeronicaIntegration

# Default: NullClient (no LLM features)
veronica = VeronicaIntegration()
# veronica.client.generate("test") -> RuntimeError
```

#### DummyClient

Dummy LLM client for testing (returns fixed responses).

```python
from veronica_core import DummyClient

client = DummyClient(fixed_response="SAFE")
response = client.generate("Is this safe?")
# Returns: "SAFE"

print(client.call_count)  # 1
print(client.last_prompt)  # "Is this safe?"
```

**Use cases:**
- Unit tests that need LLM without external dependencies
- Mocking AI responses for reproducible tests
- Development without real LLM API

---

### veronica_core.integration

#### VeronicaIntegration

Main entry point for VERONICA functionality.

**Constructor:**
```python
VeronicaIntegration(
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600,
    auto_save_interval: int = 100,
    backend: Optional[PersistenceBackend] = None,
    guard: Optional[VeronicaGuard] = None,
    client: Optional[LLMClient] = None,
)
```

**Parameters:**
- `cooldown_fails`: Fail threshold for cooldown
- `cooldown_seconds`: Cooldown duration (seconds)
- `auto_save_interval`: Auto-save every N operations (0 = disabled)
- `backend`: Persistence backend (default: VeronicaPersistence)
- `guard`: Validation guard (default: PermissiveGuard)
- `client`: LLM client (optional, default: NullClient - no LLM features)

**Methods:**

##### `is_in_cooldown(pair: str) -> bool`
Check if entity is in cooldown.

##### `record_fail(pair: str, context: Optional[Dict] = None) -> bool`
Record failure with optional guard context. Returns True if cooldown activated.

**Example:**
```python
context = {"error_rate": 0.7, "latency_ms": 500}
cooldown = veronica.record_fail("api_endpoint", context=context)
```

##### `record_pass(pair: str) -> None`
Record success. Resets fail counter.

##### `cleanup_expired() -> None`
Remove expired cooldowns.

##### `get_stats() -> Dict`
Get current statistics.

##### `get_fail_count(pair: str) -> int`
Get current fail count for entity.

##### `get_cooldown_remaining(pair: str) -> Optional[float]`
Get remaining cooldown seconds. Returns None if not in cooldown.

##### `save() -> bool`
Manually save state. Returns True on success.

---

### veronica_core.exit

#### ExitTier (Enum)

Exit priority levels.

```python
class ExitTier(IntEnum):
    GRACEFUL = 1   # Save state, cleanup, log
    EMERGENCY = 2  # Save state, minimal cleanup
    FORCE = 3      # Immediate exit (no save)
```

#### VeronicaExit

Exit handler with signal handling and 3-tier shutdown.

**Constructor:**
```python
VeronicaExit(
    state_machine: VeronicaStateMachine,
    persistence: Optional[VeronicaPersistence] = None
)
```

**Automatically registers:**
- SIGTERM handler (GRACEFUL exit)
- SIGINT handler (EMERGENCY exit)
- atexit fallback (EMERGENCY exit)

**Methods:**

##### `request_exit(tier: ExitTier, reason: str) -> None`
Request exit at specified tier.

**Example:**
```python
exit_handler.request_exit(ExitTier.GRACEFUL, "User shutdown request")
```

##### `is_exit_requested() -> bool`
Check if exit has been requested.

---

## Usage Examples

### Basic Usage

```python
from veronica_core import VeronicaIntegration

# Initialize
veronica = VeronicaIntegration()

# Check cooldown
if not veronica.is_in_cooldown("task_1"):
    try:
        execute_task()
        veronica.record_pass("task_1")
    except Exception as e:
        cooldown = veronica.record_fail("task_1")
        if cooldown:
            print("Cooldown activated")
```

### Custom Backend

```python
from veronica_core import VeronicaIntegration, JSONBackend

backend = JSONBackend("my_state.json")
veronica = VeronicaIntegration(backend=backend)
```

### Custom Guard

```python
from veronica_core import VeronicaIntegration, VeronicaGuard

class CustomGuard(VeronicaGuard):
    def should_cooldown(self, entity: str, context: dict) -> bool:
        return context.get("severity") == "critical"

    def validate_state(self, state_data: dict) -> bool:
        return True  # Accept all states

guard = CustomGuard()
veronica = VeronicaIntegration(guard=guard)
```

### LLM Client Injection (Optional)

```python
from veronica_core import VeronicaIntegration, DummyClient

# Example 1: Testing with DummyClient
client = DummyClient(fixed_response="SAFE")
veronica = VeronicaIntegration(client=client)

# Use LLM for decision
response = veronica.client.generate("Is this operation safe?")
if response == "SAFE":
    execute_operation()

# Example 2: Custom LLM client (Ollama)
class OllamaClient:
    def generate(self, prompt: str, *, context=None, **kwargs) -> str:
        # Implement Ollama HTTP client
        return ollama_api_call(prompt)

veronica = VeronicaIntegration(client=OllamaClient())
```

**Note:** VERONICA Core has zero LLM dependencies. All LLM integration is optional and user-provided.

### Persistence Roundtrip

```python
from veronica_core import VeronicaIntegration, JSONBackend

# Session 1
backend = JSONBackend("state.json")
v1 = VeronicaIntegration(backend=backend)
v1.record_fail("task_1")
v1.save()

# Session 2 (after restart)
backend2 = JSONBackend("state.json")
v2 = VeronicaIntegration(backend=backend2)
print(v2.get_fail_count("task_1"))  # Output: 1
```

---

## Error Handling

All methods handle errors gracefully:

- `save()`: Returns False on error, logs exception
- `load()`: Returns None on error, creates fresh state
- `record_fail()`: Never raises, always returns bool
- `get_stats()`: Always returns valid dict

---

## Thread Safety

VERONICA Core is NOT thread-safe by default. For multi-threaded applications:

1. Use separate VeronicaIntegration instances per thread
2. OR implement external locking (threading.Lock)
3. OR use backend with atomic operations (e.g., Redis with Lua scripts)

---

## Performance

- State machine operations: O(1)
- Serialization: O(n) where n = number of entities
- JSONBackend save: ~1ms for 1000 entities
- MemoryBackend: ~0.01ms
- Auto-save overhead: Negligible (< 0.1% with interval=100)

---

## License

MIT License - See LICENSE file
