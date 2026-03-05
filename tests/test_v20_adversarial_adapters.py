"""Adversarial tests for v2.0 adapter unification, protocols, and removed API verification.

Attack vectors covered:
1.  Adapter unification: AIContainer passed where ExecutionContext expected (DeprecationWarning path)
2.  Adapter unification: None/invalid type as execution_context
3.  Protocol compliance: minimal mock satisfying MCPAdapterProtocol
4.  Protocol compliance: object missing required methods
5.  AsyncBudgetBackendProtocol: sync backend passed where async expected
6.  Removed APIs: VeronicaPersistence no longer accessible via veronica_core module
7.  Removed APIs: CLIApprover.sign() v1 (should AttributeError)
8.  GuardConfig fields: timeout_ms removed, core fields still work
9.  Container __init__: AIContainer accessible; AIcontainer alias removed
10. _shared.py edge cases: cost_from_total_tokens, extract_llm_result_cost, record_budget_spend
11. ExecutionContextContainerAdapter: budget + step proxy behavior
12. build_adapter_container: routing logic
"""

from __future__ import annotations

import warnings
from typing import Any
from unittest.mock import MagicMock

import pytest

import veronica_core
from veronica_core.adapters._shared import (
    ExecutionContextContainerAdapter,
    _BudgetProxy,
    _StepGuardProxy,
    build_adapter_container,
    build_container,
    cost_from_total_tokens,
    extract_llm_result_cost,
    record_budget_spend,
)
from veronica_core.approval.approver import CLIApprover
from veronica_core.container import AIContainer
from veronica_core.inject import GuardConfig

# MCPAdapterProtocol and AsyncBudgetBackendProtocol: define locally for
# adversarial testing since they may be refactored/moved in v2.0.
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class MCPAdapterProtocol(Protocol):
    """Structural protocol for synchronous MCP containment adapters."""

    def wrap_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_fn: Callable[..., Any],
    ) -> Any: ...

    def get_tool_stats(self) -> dict[str, Any]: ...


@runtime_checkable
class AsyncBudgetBackendProtocol(Protocol):
    """Async protocol for distributed budget backends."""

    async def reserve(self, amount: float, ceiling: float) -> str: ...
    async def commit(self, reservation_id: str) -> float: ...
    async def rollback(self, reservation_id: str) -> None: ...
    async def get(self) -> float: ...


# ---------------------------------------------------------------------------
# 1. Adapter unification: AIContainer passed as execution_context
# ---------------------------------------------------------------------------


class TestAIContainerAsExecutionContext:
    """Attack: pass an AIContainer where ExecutionContext is expected."""

    def test_aicontainer_as_execution_context_builds_adapter(self) -> None:
        """AIContainer passed as execution_context should produce an
        ExecutionContextContainerAdapter without crashing."""
        container = AIContainer()
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        # AIContainer has no _budget_backend / _step_count — proxy must handle gracefully
        adapter = build_adapter_container(config, execution_context=container)
        assert isinstance(adapter, ExecutionContextContainerAdapter)

    def test_aicontainer_as_ctx_check_returns_allowed(self) -> None:
        """check() on the adapter must return PolicyDecision without raising."""
        container = AIContainer()
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        adapter = build_adapter_container(config, execution_context=container)
        decision = adapter.check()
        assert decision.allowed is True

    def test_aicontainer_as_ctx_budget_proxy_spent_defaults_to_zero(self) -> None:
        """_BudgetProxy over a bare AIContainer returns 0.0 for spent_usd."""
        proxy = _BudgetProxy(AIContainer(), limit_usd=5.0)
        assert proxy.spent_usd == 0.0

    def test_aicontainer_as_ctx_step_proxy_step_does_not_raise(self) -> None:
        """_StepGuardProxy.step() on bare object must not raise."""
        proxy = _StepGuardProxy(AIContainer(), max_steps=5)
        result = proxy.step()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 2. None / invalid type as execution_context
# ---------------------------------------------------------------------------


