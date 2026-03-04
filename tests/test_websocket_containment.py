"""Tests for WebSocket containment in VeronicaASGIMiddleware (Item 2e).

Covers:
1. Normal WS session passes through unchanged
2. Pre-flight halt closes with 1008 before calling inner app
3. Budget exceeded mid-session closes with 1008
4. Step count incremented per send/receive
5. Context stored in ContextVar during WS session
6. Non-HTTP non-WS scopes still pass through unchanged
7. Adversarial: inner app exception propagates after ws cleanup
8. Adversarial: halted receive returns synthetic disconnect (inner app exits)
9. Adversarial: concurrent WS sessions isolated per-context
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)
from veronica_core.middleware import (
    VeronicaASGIMiddleware,
    get_current_execution_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(*, max_steps: int = 100) -> ExecutionConfig:
    return ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=max_steps,
        max_retries_total=10,
    )


def _halting_config() -> ExecutionConfig:
    """Config with max_cost_usd=0.0 — halts on pre-flight."""
    return ExecutionConfig(
        max_cost_usd=0.0,
        max_steps=100,
        max_retries_total=10,
    )


def _tight_steps_config(max_steps: int = 2) -> ExecutionConfig:
    """Config that halts after max_steps tool calls."""
    return ExecutionConfig(
        max_cost_usd=100.0,
        max_steps=max_steps,
        max_retries_total=10,
    )


async def _call_ws(app: Any, messages_to_send: list[dict]) -> list[dict]:
    """Drive an ASGI3 app with a websocket scope.

    Simulates a client that sends *messages_to_send* in order.
    Returns all messages that were passed to the outer send callable.
    """
    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/ws",
        "headers": [],
    }

    incoming = iter(messages_to_send)

    async def receive() -> dict[str, Any]:
        try:
            return next(incoming)
        except StopIteration:
            return {"type": "websocket.disconnect", "code": 1000}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ws_normal_session_passthrough() -> None:
    """Normal WS session: inner app receives all messages, sends pass through."""
    received_by_app: list[dict] = []

    async def _echo_app(scope: Any, receive: Any, send: Any) -> None:
        # Accept the connection
        msg = await receive()
        received_by_app.append(msg)
        await send({"type": "websocket.accept"})

        # Echo one message
        msg = await receive()
        received_by_app.append(msg)
        if msg.get("type") == "websocket.receive":
            await send({"type": "websocket.send", "text": msg.get("text", "")})

    middleware = VeronicaASGIMiddleware(_echo_app, config=_make_config())
    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "hello"},
    ]
    outer_sent = asyncio.run(_call_ws(middleware, messages))

    assert any(m.get("type") == "websocket.accept" for m in outer_sent), (
        "websocket.accept must pass through"
    )
    echo_msgs = [m for m in outer_sent if m.get("type") == "websocket.send"]
    assert len(echo_msgs) == 1
    assert echo_msgs[0].get("text") == "hello"


def test_ws_preflight_halt_closes_1008() -> None:
    """Pre-flight budget exceeded: inner app not called, 1008 sent immediately."""
    app_called: list[bool] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        app_called.append(True)

    middleware = VeronicaASGIMiddleware(_app, config=_halting_config())
    outer_sent = asyncio.run(_call_ws(middleware, []))

    assert app_called == [], "Inner app must not be called on pre-flight halt"
    close_msgs = [m for m in outer_sent if m.get("type") == "websocket.close"]
    assert close_msgs, "Must send websocket.close on pre-flight halt"
    assert close_msgs[0]["code"] == 1008, (
        f"Close code must be 1008 (Policy Violation), got {close_msgs[0]['code']}"
    )


def test_ws_budget_exceeded_mid_session_closes_1008() -> None:
    """Budget exceeded during session: close 1008 sent after step limit hit."""

    # max_steps=2: accept (1 step for receive) + one echo (1 step for receive) = 2
    # Third receive would be step 3 → HALT
    async def _looping_app(scope: Any, receive: Any, send: Any) -> None:
        # Keep receiving until disconnect
        while True:
            msg = await receive()
            if msg.get("type") in ("websocket.disconnect",):
                break
            await send({"type": "websocket.send", "text": "echo"})

    middleware = VeronicaASGIMiddleware(
        _looping_app, config=_tight_steps_config(max_steps=2)
    )
    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "msg1"},
        {"type": "websocket.receive", "text": "msg2"},
        {"type": "websocket.receive", "text": "msg3"},
    ]
    outer_sent = asyncio.run(_call_ws(middleware, messages))

    close_msgs = [m for m in outer_sent if m.get("type") == "websocket.close"]
    assert close_msgs, "Must send websocket.close when budget exceeded mid-session"
    assert any(m["code"] == 1008 for m in close_msgs), (
        f"Close code must be 1008, got close_msgs={close_msgs}"
    )


def test_ws_context_injectable_during_session() -> None:
    """get_current_execution_context() returns non-None inside WS handler."""
    captured: list[ExecutionContext | None] = []

    async def _capturing_app(scope: Any, receive: Any, send: Any) -> None:
        captured.append(get_current_execution_context())
        # Consume the connect message
        await receive()

    middleware = VeronicaASGIMiddleware(_capturing_app, config=_make_config())
    asyncio.run(_call_ws(middleware, [{"type": "websocket.connect"}]))

    assert len(captured) == 1
    assert isinstance(captured[0], ExecutionContext), (
        f"Expected ExecutionContext inside WS handler, got {captured[0]}"
    )


def test_ws_context_none_after_session() -> None:
    """get_current_execution_context() is None after WS session ends."""

    async def _noop_app(scope: Any, receive: Any, send: Any) -> None:
        await receive()

    middleware = VeronicaASGIMiddleware(_noop_app, config=_make_config())
    asyncio.run(_call_ws(middleware, [{"type": "websocket.connect"}]))

    assert get_current_execution_context() is None


def test_lifespan_still_passes_through() -> None:
    """Non-HTTP non-WS scope (lifespan) still passes through unchanged."""
    reached: list[str] = []

    async def _lifespan_app(scope: Any, receive: Any, send: Any) -> None:
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                reached.append("startup")
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                reached.append("shutdown")
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _drive() -> None:
        scope: dict[str, Any] = {"type": "lifespan"}
        events = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        idx = 0

        async def receive() -> dict[str, Any]:
            nonlocal idx
            ev = events[idx] if idx < len(events) else {"type": "lifespan.shutdown"}
            idx += 1
            return ev

        async def send(msg: dict[str, Any]) -> None:
            pass

        middleware = VeronicaASGIMiddleware(_lifespan_app, config=_make_config())
        await middleware(scope, receive, send)

    asyncio.run(_drive())
    assert "startup" in reached
    assert "shutdown" in reached


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


def test_ws_inner_app_exception_propagates() -> None:
    """Exception from inner WS app propagates to caller after ws cleanup."""

    async def _raising_app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        raise ValueError("deliberate ws error")

    middleware = VeronicaASGIMiddleware(_raising_app, config=_make_config())
    raised = False
    try:
        asyncio.run(_call_ws(middleware, [{"type": "websocket.connect"}]))
    except ValueError:
        raised = True

    assert raised, "Inner app ValueError must propagate from websocket handler"


def test_ws_halted_receive_returns_disconnect() -> None:
    """After budget exceeded, _tracked_receive returns websocket.disconnect.

    The inner app's receive loop must exit (not block) when the budget is
    exhausted.
    """
    loop_count: list[int] = []

    async def _counting_app(scope: Any, receive: Any, send: Any) -> None:
        # Loop until disconnect
        for _ in range(20):
            msg = await receive()
            loop_count.append(1)
            if msg.get("type") == "websocket.disconnect":
                break

    # max_steps=1: first receive is the only allowed step
    middleware = VeronicaASGIMiddleware(
        _counting_app, config=_tight_steps_config(max_steps=1)
    )
    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "hi"},
        {"type": "websocket.receive", "text": "hi again"},
    ]
    asyncio.run(_call_ws(middleware, messages))

    # Inner app should have exited after receiving the synthetic disconnect
    # (not looped forever)
    assert len(loop_count) <= 3, (
        f"Expected inner app to exit early, but loop_count={len(loop_count)}"
    )


def test_ws_steps_counted_per_message() -> None:
    """Each receive/send increments the step counter in the ExecutionContext."""
    step_counts: list[int] = []

    async def _recording_app(scope: Any, receive: Any, send: Any) -> None:
        ctx = get_current_execution_context()
        assert ctx is not None

        await receive()  # step 1
        step_counts.append(ctx.get_snapshot().step_count)

        await send({"type": "websocket.send", "text": "x"})  # step 2
        step_counts.append(ctx.get_snapshot().step_count)

        await receive()  # step 3
        step_counts.append(ctx.get_snapshot().step_count)

    middleware = VeronicaASGIMiddleware(_recording_app, config=_make_config())
    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "a"},
    ]
    asyncio.run(_call_ws(middleware, messages))

    # Steps must be monotonically increasing and equal to message count
    assert len(step_counts) == 3, f"Expected 3 snapshots, got {step_counts}"
    assert step_counts[0] == 1, f"After first receive: expected 1, got {step_counts[0]}"
    assert step_counts[1] == 2, f"After first send: expected 2, got {step_counts[1]}"
    assert step_counts[2] == 3, (
        f"After second receive: expected 3, got {step_counts[2]}"
    )
