"""v3.0.3 audit coverage tests -- fills gaps found by Iron Legion parallel audit.

Tests:
1. URL exfil: semicolon query separator, matrix params, %2F per-segment unquote
2. URL exfil: backslash in userinfo (LOW risk)
3. ComplianceExporter HTTPS enforcement + allow_insecure_http bypass
4. AsyncMCPContainmentAdapter: sync/async budget backend dispatch via isawaitable
5. WSGI middleware: iterable close() on halt + exception logging at WARNING
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import pytest

from veronica_core.compliance.exporter import ComplianceExporter
from veronica_core.containment.execution_context import ExecutionConfig
from veronica_core.middleware import VeronicaWSGIMiddleware
from veronica_core.security.policy_rules import _check_data_exfil


class TestExfilSemicolonQuerySeparator:
    """Semicolons in query strings must be treated as parameter separators."""

    def test_semicolon_separated_base64_value_denied(self) -> None:
        """?a=1;secret=<base64> must be caught (semicolon = separator)."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        url = f"https://example.com/api?a=1;secret={b64}"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"

    def test_semicolon_only_query_base64_denied(self) -> None:
        """?token=<base64> using only semicolons as separators."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        url = f"https://example.com/api?token={b64};format=json"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"


class TestExfilMatrixParams:
    """Matrix parameters (;key=value) on path segments must be inspected."""

    def test_matrix_param_base64_on_middle_segment_denied(self) -> None:
        """Path like /api/resource;token=<base64>/action must be caught."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        url = f"https://example.com/api/resource;token={b64}/action"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"

    def test_matrix_param_hex_on_first_segment_denied(self) -> None:
        """Hex token in matrix param on first segment must be caught."""
        hex_token = "deadbeefcafebabe" * 3  # 48 hex chars
        url = f"https://example.com/data;id={hex_token}/view"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"

    def test_parsed_params_last_segment_denied(self) -> None:
        """urlparse puts params from last segment in parsed.params -- must be checked."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        # urlparse extracts params from the LAST path segment
        url = f"https://example.com/api/action;token={b64}"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"


class TestExfilPerSegmentUnquote:
    """Percent-encoded slashes (%2F) must not alter segment boundaries."""

    def test_percent_2f_does_not_split_segment(self) -> None:
        """Base64 with %2F embedded should still be caught as one token."""
        # A base64 value like "abc/def" URL-encoded as "abc%2Fdef"
        # If decoded before split, it becomes two short segments that evade detection
        raw_b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        # Insert a %2F that must NOT create a new segment boundary
        encoded_b64 = raw_b64[:10] + "%2F" + raw_b64[10:]
        url = f"https://example.com/exfil/{encoded_b64}"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"

    def test_double_encoded_percent_2f_in_path(self) -> None:
        """Double-encoded %252F should stay as %2F after one unquote."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        url = f"https://example.com/data/{b64}"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"


class TestExfilBackslashUserinfo:
    """Backslash in userinfo must not confuse hostname resolution."""

    def test_backslash_at_in_url_does_not_bypass(self) -> None:
        """https://evil.com\\@pypi.org/... -- ensure exfil check still runs."""
        b64 = base64.b64encode(b"SUPERSECRETAPIKEY123456").decode()
        # Even if hostname resolves to pypi.org, userinfo contains exfil data
        url = f"https://{b64}@example.com/path"
        decision = _check_data_exfil(url)
        assert decision is not None
        assert decision.verdict == "DENY"


class TestExfilSafeUrlsAllowed:
    """Normal URLs without suspicious content must not be denied."""

    def test_normal_url_returns_none(self) -> None:
        decision = _check_data_exfil("https://example.com/api/users?page=1&limit=10")
        assert decision is None

    def test_short_path_returns_none(self) -> None:
        decision = _check_data_exfil("https://example.com/a/b/c")
        assert decision is None


# ---------------------------------------------------------------------------
# 2. ComplianceExporter HTTPS enforcement
# ---------------------------------------------------------------------------