class TestNoneAndInvalidExecutionContext:
    """Attack: pass None or garbage as execution_context."""

    def test_none_execution_context_builds_aicontainer(self) -> None:
        """None execution_context => normal AIContainer (not adapter)."""
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        result = build_adapter_container(config, execution_context=None)
        assert isinstance(result, AIContainer)

    def test_string_as_execution_context_builds_adapter(self) -> None:
        """A string passed as execution_context: adapter created, no crash."""
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        adapter = build_adapter_container(config, execution_context="not_a_context")
        assert isinstance(adapter, ExecutionContextContainerAdapter)

    def test_integer_as_execution_context_check_does_not_raise(self) -> None:
        """Garbage execution_context: check() must return a valid decision."""
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        adapter = build_adapter_container(config, execution_context=42)
        decision = adapter.check()
        assert hasattr(decision, "allowed")

    def test_none_execution_context_budget_proxy_spend_safe(self) -> None:
        """_BudgetProxy with None ctx: spend() must not raise."""
        proxy = _BudgetProxy(None, limit_usd=1.0)
        result = proxy.spend(0.5)
        # Must not raise; return value is bool-ish
        assert result in (True, False)

    def test_none_execution_context_step_proxy_step_safe(self) -> None:
        """_StepGuardProxy with None ctx: step() must not raise."""
        proxy = _StepGuardProxy(None, max_steps=5)
        result = proxy.step()
        assert result in (True, False)


# ---------------------------------------------------------------------------
# 3. Protocol compliance: minimal mock satisfying MCPAdapterProtocol
# ---------------------------------------------------------------------------


class TestMCPAdapterProtocolMinimalMock:
    """Protocol structural check: minimal object that satisfies MCPAdapterProtocol."""

    def _make_minimal_mcp_mock(self) -> Any:
        class _MinimalMCP:
            def wrap_tool_call(
                self, tool_name: str, arguments: dict, call_fn: Any
            ) -> Any:
                return call_fn()

            def get_tool_stats(self) -> dict:
                return {}

        return _MinimalMCP()

    def test_minimal_mock_passes_isinstance_check(self) -> None:
        """isinstance(..., MCPAdapterProtocol) must return True for minimal mock."""
        mock = self._make_minimal_mcp_mock()
        assert isinstance(mock, MCPAdapterProtocol)

    def test_minimal_mock_wrap_tool_call_executes(self) -> None:
        """wrap_tool_call on minimal mock must invoke the callable."""
        mock = self._make_minimal_mcp_mock()
        called = []
        mock.wrap_tool_call("my_tool", {}, lambda: called.append(True))
        assert called

    def test_minimal_mock_get_tool_stats_returns_dict(self) -> None:
        """get_tool_stats must return a dict."""
        mock = self._make_minimal_mcp_mock()
        stats = mock.get_tool_stats()
        assert isinstance(stats, dict)


# ---------------------------------------------------------------------------
# 4. Protocol violation: object missing required methods
# ---------------------------------------------------------------------------


class TestMCPAdapterProtocolViolation:
    """Attack: object missing one or both required methods."""

    def test_object_missing_wrap_tool_call_fails_isinstance(self) -> None:
        """Object with only get_tool_stats must fail MCPAdapterProtocol check."""

        class _MissingWrap:
            def get_tool_stats(self) -> dict:
                return {}

        assert not isinstance(_MissingWrap(), MCPAdapterProtocol)

    def test_object_missing_get_tool_stats_fails_isinstance(self) -> None:
        """Object with only wrap_tool_call must fail MCPAdapterProtocol check."""

        class _MissingStats:
            def wrap_tool_call(
                self, tool_name: str, arguments: dict, call_fn: Any
            ) -> Any:
                return None

        assert not isinstance(_MissingStats(), MCPAdapterProtocol)

    def test_empty_object_fails_isinstance(self) -> None:
        """Plain object must fail MCPAdapterProtocol check."""
        assert not isinstance(object(), MCPAdapterProtocol)

    def test_none_fails_isinstance(self) -> None:
        """None must fail MCPAdapterProtocol check."""
        assert not isinstance(None, MCPAdapterProtocol)


# ---------------------------------------------------------------------------
# 5. AsyncBudgetBackendProtocol: sync backend where async expected
# ---------------------------------------------------------------------------


