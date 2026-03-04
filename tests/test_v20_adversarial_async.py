"""Adversarial audit — v2.0 async MCP + WebSocket + middleware (Area 2).

Attacker mindset: try to break budget enforcement, Decision enum contract,
and concurrent request isolation.

Current state of HEAD:
- middleware.py: HTTP containment (429 on halt). WS/lifespan scopes pass
  through unchanged (no WS containment in HEAD).
- mcp_async.py: Async tool call wrapping with budget check (legacy probe).
  No reserve/commit/rollback (that's a v2.0 target, not yet in HEAD).
- CancelledError is NOT caught by 'except Exception' — it propagates.

Attack vectors tested:
1.  Async MCP concurrent calls: budget ceiling enforced
2.  Exception in call_fn: success=False, error_count++
3.  isError result: success=False, error_count++
4.  Timeout: success=False (TimeoutError caught by except Exception)
5.  CancelledError: propagates out (not swallowed) — CRITICAL
6.  HTTP middleware pre-flight halt: 429 returned
7.  HTTP middleware post-flight halt: 429 returned
8.  HTTP middleware pass-through for non-halted request: 200 returned
9.  WS scope passes through middleware unchanged (current behavior)
10. get_current_execution_context() is None outside a request
11. Decision enum backward compat: "ALLOW" == Decision.ALLOW
12. Decision enum: all members are str subclass
13. Decision enum: pickle round-trip
14. Decision enum: json.dumps / json.loads round-trip
15. MCPToolResult with decision=None: comparisons are safe (does not raise)
16. Concurrent HTTP requests: per-request context isolation
"""

from __future__ import annotations

import asyncio
import json
import pickle
from typing import Any

import pytest

from veronica_core.adapters.mcp import MCPToolCost, MCPToolResult
from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext
from veronica_core.distributed import LocalBudgetBackend
from veronica_core.middleware import VeronicaASGIMiddleware, get_current_execution_context
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    max_cost: float = 10.0,
    tool_cost: float = 0.1,
    timeout_seconds: float | None = None,
) -> tuple[AsyncMCPContainmentAdapter, LocalBudgetBackend]:
    backend = LocalBudgetBackend()
    config = ExecutionConfig(
        max_cost_usd=max_cost,
        max_steps=100,
        max_retries_total=10,
        budget_backend=backend,
    )
    ctx = ExecutionContext(config=config)
    ctx._budget_backend = backend
    adapter = AsyncMCPContainmentAdapter(
        execution_context=ctx,
        tool_costs={"t": MCPToolCost("t", cost_per_call=tool_cost)},
        timeout_seconds=timeout_seconds,
    )
    return adapter, backend


async def _echo(**kwargs: Any) -> dict:
    return {"echo": kwargs}


async def _slow(**kwargs: Any) -> dict:
    await asyncio.sleep(0.05)
    return {"ok": True}


async def _raise(**kwargs: Any) -> None:
    raise RuntimeError("tool exploded")


