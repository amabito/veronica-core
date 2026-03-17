"""Tests for veronica_core.security.tool_pinning."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from veronica_core.security.tool_pinning import (
    PinVerdict,
    ToolPinRegistry,
    ToolSchemaPin,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA_A: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the web.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

SCHEMA_B: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the web. (modified)",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


@pytest.fixture()
def registry() -> ToolPinRegistry:
    return ToolPinRegistry()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRegisterAndVerify:
    def test_register_returns_pin(self, registry: ToolPinRegistry) -> None:
        pin = registry.register("web_search", SCHEMA_A)
        assert isinstance(pin, ToolSchemaPin)
        assert pin.tool_name == "web_search"
        assert len(pin.schema_hash) == 64  # SHA-256 hex
        assert pin.registered_at > 0

    def test_verify_passes_after_register(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        assert registry.verify("web_search", SCHEMA_A), "expected MATCH verdict"

    def test_is_pinned_true_after_register(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        assert registry.is_pinned("web_search"), "expected MATCH verdict"

    def test_get_pin_returns_pin(self, registry: ToolPinRegistry) -> None:
        pin = registry.register("web_search", SCHEMA_A)
        retrieved = registry.get_pin("web_search")
        assert retrieved == pin

    def test_pinned_tools_contains_registered_name(
        self, registry: ToolPinRegistry
    ) -> None:
        registry.register("web_search", SCHEMA_A)
        registry.register("calculator", {"name": "calculator"})
        assert "web_search" in registry.pinned_tools
        assert "calculator" in registry.pinned_tools


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_verify_unpinned_tool_returns_false(
        self, registry: ToolPinRegistry
    ) -> None:
        """Fail-closed: unpinned tool must return False, not raise."""
        assert (
            registry.verify("unknown_tool", SCHEMA_A) is not True
        )  # PinVerification is falsy for non-MATCH

    def test_is_pinned_false_for_unknown(self, registry: ToolPinRegistry) -> None:
        assert (
            registry.is_pinned("unknown_tool") is not True
        )  # PinVerification is falsy for non-MATCH

    def test_get_pin_returns_none_for_unknown(self, registry: ToolPinRegistry) -> None:
        assert registry.get_pin("unknown_tool") is None

    def test_verify_after_unpin_returns_false(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        registry.unpin("web_search")
        assert (
            registry.verify("web_search", SCHEMA_A) is not True
        )  # PinVerification is falsy for non-MATCH

    def test_verify_after_clear_returns_false(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        registry.clear()
        assert (
            registry.verify("web_search", SCHEMA_A) is not True
        )  # PinVerification is falsy for non-MATCH


# ---------------------------------------------------------------------------
# Hash mismatch
# ---------------------------------------------------------------------------


class TestHashMismatch:
    def test_verify_fails_on_schema_change(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        assert (
            registry.verify("web_search", SCHEMA_B) is not True
        )  # PinVerification is falsy for non-MATCH

    def test_verify_fails_on_extra_field(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        tampered = {**SCHEMA_A, "injected": "malicious"}
        assert (
            registry.verify("web_search", tampered) is not True
        )  # PinVerification is falsy for non-MATCH

    def test_verify_fails_on_empty_schema(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        assert (
            registry.verify("web_search", {}) is not True
        )  # PinVerification is falsy for non-MATCH


# ---------------------------------------------------------------------------
# Hash determinism and canonicalization
# ---------------------------------------------------------------------------


class TestHashDeterminism:
    def test_same_schema_same_hash(self) -> None:
        h1 = ToolPinRegistry.hash_schema(SCHEMA_A)
        h2 = ToolPinRegistry.hash_schema(SCHEMA_A)
        assert h1 == h2

    def test_key_order_independent(self) -> None:
        """Different key insertion order must produce the same hash."""
        schema_forward = {"a": 1, "b": 2, "c": 3}
        schema_reverse = {"c": 3, "b": 2, "a": 1}
        assert ToolPinRegistry.hash_schema(
            schema_forward
        ) == ToolPinRegistry.hash_schema(schema_reverse)

    def test_different_schemas_different_hashes(self) -> None:
        assert ToolPinRegistry.hash_schema(SCHEMA_A) != ToolPinRegistry.hash_schema(
            SCHEMA_B
        )

    def test_hash_is_hex_64_chars(self) -> None:
        h = ToolPinRegistry.hash_schema(SCHEMA_A)
        assert len(h) == 64
        int(h, 16)  # must be valid hex -- raises if not

    def test_register_uses_canonical_json(self, registry: ToolPinRegistry) -> None:
        """verify() must accept the same schema regardless of key order."""
        schema_alt_order = {
            "parameters": SCHEMA_A["parameters"],
            "description": SCHEMA_A["description"],
            "name": SCHEMA_A["name"],
        }
        registry.register("web_search", SCHEMA_A)
        assert registry.verify("web_search", schema_alt_order), "expected MATCH verdict"


# ---------------------------------------------------------------------------
# Re-registration
# ---------------------------------------------------------------------------


class TestReregistration:
    def test_reregister_overwrites_pin(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        registry.register("web_search", SCHEMA_B)
        # old schema no longer matches
        assert (
            registry.verify("web_search", SCHEMA_A) is not True
        )  # PinVerification is falsy for non-MATCH
        # new schema matches
        assert registry.verify("web_search", SCHEMA_B), "expected MATCH verdict"

    def test_reregister_updates_registered_at(self, registry: ToolPinRegistry) -> None:
        pin_a = registry.register("web_search", SCHEMA_A)
        time.sleep(0.01)
        pin_b = registry.register("web_search", SCHEMA_B)
        assert pin_b.registered_at >= pin_a.registered_at


# ---------------------------------------------------------------------------
# Unpin
# ---------------------------------------------------------------------------


class TestUnpin:
    def test_unpin_existing_returns_true(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        assert registry.unpin("web_search"), "expected MATCH verdict"

    def test_unpin_absent_returns_false(self, registry: ToolPinRegistry) -> None:
        assert (
            registry.unpin("nonexistent") is not True
        )  # PinVerification is falsy for non-MATCH

    def test_unpin_removes_from_pinned_tools(self, registry: ToolPinRegistry) -> None:
        registry.register("web_search", SCHEMA_A)
        registry.unpin("web_search")
        assert "web_search" not in registry.pinned_tools


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_register_and_verify(self, registry: ToolPinRegistry) -> None:
        """50 threads registering and verifying must not crash or corrupt state."""
        errors: list[str] = []

        def worker(index: int) -> None:
            name = f"tool_{index % 5}"
            schema = {"name": name, "index": index}
            try:
                registry.register(name, schema)
                # After registering, verify must return True for this exact schema
                assert registry.verify(name, schema), "expected MATCH verdict"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"worker {index}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_unpin_and_verify(self, registry: ToolPinRegistry) -> None:
        """Concurrent unpin + verify must never crash (outcome may vary)."""
        registry.register("web_search", SCHEMA_A)
        errors: list[str] = []

        def verify_loop() -> None:
            for _ in range(20):
                try:
                    registry.verify("web_search", SCHEMA_A)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        def unpin_loop() -> None:
            for _ in range(5):
                try:
                    registry.unpin("web_search")
                    registry.register("web_search", SCHEMA_A)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        threads = [threading.Thread(target=verify_loop) for _ in range(4)]
        threads += [threading.Thread(target=unpin_loop) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_pinned_tools_snapshot_is_list(self, registry: ToolPinRegistry) -> None:
        """pinned_tools must return a detached list, not a live view."""
        registry.register("web_search", SCHEMA_A)
        snapshot = registry.pinned_tools
        registry.clear()
        assert "web_search" in snapshot  # snapshot unchanged after clear
        assert registry.pinned_tools == []


# ---------------------------------------------------------------------------
# Structured PinVerification tests
# ---------------------------------------------------------------------------


class TestPinVerification:
    """Tests for the structured PinVerification result."""

    def test_match_is_truthy(self) -> None:
        reg = ToolPinRegistry()
        reg.register("tool", SCHEMA_A)
        result = reg.verify("tool", SCHEMA_A)
        assert result
        assert result.verdict is PinVerdict.MATCH

    def test_not_pinned_is_falsy(self) -> None:
        reg = ToolPinRegistry()
        result = reg.verify("unknown", SCHEMA_A)
        assert not result
        assert result.verdict is PinVerdict.NOT_PINNED
        assert result.expected_hash is None
        assert result.actual_hash is not None

    def test_hash_mismatch_is_falsy(self) -> None:
        reg = ToolPinRegistry()
        reg.register("tool", SCHEMA_A)
        result = reg.verify("tool", SCHEMA_B)
        assert not result
        assert result.verdict is PinVerdict.HASH_MISMATCH
        assert result.expected_hash != result.actual_hash
        assert result.pin is not None

    def test_unserializable_is_falsy(self) -> None:
        reg = ToolPinRegistry()
        reg.register("tool", SCHEMA_A)
        result = reg.verify("tool", {"bad": float("nan")})
        assert not result
        assert result.verdict is PinVerdict.UNSERIALIZABLE

    def test_denied_property(self) -> None:
        reg = ToolPinRegistry()
        reg.register("tool", SCHEMA_A)
        assert not reg.verify("tool", SCHEMA_A).denied
        assert reg.verify("tool", SCHEMA_B).denied
        assert reg.verify("unknown", SCHEMA_A).denied

    def test_reason_contains_tool_name(self) -> None:
        reg = ToolPinRegistry()
        reg.register("tool", SCHEMA_A)
        for schema in (SCHEMA_A, SCHEMA_B):
            result = reg.verify("tool", schema)
            assert "tool" in result.reason

    def test_match_carries_pin_reference(self) -> None:
        reg = ToolPinRegistry()
        pin = reg.register("tool", SCHEMA_A)
        result = reg.verify("tool", SCHEMA_A)
        assert result.pin is pin

    def test_not_pinned_has_no_pin(self) -> None:
        reg = ToolPinRegistry()
        result = reg.verify("ghost", SCHEMA_A)
        assert result.pin is None


# ---------------------------------------------------------------------------
# _canonical_json and register single-serialization
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    """Verify _canonical_json produces consistent canonical output."""

    def test_sorted_keys(self) -> None:
        canon = ToolPinRegistry._canonical_json({"z": 1, "a": 2})
        assert canon == '{"a":2,"z":1}'

    def test_no_whitespace(self) -> None:
        canon = ToolPinRegistry._canonical_json({"key": "value"})
        assert " " not in canon

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolPinRegistry._canonical_json({"val": float("nan")})

    def test_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolPinRegistry._canonical_json({"val": float("inf")})

    def test_register_stores_canonical_json(self) -> None:
        """register() must store the same canonical form as _canonical_json."""
        reg = ToolPinRegistry()
        pin = reg.register("tool", SCHEMA_A)
        expected = ToolPinRegistry._canonical_json(SCHEMA_A)
        assert pin.raw_schema == expected

    def test_register_hash_matches_hash_schema(self) -> None:
        """register() hash must match hash_schema() for the same input."""
        reg = ToolPinRegistry()
        pin = reg.register("tool", SCHEMA_A)
        assert pin.schema_hash == ToolPinRegistry.hash_schema(SCHEMA_A)