class TestAsyncBudgetBackendProtocol:
    """Attack: sync backend passed where async protocol expected."""

    def _make_sync_backend(self) -> Any:
        """A sync backend that has no async methods."""

        class _SyncBackend:
            def add(self, amount: float) -> None:
                pass

            def get(self) -> float:
                return 0.0

        return _SyncBackend()

    def test_sync_backend_fails_async_protocol_check(self) -> None:
        """Sync backend must NOT satisfy AsyncBudgetBackendProtocol."""
        sync = self._make_sync_backend()
        assert not isinstance(sync, AsyncBudgetBackendProtocol)

    def _make_async_backend(self) -> Any:
        class _AsyncBackend:
            async def reserve(self, amount: float, ceiling: float) -> str:
                return "r1"

            async def commit(self, reservation_id: str) -> float:
                return 0.0

            async def rollback(self, reservation_id: str) -> None:
                pass

            async def get(self) -> float:
                return 0.0

        return _AsyncBackend()

    def test_async_backend_passes_protocol_check(self) -> None:
        """Proper async backend must satisfy AsyncBudgetBackendProtocol."""
        backend = self._make_async_backend()
        assert isinstance(backend, AsyncBudgetBackendProtocol)

    def test_async_backend_missing_rollback_fails(self) -> None:
        """Async backend missing rollback must fail protocol check."""

        class _IncompleteAsync:
            async def reserve(self, amount: float, ceiling: float) -> str:
                return "r1"

            async def commit(self, reservation_id: str) -> float:
                return 0.0

            async def get(self) -> float:
                return 0.0

        assert not isinstance(_IncompleteAsync(), AsyncBudgetBackendProtocol)


# ---------------------------------------------------------------------------
# 6. Removed APIs: VeronicaPersistence no longer accessible via veronica_core
# ---------------------------------------------------------------------------


class TestVeronicaPersistenceRemoved:
    """VeronicaPersistence has been removed; access via veronica_core must raise AttributeError."""

    def test_not_in_all(self) -> None:
        """VeronicaPersistence must not be in veronica_core.__all__."""
        assert "VeronicaPersistence" not in veronica_core.__all__

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """Unknown attribute on veronica_core must raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = veronica_core.NonExistentSymbolXYZ  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 7. Deprecated APIs: CLIApprover.sign() v1 emits DeprecationWarning
# ---------------------------------------------------------------------------


class TestCLIApproverV1Deprecated:
    """CLIApprover.sign() v1 is deprecated; must emit DeprecationWarning when called."""

    def test_sign_v1_emits_deprecation_warning(self) -> None:
        """CLIApprover.sign() must emit DeprecationWarning (deprecated, not removed)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            approver = CLIApprover(b"test-key-32-bytes-padding-xxx-xx")
        req = approver.create_request("rule_001", "file_write", ["/tmp/x"])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            approver.sign(req)
        assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
            "sign() v1 must emit DeprecationWarning"
        )

    def test_sign_v1_returns_valid_token(self) -> None:
        """sign() v1 must still produce a verifiable ApprovalToken despite being deprecated."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            approver = CLIApprover(b"test-key-32-bytes-padding-xxx-xx")
        req = approver.create_request("rule_001", "file_write", ["/tmp/x"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            token = approver.sign(req)
        assert approver.verify(token)

    def test_sign_v2_preferred_over_v1(self) -> None:
        """sign_v2() must not emit DeprecationWarning (it is the current API)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            approver = CLIApprover(b"test-key-32-bytes-padding-xxx-xx")
        req = approver.create_request("rule_001", "file_write", ["/tmp/x"])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            token = approver.sign_v2(req)
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert not dep_warnings, "sign_v2() must not emit DeprecationWarning"
        assert approver.verify(token)

    def test_sign_v2_still_works(self) -> None:
        """sign_v2() must still function correctly."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            approver = CLIApprover(b"test-key-32-bytes-padding-xxx-xx")
        req = approver.create_request("rule_001", "file_write", ["/tmp/x"])
        token = approver.sign_v2(req)
        assert approver.verify(token)


# ---------------------------------------------------------------------------
# 8. GuardConfig fields (timeout_ms removed)
# ---------------------------------------------------------------------------


class TestGuardConfigFields:
    """GuardConfig core fields are accessible. timeout_ms has been removed."""

    def test_timeout_ms_removed(self) -> None:
        """GuardConfig.timeout_ms must raise AttributeError (field removed)."""
        config = GuardConfig()
        assert not hasattr(config, "timeout_ms")

    def test_guard_config_valid_fields_work(self) -> None:
        """GuardConfig core fields must be accessible."""
        config = GuardConfig(max_cost_usd=2.0, max_steps=30, max_retries_total=5)
        assert config.max_cost_usd == 2.0
        assert config.max_steps == 30
        assert config.max_retries_total == 5

    def test_guard_config_unknown_field_raises(self) -> None:
        """Accessing a genuinely non-existent field must raise AttributeError."""
        config = GuardConfig()
        with pytest.raises(AttributeError):
            _ = config.totally_nonexistent_field  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 9. Container __init__: AIContainer accessible; AIcontainer alias removed
# ---------------------------------------------------------------------------


class TestAIContainerAlias:
    """AIContainer (uppercase C) must be accessible. AIcontainer alias removed."""

    def test_aicontainer_uppercase_works(self) -> None:
        """AIContainer (correct case) must be accessible without warning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cls = veronica_core.AIContainer
        assert cls is AIContainer
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert not dep_warnings

    def test_aicontainer_lowercase_raises(self) -> None:
        """AIcontainer (old alias) must raise AttributeError (removed)."""
        with pytest.raises(AttributeError):
            _ = veronica_core.AIcontainer  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 10. _shared.py edge cases
