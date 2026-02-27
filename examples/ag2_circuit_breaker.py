"""AG2 + veronica-core: Circuit breaker and SAFE_MODE integration examples.

Three self-contained demos showing how CircuitBreakerCapability integrates
with AG2's ConversableAgent pattern:

  demo_basic()      -- Circuit opens after N consecutive None replies
  demo_safe_mode()  -- System-wide halt blocks all agents simultaneously
  demo_isolation()  -- One broken agent does not affect healthy ones

Run all demos:
    python examples/ag2_circuit_breaker.py

Requirements:
    pip install autogen veronica-core
"""

from __future__ import annotations

from autogen import ConversableAgent

from veronica_core import (
    CircuitBreakerCapability,
    CircuitState,
    MemoryBackend,
    VeronicaIntegration,
    VeronicaState,
)


# ---------------------------------------------------------------------------
# Demo 1: Basic circuit breaker
# ---------------------------------------------------------------------------

def demo_basic() -> None:
    """Circuit opens after failure_threshold consecutive None replies."""
    print("--- Demo 1: Basic circuit breaker ---")

    # An agent that always returns None (simulates a degraded LLM endpoint)
    planner = ConversableAgent("planner", llm_config=False)
    planner.register_reply(
        trigger=lambda _: True,
        reply_func=lambda agent, messages, sender, config: (True, None),
        position=0,
        remove_other_reply_funcs=True,
    )

    cap = CircuitBreakerCapability(failure_threshold=3)
    cap.add_to_agent(planner)

    breaker = cap.get_breaker("planner")
    assert breaker.state == CircuitState.CLOSED, "circuit should start CLOSED"
    print(f"  initial state: {breaker.state.value}")

    msg = [{"role": "user", "content": "test"}]

    # Three None replies trip the circuit
    for _ in range(3):
        planner.generate_reply(msg)

    assert breaker.state == CircuitState.OPEN, "circuit should be OPEN after 3 failures"
    assert breaker.failure_count == 3
    print(f"  after 3 failures: {breaker.state.value}, failure_count={breaker.failure_count}")

    # Further calls are short-circuited without invoking the agent
    reply = planner.generate_reply(msg)
    assert reply is None, "OPEN circuit returns None without calling agent"
    print(f"  reply when OPEN: {reply!r}")

    print("[PASS] circuit opened after 3 failures\n")


# ---------------------------------------------------------------------------
# Demo 2: SAFE_MODE — system-wide halt
# ---------------------------------------------------------------------------

def demo_safe_mode() -> None:
    """SAFE_MODE blocks all registered agents simultaneously."""
    print("--- Demo 2: SAFE_MODE (system-wide halt) ---")

    def _always_ok(
        agent: ConversableAgent,
        messages: list,
        sender: object,
        config: object,
    ) -> tuple[bool, str]:
        return True, f"{agent.name}: ok"

    # MemoryBackend avoids file I/O during the example
    veronica = VeronicaIntegration(backend=MemoryBackend())
    cap = CircuitBreakerCapability(failure_threshold=5, veronica=veronica)

    planner = ConversableAgent("planner", llm_config=False)
    executor = ConversableAgent("executor", llm_config=False)
    for agent in (planner, executor):
        agent.register_reply(
            trigger=lambda _: True,
            reply_func=_always_ok,
            position=0,
            remove_other_reply_funcs=True,
        )
        cap.add_to_agent(agent)

    msg = [{"role": "user", "content": "test"}]

    # Both agents reply normally before SAFE_MODE
    assert planner.generate_reply(msg) == "planner: ok"
    assert executor.generate_reply(msg) == "executor: ok"
    print("  before SAFE_MODE: both agents healthy")

    # Trigger system-wide emergency halt
    # VeronicaIntegration starts in SCREENING; SCREENING -> SAFE_MODE is valid
    veronica.state.transition(VeronicaState.SAFE_MODE, reason="anomaly detected")
    assert planner.generate_reply(msg) is None
    assert executor.generate_reply(msg) is None
    print("  during SAFE_MODE: both agents blocked")

    # Two-step recovery: SAFE_MODE -> IDLE -> SCREENING
    veronica.state.transition(VeronicaState.IDLE, reason="anomaly resolved")
    veronica.state.transition(VeronicaState.SCREENING, reason="resuming")
    assert planner.generate_reply(msg) == "planner: ok"
    assert executor.generate_reply(msg) == "executor: ok"
    print("  after recovery (IDLE -> SCREENING): both agents restored")

    print("[PASS] SAFE_MODE blocked all agents, recovery worked\n")


# ---------------------------------------------------------------------------
# Demo 3: Per-agent isolation
# ---------------------------------------------------------------------------

def demo_isolation() -> None:
    """A broken agent's OPEN circuit does not affect healthy agents."""
    print("--- Demo 3: Per-agent isolation ---")

    cap = CircuitBreakerCapability(failure_threshold=2)

    healthy = ConversableAgent("healthy", llm_config=False)
    healthy.register_reply(
        trigger=lambda _: True,
        reply_func=lambda agent, messages, sender, config: (True, "healthy: ok"),
        position=0,
        remove_other_reply_funcs=True,
    )

    broken = ConversableAgent("broken", llm_config=False)
    broken.register_reply(
        trigger=lambda _: True,
        reply_func=lambda agent, messages, sender, config: (True, None),
        position=0,
        remove_other_reply_funcs=True,
    )

    cap.add_to_agent(healthy)
    cap.add_to_agent(broken)

    msg = [{"role": "user", "content": "test"}]

    # Trip the broken agent's circuit (2 failures)
    broken.generate_reply(msg)
    broken.generate_reply(msg)
    assert cap.get_breaker("broken").state == CircuitState.OPEN
    print(f"  broken agent: {cap.get_breaker('broken').state.value}")

    # Healthy agent is completely unaffected
    assert healthy.generate_reply(msg) == "healthy: ok"
    assert cap.get_breaker("healthy").state == CircuitState.CLOSED
    print(f"  healthy agent: {cap.get_breaker('healthy').state.value}, reply='healthy: ok'")

    print("[PASS] broken agent did not affect healthy agent\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demo_basic()
    demo_safe_mode()
    demo_isolation()
    print("All demos passed.")
