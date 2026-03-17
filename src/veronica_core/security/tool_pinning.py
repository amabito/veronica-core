"""Tool description pinning -- hash tool schemas at registration, verify at invocation.

Fail-closed design: a tool that is not pinned is untrusted. verify() returns
False for any tool name that has not been explicitly registered.

Typical use:
    registry = ToolPinRegistry()
    registry.register("web_search", tool_schema_dict)

    # At invocation time:
    result = registry.verify("web_search", received_schema)
    if not result:
        raise PermissionError(result.reason)
    # result.verdict, result.expected_hash, result.actual_hash, result.pin
    # are available for policy decisions and audit logging.
"""

from __future__ import annotations

import enum
import hashlib
import json
import threading
import time
from dataclasses import dataclass


class PinVerdict(enum.Enum):
    """Outcome of a tool schema pin verification.

    Structured result instead of bare bool so that policy engines and
    audit systems can distinguish *why* verification failed, not just
    *that* it failed.
    """

    MATCH = "match"
    """Schema hash matches the pinned value."""

    NOT_PINNED = "not_pinned"
    """Tool was never registered. Fail-closed: treated as untrusted."""

    HASH_MISMATCH = "hash_mismatch"
    """Schema hash differs from the pin. Possible schema mutation / tool poisoning."""

    UNSERIALIZABLE = "unserializable"
    """Schema could not be serialized to canonical JSON (NaN, custom objects, etc.)."""


@dataclass(frozen=True)
class PinVerification:
    """Structured result of a tool schema pin check.

    Designed to flow into PolicyDecision / AuditLog / ShieldPipeline
    without losing context. ``bool(result)`` is True only for MATCH,
    so existing ``if registry.verify(...)`` call sites still work.

    Attributes:
        verdict: Why verification succeeded or failed.
        tool_name: The tool that was checked.
        expected_hash: Hash from the pin (None if not pinned).
        actual_hash: Hash of the presented schema (None if unserializable).
        pin: The full ToolSchemaPin if one existed, else None.
    """

    verdict: PinVerdict
    tool_name: str
    expected_hash: str | None = None
    actual_hash: str | None = None
    pin: ToolSchemaPin | None = None

    def __bool__(self) -> bool:
        """True only when schema matches the pin."""
        return self.verdict is PinVerdict.MATCH

    @property
    def denied(self) -> bool:
        """True when the tool should be blocked (anything except MATCH)."""
        return self.verdict is not PinVerdict.MATCH

    @property
    def reason(self) -> str:
        """Human-readable one-line reason, suitable for audit logs."""
        if self.verdict is PinVerdict.MATCH:
            return f"tool '{self.tool_name}' schema verified"
        if self.verdict is PinVerdict.NOT_PINNED:
            return f"tool '{self.tool_name}' is not pinned (fail-closed)"
        if self.verdict is PinVerdict.HASH_MISMATCH:
            return (
                f"tool '{self.tool_name}' schema hash mismatch: "
                f"expected {self.expected_hash!s:.16}..., "
                f"got {self.actual_hash!s:.16}..."
            )
        return f"tool '{self.tool_name}' schema unserializable (fail-closed)"


@dataclass(frozen=True)
class ToolSchemaPin:
    """Immutable record of a tool's schema at registration time."""

    tool_name: str
    schema_hash: str  # SHA-256 of canonical JSON
    registered_at: float  # time.monotonic()
    raw_schema: str  # canonical JSON (for debugging)