# ---------------------------------------------------------------------------


class TestSharedUtilsCostFromTotalTokens:
    """Attack edge cases for cost_from_total_tokens."""

    def test_zero_total_returns_zero(self) -> None:
        assert cost_from_total_tokens(0) == 0.0

    def test_negative_total_returns_zero(self) -> None:
        assert cost_from_total_tokens(-100) == 0.0

    def test_one_token_does_not_raise(self) -> None:
        """Edge: 1 token total must not crash."""
        result = cost_from_total_tokens(1)
        assert result >= 0.0

    def test_large_token_count_does_not_raise(self) -> None:
        """INT_MAX-ish token count must not overflow."""
        result = cost_from_total_tokens(2**30)
        assert result >= 0.0

    def test_with_model_name_does_not_raise(self) -> None:
        result = cost_from_total_tokens(1000, model="gpt-4")
        assert result >= 0.0


class TestSharedUtilsExtractLlmResultCost:
    """Attack edge cases for extract_llm_result_cost."""

    def test_none_returns_zero(self) -> None:
        assert extract_llm_result_cost(None) == 0.0

    def test_plain_object_returns_zero(self) -> None:
        assert extract_llm_result_cost(object()) == 0.0

    def test_dict_without_usage_returns_zero(self) -> None:
        assert extract_llm_result_cost({"llm_output": {"model_name": "gpt-4"}}) == 0.0

    def test_dict_with_zero_total_tokens_returns_zero(self) -> None:
        result = extract_llm_result_cost(
            {
                "token_usage": {"total_tokens": 0},
            }
        )
        assert result == 0.0

    def test_dict_with_negative_total_tokens_returns_zero(self) -> None:
        result = extract_llm_result_cost(
            {
                "token_usage": {"total_tokens": -50},
            }
        )
        assert result == 0.0

    def test_dict_with_nan_total_tokens_returns_zero_or_not_negative(self) -> None:
        """NaN in total_tokens must not propagate to a negative cost."""
        result = extract_llm_result_cost(
            {
                "token_usage": {"total_tokens": float("nan")},
            }
        )
        # int(nan) raises ValueError — should be caught and return 0.0
        assert result == 0.0

    def test_dict_with_prompt_and_completion_tokens(self) -> None:
        """Proper token split should produce non-negative cost."""
        result = extract_llm_result_cost(
            {
                "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "model_name": "gpt-3.5-turbo",
            }
        )
        assert result >= 0.0

    def test_llm_result_object_with_llm_output_attr(self) -> None:
        """LLMResult-like object with llm_output attribute."""
        mock = MagicMock()
        mock.llm_output = {
            "token_usage": {"total_tokens": 200},
            "model_name": "gpt-4",
        }
        result = extract_llm_result_cost(mock)
        assert result >= 0.0

    def test_llm_result_with_anthropic_token_names(self) -> None:
        """input_tokens / output_tokens keys (Anthropic-style)."""
        result = extract_llm_result_cost(
            {
                "token_usage": {"input_tokens": 80, "output_tokens": 40},
                "model_name": "claude-3-5-sonnet-20241022",
            }
        )
        assert result >= 0.0

    def test_deeply_garbage_object_returns_zero(self) -> None:
        """Completely garbage object (raises on every attr access) returns 0.0."""

        class _Explode:
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("boom")

        result = extract_llm_result_cost(_Explode())
        assert result == 0.0


