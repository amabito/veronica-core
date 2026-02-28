"""Tests for veronica_core.adapters.crewai — CrewAI event listener adapter.

Uses fake crewai stubs injected into sys.modules so crewai does not need to
be installed in the test environment. The real adapter code is re-imported
after the stubs are in place.
"""
from __future__ import annotations

import importlib
import math
import sys
import types
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Build and inject fake crewai stubs BEFORE importing the adapter
# ---------------------------------------------------------------------------


def _build_fake_crewai() -> tuple[type, type, type, Any]:
    """Create minimal crewai stubs and register them in sys.modules.

    Returns (FakeLLMCallStartedEvent, FakeLLMCallCompletedEvent,
             FakeLLMCallFailedEvent) for use in test helpers.
    """
    # ---- base event ----
    class FakeBaseEvent:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    # ---- LLM events ----
    class FakeLLMCallStartedEvent(FakeBaseEvent):
        type = "llm_call_started"

        def __init__(self, call_id: str = "", model: str | None = None, **kw: Any) -> None:
            self.call_id = call_id
            self.model = model
            super().__init__(**kw)

    class FakeLLMCallCompletedEvent(FakeBaseEvent):
        type = "llm_call_completed"

        def __init__(
            self,
            call_id: str = "",
            model: str | None = None,
            response: Any = None,
            **kw: Any,
        ) -> None:
            self.call_id = call_id
            self.model = model
            self.response = response
            super().__init__(**kw)

    class FakeLLMCallFailedEvent(FakeBaseEvent):
        type = "llm_call_failed"

        def __init__(self, call_id: str = "", error: str = "", **kw: Any) -> None:
            self.call_id = call_id
            self.error = error
            super().__init__(**kw)

    # ---- event bus ----
    class FakeEventBus:
        """Minimal CrewAI event bus stub."""

        def __init__(self) -> None:
            self._handlers: dict[type, list] = {}

        def on(self, event_type: type):  # noqa: ANN001
            """Decorator that registers a handler for event_type."""
            def decorator(fn):  # noqa: ANN001
                self._handlers.setdefault(event_type, []).append(fn)
                return fn
            return decorator

        def emit(self, source: Any, event: FakeBaseEvent) -> None:
            """Synchronously invoke all registered handlers for the event type."""
            for handler in self._handlers.get(type(event), []):
                handler(source, event)

        def validate_dependencies(self) -> None:
            pass

    fake_event_bus = FakeEventBus()

    # ---- BaseEventListener ----
    class FakeBaseEventListener:
        """Minimal BaseEventListener stub that calls setup_listeners on init."""

        def __init__(self) -> None:
            self.setup_listeners(fake_event_bus)
            fake_event_bus.validate_dependencies()

        def setup_listeners(self, bus: FakeEventBus) -> None:
            raise NotImplementedError

    # ---- Wire up modules ----
    crewai_mod = types.ModuleType("crewai")
    crewai_events_mod = types.ModuleType("crewai.events")
    crewai_events_mod.BaseEventListener = FakeBaseEventListener
    crewai_events_mod.crewai_event_bus = fake_event_bus
    crewai_events_mod.BaseEvent = FakeBaseEvent

    crewai_events_types_mod = types.ModuleType("crewai.events.types")
    crewai_events_llm_mod = types.ModuleType("crewai.events.types.llm_events")
    crewai_events_llm_mod.LLMCallStartedEvent = FakeLLMCallStartedEvent
    crewai_events_llm_mod.LLMCallCompletedEvent = FakeLLMCallCompletedEvent
    crewai_events_llm_mod.LLMCallFailedEvent = FakeLLMCallFailedEvent

    sys.modules.update(
        {
            "crewai": crewai_mod,
            "crewai.events": crewai_events_mod,
            "crewai.events.types": crewai_events_types_mod,
            "crewai.events.types.llm_events": crewai_events_llm_mod,
        }
    )
    return FakeLLMCallStartedEvent, FakeLLMCallCompletedEvent, FakeLLMCallFailedEvent, fake_event_bus


(
    FakeLLMCallStartedEvent,
    FakeLLMCallCompletedEvent,
    FakeLLMCallFailedEvent,
    _fake_event_bus,
) = _build_fake_crewai()