class ToolPinRegistry:
    """Thread-safe registry for tool schema pins.

    Fail-closed: if a tool is not pinned, verify() returns False.
    Re-registration overwrites the existing pin.
    """

    def __init__(self) -> None:
        self._pins: dict[str, ToolSchemaPin] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _canonical_json(schema: dict) -> str:
        """Return the canonical JSON representation of *schema*.

        Canonical form: keys sorted, no extra whitespace, allow_nan=False.
        This makes the output independent of key insertion order and rejects
        ambiguous IEEE 754 values (NaN/Infinity).

        Raises:
            TypeError: If schema contains non-serializable values.
            ValueError: If schema contains NaN/Infinity.
        """
        return json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def hash_schema(schema: dict) -> str:
        """Return the SHA-256 hex digest of the canonical JSON representation.

        Args:
            schema: Tool schema as a plain dict. Must be JSON-serializable.

        Returns:
            Lowercase hex string (64 characters).

        Raises:
            TypeError: If schema contains non-serializable values.
            ValueError: If schema contains NaN/Infinity.
        """
        canonical = ToolPinRegistry._canonical_json(schema)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def register(self, tool_name: str, schema: dict) -> ToolSchemaPin:
        """Pin a tool's schema. Overwrites any existing pin for the same name.

        Args:
            tool_name: Unique name of the tool.
            schema: Dict describing the tool (name, description, parameters, ...).

        Returns:
            The newly created ToolSchemaPin.
        """
        canonical = self._canonical_json(schema)
        schema_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        pin = ToolSchemaPin(
            tool_name=tool_name,
            schema_hash=schema_hash,
            registered_at=time.monotonic(),
            raw_schema=canonical,
        )
        with self._lock:
            self._pins[tool_name] = pin
        return pin

    def verify(self, tool_name: str, schema: dict) -> PinVerification:
        """Check that *schema* matches the pinned hash for *tool_name*.

        Returns a structured PinVerification that is truthy on match and
        falsy otherwise, so ``if registry.verify(name, schema):`` still
        works. The result carries verdict, hashes, and the pin record for
        downstream policy engines and audit logs.

        Fail-closed: any failure mode (not pinned, hash mismatch,
        unserializable schema) returns a falsy PinVerification.

        The hash is computed before acquiring the lock, then compared
        inside the lock to avoid TOCTOU races with concurrent unpin/clear.

        Args:
            tool_name: Name to look up in the registry.
            schema: Schema presented at invocation time.

        Returns:
            PinVerification with verdict, hashes, and pin reference.
        """
        try:
            schema_hash = self.hash_schema(schema)
        except (TypeError, ValueError, OverflowError):
            return PinVerification(
                verdict=PinVerdict.UNSERIALIZABLE,
                tool_name=tool_name,
            )
        with self._lock:
            pin = self._pins.get(tool_name)
            if pin is None:
                return PinVerification(
                    verdict=PinVerdict.NOT_PINNED,
                    tool_name=tool_name,
                    actual_hash=schema_hash,
                )
            if pin.schema_hash == schema_hash:
                return PinVerification(
                    verdict=PinVerdict.MATCH,
                    tool_name=tool_name,
                    expected_hash=pin.schema_hash,
                    actual_hash=schema_hash,
                    pin=pin,
                )
            return PinVerification(
                verdict=PinVerdict.HASH_MISMATCH,
                tool_name=tool_name,
                expected_hash=pin.schema_hash,
                actual_hash=schema_hash,
                pin=pin,
            )

    def is_pinned(self, tool_name: str) -> bool:
        """Return True if *tool_name* has a registered pin."""
        with self._lock:
            return tool_name in self._pins

    def get_pin(self, tool_name: str) -> ToolSchemaPin | None:
        """Return the pin for *tool_name*, or None if not registered."""
        with self._lock:
            return self._pins.get(tool_name)

    def unpin(self, tool_name: str) -> bool:
        """Remove the pin for *tool_name*.

        Returns:
            True if a pin existed and was removed, False if it was absent.
        """
        with self._lock:
            return self._pins.pop(tool_name, None) is not None

    def clear(self) -> None:
        """Remove all pins from the registry."""
        with self._lock:
            self._pins.clear()

    @property
    def pinned_tools(self) -> list[str]:
        """Return a snapshot list of all currently pinned tool names."""
        with self._lock:
            return list(self._pins.keys())


__all__ = [
    "PinVerdict",
    "PinVerification",
    "ToolSchemaPin",
    "ToolPinRegistry",
]
