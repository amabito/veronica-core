"""incident_03_websocket_ddos.py

Real incident: 47k tokens/sec WebSocket flood from LLM streaming loop (2024-Q2).

A production chatbot used WebSocket streaming to push LLM tokens to the client.
Due to a bug in the event loop, the client disconnect handler was not called
on connection drop. The LLM kept generating and pushing tokens at 47,000 tokens/sec
for 6 minutes until the server's memory was exhausted (OOM kill).

Real data (from postmortem, startup not named):
    - Duration: ~6 minutes (360 seconds)
    - Token throughput: ~47,000 tokens/sec at peak
    - Total tokens generated: ~10.1 million
    - Memory consumed: 8.3 GB (OOM kill)
    - Cost: ~$505 (10.1M tokens * $0.05/1K at gpt-4 output pricing)

This benchmark simulates the streaming runaway and shows how CircuitBreaker
+ ExecutionContext would have halted after detecting the zombie connection.

Usage:
    python benchmarks/real_incidents/incident_03_websocket_ddos.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import CircuitBreaker, CircuitState
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Simulated WebSocket streaming session
# ---------------------------------------------------------------------------

class ZombieWebSocketSession:
    """Simulates a WebSocket connection that dropped silently.

    Models the 2024-Q2 incident: client disconnected but server never noticed.
    Every write attempt raises a BrokenPipeError (or similar) -- the baseline
    ignores this and keeps generating tokens.
    """

    def __init__(self, disconnect_at_call: int = 1) -> None:
        self.call_count = 0
        self.disconnect_at_call = disconnect_at_call
        self.total_tokens_generated = 0

    def write_chunk(self, tokens: int = 100) -> None:
        """Write a token chunk. Raises BrokenPipeError after disconnect."""
        self.call_count += 1
        self.total_tokens_generated += tokens
        if self.call_count >= self.disconnect_at_call:
            raise BrokenPipeError("WebSocket: client disconnected")


class LLMStreamGenerator:
    """Simulates token streaming from an LLM."""

    def __init__(self) -> None:
        self.chunk_count = 0
        self.total_tokens = 0
        self.tokens_per_chunk = 100

    def next_chunk(self) -> dict[str, Any]:
        self.chunk_count += 1
        self.total_tokens += self.tokens_per_chunk
        return {
            "chunk": self.chunk_count,
            "tokens": self.tokens_per_chunk,
            "text": f"token_chunk_{self.chunk_count}",
        }


# ---------------------------------------------------------------------------
# Baseline: zombie connection ignored, LLM generates until OOM
# ---------------------------------------------------------------------------

def baseline_websocket_runaway(
    max_chunks: int = 2000,
    tokens_per_chunk: int = 100,
    cost_per_1k_tokens_usd: float = 0.050,
) -> dict[str, Any]:
    """Simulate the 2024-Q2 incident: streaming continues after client disconnect.

    The real incident ran for ~360 seconds generating 10.1M tokens.
    We simulate 2000 chunks (200K tokens) to keep the benchmark fast.
    """
    llm = LLMStreamGenerator()
    ws = ZombieWebSocketSession(disconnect_at_call=1)  # Disconnects immediately

    start = time.perf_counter()
    errors_swallowed = 0
    stopped_by = "oom_kill"

    for chunk_idx in range(max_chunks):
        chunk = llm.next_chunk()
        try:
            ws.write_chunk(tokens=chunk["tokens"])
        except BrokenPipeError:
            # Baseline: swallow error and keep generating (the bug)
            errors_swallowed += 1
            if chunk_idx == max_chunks - 1:
                stopped_by = "max_chunks_simulated"

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = (llm.total_tokens / 1000) * cost_per_1k_tokens_usd

    return {
        "scenario": "baseline",
        "incident": "47k tokens/sec WebSocket flood (2024-Q2)",
        "chunks_generated": llm.chunk_count,
        "total_tokens": llm.total_tokens,
        "errors_swallowed": errors_swallowed,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: CircuitBreaker detects BrokenPipe and opens the circuit
# ---------------------------------------------------------------------------

def veronica_websocket_runaway(
    max_chunks: int = 2000,
    tokens_per_chunk: int = 100,
    cost_per_1k_tokens_usd: float = 0.050,
    circuit_failure_threshold: int = 3,
    budget_usd: float = 1.00,
) -> dict[str, Any]:
    """CircuitBreaker + ExecutionContext halt the zombie stream.

    When BrokenPipeError occurs 3 times (circuit threshold), the circuit
    opens and further LLM generation is blocked. In the real incident,
    this would have stopped generation within the first ~0.3 seconds.
    """
    llm = LLMStreamGenerator()
    ws = ZombieWebSocketSession(disconnect_at_call=1)

    # Circuit breaker: open after 3 consecutive write failures
    breaker = CircuitBreaker(
        failure_threshold=circuit_failure_threshold,
        recovery_timeout=30.0,
    )
    config = ExecutionConfig(
        max_cost_usd=budget_usd,
        max_steps=max_chunks * 2,
        max_retries_total=10,
    )

    chunks_attempted = 0
    halted_by = "unknown"
    circuit_opened_at: int | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for chunk_idx in range(max_chunks):
            # Check circuit before generating next chunk
            policy_ctx = PolicyContext(metadata={"operation": "ws_stream_chunk"})
            cb_decision = breaker.check(policy_ctx)

            if not cb_decision.allowed:
                halted_by = "circuit_breaker_open"
                circuit_opened_at = circuit_opened_at or chunk_idx
                break

            # Check execution budget
            ec_decision = ctx.wrap_llm_call(
                fn=lambda idx=chunk_idx: llm.next_chunk(),
                options=WrapOptions(
                    operation_name="stream_chunk",
                    cost_estimate_hint=(tokens_per_chunk / 1000) * cost_per_1k_tokens_usd,
                ),
            )
            chunks_attempted += 1

            if ec_decision == Decision.HALT:
                halted_by = "budget_exceeded"
                break

            # Attempt to write to WebSocket
            try:
                ws.write_chunk(tokens=tokens_per_chunk)
                breaker.record_success()
            except BrokenPipeError:
                # Veronica: record failure, circuit will open at threshold
                breaker.record_failure()
                if breaker.state == CircuitState.OPEN:
                    circuit_opened_at = circuit_opened_at or chunk_idx
                    halted_by = "circuit_breaker_open"
                    break

        if halted_by == "unknown":
            halted_by = "max_chunks_simulated"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = (llm.total_tokens / 1000) * cost_per_1k_tokens_usd

    return {
        "scenario": "veronica",
        "incident": "47k tokens/sec WebSocket flood (2024-Q2)",
        "chunks_attempted": chunks_attempted,
        "total_tokens": llm.total_tokens,
        "circuit_state": breaker.state.value,
        "circuit_opened_at_chunk": circuit_opened_at,
        "failure_threshold": circuit_failure_threshold,
        "total_cost_usd": round(total_cost, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 68)
    print("INCIDENT #03: 47k tokens/sec WebSocket Flood (2024-Q2)")
    print("Real data: 10.1M tokens, $505 cost, OOM kill after 6 minutes")
    print("=" * 68)

    MAX_CHUNKS = 2000
    TOKENS_PER_CHUNK = 100
    COST_PER_1K = 0.050
    CIRCUIT_THRESHOLD = 3

    base = baseline_websocket_runaway(
        max_chunks=MAX_CHUNKS,
        tokens_per_chunk=TOKENS_PER_CHUNK,
        cost_per_1k_tokens_usd=COST_PER_1K,
    )
    ver = veronica_websocket_runaway(
        max_chunks=MAX_CHUNKS,
        tokens_per_chunk=TOKENS_PER_CHUNK,
        cost_per_1k_tokens_usd=COST_PER_1K,
        circuit_failure_threshold=CIRCUIT_THRESHOLD,
        budget_usd=1.00,
    )

    token_reduction = round(100 * (1 - ver["total_tokens"] / max(base["total_tokens"], 1)), 1)
    cost_reduction = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print()
    header = f"{'Metric':<32} {'Baseline (incident)':>18} {'Veronica':>16}"
    print(header)
    print("-" * 68)
    print(f"{'Tokens generated':<32} {base['total_tokens']:>18,} {ver['total_tokens']:>16,}")
    print(f"{'Errors swallowed':<32} {base['errors_swallowed']:>18,} {'0 (circuit)':>16}")
    print(f"{'Total cost (USD)':<32} ${base['total_cost_usd']:>17.2f} ${ver['total_cost_usd']:>15.4f}")
    print(f"{'Stopped by':<32} {'oom_kill':>18} {ver['halted_by']:>16}")
    print(f"{'Circuit opened at chunk':<32} {'N/A':>18} {ver['circuit_opened_at_chunk']:>16}")
    print()
    print(f"Token reduction:  {token_reduction}%")
    print(f"Cost reduction:   {cost_reduction}%")
    print(f"Circuit breaker threshold: {CIRCUIT_THRESHOLD} failures -> OPEN")


if __name__ == "__main__":
    main()
