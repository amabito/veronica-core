"""Tests for failure classification (predicate-based exception filtering).

Covers:
- Predicate factory unit tests (8)
- CircuitBreaker integration tests (8)
- DistributedCircuitBreaker tests (4)
- Adapter tests (2)
- Adversarial tests (2)
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from veronica_core.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    count_exception_types,
    ignore_exception_types,
    ignore_status_codes,
)
from veronica_core.runtime_policy import PolicyContext


def _ctx() -> PolicyContext:
    return PolicyContext()


# ---------------------------------------------------------------------------
# Custom exception hierarchy for testing
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Simulates a provider-side error (500, timeout)."""


class BadRequestError(Exception):
    """Simulates a user-caused error (400)."""


class RateLimitError(Exception):
    """Simulates a rate-limit error (429)."""


class HttpError(Exception):
    """Simulates an HTTP error with status_code attribute."""
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class HttpErrorWithResponse(Exception):
    """Simulates an HTTP error with response.status_code attribute."""
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.response = MagicMock(status_code=status_code)


# ===========================================================================
# 1. Predicate factory unit tests (8)
# ===========================================================================

class TestIgnoreExceptionTypes:
    """ignore_exception_types(*types) returns False for matching types."""

    def test_ignores_specified_type(self):
        pred = ignore_exception_types(BadRequestError)
        assert pred(BadRequestError("bad input")) is False

    def test_counts_unspecified_type(self):
        pred = ignore_exception_types(BadRequestError)
        assert pred(ProviderError("server down")) is True

    def test_ignores_multiple_types(self):
        pred = ignore_exception_types(BadRequestError, RateLimitError)
        assert pred(BadRequestError()) is False
        assert pred(RateLimitError()) is False
        assert pred(ProviderError()) is True


class TestCountExceptionTypes:
    """count_exception_types(*types) returns True only for matching types."""

    def test_counts_specified_type(self):
        pred = count_exception_types(ProviderError)
        assert pred(ProviderError("500")) is True

    def test_ignores_unspecified_type(self):
        pred = count_exception_types(ProviderError)
        assert pred(BadRequestError("400")) is False

    def test_counts_multiple_types(self):
        pred = count_exception_types(ProviderError, TimeoutError)
        assert pred(ProviderError()) is True
        assert pred(TimeoutError()) is True
        assert pred(BadRequestError()) is False


class TestIgnoreStatusCodes:
    """ignore_status_codes(*codes) inspects .status_code or .response.status_code."""

    def test_ignores_direct_status_code(self):
        pred = ignore_status_codes(400, 404, 422)
        assert pred(HttpError(400, "bad request")) is False
        assert pred(HttpError(404, "not found")) is False

    def test_counts_non_ignored_status_code(self):
        pred = ignore_status_codes(400, 404)
        assert pred(HttpError(500, "server error")) is True
        assert pred(HttpError(503, "unavailable")) is True

    def test_inspects_response_attribute(self):
        pred = ignore_status_codes(400)
        assert pred(HttpErrorWithResponse(400)) is False
        assert pred(HttpErrorWithResponse(500)) is True

    def test_non_http_exception_always_counts(self):
        """Exceptions without status_code attribute always count as failures."""
        pred = ignore_status_codes(400, 404)
        assert pred(ProviderError("no status_code")) is True
        assert pred(ValueError("plain error")) is True


# ===========================================================================
# 2. CircuitBreaker integration tests (8)
# ===========================================================================

