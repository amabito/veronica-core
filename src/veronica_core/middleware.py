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
#   Returns HTTP 429 when Decision.HALT is produced or the context is
#   aborted after the call. Non-HTTP scopes pass through unchanged.
# Added VeronicaWSGIMiddleware: WSGI middleware with identical semantics
#   for synchronous WSGI apps.
# Added get_current_execution_context(): returns the ExecutionContext
#   bound to the current request, or None if called outside a request.
# ---------------------------------------------------------------------------

from __future__ import annotations

import contextvars
from typing import Any, Callable, Iterable

from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext
from veronica_core.shield.types import Decision

from typing import TYPE_CHECKING

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

    On Decision.HALT (pre-flight limit hit) or when the context is found
    to be aborted after the app call completes, the middleware responds
    with HTTP 429 instead of forwarding the app's response.
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
        if scope.get("type") != "http":
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

            # Pre-flight: check limits before calling the app.
            decision = ctx.wrap_llm_call(fn=lambda: None)
            if decision == Decision.HALT:
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

    On Decision.HALT (pre-flight limit hit) or when the context is found
    to be aborted after the app call completes, responds with
    '429 Too Many Requests'.
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
            # Pre-flight containment check.
            decision = ctx.wrap_llm_call(fn=lambda: None)
            if decision == Decision.HALT:
                return _wsgi_429(start_response)

            result = self._app(environ, start_response)

            # Post-flight: check if aborted during the call.
            if ctx.get_snapshot().aborted:
                return _wsgi_429(start_response)

            return result
        finally:
            _current_execution_context.reset(token)
            ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
