# Middleware — Per-Request ExecutionContext for ASGI and WSGI

## 1. Purpose

`ExecutionContext` is constructed once and passed explicitly in most use cases.
Web frameworks need a way to create a fresh context for each incoming request
and make it accessible to downstream handlers without threading the object
through every layer.

`VeronicaASGIMiddleware` and `VeronicaWSGIMiddleware` handle this wiring.
Each request gets its own `ExecutionContext`; the context is stored in a
`ContextVar` and can be retrieved anywhere within the same request via
`get_current_execution_context()`.

---

## 2. ASGI Middleware

### Import

```python
from veronica_core.middleware import (
    VeronicaASGIMiddleware,
    get_current_execution_context,
)
```

### Constructor

```python
VeronicaASGIMiddleware(
    app: ASGIApp,
    config: ExecutionConfig,
    pipeline: ShieldPipeline | None = None,
)
```

- `app` — the ASGI application to wrap.
- `config` — limits applied to every request (same config is reused; each request
  gets a fresh `ExecutionContext` instance constructed from it).
- `pipeline` — optional `ShieldPipeline`; forwarded to the `ExecutionContext`
  constructor. Pass `None` to run limit enforcement only.

### Request lifecycle

1. Non-HTTP scopes (`lifespan`, `websocket`) are forwarded to `app` directly.
   No `ExecutionContext` is created.
2. For HTTP scopes, a new `ExecutionContext` is created and stored in a `ContextVar`.
3. Pre-flight: `ctx.wrap_llm_call(fn=lambda: None)` runs the containment checks
   against the current limits. If the result is `Decision.HALT`, the inner app is
   skipped and a 429 response is sent.
4. If pre-flight passes, the inner `app` is called. A wrapper around `send` tracks
   whether `http.response.start` has been forwarded.
5. Post-flight: if `ctx.get_snapshot().aborted` is `True` and the response has not
   yet started, a 429 response is sent instead of the app's response.
6. The `ContextVar` is reset and `ctx.__exit__` is called in a `finally` block
   regardless of outcome.

### Example — FastAPI

```python
from fastapi import FastAPI
from veronica_core.containment import ExecutionConfig
from veronica_core.middleware import VeronicaASGIMiddleware, get_current_execution_context

app = FastAPI()

config = ExecutionConfig(max_cost_usd=1.00, max_steps=50, max_retries_total=10)
app.add_middleware(VeronicaASGIMiddleware, config=config)


@app.get("/generate")
def generate():
    ctx = get_current_execution_context()
    if ctx is None:
        return {"error": "no context"}  # should not happen under middleware
    snap = ctx.get_snapshot()
    return {"steps_used": snap.step_count, "cost_so_far": snap.cost_usd_accumulated}
```

### Example — Starlette

```python
from starlette.applications import Starlette
from starlette.middleware import Middleware
from veronica_core.middleware import VeronicaASGIMiddleware

middleware = [
    Middleware(VeronicaASGIMiddleware, config=config),
]
app = Starlette(middleware=middleware, routes=[...])
```

---

## 3. WSGI Middleware

### Import

```python
from veronica_core.middleware import VeronicaWSGIMiddleware
```

### Constructor

```python
VeronicaWSGIMiddleware(
    app: WSGIApp,
    config: ExecutionConfig,
    pipeline: ShieldPipeline | None = None,
)
```

### Request lifecycle

1. A new `ExecutionContext` is created and stored under `environ["veronica.context"]`
   and in a `ContextVar`.
2. Pre-flight: same limit check as the ASGI variant. Returns 429 immediately if halted.
3. The inner `app` is called with the modified `environ`.
4. Post-flight: if `ctx.get_snapshot().aborted`, returns 429 instead of the app's
   response.
5. `ContextVar` is reset and `ctx.__exit__` is called in a `finally` block.

### Example — Flask

```python
from flask import Flask, g
from veronica_core.containment import ExecutionConfig
from veronica_core.middleware import VeronicaWSGIMiddleware

flask_app = Flask(__name__)
config = ExecutionConfig(max_cost_usd=1.00, max_steps=50, max_retries_total=10)
flask_app.wsgi_app = VeronicaWSGIMiddleware(flask_app.wsgi_app, config=config)


@flask_app.route("/generate")
def generate():
    ctx = flask_app.wsgi_app  # access via environ["veronica.context"] in practice
    return "ok"
```

```python
# Inside a request handler, retrieve via environ:
def my_view(environ, start_response):
    ctx = environ.get("veronica.context")
    if ctx:
        snap = ctx.get_snapshot()
```

---

## 4. `get_current_execution_context()`

```python
from veronica_core.middleware import get_current_execution_context

ctx = get_current_execution_context()  # ExecutionContext | None
```

Returns the `ExecutionContext` bound to the current request task/thread. Returns
`None` when called outside an active request (e.g., at startup, in background tasks
not spawned from within a request).

Works for both ASGI (asyncio tasks running within the same request) and WSGI
(thread-local context via `contextvars`).

---

## 5. HTTP 429 Response

Both middlewares respond with:

```
HTTP/1.1 429 Too Many Requests
Content-Type: text/plain; charset=utf-8

429 Too Many Requests
```

The 429 is sent when either:
- The pre-flight limit check returns `Decision.HALT` (limits already exhausted
  before the inner app is called), or
- The inner app completes and `ctx.get_snapshot().aborted` is `True` and the
  response has not yet started.

The post-flight 429 is suppressed if `http.response.start` has already been
forwarded to the client (ASGI) — sending two responses would violate the ASGI
protocol.

---

## 6. Thread and Task Safety

`ContextVar` is used for context storage. Each asyncio task and each OS thread
has its own `ContextVar` slot. Concurrent requests do not interfere.

For ASGI, the context is set and reset within the middleware's `__call__` coroutine
and is visible to all awaitable calls within the same task. Sub-tasks spawned with
`asyncio.create_task` do **not** inherit the context by default (Python's task
factory does not copy the context automatically). If sub-tasks need the context,
pass it explicitly or use `contextvars.copy_context().run(...)`.
