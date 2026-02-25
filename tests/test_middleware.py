"""Tests for VeronicaASGIMiddleware and VeronicaWSGIMiddleware.

Tests:
ASGI:
1. test_asgi_passthrough_non_http        - lifespan scope passes through unchanged
2. test_asgi_allow_request               - normal request completes, app called once
3. test_asgi_halt_returns_429            - HALT config returns 429
4. test_asgi_context_injectable          - get_current_execution_context() non-None inside handler
5. test_asgi_context_none_outside        - get_current_execution_context() is None outside request

WSGI:
6. test_wsgi_allow_request               - normal request passes through
7. test_wsgi_halt_returns_429            - HALT returns 429
8. test_wsgi_context_in_environ          - environ["veronica.context"] is set during request
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext
from veronica_core.middleware import (
    VeronicaASGIMiddleware,
    VeronicaWSGIMiddleware,
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
    """Config that halts immediately (max_cost_usd=0.0 triggers HALT on first call)."""
    return ExecutionConfig(
        max_cost_usd=0.0,
        max_steps=100,
        max_retries_total=10,
    )


# ---------------------------------------------------------------------------
# Minimal ASGI transport
# ---------------------------------------------------------------------------


async def _call_asgi_http(app: Any, path: str = "/") -> tuple[int, bytes]:
    """Drive an ASGI3 app with a minimal HTTP scope. Returns (status, body)."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    status = 200
    body = b""
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            body += msg.get("body", b"")
    return status, body


async def _call_asgi_lifespan(app: Any) -> None:
    """Drive an ASGI3 app with a lifespan scope through startup and shutdown."""
    scope: dict[str, Any] = {"type": "lifespan"}
    events = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    idx = 0

    async def receive() -> dict[str, Any]:
        nonlocal idx
        ev = events[idx] if idx < len(events) else {"type": "lifespan.shutdown"}
        idx += 1
        return ev

    async def send(message: dict[str, Any]) -> None:
        pass

    await app(scope, receive, send)


# ---------------------------------------------------------------------------
# Minimal inline ASGI apps
# ---------------------------------------------------------------------------


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [[b"content-type", b"text/plain"]],
    })
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


# ---------------------------------------------------------------------------
# ASGI tests
# ---------------------------------------------------------------------------


def test_asgi_passthrough_non_http() -> None:
    """Lifespan scope passes through to the inner app without wrapping."""
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

    middleware = VeronicaASGIMiddleware(_lifespan_app, config=_make_config())
    asyncio.get_event_loop().run_until_complete(_call_asgi_lifespan(middleware))

    assert "startup" in reached
    assert "shutdown" in reached


def test_asgi_allow_request() -> None:
    """Normal request completes with 200, inner app called exactly once."""
    call_count: list[int] = []

    async def _counting_app(scope: Any, receive: Any, send: Any) -> None:
        call_count.append(1)
        await _ok_app(scope, receive, send)

    middleware = VeronicaASGIMiddleware(_counting_app, config=_make_config())
    status, body = asyncio.get_event_loop().run_until_complete(_call_asgi_http(middleware))

    assert status == 200
    assert body == b"ok"
    assert call_count == [1]


def test_asgi_halt_returns_429() -> None:
    """Exhausted config (max_cost_usd=0.0) causes middleware to return 429."""
    app_called: list[bool] = []

    async def _tracking_app(scope: Any, receive: Any, send: Any) -> None:
        app_called.append(True)
        await _ok_app(scope, receive, send)

    middleware = VeronicaASGIMiddleware(_tracking_app, config=_halting_config())
    status, _ = asyncio.get_event_loop().run_until_complete(_call_asgi_http(middleware))

    assert status == 429
    assert app_called == [], "inner app must not be called when halted"


def test_asgi_context_injectable() -> None:
    """get_current_execution_context() returns a non-None ExecutionContext inside handler."""
    captured: list[ExecutionContext | None] = []

    async def _capturing_app(scope: Any, receive: Any, send: Any) -> None:
        captured.append(get_current_execution_context())
        await _ok_app(scope, receive, send)

    middleware = VeronicaASGIMiddleware(_capturing_app, config=_make_config())
    asyncio.get_event_loop().run_until_complete(_call_asgi_http(middleware))

    assert len(captured) == 1
    assert isinstance(captured[0], ExecutionContext)


def test_asgi_context_none_outside() -> None:
    """get_current_execution_context() returns None after the request completes."""
    middleware = VeronicaASGIMiddleware(_ok_app, config=_make_config())
    asyncio.get_event_loop().run_until_complete(_call_asgi_http(middleware))

    assert get_current_execution_context() is None


# ---------------------------------------------------------------------------
# WSGI tests
# ---------------------------------------------------------------------------


def _run_wsgi(
    middleware: VeronicaWSGIMiddleware,
    environ: dict[str, Any] | None = None,
) -> tuple[str, list[bytes]]:
    """Run a WSGI middleware call, return (status_string, body_chunks)."""
    env: dict[str, Any] = environ if environ is not None else {}
    status_holder: list[str] = []

    def _start_response(status: str, headers: list[tuple[str, str]]) -> None:
        status_holder.append(status)

    body = list(middleware(env, _start_response))
    return status_holder[0] if status_holder else "", body


def test_wsgi_allow_request() -> None:
    """Normal WSGI request passes through and returns expected body."""

    def _simple_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"hello"]

    middleware = VeronicaWSGIMiddleware(_simple_app, config=_make_config())
    status, body = _run_wsgi(middleware)

    assert status == "200 OK"
    assert body == [b"hello"]


def test_wsgi_halt_returns_429() -> None:
    """Exhausted config causes WSGI middleware to return 429, inner app not called."""
    app_called: list[bool] = []

    def _tracking_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        app_called.append(True)
        start_response("200 OK", [])
        return [b"ok"]

    middleware = VeronicaWSGIMiddleware(_tracking_app, config=_halting_config())
    status, _ = _run_wsgi(middleware)

    assert "429" in status
    assert app_called == [], "inner app must not be called when halted"


def test_wsgi_context_in_environ() -> None:
    """environ['veronica.context'] is an ExecutionContext during the request."""
    captured: list[Any] = []

    def _capturing_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        captured.append(environ.get("veronica.context"))
        start_response("200 OK", [])
        return [b"ok"]

    environ: dict[str, Any] = {}
    middleware = VeronicaWSGIMiddleware(_capturing_app, config=_make_config())
    _run_wsgi(middleware, environ=environ)

    assert len(captured) == 1
    assert isinstance(captured[0], ExecutionContext)