# ---------------------------------------------------------------------------
# Now safe to import the adapter
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veronica_core import GuardConfig  # noqa: E402
from veronica_core.adapters.crewai import VeronicaCrewAIListener, _estimate_cost  # noqa: E402
from veronica_core.containment import ExecutionConfig  # noqa: E402
from veronica_core.inject import VeronicaHalt  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_event_bus_handlers():
    """Clear accumulated handlers before each test to ensure isolation.

    Without this, each _make_listener() call registers new handlers on
    the global _fake_event_bus, causing handlers from prior tests to fire
    on subsequent emits.
    """
    _fake_event_bus._handlers.clear()
    yield
    _fake_event_bus._handlers.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_listener(
    max_cost_usd: float = 10.0,
    max_steps: int = 20,
    max_retries_total: int = 3,
) -> VeronicaCrewAIListener:
    return VeronicaCrewAIListener(
        GuardConfig(
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_retries_total=max_retries_total,
        )
    )


def _started_event(model: str | None = None) -> FakeLLMCallStartedEvent:
    return FakeLLMCallStartedEvent(call_id=str(uuid.uuid4()), model=model)


def _completed_event(response: Any = None, model: str | None = None) -> FakeLLMCallCompletedEvent:
    return FakeLLMCallCompletedEvent(
        call_id=str(uuid.uuid4()),
        model=model,
        response=response,
    )


def _failed_event(error: str = "timeout") -> FakeLLMCallFailedEvent:
    return FakeLLMCallFailedEvent(call_id=str(uuid.uuid4()), error=error)


def _response_with_total(total_tokens: int) -> Any:
    """Build a minimal response object with total_tokens in usage."""

    class FakeUsage:
        def __init__(self, total: int) -> None:
            self.total_tokens = total

    class FakeResponse:
        def __init__(self, total: int) -> None:
            self.usage = FakeUsage(total)

    return FakeResponse(total_tokens)


def _response_with_prompt_completion(prompt: int, completion: int) -> Any:
    """Build a response object with explicit prompt/completion tokens."""

    class FakeUsage:
        def __init__(self, p: int, c: int) -> None:
            self.prompt_tokens = p
            self.completion_tokens = c

    class FakeResponse:
        def __init__(self, p: int, c: int) -> None:
            self.usage = FakeUsage(p, c)

    return FakeResponse(prompt, completion)


# ---------------------------------------------------------------------------
# Allow path — event bus fires, state is recorded
# ---------------------------------------------------------------------------


class TestAllowPath:
    def test_llm_call_started_within_limits_does_not_raise(self) -> None:
        """LLMCallStartedEvent within policy limits: no exception raised."""
        _make_listener()  # registers handlers on fake bus
        # Emit via fake bus — should not raise
        _fake_event_bus.emit(None, _started_event())

    def test_llm_call_completed_increments_step_counter(self) -> None:
        """LLMCallCompletedEvent: step_guard.current_step increments by 1."""
        listener = _make_listener()
        assert listener.container.step_guard.current_step == 0
        _fake_event_bus.emit(None, _completed_event())
        assert listener.container.step_guard.current_step == 1

    def test_llm_call_completed_multiple_increments(self) -> None:
        """Multiple LLMCallCompletedEvents accumulate step count."""
        listener = _make_listener()
        _fake_event_bus.emit(None, _completed_event())
        _fake_event_bus.emit(None, _completed_event())
        assert listener.container.step_guard.current_step == 2

    def test_llm_call_completed_records_token_cost(self) -> None:
        """LLMCallCompletedEvent: budget.spend() is called with estimated cost.

        1000 total_tokens, 75/25 split, unknown model pricing:
          750 * 0.030/1000 + 250 * 0.060/1000 = 0.0225 + 0.0150 = 0.0375
        """
        listener = _make_listener(max_cost_usd=10.0)
        _fake_event_bus.emit(None, _completed_event(response=_response_with_total(1000)))
        assert listener.container.budget.call_count == 1
        assert listener.container.budget.spent_usd == pytest.approx(0.0375)

    def test_llm_call_completed_zero_cost_when_no_usage(self) -> None:
        """LLMCallCompletedEvent: spend(0.0) when response has no usage."""
        listener = _make_listener()
        _fake_event_bus.emit(None, _completed_event(response=None))
        assert listener.container.budget.spent_usd == 0.0

    def test_llm_call_failed_does_not_raise_or_charge_budget(self) -> None:
        """LLMCallFailedEvent: logs but does not raise or charge budget."""
        listener = _make_listener()
        _fake_event_bus.emit(None, _failed_event())  # must not raise
        assert listener.container.budget.spent_usd == 0.0

    def test_check_or_raise_within_limits_does_not_raise(self) -> None:
        """check_or_raise(): no exception when policies allow."""
        listener = _make_listener()
        listener.check_or_raise()  # must not raise

    def test_check_or_raise_accepts_arbitrary_args(self) -> None:
        """check_or_raise() accepts *args/**kwargs for step_callback compatibility."""
        listener = _make_listener()
        listener.check_or_raise("some_step", key="value")  # must not raise


