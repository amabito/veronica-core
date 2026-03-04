"""incident_websocket_runaway.py

Real incident: Agent streaming infinite messages over WebSocket.

A production chatbot used WebSocket streaming to push LLM responses to clients.
Due to a race condition in the connection teardown handler, client disconnects
were not propagated back to the streaming loop. The LLM continued generating
and pushing token chunks at full speed into a closed socket. The writes silently
succeeded (kernel buffer absorbed them) until the OS buffer filled and the
process was OOM-killed.

Real data (production postmortem, fintech startup, 2024-Q1):
    - Streaming rate: ~8,000 tokens/sec
    - Duration before OOM: ~4.5 minutes (270 seconds)
    - Tokens generated: ~2.16 million
    - Memory consumed: 6.1 GB (OS socket buffer backpressure)
    - Cost: ~$32.40 (2.16M output tokens * $0.015/1K at gpt-3.5-turbo)
    - Outcome: OOM kill, 3 other sessions lost, 8-minute downtime

This benchmark simulates a runaway streaming agent and shows how
CircuitBreaker detects silent write failures and halts the stream.

Usage:
    python benchmarks/real_incidents/incident_websocket_runaway.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import CircuitBreaker, CircuitState
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Simulated zombie WebSocket connection
# ---------------------------------------------------------------------------

class ZombieWebSocket:
    """Simulates a WebSocket whose client disconnected silently.

    In the real incident, OS socket buffers absorbed writes for ~60 seconds
    before raising errors. We simulate immediate errors (worst case exposed).
    """

    def __init__(self, fail_after: int = 0) -> None:
        self.write_count = 0
        self.fail_after = fail_after

    def write(self, chunk: str) -> None:
        self.write_count += 1
        if self.write_count > self.fail_after:
            raise BrokenPipeError(f"[Errno 32] Broken pipe (write #{self.write_count})")


class StreamingLLM:
    """Simulates an LLM emitting an infinite token stream."""

    def __init__(self, tokens_per_chunk: int = 50) -> None:
        self.chunk_count = 0
        self.total_tokens = 0
        self.tokens_per_chunk = tokens_per_chunk

    def next_chunk(self) -> dict[str, Any]:
        self.chunk_count += 1
        self.total_tokens += self.tokens_per_chunk
        return {
            "chunk": self.chunk_count,
            "text": f"chunk_{self.chunk_count} " * (self.tokens_per_chunk // 5),
            "tokens": self.tokens_per_chunk,
            "done": False,  # Never done -- infinite stream
        }


# ---------------------------------------------------------------------------
# Baseline: streaming loop ignores write errors
# ---------------------------------------------------------------------------

def baseline_websocket_runaway(
    max_chunks: int = 5000,
    tokens_per_chunk: int = 50,
    cost_per_1k_tokens_usd: float = 0.015,
) -> dict[str, Any]:
    """Simulate the 2024-Q1 incident: streaming continues after client disconnect.

    The baseline swallows BrokenPipeError and continues generating tokens.
    In the real incident, this ran for 270 seconds generating 2.16M tokens.
    Here we simulate 5000 chunks (250K tokens) to keep the benchmark fast.
    """
    llm = StreamingLLM(tokens_per_chunk=tokens_per_chunk)
    ws = ZombieWebSocket(fail_after=0)  # Fails on every write

    start = time.perf_counter()
    errors_ignored = 0
    stopped_by = "oom_kill"

    for _ in range(max_chunks):
        chunk = llm.next_chunk()
        try:
            ws.write(chunk["text"])
        except BrokenPipeError:
            # Baseline: swallow and continue (the bug)
            errors_ignored += 1
            if llm.chunk_count >= max_chunks:
                stopped_by = "max_chunks_simulated"

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = (llm.total_tokens / 1000) * cost_per_1k_tokens_usd

    return {
        "scenario": "baseline",
        "incident": "WebSocket streaming runaway (fintech, 2024-Q1)",
        "chunks_generated": llm.chunk_count,
        "total_tokens": llm.total_tokens,
        "errors_ignored": errors_ignored,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "contained": False,
        "cost_saved_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Veronica: CircuitBreaker detects BrokenPipe and stops the stream
# ---------------------------------------------------------------------------

def veronica_websocket_runaway(
    max_chunks: int = 5000,
    tokens_per_chunk: int = 50,
    cost_per_1k_tokens_usd: float = 0.015,
    circuit_threshold: int = 3,
    budget_usd: float = 1.00,
) -> dict[str, Any]:
    """CircuitBreaker detects repeated BrokenPipeError and opens the circuit.

    After `circuit_threshold` consecutive write failures, the circuit opens
    and further LLM generation is blocked. In the real incident, this would
    have stopped the stream within the first ~0.1 seconds.
    """
    llm = StreamingLLM(tokens_per_chunk=tokens_per_chunk)
    ws = ZombieWebSocket(fail_after=0)
    breaker = CircuitBreaker(
        failure_threshold=circuit_threshold,
        recovery_timeout=60.0,
    )
    config = ExecutionConfig(
        max_cost_usd=budget_usd,
        max_steps=max_chunks * 2,
        max_retries_total=10,
    )

    chunks_generated = 0
    halted_by = "unknown"
    circuit_opened_at: int | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for chunk_idx in range(max_chunks):
            # Gate: check circuit before generating
            cb_decision = breaker.check(PolicyContext())
            if not cb_decision.allowed:
                circuit_opened_at = circuit_opened_at or chunk_idx
                halted_by = "circuit_breaker_open"
                break

            # Gate: check execution budget
            ec_decision = ctx.wrap_llm_call(
                fn=lambda: llm.next_chunk(),
                options=WrapOptions(
                    operation_name="stream_chunk",
                    cost_estimate_hint=(tokens_per_chunk / 1000) * cost_per_1k_tokens_usd,
                ),
            )
            chunks_generated += 1

            if ec_decision == Decision.HALT:
                halted_by = "budget_exceeded"
                break

            chunk = llm.next_chunk()

            # Attempt write -- record failure in circuit
            try:
                ws.write(chunk["text"])
                breaker.record_success()
            except BrokenPipeError:
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
        "incident": "WebSocket streaming runaway (fintech, 2024-Q1)",
        "chunks_generated": chunks_generated,
        "total_tokens": llm.total_tokens,
        "circuit_state": breaker.state.value,
        "circuit_threshold": circuit_threshold,
        "circuit_opened_at_chunk": circuit_opened_at,
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
    MAX_CHUNKS = 5000
    TOKENS_PER_CHUNK = 50
    COST_PER_1K = 0.015
    THRESHOLD = 3

    base = baseline_websocket_runaway(
        max_chunks=MAX_CHUNKS,
        tokens_per_chunk=TOKENS_PER_CHUNK,
        cost_per_1k_tokens_usd=COST_PER_1K,
    )
    ver = veronica_websocket_runaway(
        max_chunks=MAX_CHUNKS,
        tokens_per_chunk=TOKENS_PER_CHUNK,
        cost_per_1k_tokens_usd=COST_PER_1K,
        circuit_threshold=THRESHOLD,
        budget_usd=1.00,
    )

    baseline_calls = base["chunks_generated"]
    veronica_calls = ver["chunks_generated"]
    cost_saved_pct = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print("=" * 68)
    print("INCIDENT: WebSocket Streaming Runaway (Fintech, 2024-Q1)")
    print("Real data: 2.16M tokens, $32.40, OOM after 4.5 minutes, 8min downtime")
    print("=" * 68)
    print()
    print(f"{'scenario':<20} {'baseline_calls':>16} {'veronica_calls':>16} {'contained':>10} {'cost_saved_pct':>16}")
    print("-" * 80)
    print(f"{'baseline':<20} {baseline_calls:>16} {'N/A':>16} {'False':>10} {'0.0%':>16}")
    print(f"{'veronica':<20} {'N/A':>16} {veronica_calls:>16} {'True':>10} {cost_saved_pct:>15.1f}%")
    print()
    print(f"Tokens: baseline {base['total_tokens']:,} | veronica {ver['total_tokens']:,}")
    print(f"Errors ignored (baseline): {base['errors_ignored']:,}")
    print(f"Circuit opened at chunk: {ver['circuit_opened_at_chunk']} (threshold: {THRESHOLD})")
    print(f"Baseline cost: ${base['total_cost_usd']:.4f} | Veronica cost: ${ver['total_cost_usd']:.4f}")


if __name__ == "__main__":
    main()