class TestComplianceExporterHTTPS:
    """HTTPS enforcement and allow_insecure_http bypass."""

    def test_http_non_local_raises_value_error(self) -> None:
        """http://remote.example.com must be rejected."""
        with pytest.raises(ValueError, match="HTTPS"):
            ComplianceExporter(
                api_key="test-key", endpoint="http://remote.example.com/ingest"
            )

    def test_https_accepted(self) -> None:
        """https:// endpoint must be accepted."""
        exporter = ComplianceExporter(
            api_key="test-key", endpoint="https://secure.example.com/ingest"
        )
        try:
            assert exporter._endpoint == "https://secure.example.com/ingest"
        finally:
            exporter.close()

    def test_http_localhost_accepted(self) -> None:
        """http://localhost is allowed without allow_insecure_http."""
        exporter = ComplianceExporter(
            api_key="test-key", endpoint="http://localhost:8080/ingest"
        )
        try:
            assert exporter._endpoint == "http://localhost:8080/ingest"
        finally:
            exporter.close()

    def test_http_127_0_0_1_accepted(self) -> None:
        """http://127.0.0.1 is allowed without allow_insecure_http."""
        exporter = ComplianceExporter(
            api_key="test-key", endpoint="http://127.0.0.1:8080/ingest"
        )
        try:
            assert "127.0.0.1" in exporter._endpoint
        finally:
            exporter.close()

    def test_allow_insecure_http_bypasses_check(self) -> None:
        """allow_insecure_http=True allows plain HTTP to remote hosts."""
        exporter = ComplianceExporter(
            api_key="test-key",
            endpoint="http://internal.corp.net/ingest",
            allow_insecure_http=True,
        )
        try:
            assert exporter._endpoint == "http://internal.corp.net/ingest"
        finally:
            exporter.close()

    def test_empty_endpoint_raises_value_error(self) -> None:
        """Empty endpoint must be rejected."""
        with pytest.raises(ValueError, match="explicit endpoint"):
            ComplianceExporter(api_key="test-key", endpoint="")

    def test_api_key_redacted_in_repr(self) -> None:
        """API key must not appear in repr."""
        exporter = ComplianceExporter(
            api_key="sk-supersecretkey123", endpoint="https://x.com/ingest"
        )
        try:
            r = repr(exporter)
            assert "supersecretkey123" not in r
            assert "sk-s..." in r
        finally:
            exporter.close()


# ---------------------------------------------------------------------------
# 3. AsyncMCPContainmentAdapter: sync/async budget backend dispatch
# ---------------------------------------------------------------------------


class TestAsyncMCPBudgetDispatch:
    """isawaitable() dispatch for sync and async budget backends."""

    def test_sync_budget_backend_reserve_commit(self) -> None:
        """Sync reserve()/commit() must work without await."""
        from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
        from veronica_core.adapters.mcp import MCPToolCost
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        class SyncBudget:
            def __init__(self) -> None:
                self.reserved: list[float] = []
                self.committed: list[str] = []

            def reserve(self, amount: float, chain_id: str = "") -> str:
                self.reserved.append(amount)
                return "res-001"

            def commit(self, reservation_id: str) -> None:
                self.committed.append(reservation_id)

            def rollback(self, reservation_id: str) -> None:
                pass

        budget = SyncBudget()
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)
        # Inject sync budget backend
        ctx._budget_backend = budget  # type: ignore[attr-defined]

        adapter = AsyncMCPContainmentAdapter(
            execution_context=ctx,
            tool_costs={
                "expensive_tool": MCPToolCost(
                    tool_name="expensive_tool", cost_per_call=0.5
                )
            },
        )

        async def _tool(**kwargs: Any) -> str:
            return "result"

        result = asyncio.run(adapter.wrap_tool_call("expensive_tool", {}, _tool))
        assert result.success
        assert len(budget.reserved) == 1
        assert budget.reserved[0] == pytest.approx(0.5)
        assert budget.committed == ["res-001"]

    def test_async_budget_backend_reserve_commit(self) -> None:
        """Async reserve()/commit() must be awaited."""
        from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
        from veronica_core.adapters.mcp import MCPToolCost
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        class AsyncBudget:
            def __init__(self) -> None:
                self.reserved: list[float] = []
                self.committed: list[str] = []

            async def reserve(self, amount: float, chain_id: str = "") -> str:
                self.reserved.append(amount)
                return "async-res-001"

            async def commit(self, reservation_id: str) -> None:
                self.committed.append(reservation_id)

            async def rollback(self, reservation_id: str) -> None:
                pass

        budget = AsyncBudget()
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = budget  # type: ignore[attr-defined]

        adapter = AsyncMCPContainmentAdapter(
            execution_context=ctx,
            tool_costs={
                "expensive_tool": MCPToolCost(
                    tool_name="expensive_tool", cost_per_call=0.5
                )
            },
        )

        async def _tool(**kwargs: Any) -> str:
            return "result"

        result = asyncio.run(adapter.wrap_tool_call("expensive_tool", {}, _tool))
        assert result.success
        assert len(budget.reserved) == 1
        assert budget.committed == ["async-res-001"]

    def test_sync_budget_backend_rollback_on_failure(self) -> None:
        """Sync rollback() called when tool raises."""
        from veronica_core.adapters.mcp_async import AsyncMCPContainmentAdapter
        from veronica_core.adapters.mcp import MCPToolCost
        from veronica_core.containment.execution_context import (
            ExecutionConfig,
            ExecutionContext,
        )

        class SyncBudget:
            def __init__(self) -> None:
                self.rolled_back: list[str] = []

            def reserve(self, amount: float, chain_id: str = "") -> str:
                return "res-fail"

            def commit(self, reservation_id: str) -> None:
                pass

            def rollback(self, reservation_id: str) -> None:
                self.rolled_back.append(reservation_id)

        budget = SyncBudget()
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=5)
        ctx = ExecutionContext(config=config)
        ctx._budget_backend = budget  # type: ignore[attr-defined]

        adapter = AsyncMCPContainmentAdapter(
            execution_context=ctx,
            tool_costs={
                "fail_tool": MCPToolCost(tool_name="fail_tool", cost_per_call=0.5)
            },
        )

        async def _failing_tool(**kwargs: Any) -> str:
            raise RuntimeError("boom")

        result = asyncio.run(adapter.wrap_tool_call("fail_tool", {}, _failing_tool))
        assert not result.success
        assert "res-fail" in budget.rolled_back


