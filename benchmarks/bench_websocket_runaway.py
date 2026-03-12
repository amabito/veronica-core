"""bench_websocket_runaway.py

Measures WebSocket runaway agent message sending.
Simulates an agent sending unlimited messages vs one constrained by veronica step limits.

Uses ExecutionContext directly (no ASGI server required -- self-contained).

Usage:
    python benchmarks/bench_websocket_runaway.py
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Stub WebSocket session (no network)
# ---------------------------------------------------------------------------

@dataclass
class StubWebSocketSession:
    """Simulates a WebSocket agent sending/receiving messages."""

    messages_sent: int = 0
    messages_received: int = 0
    bytes_sent: int = field(default=0)
    is_closed: bool = False
    close_code: int | None = None

    def send(self, payload: str) -> None:
        if self.is_closed:
            raise RuntimeError("WebSocket already closed")
        self.messages_sent += 1
        self.bytes_sent += len(payload.encode())

    def receive(self) -> dict[str, Any]:
        if self.is_closed:
            return {"type": "websocket.disconnect", "code": 1000}
        self.messages_received += 1
        return {"type": "websocket.receive", "text": f"msg_{self.messages_received}"}

    def close(self, code: int = 1000) -> None:
        self.is_closed = True
        self.close_code = code

    @property
    def total_operations(self) -> int:
        return self.messages_sent + self.messages_received


# ---------------------------------------------------------------------------
# Baseline: WebSocket agent sending unlimited messages
# ---------------------------------------------------------------------------

def baseline_websocket_runaway(target_messages: int = 50) -> dict[str, Any]:
    """No containment -- agent sends target_messages unconditionally."""
    ws = StubWebSocketSession()
    payload = "Hello from agent! " * 10  # ~180 bytes per message

    start = time.perf_counter()
    for i in range(target_messages):
        ws.send(payload)
        ws.receive()

    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "scenario": "baseline",
        "messages_sent": ws.messages_sent,
        "messages_received": ws.messages_received,
        "total_operations": ws.total_operations,
        "bytes_sent": ws.bytes_sent,
        "contained": False,
        "close_code": ws.close_code,
        "elapsed_ms": round(elapsed_ms, 2),
    }


# ---------------------------------------------------------------------------
# Veronica: ExecutionContext step limit enforced per send/receive
# ---------------------------------------------------------------------------

def veronica_websocket_runaway(
    max_steps: int = 10,
    max_cost_usd: float = 0.10,
    cost_per_op: float = 0.002,
    target_messages: int = 50,
) -> dict[str, Any]:
    """ExecutionContext limits total steps (send+receive) for the WebSocket session."""
    ws = StubWebSocketSession()
    payload = "Hello from agent! " * 10  # ~180 bytes per message

    config = ExecutionConfig(
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_retries_total=100,
    )

    halted_by: str = "natural_end"
    decisions: list[str] = []
    containment_latency_ms: float | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for i in range(target_messages):
            # Track send operations
            send_start = time.perf_counter()
            decision = ctx.wrap_tool_call(
                fn=lambda p=payload: ws.send(p),
                options=WrapOptions(
                    operation_name="ws_send",
                    cost_estimate_hint=cost_per_op,
                ),
            )
            decisions.append(f"send:{decision.name}")

            if decision == Decision.HALT:
                if containment_latency_ms is None:
                    containment_latency_ms = (time.perf_counter() - send_start) * 1000
                # Simulate sending websocket.close 1008 on containment
                ws.close(code=1008)
                halted_by = "step_limit_send"
                break

            # Track receive operations
            recv_start = time.perf_counter()
            decision = ctx.wrap_tool_call(
                fn=lambda: ws.receive(),
                options=WrapOptions(
                    operation_name="ws_receive",
                    cost_estimate_hint=cost_per_op,
                ),
            )
            decisions.append(f"recv:{decision.name}")

            if decision == Decision.HALT:
                if containment_latency_ms is None:
                    containment_latency_ms = (time.perf_counter() - recv_start) * 1000
                ws.close(code=1008)
                halted_by = "step_limit_receive"
                break

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()

    return {
        "scenario": "veronica",
        "messages_sent": ws.messages_sent,
        "messages_received": ws.messages_received,
        "total_operations": ws.total_operations,
        "bytes_sent": ws.bytes_sent,
        "step_count": snap.step_count,
        "cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "halted_by": halted_by,
        "close_code": ws.close_code,
        "containment_latency_ms": round(containment_latency_ms, 4)
        if containment_latency_ms is not None
        else None,
        "decisions_sample": decisions[:8],
        "elapsed_ms": round(elapsed_ms, 2),
        "contained": True,
        "max_steps": max_steps,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    TARGET_MESSAGES = 50
    MAX_STEPS = 10

    print("=" * 60)
    print("BENCHMARK: WebSocket Runaway Agent")
    print(f"Target messages: {TARGET_MESSAGES} | Step limit: {MAX_STEPS}")
    print("=" * 60)

    base = baseline_websocket_runaway(target_messages=TARGET_MESSAGES)
    ver = veronica_websocket_runaway(
        max_steps=MAX_STEPS,
        max_cost_usd=10.0,
        cost_per_op=0.001,
        target_messages=TARGET_MESSAGES,
    )

    results = {
        "benchmark": "websocket_runaway",
        "baseline": base,
        "veronica": ver,
        "operation_reduction_pct": round(
            100 * (1 - ver["total_operations"] / max(base["total_operations"], 1)), 1
        ),
    }

    print(json.dumps(results, indent=2))

    print()
    print(
        f"{'Scenario':<20} {'Ops':>8} {'Bytes Sent':>12} {'Close Code':>12} "
        f"{'Latency ms':>12}"
    )
    print("-" * 66)
    print(
        f"{'baseline':<20} {base['total_operations']:>8} {base['bytes_sent']:>12} "
        f"{'N/A':>12} {'N/A':>12}"
    )
    latency_str = (
        f"{ver['containment_latency_ms']:.4f}"
        if ver["containment_latency_ms"] is not None
        else "N/A"
    )
    print(
        f"{'veronica':<20} {ver['total_operations']:>8} {ver['bytes_sent']:>12} "
        f"{str(ver['close_code']):>12} {latency_str:>12}"
    )
    print(f"\nOperation reduction: {results['operation_reduction_pct']}%")
    print(f"Halted by: {ver['halted_by']}")
    print(f"Steps used: {ver['step_count']}/{MAX_STEPS}")


if __name__ == "__main__":
    main()