# ---------------------------------------------------------------------------
# Deny path — check_or_raise() must raise VeronicaHalt
# ---------------------------------------------------------------------------


class TestDenyPath:
    def test_step_limit_raises_veronica_halt(self) -> None:
        """check_or_raise(): raises VeronicaHalt when step limit is exhausted."""
        listener = _make_listener(max_steps=1)
        # Exhaust via completed events
        _fake_event_bus.emit(None, _completed_event())  # step = 1
        # Now check_or_raise should deny
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            listener.check_or_raise()

    def test_budget_exhausted_raises_veronica_halt(self) -> None:
        """check_or_raise(): raises VeronicaHalt when budget is pre-exhausted."""
        listener = _make_listener(max_cost_usd=1.0)
        listener.container.budget.spend(2.0)  # exhaust manually
        with pytest.raises(VeronicaHalt, match="[Bb]udget"):
            listener.check_or_raise()

    def test_veronica_halt_carries_decision(self) -> None:
        """VeronicaHalt from deny path carries a PolicyDecision."""
        listener = _make_listener(max_cost_usd=0.0)
        listener.container.budget.spend(1.0)
        with pytest.raises(VeronicaHalt) as exc_info:
            listener.check_or_raise()
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed


# ---------------------------------------------------------------------------
# Config acceptance
# ---------------------------------------------------------------------------


class TestConfigAcceptance:
    def test_accepts_guard_config(self) -> None:
        """VeronicaCrewAIListener accepts GuardConfig."""
        cfg = GuardConfig(max_cost_usd=5.0, max_steps=10, max_retries_total=3)
        listener = VeronicaCrewAIListener(cfg)
        assert listener.container.budget.limit_usd == 5.0
        assert listener.container.step_guard.max_steps == 10

    def test_accepts_execution_config(self) -> None:
        """VeronicaCrewAIListener accepts ExecutionConfig."""
        cfg = ExecutionConfig(max_cost_usd=3.0, max_steps=15, max_retries_total=5)
        listener = VeronicaCrewAIListener(cfg)
        assert listener.container.budget.limit_usd == 3.0
        assert listener.container.step_guard.max_steps == 15


# ---------------------------------------------------------------------------
# Cost estimation — _estimate_cost()
# ---------------------------------------------------------------------------


class TestCostEstimation:
    def test_cost_zero_when_no_response(self) -> None:
        """_estimate_cost: returns 0.0 when response is None."""
        event = _completed_event(response=None)
        assert _estimate_cost(event) == 0.0

    def test_cost_zero_when_no_usage(self) -> None:
        """_estimate_cost: returns 0.0 when response has no usage attribute."""

        class FakeResponseNoUsage:
            pass

        event = _completed_event(response=FakeResponseNoUsage())
        assert _estimate_cost(event) == 0.0

    def test_explicit_prompt_completion_tokens_used_directly(self) -> None:
        """_estimate_cost: uses prompt_tokens + completion_tokens directly."""
        event = _completed_event(response=_response_with_prompt_completion(800, 200))
        assert _estimate_cost(event) > 0.0

    def test_dict_response_with_usage(self) -> None:
        """_estimate_cost: handles dict response with 'usage' key."""
        event = _completed_event(response={"usage": {"total_tokens": 500}})
        assert _estimate_cost(event) > 0.0

    @pytest.mark.parametrize("total_tokens", [1, 2, 3, 100, 10_000])
    def test_cost_positive_for_various_total_tokens(self, total_tokens: int) -> None:
        """_estimate_cost: positive cost for total_tokens >= 1."""
        event = _completed_event(response=_response_with_total(total_tokens))
        result = _estimate_cost(event)
        assert result > 0.0, f"cost was {result} for total_tokens={total_tokens}"

    def test_input_output_token_keys_also_accepted(self) -> None:
        """_estimate_cost: handles Anthropic-style input_tokens/output_tokens."""

        class FakeAnthropicUsage:
            input_tokens = 800
            output_tokens = 200

        class FakeAnthropicResponse:
            usage = FakeAnthropicUsage()

        event = _completed_event(response=FakeAnthropicResponse())
        assert _estimate_cost(event) > 0.0