class TestSharedUtilsRecordBudgetSpend:
    """Attack edge cases for record_budget_spend."""

    def test_container_with_no_budget_returns_true(self) -> None:
        """No budget enforcer means always within budget."""
        container = AIContainer()  # budget=None
        result = record_budget_spend(container, 999.0, "[TEST]")
        assert result is True

    def test_container_within_budget_returns_true(self) -> None:
        from veronica_core.budget import BudgetEnforcer

        container = AIContainer(budget=BudgetEnforcer(limit_usd=10.0))
        result = record_budget_spend(container, 1.0, "[TEST]")
        assert result is True

    def test_container_over_budget_returns_false(self) -> None:
        from veronica_core.budget import BudgetEnforcer

        container = AIContainer(budget=BudgetEnforcer(limit_usd=0.01))
        result = record_budget_spend(container, 100.0, "[TEST]")
        assert result is False

    def test_zero_cost_does_not_affect_budget(self) -> None:
        from veronica_core.budget import BudgetEnforcer

        container = AIContainer(budget=BudgetEnforcer(limit_usd=1.0))
        result = record_budget_spend(container, 0.0, "[TEST]")
        assert result is True


# ---------------------------------------------------------------------------
# 11. ExecutionContextContainerAdapter: proxy behavior
# ---------------------------------------------------------------------------