class TestCircuitBreakerWithPredicate:
    """CircuitBreaker + failure_predicate integration."""

    def test_filtered_failure_not_counted(self):
        """Filtered failures should not increment the failure counter."""
        cb = CircuitBreaker(
            failure_threshold=3,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        for _ in range(10):
            result = cb.record_failure(error=BadRequestError("bad"))
            assert result is False
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_counted_failure_increments(self):
        """Non-filtered failures should increment and eventually open."""
        cb = CircuitBreaker(
            failure_threshold=3,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        for _ in range(3):
            result = cb.record_failure(error=ProviderError("500"))
            assert result is True
        assert cb.failure_count == 3
        assert cb.state == CircuitState.OPEN

    def test_mixed_failures(self):
        """Mix of filtered and counted: only counted ones open the circuit."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        cb.record_failure(error=BadRequestError())  # ignored
        cb.record_failure(error=ProviderError())     # counted: 1
        cb.record_failure(error=BadRequestError())  # ignored
        cb.record_failure(error=ProviderError())     # counted: 2 -> OPEN
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 2

    def test_error_none_bypasses_predicate(self):
        """error=None should always count (backward compat, AG2 null reply)."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        cb.record_failure()  # error=None -> always counts
        cb.record_failure()  # -> OPEN
        assert cb.state == CircuitState.OPEN

    def test_no_predicate_always_counts(self):
        """Without predicate, all failures count (default behavior)."""
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(error=BadRequestError())
        cb.record_failure(error=ProviderError())
        assert cb.state == CircuitState.OPEN

    def test_record_failure_returns_bool(self):
        """record_failure returns True when counted, False when filtered."""
        cb = CircuitBreaker(
            failure_threshold=5,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        assert cb.record_failure(error=BadRequestError()) is False
        assert cb.record_failure(error=ProviderError()) is True
        assert cb.record_failure() is True  # error=None

    def test_count_exception_types_integration(self):
        """count_exception_types: only specified types trip the breaker."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=count_exception_types(TimeoutError),
        )
        cb.record_failure(error=BadRequestError())  # ignored
        cb.record_failure(error=ProviderError())     # ignored
        assert cb.state == CircuitState.CLOSED
        cb.record_failure(error=TimeoutError())
        cb.record_failure(error=TimeoutError())
        assert cb.state == CircuitState.OPEN

    def test_ignore_status_codes_integration(self):
        """ignore_status_codes: 4xx filtered, 5xx counts."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=ignore_status_codes(400, 404, 422),
        )
        cb.record_failure(error=HttpError(400))
        cb.record_failure(error=HttpError(404))
        cb.record_failure(error=HttpError(422))
        assert cb.state == CircuitState.CLOSED
        cb.record_failure(error=HttpError(500))
        cb.record_failure(error=HttpError(503))
        assert cb.state == CircuitState.OPEN


# ===========================================================================
# 3. DistributedCircuitBreaker tests (4)
# ===========================================================================

class TestDistributedCircuitBreakerPredicate:
    """DistributedCircuitBreaker + failure_predicate."""

    @pytest.fixture()
    def dcb(self):
        """Create a DistributedCircuitBreaker with fakeredis and a predicate."""
        import fakeredis
        from veronica_core.distributed import DistributedCircuitBreaker

        client = fakeredis.FakeRedis(decode_responses=True)
        return DistributedCircuitBreaker(
            redis_url="redis://fake",
            circuit_id="test-pred",
            failure_threshold=3,
            recovery_timeout=1.0,
            redis_client=client,
            failure_predicate=ignore_exception_types(BadRequestError),
        )

    def test_filtered_failure_not_counted(self, dcb):
        """Filtered failures should not increment Redis failure count."""
        result = dcb.record_failure(error=BadRequestError("bad input"))
        assert result is False
        assert dcb.failure_count == 0
        assert dcb.state == CircuitState.CLOSED

    def test_counted_failure_opens_circuit(self, dcb):
        """Non-filtered failures open the circuit via Redis."""
        for _ in range(3):
            result = dcb.record_failure(error=ProviderError("500"))
            assert result is True
        assert dcb.state == CircuitState.OPEN

    def test_error_none_bypasses_predicate(self, dcb):
        """error=None counts as failure (backward compat)."""
        for _ in range(3):
            result = dcb.record_failure()
            assert result is True
        assert dcb.state == CircuitState.OPEN

    def test_predicate_evaluated_before_redis(self, dcb):
        """Predicate evaluation should prevent any Redis call for filtered errors."""
        with patch.object(dcb, "_script_failure") as mock_lua:
            dcb.record_failure(error=BadRequestError())
            mock_lua.assert_not_called()


# ===========================================================================
# 4. Adapter tests (2)
# ===========================================================================

class TestExecutionContextAdapter:
    """ExecutionContext passes error= to circuit breaker."""

    def test_wrap_llm_call_passes_error_to_circuit_breaker(self):
        """When fn() raises, ExecutionContext should pass error= to record_failure."""
        from veronica_core.containment import ExecutionConfig, ExecutionContext

        cb = CircuitBreaker(
            failure_threshold=5,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=100)
        with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
            ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(BadRequestError("bad")))
        # BadRequestError is filtered -> failure_count should be 0
        assert cb.failure_count == 0

    def test_wrap_llm_call_counts_provider_error(self):
        """Provider errors should be counted by the circuit breaker."""
        from veronica_core.containment import ExecutionConfig, ExecutionContext

        cb = CircuitBreaker(
            failure_threshold=5,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=100, max_retries_total=100)
        with ExecutionContext(config=config, circuit_breaker=cb) as ctx:
            ctx.wrap_llm_call(fn=lambda: (_ for _ in ()).throw(ProviderError("500")))
        assert cb.failure_count == 1


# ===========================================================================
# 5. Adversarial tests (2)
# ===========================================================================

class TestAdversarialPredicate:
    """Adversarial tests: try to break the predicate system."""

    # ------------------------------------------------------------------
    # Predicate itself is broken
    # ------------------------------------------------------------------

    def test_predicate_exception_counts_as_failure(self):
        """If the predicate itself raises, the failure should be counted (fail-safe)."""
        def broken_predicate(error: BaseException) -> bool:
            raise RuntimeError("predicate bug")

        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=broken_predicate,
        )
        result = cb.record_failure(error=ProviderError("500"))
        assert result is True
        assert cb.failure_count == 1

    def test_predicate_raises_system_exit(self):
        """Predicate raising SystemExit should NOT be caught (BaseException).

        Our fail-safe catches Exception, not BaseException. SystemExit from
        a predicate should propagate -- we don't want to swallow fatal signals.
        """
        def fatal_predicate(error: BaseException) -> bool:
            raise SystemExit(99)

        cb = CircuitBreaker(
            failure_threshold=5,
            failure_predicate=fatal_predicate,
        )
        with pytest.raises(SystemExit):
            cb.record_failure(error=ProviderError())

    def test_predicate_raises_keyboard_interrupt(self):
        """KeyboardInterrupt from predicate should propagate."""
        def interrupt_predicate(error: BaseException) -> bool:
            raise KeyboardInterrupt()

        cb = CircuitBreaker(
            failure_threshold=5,
            failure_predicate=interrupt_predicate,
        )
        with pytest.raises(KeyboardInterrupt):
            cb.record_failure(error=ProviderError())

    # ------------------------------------------------------------------
    # Predicate returns truthy/falsy non-bool values
    # ------------------------------------------------------------------

    def test_predicate_returns_truthy_int(self):
        """Predicate returning 1 (truthy) should count as failure."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=lambda e: 1,  # type: ignore[return-value]
        )
        result = cb.record_failure(error=ProviderError())
        assert result is True
        assert cb.failure_count == 1

    def test_predicate_returns_falsy_zero(self):
        """Predicate returning 0 (falsy) should filter the failure."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=lambda e: 0,  # type: ignore[return-value]
        )
        result = cb.record_failure(error=ProviderError())
        assert result is False
        assert cb.failure_count == 0

    # ------------------------------------------------------------------
    # Edge-case error objects
    # ------------------------------------------------------------------

    def test_system_exit_as_error(self):
        """SystemExit passed as error should still be evaluated by predicate."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=count_exception_types(ProviderError),
        )
        # SystemExit is not a ProviderError, so it should be filtered
        result = cb.record_failure(error=SystemExit(1))
        assert result is False
        assert cb.failure_count == 0

    def test_error_with_very_long_repr(self):
        """Error with enormous message should not crash predicate."""
        cb = CircuitBreaker(
            failure_threshold=2,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        huge_msg = "x" * 10_000_000  # 10MB string
        result = cb.record_failure(error=ProviderError(huge_msg))
        assert result is True

    # ------------------------------------------------------------------
    # Predicate + state interaction (HALF_OPEN)
    # ------------------------------------------------------------------

    def test_filtered_failure_in_half_open_does_not_reopen(self):
        """Filtered failure during HALF_OPEN should NOT reopen the circuit.

        This is critical: if a user-caused 400 error arrives during the
        test request, we should ignore it, not punish the service.
        """
        cb = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        # Open the circuit with real failures
        cb.record_failure(error=ProviderError())
        cb.record_failure(error=ProviderError())
        assert cb.state == CircuitState.OPEN

        # Wait for HALF_OPEN
        import time
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        # Filtered failure during HALF_OPEN should be ignored
        result = cb.record_failure(error=BadRequestError("user error"))
        assert result is False
        # Circuit should still be HALF_OPEN (not reopened to OPEN)
        assert cb.state == CircuitState.HALF_OPEN

    def test_counted_failure_in_half_open_reopens(self):
        """Counted failure during HALF_OPEN should reopen."""
        cb = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        cb.record_failure(error=ProviderError())
        cb.record_failure(error=ProviderError())

        import time
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        result = cb.record_failure(error=ProviderError("real failure"))
        assert result is True
        assert cb.state == CircuitState.OPEN

    # ------------------------------------------------------------------
    # Stateful / mutating predicate (bad practice but possible)
    # ------------------------------------------------------------------

    def test_stateful_predicate_call_count(self):
        """Predicate that mutates state -- verify it's called exactly once per failure."""
        call_count = {"n": 0}

        def counting_pred(error: BaseException) -> bool:
            call_count["n"] += 1
            return True

        cb = CircuitBreaker(
            failure_threshold=100,
            failure_predicate=counting_pred,
        )
        cb.record_failure(error=ProviderError())
        cb.record_failure(error=ProviderError())
        cb.record_failure()  # error=None -> predicate NOT called
        assert call_count["n"] == 2  # Only called for error != None

    def test_predicate_receives_exact_error_object(self):
        """Predicate should receive the exact same error object (not a copy)."""
        received = []

        def capturing_pred(error: BaseException) -> bool:
            received.append(error)
            return True

        cb = CircuitBreaker(failure_threshold=100, failure_predicate=capturing_pred)
        specific_error = ProviderError("specific")
        cb.record_failure(error=specific_error)
        assert len(received) == 1
        assert received[0] is specific_error  # identity check, not equality

    # ------------------------------------------------------------------
    # Concurrency
    # ------------------------------------------------------------------

    def test_concurrent_predicate_evaluation(self):
        """Predicate evaluation is thread-safe (no lock needed for pure function)."""
        cb = CircuitBreaker(
            failure_threshold=100,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        results = []
        errors = []

        def worker(error_cls):
            try:
                r = cb.record_failure(error=error_cls())
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(20):
            cls = BadRequestError if i % 2 == 0 else ProviderError
            t = threading.Thread(target=worker, args=(cls,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert results.count(False) == 10  # BadRequestError filtered
        assert results.count(True) == 10   # ProviderError counted
        assert cb.failure_count == 10

    def test_concurrent_filtered_in_half_open(self):
        """Multiple threads: filtered failures during HALF_OPEN should not reopen.

        Race condition target: predicate evaluation is outside the lock,
        state check is inside the lock. Verify no corruption.
        """
        import time

        cb = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
            failure_predicate=ignore_exception_types(BadRequestError),
        )
        cb.record_failure(error=ProviderError())
        cb.record_failure(error=ProviderError())
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        errors = []

        def spam_filtered():
            try:
                for _ in range(50):
                    cb.record_failure(error=BadRequestError())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=spam_filtered) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # After 500 filtered failures, circuit should NOT have reopened.
        # It stays HALF_OPEN (or could have transitioned to CLOSED/OPEN
        # via other means, but never via filtered failures).
        # failure_count should not have increased from the filtered ones.
        # The 2 initial ProviderErrors are in the count from before HALF_OPEN.

    # ------------------------------------------------------------------
    # ignore_status_codes edge cases
    # ------------------------------------------------------------------

    def test_status_code_attribute_is_string(self):
        """status_code that is a string (not int) -- should not crash."""
        pred = ignore_status_codes(400)

        class WeirdError(Exception):
            status_code = "400"  # string, not int

        # "400" (str) is in the frozenset({400}) -- this is False because
        # str != int in Python set membership
        result = pred(WeirdError())
        assert result is True  # NOT filtered (type mismatch)

    def test_status_code_is_none(self):
        """status_code attribute exists but is None."""
        pred = ignore_status_codes(400)

        class NoneStatusError(Exception):
            status_code = None

        result = pred(NoneStatusError())
        assert result is True  # Not filtered (None not in code set)

    def test_response_attribute_without_status_code(self):
        """response attribute exists but has no status_code."""
        pred = ignore_status_codes(400)

        class WeirdResponseError(Exception):
            response = "not an object with status_code"

        result = pred(WeirdResponseError())
        assert result is True  # Falls through, counts as failure


# ===========================================================================
# 6. Export tests (1)
# ===========================================================================

class TestExports:
    """Verify public API exports from veronica_core."""

    def test_failure_predicate_exported(self):
        import veronica_core
        assert hasattr(veronica_core, "FailurePredicate")
        assert hasattr(veronica_core, "ignore_exception_types")
        assert hasattr(veronica_core, "count_exception_types")
        assert hasattr(veronica_core, "ignore_status_codes")
