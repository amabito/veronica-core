"""Tests for LLM client injection and pluggability."""

import pytest
from veronica_core import (
    VeronicaIntegration,
    LLMClient,
    NullClient,
    DummyClient,
)


def test_default_client_is_null():
    """Default VeronicaIntegration should use NullClient."""
    veronica = VeronicaIntegration()
    assert isinstance(veronica.client, NullClient)


def test_null_client_raises_on_generate():
    """NullClient should raise RuntimeError when invoked."""
    client = NullClient()
    with pytest.raises(RuntimeError, match="LLMClient not configured"):
        client.generate("test prompt")


def test_dummy_client_returns_fixed_response():
    """DummyClient should return fixed response."""
    client = DummyClient(fixed_response="TEST_OK")
    response = client.generate("any prompt")
    assert response == "TEST_OK"


def test_integration_with_dummy_client():
    """VeronicaIntegration should accept DummyClient and return correct response."""
    client = DummyClient(fixed_response="SAFE")
    veronica = VeronicaIntegration(client=client)

    response = veronica.client.generate("Is this safe?")
    assert response == "SAFE"


def test_custom_client_injection():
    """Custom LLM client should work via Protocol."""

    class CustomClient:
        """Custom LLM client for testing."""

        def generate(self, prompt: str, *, context=None, **kwargs):
            return f"CUSTOM: {prompt[:10]}"

    client = CustomClient()
    veronica = VeronicaIntegration(client=client)

    response = veronica.client.generate("Hello world")
    assert response == "CUSTOM: Hello worl"


def test_llm_client_is_optional():
    """VERONICA should work fine without LLM client (core features)."""
    from veronica_core import MemoryBackend

    # Use MemoryBackend for test isolation (no state file)
    backend = MemoryBackend()
    veronica = VeronicaIntegration(cooldown_fails=2, backend=backend)

    # Core features should work without LLM
    task = "test_task"
    assert not veronica.is_in_cooldown(task)

    veronica.record_fail(task)
    veronica.record_fail(task)  # Activates cooldown

    assert veronica.is_in_cooldown(task)
    assert veronica.get_fail_count(task) == 2


def test_no_llm_dependency_in_core():
    """Core modules should not import LLM-specific packages."""
    import sys
    import importlib

    # Import core modules
    importlib.import_module("veronica_core.state")
    importlib.import_module("veronica_core.backends")
    importlib.import_module("veronica_core.guards")
    importlib.import_module("veronica_core.exit")

    # Check that no external LLM packages are imported
    forbidden = ["ollama", "openai", "anthropic", "google.generativeai"]
    for module_name in forbidden:
        assert module_name not in sys.modules, (
            f"Core module imported {module_name} (LLM dependency leak)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
