"""Tests for AdapterCapabilities and FrameworkAdapterProtocol.capabilities() wiring."""

from __future__ import annotations

from typing import Any

import pytest

from veronica_core.adapter_capabilities import AdapterCapabilities
from veronica_core.protocols import FrameworkAdapterProtocol


# ---------------------------------------------------------------------------
# AdapterCapabilities dataclass
# ---------------------------------------------------------------------------


class TestAdapterCapabilities:
    """Unit tests for AdapterCapabilities frozen dataclass."""

    def test_defaults_all_false(self) -> None:
        caps = AdapterCapabilities()
        assert caps.supports_streaming is False
        assert caps.supports_cost_extraction is False
        assert caps.supports_token_extraction is False
        assert caps.supports_async is False
        assert caps.supports_reserve_commit is False
        assert caps.supports_agent_identity is False
        assert caps.framework_name == ""
        assert caps.framework_version_constraint == ""
        assert caps.extra == {}

    def test_custom_values(self) -> None:
        caps = AdapterCapabilities(
            supports_streaming=True,
            supports_cost_extraction=True,
            framework_name="LangChain",
            framework_version_constraint=">=0.3",
            extra={"custom_key": 42},
        )
        assert caps.supports_streaming is True
        assert caps.supports_cost_extraction is True
        assert caps.framework_name == "LangChain"
        assert caps.framework_version_constraint == ">=0.3"
        assert caps.extra == {"custom_key": 42}

    def test_frozen_immutable(self) -> None:
        caps = AdapterCapabilities()
        with pytest.raises(AttributeError):
            caps.supports_streaming = True  # type: ignore[misc]

    def test_equality(self) -> None:
        a = AdapterCapabilities(framework_name="X")
        b = AdapterCapabilities(framework_name="X")
        assert a == b

    def test_inequality(self) -> None:
        a = AdapterCapabilities(supports_streaming=True)
        b = AdapterCapabilities(supports_streaming=False)
        assert a != b

    def test_not_hashable_due_to_dict(self) -> None:
        """Frozen dataclass with dict field is NOT hashable."""
        caps = AdapterCapabilities(framework_name="test")
        with pytest.raises(TypeError, match="unhashable"):
            hash(caps)

    def test_extra_dict_isolation(self) -> None:
        """Default extra dict must not be shared across instances."""
        a = AdapterCapabilities()
        b = AdapterCapabilities()
        assert a.extra is not b.extra


# ---------------------------------------------------------------------------
# Protocol conformance: capabilities()
# ---------------------------------------------------------------------------


class TestCapabilitiesProtocol:
    """Verify that capabilities() is required for FrameworkAdapterProtocol."""

    def test_adapter_without_capabilities_fails_isinstance(self) -> None:
        """Adapter missing capabilities() must NOT satisfy the protocol."""

        class OldStyleAdapter:
            def extract_cost(self, result: Any) -> float:
                return 0.0

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                return 0, 0

            def handle_halt(self, reason: str) -> Any:
                return None

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return None

        assert not isinstance(OldStyleAdapter(), FrameworkAdapterProtocol)

    def test_adapter_with_capabilities_passes_isinstance(self) -> None:
        """Adapter with all methods including capabilities() satisfies protocol."""

        class FullAdapter:
            def capabilities(self) -> AdapterCapabilities:
                return AdapterCapabilities(framework_name="Full")

            def extract_cost(self, result: Any) -> float:
                return 0.0

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                return 0, 0

            def handle_halt(self, reason: str) -> Any:
                return None

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return None

        assert isinstance(FullAdapter(), FrameworkAdapterProtocol)


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialAdapterCapabilities:
    """Adversarial tests for AdapterCapabilities -- attacker mindset."""

    def test_extra_dict_mutation_does_not_affect_other_instances(self) -> None:
        """Mutating extra dict on one instance must not leak to others."""
        a = AdapterCapabilities(extra={"k": "v"})
        b = AdapterCapabilities(extra={"k": "v"})
        # Even though frozen, extra dict values themselves are mutable
        a.extra["injected"] = "evil"
        assert "injected" not in b.extra

    def test_capabilities_returns_wrong_type_still_satisfies_protocol(self) -> None:
        """Protocol isinstance check is structural, not return-type checked.
        An adapter returning garbage from capabilities() still passes isinstance.
        Callers must validate the return type themselves."""

        class BadAdapter:
            def capabilities(self) -> AdapterCapabilities:
                return "not_capabilities"  # type: ignore[return-value]

            def extract_cost(self, result: Any) -> float:
                return 0.0

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                return 0, 0

            def handle_halt(self, reason: str) -> Any:
                return None

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return None

        # isinstance passes (structural, no return type check at runtime)
        assert isinstance(BadAdapter(), FrameworkAdapterProtocol)
        # But actual return is wrong type
        result = BadAdapter().capabilities()
        assert not isinstance(result, AdapterCapabilities)

    def test_capabilities_raising_exception(self) -> None:
        """Adapter whose capabilities() raises should still satisfy isinstance
        but fail at call time."""

        class ExplodingAdapter:
            def capabilities(self) -> AdapterCapabilities:
                raise RuntimeError("boom")

            def extract_cost(self, result: Any) -> float:
                return 0.0

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                return 0, 0

            def handle_halt(self, reason: str) -> Any:
                return None

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return None

        adapter = ExplodingAdapter()
        assert isinstance(adapter, FrameworkAdapterProtocol)
        with pytest.raises(RuntimeError, match="boom"):
            adapter.capabilities()

    def test_extra_with_non_serializable_values(self) -> None:
        """extra dict accepts arbitrary objects without error."""
        caps = AdapterCapabilities(extra={
            "lambda": lambda: None,
            "set": {1, 2, 3},
            "bytes": b"\xff\x00",
        })
        assert callable(caps.extra["lambda"])
        assert isinstance(caps.extra["set"], set)

    def test_framework_name_with_unicode_and_special_chars(self) -> None:
        """Framework name with unicode/special chars must work."""
        caps = AdapterCapabilities(framework_name="LangChain\x00\xff\u200b")
        assert "\x00" in caps.framework_name

    def test_adapter_subclass_inherits_capabilities(self) -> None:
        """Subclassed adapter that inherits capabilities() from parent
        should still satisfy the protocol."""

        class BaseAdapter:
            def capabilities(self) -> AdapterCapabilities:
                return AdapterCapabilities(framework_name="base")

            def extract_cost(self, result: Any) -> float:
                return 0.0

            def extract_tokens(self, result: Any) -> tuple[int, int]:
                return 0, 0

            def handle_halt(self, reason: str) -> Any:
                return None

            def handle_degrade(self, reason: str, suggestion: str) -> Any:
                return None

        class ChildAdapter(BaseAdapter):
            pass

        assert isinstance(ChildAdapter(), FrameworkAdapterProtocol)
        assert ChildAdapter().capabilities().framework_name == "base"
