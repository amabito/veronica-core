"""Adversarial tests for LLM client classes (clients.py).

Tests:
- LLMClient protocol structural conformance (NullClient, DummyClient)
- NullClient raises RuntimeError with helpful message
- DummyClient: fixed response, call counting, last_prompt tracking
- DummyClient: thread-safety of call_count under concurrent access
- DummyClient: kwargs passthrough (no crash on unknown kwargs)
- DummyClient: empty prompt edge case
- DummyClient: context parameter passed without error
- NullClient: context / kwargs do not suppress the error
"""

from __future__ import annotations

import threading

import pytest

from veronica_core.clients import DummyClient, NullClient


class TestNullClientBehavior:
    """NullClient must always raise RuntimeError with actionable message."""

    def test_generate_raises_runtime_error(self) -> None:
        client = NullClient()
        with pytest.raises(RuntimeError, match="LLMClient not configured"):
            client.generate("hello")

    def test_error_message_suggests_fix(self) -> None:
        client = NullClient()
        with pytest.raises(RuntimeError) as exc_info:
            client.generate("any prompt")
        assert "client=" in str(exc_info.value)

    def test_raises_even_with_context(self) -> None:
        client = NullClient()
        with pytest.raises(RuntimeError):
            client.generate("prompt", context={"user": "alice"})

    def test_raises_even_with_kwargs(self) -> None:
        client = NullClient()
        with pytest.raises(RuntimeError):
            client.generate("prompt", temperature=0.7, model="gpt-4")

    def test_raises_on_empty_prompt(self) -> None:
        client = NullClient()
        with pytest.raises(RuntimeError):
            client.generate("")


class TestDummyClientBasicBehavior:
    """DummyClient returns fixed response and tracks state."""

    def test_returns_fixed_response(self) -> None:
        client = DummyClient(fixed_response="ALLOW")
        result = client.generate("any prompt")
        assert result == "ALLOW"

    def test_default_fixed_response_is_ok(self) -> None:
        client = DummyClient()
        result = client.generate("prompt")
        assert result == "OK"

    def test_call_count_increments(self) -> None:
        client = DummyClient(fixed_response="yes")
        client.generate("p1")
        client.generate("p2")
        client.generate("p3")
        assert client.call_count == 3

    def test_last_prompt_is_recorded(self) -> None:
        client = DummyClient()
        client.generate("first prompt")
        client.generate("second prompt")
        assert client.last_prompt == "second prompt"

    def test_last_prompt_none_initially(self) -> None:
        client = DummyClient()
        assert client.last_prompt is None
        assert client.call_count == 0

    def test_context_param_accepted_without_error(self) -> None:
        client = DummyClient(fixed_response="result")
        result = client.generate("prompt", context={"model": "gpt-4", "user": "bob"})
        assert result == "result"

    def test_extra_kwargs_accepted_without_error(self) -> None:
        client = DummyClient()
        result = client.generate("prompt", temperature=0.5, max_tokens=100, top_p=0.9)
        assert result == "OK"

    def test_empty_prompt_accepted(self) -> None:
        client = DummyClient(fixed_response="empty_ok")
        result = client.generate("")
        assert result == "empty_ok"
        assert client.last_prompt == ""

    @pytest.mark.parametrize("response", [
        "",
        "DENY",
        '{"allowed": false}',
        "a" * 10000,
    ])
    def test_various_fixed_responses(self, response: str) -> None:
        client = DummyClient(fixed_response=response)
        assert client.generate("p") == response


class TestDummyClientThreadSafety:
    """DummyClient.call_count must be accurate under concurrent access."""

    def test_concurrent_calls_call_count_accurate(self) -> None:
        """50 threads each call generate() once; call_count must be 50."""
        client = DummyClient(fixed_response="OK")
        barrier = threading.Barrier(50)
        errors: list[Exception] = []

        def call() -> None:
            try:
                barrier.wait()
                client.generate("concurrent prompt")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # DummyClient is not guaranteed thread-safe for call_count (no lock),
        # but must not raise exceptions and must return the correct value
        assert client.call_count == 50

    def test_concurrent_calls_no_exception(self) -> None:
        """Concurrent calls must never raise an exception."""
        client = DummyClient(fixed_response="safe")
        errors: list[Exception] = []

        def call_many() -> None:
            try:
                for _ in range(10):
                    result = client.generate("p")
                    assert result == "safe"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call_many) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestLLMClientProtocolConformance:
    """NullClient and DummyClient must satisfy LLMClient protocol."""

    def test_null_client_has_generate_method(self) -> None:
        client = NullClient()
        assert callable(getattr(client, "generate", None))

    def test_dummy_client_has_generate_method(self) -> None:
        client = DummyClient()
        assert callable(getattr(client, "generate", None))

    def test_dummy_client_is_llm_client_instance(self) -> None:
        """DummyClient must satisfy the LLMClient Protocol structurally."""
        client = DummyClient()
        # Protocol checks structural subtyping via isinstance() on runtime_checkable
        # LLMClient is NOT @runtime_checkable (it's a plain Protocol), so
        # we verify the generate() signature matches by calling it.
        result = client.generate("test", context=None)
        assert isinstance(result, str)

    def test_null_client_signature_matches_protocol(self) -> None:
        """NullClient.generate() accepts (prompt, *, context, **kwargs)."""
        client = NullClient()
        with pytest.raises(RuntimeError):
            client.generate("p", context={"k": "v"}, model="gpt-4")