# ---------------------------------------------------------------------------
# Import error when crewai absent
# ---------------------------------------------------------------------------


class TestImportError:
    def test_raises_import_error_when_crewai_absent(self) -> None:
        """Importing the adapter without crewai raises a clear ImportError.

        Because crewai may be installed in the test environment, we block
        the crewai modules by inserting None sentinels into sys.modules
        (Python treats None entries as "not found" for import purposes).
        """
        adapter_key = "veronica_core.adapters.crewai"
        crewai_keys = [k for k in sys.modules if k.startswith("crewai")]
        saved_adapter = sys.modules.pop(adapter_key, None)
        saved_crewai = {k: sys.modules.pop(k) for k in crewai_keys}

        # Block all crewai imports by inserting None sentinels
        blocked_keys = ["crewai", "crewai.events", "crewai.events.types.llm_events"]
        for k in blocked_keys:
            sys.modules[k] = None  # type: ignore[assignment]

        try:
            with pytest.raises(ImportError, match="crewai"):
                importlib.import_module("veronica_core.adapters.crewai")
        finally:
            # Remove sentinels
            for k in blocked_keys:
                sys.modules.pop(k, None)
            # Restore originals
            if saved_adapter is not None:
                sys.modules[adapter_key] = saved_adapter
            sys.modules.update(saved_crewai)


# ---------------------------------------------------------------------------
# Adversarial tests — corrupted input, concurrent access, boundary abuse
# ---------------------------------------------------------------------------