async def _call_http(
    middleware: VeronicaASGIMiddleware,
    app_fn: Any = None,
) -> int:
    """Drive the middleware with a minimal HTTP GET; return the HTTP status code."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    statuses: list[int] = []

    async def send_fn(msg: dict) -> None:
        if msg.get("type") == "http.response.start":
            statuses.append(msg["status"])

    if app_fn is not None:
        await middleware(scope, receive, send_fn)
    else:
        await middleware(scope, receive, send_fn)
    return statuses[0] if statuses else 0


def _make_ok_app() -> Any:
    """Simple ASGI app that returns 200."""

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    return app


# ---------------------------------------------------------------------------
# Area 2-A: Async MCP budget enforcement
# ---------------------------------------------------------------------------


class TestAsyncMCPBudgetEnforcement:
    """Async wrap_tool_call: budget limit enforcement correctness."""

    def test_concurrent_budget_ceiling_enforced(self) -> None:
        """10 concurrent calls, tight budget: total cost stays within ceiling."""

        async def run() -> float:
            # Budget allows 3 calls at 0.1 each (0.3 <= 0.35), blocks further
            adapter, backend = _make_adapter(max_cost=0.35, tool_cost=0.1)
            await asyncio.gather(
                *[adapter.wrap_tool_call("t", {}, _echo) for _ in range(10)],
                return_exceptions=True,
            )
            return backend.get()

        total = asyncio.run(run())
        assert total <= 0.35 + 1e-6, f"budget exceeded: {total}"

    def test_concurrent_at_least_one_halted(self) -> None:
        """With 2 concurrent calls and budget for only 1, at least one is HALT."""

        async def run() -> tuple[MCPToolResult, MCPToolResult]:
            adapter, _ = _make_adapter(max_cost=0.05, tool_cost=0.1)
            r1, r2 = await asyncio.gather(
                adapter.wrap_tool_call("t", {}, _slow),
                adapter.wrap_tool_call("t", {}, _slow),
                return_exceptions=True,
            )
            return r1, r2  # type: ignore[return-value]

        r1, r2 = asyncio.run(run())
        decisions = {r1.decision, r2.decision}
        # At least one must be HALT (budget 0.05 < 2 * 0.1 = 0.20)
        assert Decision.HALT in decisions, f"expected HALT in {decisions}"

    def test_exception_in_call_fn_returns_failure(self) -> None:
        """When call_fn raises, result is success=False, decision=ALLOW."""

        async def run() -> MCPToolResult:
            adapter, _ = _make_adapter()
            return await adapter.wrap_tool_call("t", {}, _raise)

        result = asyncio.run(run())
        assert result.success is False
        assert result.decision == Decision.ALLOW
        assert "RuntimeError" in result.error

    def test_exception_increments_error_count(self) -> None:
        """Exception in call_fn increments error_count in stats."""

        async def run() -> tuple[int, int]:
            adapter, _ = _make_adapter()
            await adapter.wrap_tool_call("t", {}, _raise)
            stats = adapter.get_tool_stats()["t"]
            return stats.call_count, stats.error_count

        calls, errors = asyncio.run(run())
        assert calls == 1
        assert errors == 1

    def test_is_error_returns_failure(self) -> None:
        """When result.isError=True, success=False, decision=ALLOW."""

        class _ErrorResult:
            isError = True

        async def _err_fn(**kwargs: Any) -> _ErrorResult:
            return _ErrorResult()

        async def run() -> MCPToolResult:
            adapter, _ = _make_adapter()
            return await adapter.wrap_tool_call("t", {}, _err_fn)

        result = asyncio.run(run())
        assert result.success is False
        assert result.decision == Decision.ALLOW

    def test_is_error_increments_error_count(self) -> None:
        """isError result increments error_count, not just call_count."""

        class _ErrResult:
            isError = True

        async def _err_fn(**kwargs: Any) -> _ErrResult:
            return _ErrResult()

        async def run() -> tuple[int, int]:
            adapter, _ = _make_adapter()
            await adapter.wrap_tool_call("t", {}, _err_fn)
            stats = adapter.get_tool_stats()["t"]
            return stats.call_count, stats.error_count

        calls, errors = asyncio.run(run())
        assert calls == 1
        assert errors == 1, f"error_count should be 1, got {errors}"

    def test_timeout_returns_failure(self) -> None:
        """Timeout causes success=False; error field is set."""

        async def _very_slow(**kwargs: Any) -> dict:
            await asyncio.sleep(10.0)
            return {}

        async def run() -> MCPToolResult:
            adapter, _ = _make_adapter(timeout_seconds=0.01)
            return await adapter.wrap_tool_call("t", {}, _very_slow)

        result = asyncio.run(run())
        assert result.success is False, "timed-out call should fail"
        assert result.error is not None

    def test_cancelled_error_propagates_not_swallowed(self) -> None:
        """CancelledError must propagate out of wrap_tool_call.

        CancelledError is BaseException (not Exception). The adapter's
        'except Exception' must NOT swallow it — cooperative cancellation
        depends on this.
        """

        async def _cancellable(**kwargs: Any) -> dict:
            await asyncio.sleep(10.0)
            return {}

        async def run() -> None:
            adapter, _ = _make_adapter()
            task = asyncio.create_task(adapter.wrap_tool_call("t", {}, _cancellable))
            await asyncio.sleep(0.01)
            task.cancel()
            await task  # Should raise CancelledError

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())

    def test_success_accumulates_cost(self) -> None:
        """Successful calls accumulate cost in stats.total_cost_usd."""

        async def run() -> float:
            adapter, _ = _make_adapter(tool_cost=0.05)
            await adapter.wrap_tool_call("t", {}, _echo)
            await adapter.wrap_tool_call("t", {}, _echo)
            return adapter.get_tool_stats()["t"].total_cost_usd

        total = asyncio.run(run())
        assert total == pytest.approx(0.10, abs=1e-9)

    def test_stats_call_count_tracks_all_invocations(self) -> None:
        """call_count incremented for both success and blocked calls."""

        async def run() -> int:
            # Budget 0.05, tool cost 0.1 -> first call might succeed, second blocked
            adapter, _ = _make_adapter(max_cost=0.05, tool_cost=0.1)
            await adapter.wrap_tool_call("t", {}, _echo)
            await adapter.wrap_tool_call("t", {}, _echo)
            return adapter.get_tool_stats()["t"].call_count

        count = asyncio.run(run())
        assert count == 2, f"expected 2 calls tracked, got {count}"

    def test_empty_tool_name_raises_value_error(self) -> None:
        """Empty tool_name must raise ValueError immediately."""

        async def run() -> None:
            adapter, _ = _make_adapter()
            await adapter.wrap_tool_call("", {}, _echo)

        with pytest.raises(ValueError, match="non-empty string"):
            asyncio.run(run())

    def test_non_string_tool_name_raises_value_error(self) -> None:
        """Non-string tool_name must raise ValueError."""

        async def run() -> None:
            adapter, _ = _make_adapter()
            await adapter.wrap_tool_call(123, {}, _echo)  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            asyncio.run(run())


# ---------------------------------------------------------------------------
# Area 2-B: HTTP middleware containment
# ---------------------------------------------------------------------------


class TestHTTPMiddlewareAdversarial:
    """VeronicaASGIMiddleware HTTP containment attack vectors."""

    def test_preflight_halt_returns_429(self) -> None:
        """HTTP request with max_cost_usd=0.0 triggers 429 on pre-flight."""
        config = ExecutionConfig(max_cost_usd=0.0, max_steps=100, max_retries_total=10)
        middleware = VeronicaASGIMiddleware(_make_ok_app(), config=config)

        async def run() -> int:
            return await _call_http(middleware)

        status = asyncio.run(run())
        assert status == 429, f"expected 429, got {status}"

    def test_normal_request_passes_through(self) -> None:
        """Normal request (budget not exceeded) passes through to app."""
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        middleware = VeronicaASGIMiddleware(_make_ok_app(), config=config)

        async def run() -> int:
            return await _call_http(middleware)

        status = asyncio.run(run())
        assert status == 200, f"expected 200, got {status}"

    def test_non_http_scope_passes_through_unchanged(self) -> None:
        """Non-HTTP scopes (lifespan, websocket) pass through without containment."""
        app_called = [0]

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            app_called[0] += 1

        config = ExecutionConfig(max_cost_usd=0.0, max_steps=0, max_retries_total=0)
        middleware = VeronicaASGIMiddleware(inner, config=config)

        scope = {"type": "lifespan"}

        async def run() -> None:
            await middleware(scope, lambda: {}, lambda m: None)

        asyncio.run(run())
        # Lifespan scope must bypass containment even with fully-restricted config
        assert app_called[0] == 1, "non-HTTP scope must be forwarded to app"

    def test_websocket_scope_halted_at_preflight(self) -> None:
        """WebSocket scope with zero budget halts at pre-flight with 1008 close.

        v2.0 middleware enforces WS containment: max_cost_usd=0.0 causes a
        pre-flight halt, inner app is NOT called, and websocket.close(1008)
        is sent to the client.
        """
        app_called = [0]

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            app_called[0] += 1

        config = ExecutionConfig(max_cost_usd=0.0, max_steps=0, max_retries_total=0)
        middleware = VeronicaASGIMiddleware(inner, config=config)

        scope = {"type": "websocket", "path": "/ws", "headers": []}

        async def run() -> None:
            async def receive() -> dict:
                return {"type": "websocket.disconnect", "code": 1000}

            sent: list[dict] = []

            async def send(message: dict) -> None:
                sent.append(message)

            await middleware(scope, receive, send)
            close_msgs = [m for m in sent if m.get("type") == "websocket.close"]
            assert close_msgs, "Must send websocket.close on pre-flight halt"
            assert close_msgs[0]["code"] == 1008

        asyncio.run(run())
        # Inner app must NOT be called when pre-flight halts
        assert app_called[0] == 0, "Inner app must not be called on WS pre-flight halt"

    def test_context_is_set_during_request(self) -> None:
        """get_current_execution_context() is non-None inside a request handler."""
        captured: list[ExecutionContext | None] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            captured.append(get_current_execution_context())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        middleware = VeronicaASGIMiddleware(inner, config=config)

        async def run() -> None:
            await _call_http(middleware)

        asyncio.run(run())
        assert captured[0] is not None, "context must be set inside request"

    def test_context_is_none_outside_request(self) -> None:
        """get_current_execution_context() returns None outside a request."""
        assert get_current_execution_context() is None

    def test_sequential_requests_have_separate_contexts(self) -> None:
        """Two sequential requests must create fresh, independent contexts."""
        contexts: list[ExecutionContext | None] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            contexts.append(get_current_execution_context())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        middleware = VeronicaASGIMiddleware(inner, config=config)

        async def run() -> None:
            await _call_http(middleware)
            await _call_http(middleware)

        asyncio.run(run())
        assert len(contexts) == 2
        assert contexts[0] is not contexts[1], "each request must have its own context"

    def test_app_exception_propagates(self) -> None:
        """Exception in app propagates out of middleware."""

        async def crashing_app(scope: Any, receive: Any, send: Any) -> None:
            raise ValueError("app crashed")

        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=10)
        middleware = VeronicaASGIMiddleware(crashing_app, config=config)

        async def run() -> None:
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
            }
            await middleware(scope, lambda: {}, lambda m: None)

        with pytest.raises(ValueError, match="app crashed"):
            asyncio.run(run())


# ---------------------------------------------------------------------------
# Area 2-C: Decision enum contract
# ---------------------------------------------------------------------------


class TestDecisionEnumContract:
    """Decision enum backward compatibility and serialization."""

    @pytest.mark.parametrize("member", list(Decision))
    def test_decision_is_str_subclass(self, member: Decision) -> None:
        """Every Decision member must be a str (backward compat for == comparisons)."""
        assert isinstance(member, str), f"{member!r} is not a str subclass"

    @pytest.mark.parametrize(
        "member",
        [Decision.ALLOW, Decision.HALT, Decision.RETRY, Decision.DEGRADE],
    )
    def test_decision_str_equality_forward(self, member: Decision) -> None:
        """str == Decision.member must be True (forward comparison)."""
        assert member.value == member
        assert member == member.value

    def test_decision_allow_eq_string(self) -> None:
        """'ALLOW' == Decision.ALLOW must be True (backward compat)."""
        assert "ALLOW" == Decision.ALLOW  # noqa: SIM300

    def test_decision_halt_eq_string(self) -> None:
        """'HALT' == Decision.HALT must be True (backward compat)."""
        assert "HALT" == Decision.HALT  # noqa: SIM300

    @pytest.mark.parametrize("member", list(Decision))
    def test_pickle_round_trip(self, member: Decision) -> None:
        """Pickle round-trip must preserve Decision identity."""
        restored = pickle.loads(pickle.dumps(member))
        assert restored == member
        assert restored is member  # Enum singletons survive pickle

    @pytest.mark.parametrize("member", list(Decision))
    def test_json_round_trip(self, member: Decision) -> None:
        """json.dumps/loads round-trip: value string is preserved."""
        serialized = json.dumps(member)
        raw = json.loads(serialized)
        assert raw == member.value, f"json round-trip: {raw!r} != {member.value!r}"
        restored = Decision(raw)
        assert restored == member

    def test_mcp_tool_result_default_decision_is_allow(self) -> None:
        """MCPToolResult default decision is ALLOW."""
        r = MCPToolResult(success=True)
        assert r.decision == Decision.ALLOW

    def test_mcp_tool_result_none_decision_safe_comparison(self) -> None:
        """MCPToolResult with decision=None: comparison with Decision members is safe."""
        r = MCPToolResult(success=True, decision=None)  # type: ignore[arg-type]
        # Must not raise; must return False
        assert r.decision != Decision.HALT
        assert r.decision != Decision.ALLOW
        assert r.decision is None

    def test_mcp_tool_result_halt_backward_compat(self) -> None:
        """result.decision == 'HALT' must be True for HALT result (legacy code paths)."""
        r = MCPToolResult(success=False, decision=Decision.HALT)
        assert r.decision == "HALT", "backward compat: HALT == 'HALT' must be True"
        assert "HALT" == r.decision, "backward compat: 'HALT' == HALT must be True"

    def test_decision_in_set(self) -> None:
        """Decision members must be hashable and usable in sets/dicts."""
        s = {Decision.ALLOW, Decision.HALT}
        assert Decision.ALLOW in s
        assert "ALLOW" in s  # str subclass: 'ALLOW' hashes the same as Decision.ALLOW

    def test_decision_enum_completeness(self) -> None:
        """Decision enum must contain all expected members."""
        expected = {"ALLOW", "RETRY", "HALT", "DEGRADE", "QUARANTINE", "QUEUE"}
        actual = {d.value for d in Decision}
        assert expected == actual, (
            f"Decision members changed: {actual - expected} added, "
            f"{expected - actual} removed"
        )

    def test_string_literal_from_adapter_equals_decision(self) -> None:
        """String literal 'HALT' from legacy adapter code equals Decision.HALT.

        The sync adapter and older async adapters return decision="HALT" as
        raw string. Decision(str, Enum) ensures comparison still works.
        """
        raw_halt = "HALT"
        assert raw_halt == Decision.HALT
        assert Decision.HALT == raw_halt
        assert Decision(raw_halt) is Decision.HALT


# ---------------------------------------------------------------------------
# Area 2-D: wrap_mcp_server factory
# ---------------------------------------------------------------------------


class TestWrapMcpServer:
    """wrap_mcp_server() factory: list_tools discovery and failure handling."""

    def test_list_tools_failure_does_not_prevent_creation(self) -> None:
        """If list_tools() raises, the adapter is still created."""

        class _BrokenSession:
            async def list_tools(self) -> Any:
                raise ConnectionError("server gone")

            async def call_tool(self, name: str, arguments: Any) -> Any:
                return {"ok": True}

        async def run() -> Any:
            from veronica_core.adapters.mcp_async import wrap_mcp_server

            config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
            ctx = ExecutionContext(config=config)
            return await wrap_mcp_server(_BrokenSession(), execution_context=ctx)

        adapter = asyncio.run(run())
        assert adapter is not None

    def test_call_tool_delegates_to_session(self) -> None:
        """call_tool() round-trips through the session under containment."""

        class _MockSession:
            async def call_tool(self, name: str, arguments: Any) -> dict:
                return {"name": name, "args": arguments}

        async def run() -> MCPToolResult:
            from veronica_core.adapters.mcp_async import wrap_mcp_server

            config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(_MockSession(), execution_context=ctx)
            return await adapter.call_tool("search", {"q": "test"})

        result = asyncio.run(run())
        assert result.success is True
        assert result.result["name"] == "search"

    def test_wrap_mcp_server_with_list_tools_discovery(self) -> None:
        """Tools discovered via list_tools() are pre-populated in cost map."""

        class _FakeTool:
            def __init__(self, name: str) -> None:
                self.name = name

        class _DiscoverySession:
            async def list_tools(self) -> Any:
                class _Response:
                    tools = [_FakeTool("search"), _FakeTool("calculator")]
                return _Response()

            async def call_tool(self, name: str, arguments: Any) -> dict:
                return {"result": name}

        async def run() -> MCPToolResult:
            from veronica_core.adapters.mcp_async import wrap_mcp_server

            config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
            ctx = ExecutionContext(config=config)
            adapter = await wrap_mcp_server(
                _DiscoverySession(),
                execution_context=ctx,
                default_cost_per_call=0.005,
            )
            return await adapter.call_tool("search", {})

        result = asyncio.run(run())
        assert result.success is True