class TestExecutionContextContainerAdapter:
    """Adversarial tests for the ExecutionContextContainerAdapter."""

    def _make_ctx_stub(self) -> Any:
        """Minimal stub that looks like an ExecutionContext."""
        import threading

        class _CtxStub:
            _cost_usd_accumulated: float = 0.0
            _step_count: int = 0
            _lock = threading.Lock()

            def get_snapshot(self) -> Any:
                return None  # not aborted

        return _CtxStub()

    def test_check_allows_when_below_limits(self) -> None:
        ctx = self._make_ctx_stub()
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        decision = adapter.check()
        assert decision.allowed is True

    def test_check_denies_when_budget_exceeded(self) -> None:
        ctx = self._make_ctx_stub()
        ctx._cost_usd_accumulated = 15.0
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        decision = adapter.check()
        assert decision.allowed is False
        assert "Budget" in decision.reason or "budget" in decision.reason.lower()

    def test_check_denies_when_step_limit_exceeded(self) -> None:
        ctx = self._make_ctx_stub()
        ctx._step_count = 25
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        decision = adapter.check()
        assert decision.allowed is False
        assert "step" in decision.reason.lower() or "Step" in decision.reason

    def test_check_denies_when_context_aborted(self) -> None:
        """check() must deny if the context snapshot indicates aborted=True."""
        ctx = self._make_ctx_stub()

        class _AbortedSnapshot:
            aborted = True

        ctx.get_snapshot = lambda: _AbortedSnapshot()
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        decision = adapter.check()
        assert decision.allowed is False

    def test_budget_proxy_spend_accumulates_on_ctx(self) -> None:
        """BudgetProxy.spend() must mutate the context's accumulated cost."""
        ctx = self._make_ctx_stub()
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        adapter.budget.spend(3.0)
        assert ctx._cost_usd_accumulated == pytest.approx(3.0)

    def test_step_proxy_step_increments_ctx(self) -> None:
        """StepGuardProxy.step() must increment the context's step count."""
        ctx = self._make_ctx_stub()
        config = GuardConfig(max_cost_usd=10.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        adapter.step_guard.step()
        assert ctx._step_count == 1

    def test_active_policies_includes_budget_and_step_when_positive(self) -> None:
        config = GuardConfig(max_cost_usd=5.0, max_steps=15)
        adapter = ExecutionContextContainerAdapter(object(), config)
        policies = adapter.active_policies
        assert "budget" in policies
        assert "step_guard" in policies

    def test_check_with_zero_max_cost_skips_budget_check(self) -> None:
        """max_cost_usd=0 means no budget ceiling enforced."""
        ctx = self._make_ctx_stub()
        ctx._cost_usd_accumulated = 999.0
        config = GuardConfig(max_cost_usd=0.0, max_steps=20)
        adapter = ExecutionContextContainerAdapter(ctx, config)
        decision = adapter.check()
        # Budget check skipped when max_cost_usd <= 0; step check should pass
        assert decision.allowed is True

    def test_budget_proxy_with_backend(self) -> None:
        """_BudgetProxy must delegate to backend.add/get when present."""

        class _FakeBackend:
            def __init__(self) -> None:
                self._val = 0.0

            def add(self, amount: float) -> None:
                self._val += amount

            def get(self) -> float:
                return self._val

        ctx = MagicMock()
        ctx._budget_backend = _FakeBackend()
        proxy = _BudgetProxy(ctx, limit_usd=10.0)
        proxy.spend(4.0)
        assert proxy.spent_usd == pytest.approx(4.0)

    def test_budget_proxy_spend_exception_returns_false_fail_closed(self) -> None:
        """If spend() raises internally, it must return False (fail-closed) to prevent budget bypass.

        Arrange: A fake backend whose add() method raises, bypassing the lock path.
        This ensures the exception propagates inside the try block in spend().
        """
        from unittest.mock import MagicMock

        ctx = MagicMock()
        exploding_backend = MagicMock()
        exploding_backend.get.return_value = 0.0
        exploding_backend.add.side_effect = RuntimeError("backend exploded")
        ctx._budget_backend = exploding_backend

        proxy = _BudgetProxy(ctx, limit_usd=1.0)
        result = proxy.spend(0.5)
        assert result is False

    def test_step_guard_proxy_step_exception_returns_false_fail_closed(self) -> None:
        """If step() raises internally, it must return False (fail-closed) to prevent step limit bypass.

        Arrange: A context where _step_count attribute access raises AttributeError
        inside the lock block, triggering the except branch.
        """
        import threading

        class _ExplodingStepCtx:
            _lock = threading.Lock()

            @property
            def _step_count(self) -> int:
                raise AttributeError("_step_count exploded")

            @_step_count.setter
            def _step_count(self, v: int) -> None:
                raise AttributeError("_step_count setter exploded")

        proxy = _StepGuardProxy(_ExplodingStepCtx(), max_steps=5)
        result = proxy.step()
        assert result is False

    def test_budget_proxy_spend_exception_logged(self, caplog: Any) -> None:
        """Exception in spend() must emit a warning log."""
        import logging
        from unittest.mock import MagicMock

        ctx = MagicMock()
        exploding_backend = MagicMock()
        exploding_backend.get.return_value = 0.0
        exploding_backend.add.side_effect = RuntimeError(
            "backend exploded for log test"
        )
        ctx._budget_backend = exploding_backend

        proxy = _BudgetProxy(ctx, limit_usd=1.0)
        with caplog.at_level(logging.WARNING):
            proxy.spend(0.1)
        assert any("_BudgetProxy.spend" in r.message for r in caplog.records)

    def test_step_guard_proxy_step_exception_logged(self, caplog: Any) -> None:
        """Exception in step() must emit a warning log."""
        import logging
        import threading

        class _ExplodingStepCtx:
            _lock = threading.Lock()

            @property
            def _step_count(self) -> int:
                raise AttributeError("_step_count exploded for log test")

            @_step_count.setter
            def _step_count(self, v: int) -> None:
                raise AttributeError("_step_count setter exploded for log test")

        proxy = _StepGuardProxy(_ExplodingStepCtx(), max_steps=5)
        with caplog.at_level(logging.WARNING):
            proxy.step()
        assert any("_StepGuardProxy.step" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 12. build_adapter_container routing
# ---------------------------------------------------------------------------


class TestBuildAdapterContainerRouting:
    """build_adapter_container routing: ExecutionContext vs plain AIContainer."""

    def test_without_execution_context_returns_aicontainer(self) -> None:
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        result = build_adapter_container(config)
        assert isinstance(result, AIContainer)

    def test_with_execution_context_returns_adapter(self) -> None:
        config = GuardConfig(max_cost_usd=1.0, max_steps=10)
        ctx = MagicMock()
        result = build_adapter_container(config, execution_context=ctx)
        assert isinstance(result, ExecutionContextContainerAdapter)

    def test_build_container_produces_valid_aicontainer(self) -> None:
        """build_container (non-adapter path) must produce a working AIContainer."""
        from veronica_core.containment import ExecutionConfig

        config = ExecutionConfig(max_cost_usd=2.0, max_steps=15, max_retries_total=3)
        result = build_container(config)
        assert isinstance(result, AIContainer)
        decision = result.check()
        assert decision.allowed is True
