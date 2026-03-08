"""ASGI and WSGI middleware for veronica_core ExecutionContext.

Provides per-request ExecutionContext creation and ContextVar storage
so application handlers can retrieve the active context via
get_current_execution_context().
"""

# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
# Added VeronicaASGIMiddleware: ASGI3 middleware that creates an
#   ExecutionContext per HTTP request and stores it in a ContextVar.
#   Returns HTTP 429 when context is already aborted on pre-flight or
#   aborted after the call. Non-HTTP scopes pass through unchanged.
# Fix: replaced pre-flight wrap_llm_call(no-op) with get_snapshot().aborted
#   to avoid burning step_count on every HTTP request.
# Added WebSocket containment: VeronicaASGIMiddleware now enforces budget
#   and step limits for WebSocket (scope type == "websocket") sessions.
#   Pre-flight budget exceeded -> websocket.close code=1008, inner app not
#   called. Mid-session step/budget limit -> synthetic websocket.disconnect
#   returned to inner app, then websocket.close code=1008 sent to client.
#   Each receive() and send() call increments the step counter. The active
#   ExecutionContext is available via get_current_execution_context() inside
#   the inner app for the duration of the WS session.
# Added VeronicaWSGIMiddleware: WSGI middleware with identical semantics
#   for synchronous WSGI apps.
# Added get_current_execution_context(): returns the ExecutionContext
#   bound to the current request, or None if called outside a request.
# ---------------------------------------------------------------------------

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable

from veronica_core.containment.execution_context import (
    ExecutionConfig,
    ExecutionContext,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from veronica_core.shield.pipeline import ShieldPipeline


# ContextVar holding the ExecutionContext for the current request.
_current_execution_context: contextvars.ContextVar[ExecutionContext | None] = (
    contextvars.ContextVar("veronica_execution_context", default=None)
)


def get_current_execution_context() -> ExecutionContext | None:
    """Return the ExecutionContext for the current request.

    Returns None when called outside an active request context (i.e., no
    middleware has set the context for this task/thread).
    """
    return _current_execution_context.get()


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

# Type aliases for ASGI callables (stdlib only, no starlette/anyio).
_Scope = dict[str, Any]
_Receive = Callable[[], Any]
_Send = Callable[[dict[str, Any]], Any]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Any]