class TestAdversarialCrewAI:
    """Adversarial tests for CrewAI adapter — attacker mindset."""

    # -- Corrupted input: garbage response objects --

    def test_corrupted_response_string_does_not_crash(self) -> None:
        """_estimate_cost: string response must not crash, returns 0.0."""
        event = _completed_event(response="not a response object")
        assert _estimate_cost(event) == 0.0

    def test_corrupted_response_int_does_not_crash(self) -> None:
        """_estimate_cost: int response must not crash."""
        event = _completed_event(response=42)
        assert _estimate_cost(event) == 0.0

    def test_corrupted_response_list_does_not_crash(self) -> None:
        """_estimate_cost: list response must not crash."""
        event = _completed_event(response=[1, 2, 3])
        assert _estimate_cost(event) == 0.0

    def test_corrupted_usage_with_nan_tokens(self) -> None:
        """_estimate_cost: NaN total_tokens must not produce NaN cost."""

        class NaNUsage:
            total_tokens = float("nan")

        class NaNResponse:
            usage = NaNUsage()

        event = _completed_event(response=NaNResponse())
        result = _estimate_cost(event)
        # Must not be NaN — either 0.0 or a finite number
        assert not math.isnan(result)

    def test_corrupted_usage_with_negative_tokens(self) -> None:
        """_estimate_cost: negative total_tokens must not produce negative cost."""

        class NegUsage:
            total_tokens = -500

        class NegResponse:
            usage = NegUsage()

        event = _completed_event(response=NegResponse())
        result = _estimate_cost(event)
        assert result >= 0.0

    def test_corrupted_usage_with_string_tokens(self) -> None:
        """_estimate_cost: string total_tokens must not crash."""

        class StrUsage:
            total_tokens = "not_a_number"

        class StrResponse:
            usage = StrUsage()

        event = _completed_event(response=StrResponse())
        # Should not raise; fallback to 0.0
        result = _estimate_cost(event)
        assert isinstance(result, float)

    def test_corrupted_usage_none_tokens(self) -> None:
        """_estimate_cost: usage with all None token fields returns 0.0."""

        class EmptyUsage:
            prompt_tokens = None
            completion_tokens = None
            total_tokens = None

        class EmptyResponse:
            usage = EmptyUsage()

        event = _completed_event(response=EmptyResponse())
        assert _estimate_cost(event) == 0.0

    def test_corrupted_dict_response_nested_garbage(self) -> None:
        """_estimate_cost: dict with non-dict usage value returns 0.0."""
        event = _completed_event(response={"usage": "garbage"})
        assert _estimate_cost(event) == 0.0

    # -- Concurrent access: multiple threads calling check_or_raise --

    def test_concurrent_check_or_raise_exactly_one_denied(self) -> None:
        """check_or_raise: concurrent calls near step limit — exactly N-limit allowed."""
        import threading

        listener = _make_listener(max_steps=5)
        # Pre-fill 4 steps via events
        for _ in range(4):
            _fake_event_bus.emit(None, _completed_event())
        assert listener.container.step_guard.current_step == 4

        # Now step 5 = at limit. Next check_or_raise should deny.
        # Emit one more to hit the limit
        _fake_event_bus.emit(None, _completed_event())
        assert listener.container.step_guard.current_step == 5

        results = []
        errors = []

        def call_check():
            try:
                listener.check_or_raise()
                results.append("allowed")
            except VeronicaHalt:
                results.append("denied")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=call_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        # All 10 should be denied (steps exhausted)
        assert results.count("denied") == 10

    def test_concurrent_budget_spend_thread_safe(self) -> None:
        """Budget spend from multiple threads must not corrupt total."""
        import threading

        listener = _make_listener(max_cost_usd=100.0)
        num_threads = 20
        cost_per_thread = 1.0

        def spend():
            listener.container.budget.spend(cost_per_thread)

        threads = [threading.Thread(target=spend) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Total should be exactly num_threads * cost_per_thread
        assert listener.container.budget.spent_usd == pytest.approx(
            num_threads * cost_per_thread
        )

    # -- Boundary abuse: zero/extreme limits --

    def test_max_steps_zero_denies_immediately(self) -> None:
        """check_or_raise with max_steps=0: deny on first call."""
        listener = _make_listener(max_steps=0)
        with pytest.raises(VeronicaHalt, match="[Ss]tep"):
            listener.check_or_raise()

    def test_max_cost_zero_denies_after_any_spend(self) -> None:
        """check_or_raise with max_cost_usd=0.0: deny after any spend."""
        listener = _make_listener(max_cost_usd=0.0)
        listener.container.budget.spend(0.001)
        with pytest.raises(VeronicaHalt):
            listener.check_or_raise()

    def test_huge_token_count_does_not_overflow(self) -> None:
        """_estimate_cost: sys.maxsize tokens must not overflow or crash."""
        event = _completed_event(response=_response_with_total(sys.maxsize))
        result = _estimate_cost(event)
        assert isinstance(result, float)
        assert result > 0.0

    def test_zero_total_tokens_returns_zero(self) -> None:
        """_estimate_cost: 0 total_tokens returns 0.0 (no phantom cost)."""
        event = _completed_event(response=_response_with_total(0))
        assert _estimate_cost(event) == 0.0

    def test_single_token_returns_positive(self) -> None:
        """_estimate_cost: 1 total_token returns a positive cost."""
        event = _completed_event(response=_response_with_total(1))
        assert _estimate_cost(event) > 0.0

    # -- State corruption: listener used after budget exceeded --

    def test_completed_event_after_budget_exceeded_still_tracks_steps(self) -> None:
        """Step counter continues even after budget is exceeded."""
        listener = _make_listener(max_cost_usd=0.001)
        # Exhaust budget
        listener.container.budget.spend(1.0)
        assert listener.container.budget.is_exceeded

        # Emit completed event — step should still increment
        _fake_event_bus.emit(None, _completed_event())
        assert listener.container.step_guard.current_step == 1

    def test_check_or_raise_idempotent_after_deny(self) -> None:
        """check_or_raise: calling repeatedly after deny always raises."""
        listener = _make_listener(max_steps=0)
        for _ in range(5):
            with pytest.raises(VeronicaHalt):
                listener.check_or_raise()

    # -- Partial failure: event handler exceptions --

    def test_failed_event_with_none_error_does_not_crash(self) -> None:
        """LLMCallFailedEvent with error=None must not crash handler."""
        _make_listener()  # registers handlers on fake bus
        event = FakeLLMCallFailedEvent(call_id="x", error=None)
        _fake_event_bus.emit(None, event)  # must not raise

    def test_completed_event_with_exception_raising_response(self) -> None:
        """Response object that raises on attribute access must not crash."""

        class ExplodingResponse:
            @property
            def usage(self):
                raise RuntimeError("I explode")

        _make_listener()  # registers handlers on fake bus
        event = _completed_event(response=ExplodingResponse())
        cost = _estimate_cost(event)
        assert cost == 0.0  # Fallback to 0.0