# ---------------------------------------------------------------------------
# 4. WSGI middleware: iterable close() + exception logging
# ---------------------------------------------------------------------------


class TestWSGIIterableCloseOnHalt:
    """WSGI iterable must be closed when context is halted post-flight."""

    def _make_config(self, *, max_steps: int = 100) -> ExecutionConfig:
        return ExecutionConfig(
            max_cost_usd=100.0, max_steps=max_steps, max_retries_total=10
        )

    def test_iterable_close_called_on_halt(self) -> None:
        """When app returns iterable and context is aborted, close() must be called."""
        close_called: list[bool] = []

        class _ClosableIterable:
            def __iter__(self):
                return iter([b"data"])

            def close(self):
                close_called.append(True)

        def _app(environ: dict[str, Any], start_response: Any):
            # Do NOT call start_response -- so response_started stays False
            ctx = environ.get("veronica.context")
            if ctx is not None:
                ctx.abort("force halt")
            return _ClosableIterable()

        middleware = VeronicaWSGIMiddleware(_app, config=self._make_config())
        status_holder: list[str] = []

        def _start_response(status: str, headers: list) -> None:
            status_holder.append(status)

        list(middleware({}, _start_response))
        assert "429" in status_holder[0]
        assert close_called == [True], "iterable.close() must be called before 429"

    def test_exception_logged_at_warning_on_halt(self, caplog: Any) -> None:
        """When app raises and context is halted, exception is logged at WARNING."""

        def _app(environ: dict[str, Any], start_response: Any):
            ctx = environ.get("veronica.context")
            if ctx is not None:
                ctx.abort("force halt")
            raise ValueError("app crashed")

        middleware = VeronicaWSGIMiddleware(_app, config=self._make_config())
        status_holder: list[str] = []

        def _start_response(status: str, headers: list) -> None:
            status_holder.append(status)

        with caplog.at_level(logging.WARNING, logger="veronica_core.middleware"):
            list(middleware({}, _start_response))

        assert "429" in status_holder[0]
        assert any("ValueError" in record.message for record in caplog.records), (
            "WARNING log must mention the exception type"
        )

    def test_exception_propagates_when_not_halted(self) -> None:
        """When app raises and context is NOT halted, exception propagates."""

        def _app(environ: dict[str, Any], start_response: Any):
            raise RuntimeError("unrelated crash")

        middleware = VeronicaWSGIMiddleware(_app, config=self._make_config())

        def _start_response(status: str, headers: list) -> None:
            pass

        with pytest.raises(RuntimeError, match="unrelated crash"):
            list(middleware({}, _start_response))