class VeronicaASGIMiddleware:
    """ASGI3 middleware that wraps each HTTP request in an ExecutionContext.

    Creates a fresh ExecutionContext for every HTTP request and stores it
    in a ContextVar so downstream handlers can call
    get_current_execution_context().

    Non-HTTP scopes (lifespan, websocket) are passed through to the
    wrapped app without creating an ExecutionContext.

    When the context is already aborted on pre-flight (limits already hit),
    or when the context is found to be aborted after the app call completes,
    the middleware responds with HTTP 429 instead of forwarding the app's response.
    """

    def __init__(
        self,
        app: _ASGIApp,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
    ) -> None:
        self._app = app
        self._config = config
        self._pipeline = pipeline

    async def __call__(
        self,
        scope: _Scope,
        receive: _Receive,
        send: _Send,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope_type != "http":
            await self._app(scope, receive, send)
            return

        ctx = ExecutionContext(config=self._config, pipeline=self._pipeline)
        token = _current_execution_context.set(ctx)
        halted = False
        try:
            app_exception: BaseException | None = None
            response_started = False

            async def _intercepting_send(message: dict[str, Any]) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            # Pre-flight: check if context is already at limits without
            # consuming a step count (unlike wrap_llm_call).
            snap = ctx.get_snapshot()
            if snap.aborted or snap.cost_usd_accumulated >= self._config.max_cost_usd:
                halted = True
            else:
                try:
                    await self._app(scope, receive, _intercepting_send)
                except BaseException as exc:
                    app_exception = exc

                # Post-flight: check if the context was aborted during the call.
                # Only set halted when the response has not yet started; once
                # http.response.start has been forwarded to the client, sending
                # a second response would violate the ASGI protocol.
                if ctx.get_snapshot().aborted and not response_started:
                    halted = True

                if app_exception is not None:
                    raise app_exception

        finally:
            _current_execution_context.reset(token)
            ctx.__exit__(None, None, None)

        if halted:
            await _send_429(send)

    async def _handle_websocket(
        self,
        scope: _Scope,
        receive: _Receive,
        send: _Send,
    ) -> None:
        """Handle a WebSocket scope with budget and step-limit containment.

        Pre-flight: if the budget is already at or over the ceiling, send
        websocket.close(code=1008) immediately without calling the inner app.

        Mid-session: each receive() and send() call increments the step counter
        via wrap_tool_call(cost_estimate_hint=0). Once the budget or step limit
        is exceeded, _tracked_receive() returns a synthetic
        websocket.disconnect instead of blocking on the real receive, and a
        websocket.close(code=1008) is queued after the inner app returns.

        The ExecutionContext is stored in the ContextVar for the duration of
        the session so get_current_execution_context() works inside the inner
        app.
        """
        from veronica_core.containment.execution_context import WrapOptions

        ctx = ExecutionContext(config=self._config, pipeline=self._pipeline)
        token = _current_execution_context.set(ctx)
        halted = False

        try:
            snap = ctx.get_snapshot()
            if snap.aborted or snap.cost_usd_accumulated >= self._config.max_cost_usd:
                halted = True
            else:
                from veronica_core.shield.types import Decision as _Decision

                _halt_flag: list[bool] = [False]

                async def _tracked_receive() -> dict[str, Any]:
                    if _halt_flag[0]:
                        return {"type": "websocket.disconnect", "code": 1008}
                    opts = WrapOptions(
                        operation_name="ws.receive",
                        cost_estimate_hint=0.0,
                    )
                    decision = ctx.wrap_tool_call(fn=lambda: None, options=opts)
                    if decision == _Decision.HALT:
                        _halt_flag[0] = True
                        return {"type": "websocket.disconnect", "code": 1008}
                    return await receive()

                async def _tracked_send(message: dict[str, Any]) -> None:
                    if message.get("type") == "websocket.close":
                        await send(message)
                        return
                    opts = WrapOptions(
                        operation_name="ws.send",
                        cost_estimate_hint=0.0,
                    )
                    decision = ctx.wrap_tool_call(fn=lambda: None, options=opts)
                    if decision == _Decision.HALT:
                        _halt_flag[0] = True
                        return
                    await send(message)

                app_exception: BaseException | None = None
                try:
                    await self._app(scope, _tracked_receive, _tracked_send)
                except BaseException as exc:
                    app_exception = exc

                if _halt_flag[0]:
                    halted = True

                if app_exception is not None:
                    raise app_exception
        finally:
            _current_execution_context.reset(token)
            ctx.__exit__(None, None, None)

        if halted:
            await _send_ws_close_1008(send)

    # Note: _send_429 is defined at module level below.


# ---------------------------------------------------------------------------
# WSGI middleware
# ---------------------------------------------------------------------------

_WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


class VeronicaWSGIMiddleware:
    """WSGI middleware that wraps each request in an ExecutionContext.

    Creates a fresh ExecutionContext for every WSGI request and stores it
    in a ContextVar so application code can call
    get_current_execution_context().

    When the context is already aborted on pre-flight (limits already hit),
    or when the context is found to be aborted after the app call completes,
    responds with '429 Too Many Requests'.
    """

    def __init__(
        self,
        app: _WSGIApp,
        config: ExecutionConfig,
        pipeline: ShieldPipeline | None = None,
    ) -> None:
        self._app = app
        self._config = config
        self._pipeline = pipeline

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        ctx = ExecutionContext(config=self._config, pipeline=self._pipeline)
        token = _current_execution_context.set(ctx)
        environ["veronica.context"] = ctx
        try:
            # Pre-flight: check if context is already at limits without
            # consuming a step count (unlike wrap_llm_call).
            snap = ctx.get_snapshot()
            if snap.aborted or snap.cost_usd_accumulated >= self._config.max_cost_usd:
                return _wsgi_429(start_response)

            # Wrap start_response so we know if the app already started a
            # response.  If it did, we must NOT call _wsgi_429 (which would
            # invoke start_response a second time, violating the WSGI spec
            # and causing an AssertionError in compliant WSGI servers).
            tracker = _StartResponseTracker(start_response)
            app_exception: BaseException | None = None
            try:
                result = self._app(environ, tracker)
            except BaseException as exc:
                app_exception = exc

            # Post-flight: check if aborted during the call.
            # Run this check even when the app raised so that a halted context
            # returns 429 instead of propagating the exception (mirrors ASGI
            # behaviour where the halted flag takes priority).
            if ctx.get_snapshot().aborted and not tracker.started:
                if app_exception is not None:
                    logger.warning(
                        "WSGI app raised %s but context was halted; "
                        "returning 429 instead of propagating exception",
                        type(app_exception).__name__,
                        exc_info=app_exception,
                    )
                # Close the app iterable (if any) to avoid resource leaks
                # (WSGI PEP 3333: the server must call close() if present).
                if app_exception is None and hasattr(result, "close"):
                    result.close()
                return _wsgi_429(start_response)

            if app_exception is not None:
                raise app_exception

            return result
        finally:
            _current_execution_context.reset(token)
            ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StartResponseTracker:
    """Thin wrapper around WSGI start_response that tracks whether it was called.

    Used by VeronicaWSGIMiddleware to guard against invoking start_response a
    second time (for the 429 response) when the app already started a response.
    Calling start_response more than once violates the WSGI spec (PEP 3333).
    """

    def __init__(self, start_response: Callable[..., Any]) -> None:
        self._start_response = start_response
        self.started: bool = False

    def __call__(
        self,
        status: str,
        response_headers: list,
        exc_info: Any = None,
    ) -> Any:
        self.started = True
        if exc_info is not None:
            return self._start_response(status, response_headers, exc_info)
        return self._start_response(status, response_headers)


async def _send_429(send: _Send) -> None:
    """Send an HTTP 429 response via the ASGI send callable."""
    body = b"429 Too Many Requests"
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                [b"content-type", b"text/plain; charset=utf-8"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


def _wsgi_429(start_response: Callable[..., Any]) -> list[bytes]:
    """Call start_response with 429 and return the response body."""
    start_response(
        "429 Too Many Requests",
        [("Content-Type", "text/plain; charset=utf-8")],
    )
    return [b"429 Too Many Requests"]


async def _send_ws_close_1008(send: _Send) -> None:
    """Send websocket.close with code=1008 (Policy Violation)."""
    await send({"type": "websocket.close", "code": 1008})
