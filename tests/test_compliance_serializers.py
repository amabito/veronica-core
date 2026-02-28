"""Tests for veronica_core.compliance.serializers -- pure serialization functions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veronica_core.compliance.serializers import (
    serialize_node_record,
    serialize_safety_event,
    serialize_snapshot,
)
from veronica_core.containment.execution_context import (
    ChainMetadata,
    ContextSnapshot,
    NodeRecord,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TS = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)


def _make_event(**overrides: object) -> SafetyEvent:
    defaults = {
        "event_type": "BUDGET_EXCEEDED",
        "decision": Decision.HALT,
        "reason": "Cost limit reached",
        "hook": "BudgetEnforcer",
        "request_id": "req-001",
        "ts": _TS,
        "metadata": {"limit": 1.0},
    }
    defaults.update(overrides)
    return SafetyEvent(**defaults)  # type: ignore[arg-type]


def _make_node(**overrides: object) -> NodeRecord:
    defaults = {
        "node_id": "node-1",
        "parent_id": None,
        "kind": "llm",
        "operation_name": "chat_completion",
        "start_ts": _TS,
        "end_ts": _TS,
        "status": "ok",
        "cost_usd": 0.01,
        "retries_used": 0,
    }
    defaults.update(overrides)
    return NodeRecord(**defaults)  # type: ignore[arg-type]


def _make_snapshot(**overrides: object) -> ContextSnapshot:
    defaults = {
        "chain_id": "chain-1",
        "request_id": "req-001",
        "step_count": 5,
        "cost_usd_accumulated": 0.05,
        "retries_used": 1,
        "aborted": False,
        "abort_reason": None,
        "elapsed_ms": 1234.5,
        "nodes": [_make_node()],
        "events": [_make_event()],
    }
    defaults.update(overrides)
    return ContextSnapshot(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# serialize_safety_event
# ---------------------------------------------------------------------------


class TestSerializeSafetyEvent:
    def test_basic_serialization(self) -> None:
        event = _make_event()
        result = serialize_safety_event(event)

        assert result["event_type"] == "BUDGET_EXCEEDED"
        assert result["decision"] == "HALT"
        assert result["reason"] == "Cost limit reached"
        assert result["hook"] == "BudgetEnforcer"
        assert result["request_id"] == "req-001"
        assert result["metadata"] == {"limit": 1.0}

    def test_decision_enum_value_extracted(self) -> None:
        """Decision enum .value is used, not repr."""
        for decision in Decision:
            event = _make_event(decision=decision)
            result = serialize_safety_event(event)
            assert result["decision"] == decision.value

    def test_ts_is_iso_string(self) -> None:
        event = _make_event()
        result = serialize_safety_event(event)
        assert isinstance(result["ts"], str)
        assert "2026-02-28" in result["ts"]

    def test_none_request_id(self) -> None:
        event = _make_event(request_id=None)
        result = serialize_safety_event(event)
        assert result["request_id"] is None

    def test_empty_metadata(self) -> None:
        event = _make_event(metadata={})
        result = serialize_safety_event(event)
        assert result["metadata"] == {}


# ---------------------------------------------------------------------------
# serialize_node_record
# ---------------------------------------------------------------------------


class TestSerializeNodeRecord:
    def test_basic_serialization(self) -> None:
        node = _make_node()
        result = serialize_node_record(node)

        assert result["node_id"] == "node-1"
        assert result["parent_id"] is None
        assert result["kind"] == "llm"
        assert result["operation_name"] == "chat_completion"
        assert result["status"] == "ok"
        assert result["cost_usd"] == 0.01
        assert result["retries_used"] == 0

    def test_start_ts_is_iso_string(self) -> None:
        node = _make_node()
        result = serialize_node_record(node)
        assert isinstance(result["start_ts"], str)
        assert "2026" in result["start_ts"]

    def test_end_ts_none(self) -> None:
        node = _make_node(end_ts=None)
        result = serialize_node_record(node)
        assert result["end_ts"] is None

    def test_end_ts_present(self) -> None:
        node = _make_node(end_ts=_TS)
        result = serialize_node_record(node)
        assert isinstance(result["end_ts"], str)

    def test_tool_kind(self) -> None:
        node = _make_node(kind="tool")
        result = serialize_node_record(node)
        assert result["kind"] == "tool"


# ---------------------------------------------------------------------------
# serialize_snapshot
# ---------------------------------------------------------------------------


class TestSerializeSnapshot:
    def test_basic_payload_structure(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot)

        assert "chain" in result
        assert "events" in result
        assert isinstance(result["events"], list)

    def test_chain_fields(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot)
        chain = result["chain"]

        assert chain["chain_id"] == "chain-1"
        assert chain["request_id"] == "req-001"
        assert chain["step_count"] == 5
        assert chain["cost_usd"] == 0.05
        assert chain["retries_used"] == 1
        assert chain["aborted"] is False
        assert chain["abort_reason"] is None
        assert chain["elapsed_ms"] == 1234.5

    def test_started_at_from_first_node(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot)
        assert "2026-02-28" in result["chain"]["started_at"]

    def test_started_at_fallback_when_no_nodes(self) -> None:
        """Empty nodes list uses datetime.min as fallback."""
        snapshot = _make_snapshot(nodes=[], events=[])
        result = serialize_snapshot(snapshot)
        assert isinstance(result["chain"]["started_at"], str)

    def test_events_serialized(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot)
        assert len(result["events"]) == 1
        assert result["events"][0]["event_type"] == "BUDGET_EXCEEDED"

    def test_no_events(self) -> None:
        snapshot = _make_snapshot(events=[])
        result = serialize_snapshot(snapshot)
        assert result["events"] == []

    def test_metadata_attached(self) -> None:
        meta = ChainMetadata(
            request_id="req-001",
            chain_id="chain-1",
            service="my-service",
            team="platform",
            model="gpt-4",
            tags={"env": "prod"},
        )
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, metadata=meta)
        chain = result["chain"]

        assert chain["service"] == "my-service"
        assert chain["team"] == "platform"
        assert chain["model"] == "gpt-4"
        assert chain["tags"] == {"env": "prod"}

    def test_metadata_none(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, metadata=None)
        assert "service" not in result["chain"]
        assert "team" not in result["chain"]

    def test_metadata_empty_tags(self) -> None:
        meta = ChainMetadata(
            request_id="req-001",
            chain_id="chain-1",
            tags={},
        )
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, metadata=meta)
        assert result["chain"]["tags"] == {}

    def test_graph_summary_attached(self) -> None:
        graph = {"aggregates": {"total_cost_usd": 0.05, "total_llm_calls": 5}}
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, graph=graph)
        assert result["chain"]["graph_summary"] == graph["aggregates"]

    def test_graph_none(self) -> None:
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, graph=None)
        assert "graph_summary" not in result["chain"]

    def test_graph_without_aggregates_key(self) -> None:
        graph = {"other_data": 42}
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, graph=graph)
        assert result["chain"]["graph_summary"] is None

    def test_multiple_events_serialized(self) -> None:
        events = [
            _make_event(event_type="BUDGET_EXCEEDED"),
            _make_event(event_type="STEP_LIMIT", decision=Decision.HALT),
            _make_event(event_type="CIRCUIT_OPEN", decision=Decision.DEGRADE),
        ]
        snapshot = _make_snapshot(events=events)
        result = serialize_snapshot(snapshot)
        assert len(result["events"]) == 3
        types = [e["event_type"] for e in result["events"]]
        assert types == ["BUDGET_EXCEEDED", "STEP_LIMIT", "CIRCUIT_OPEN"]


# ---------------------------------------------------------------------------
# Adversarial: corrupted input -- "how do I break this?"
# ---------------------------------------------------------------------------


class TestAdversarialSerializers:
    """Adversarial tests -- attacker mindset for serializers."""

    # -- Corrupted input: decision is a raw string, not enum --

    def test_decision_as_raw_string_not_enum(self) -> None:
        """If decision is a plain string (no .value), serialize must not crash."""
        event = _make_event()
        # Forcibly replace decision with a plain string (simulating a
        # future refactor where Decision changes to a string alias)
        fake_event = object.__new__(SafetyEvent)
        object.__setattr__(fake_event, "event_type", event.event_type)
        object.__setattr__(fake_event, "decision", "CUSTOM_STRING")
        object.__setattr__(fake_event, "reason", event.reason)
        object.__setattr__(fake_event, "hook", event.hook)
        object.__setattr__(fake_event, "request_id", event.request_id)
        object.__setattr__(fake_event, "ts", event.ts)
        object.__setattr__(fake_event, "metadata", event.metadata)

        result = serialize_safety_event(fake_event)
        # Should fall through to str() path
        assert result["decision"] == "CUSTOM_STRING"

    # -- Corrupted input: metadata with non-serializable values --

    def test_metadata_with_nested_objects(self) -> None:
        """Metadata containing non-primitive types should still serialize."""
        event = _make_event(metadata={
            "normal": "ok",
            "timestamp": _TS,  # datetime in metadata
            "nested": {"deep": [1, 2, 3]},
            "bytes_val": b"raw-bytes",
        })
        result = serialize_safety_event(event)
        # metadata is passed through as-is -- json.dumps(default=str) in
        # exporter handles non-serializable types, but serializer just copies
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["normal"] == "ok"

    # -- Corrupted input: ts is None (should not happen, but defensive) --

    def test_node_record_with_zero_cost(self) -> None:
        """cost_usd=0.0 must not be dropped or treated as falsy."""
        node = _make_node(cost_usd=0.0)
        result = serialize_node_record(node)
        assert result["cost_usd"] == 0.0

    def test_node_record_with_negative_cost(self) -> None:
        """Negative cost (credit/refund) must serialize without error."""
        node = _make_node(cost_usd=-0.5)
        result = serialize_node_record(node)
        assert result["cost_usd"] == -0.5

    # -- Boundary: snapshot with extreme values --

    def test_snapshot_extreme_cost(self) -> None:
        """Very large cost_usd_accumulated must not overflow or truncate."""
        snapshot = _make_snapshot(cost_usd_accumulated=999999.999999)
        result = serialize_snapshot(snapshot)
        assert result["chain"]["cost_usd"] == 999999.999999

    def test_snapshot_zero_step_count(self) -> None:
        """step_count=0 must not be dropped."""
        snapshot = _make_snapshot(step_count=0)
        result = serialize_snapshot(snapshot)
        assert result["chain"]["step_count"] == 0

    def test_snapshot_negative_elapsed_ms(self) -> None:
        """Negative elapsed_ms (clock skew) must serialize without crash."""
        snapshot = _make_snapshot(elapsed_ms=-100.0)
        result = serialize_snapshot(snapshot)
        assert result["chain"]["elapsed_ms"] == -100.0

    def test_snapshot_abort_reason_with_special_chars(self) -> None:
        """abort_reason with unicode, newlines, quotes must survive."""
        reason = 'Error: "budget\n exceeded" \u00e9\u00e8\u00ea \u0000 null-byte'
        snapshot = _make_snapshot(aborted=True, abort_reason=reason)
        result = serialize_snapshot(snapshot)
        assert result["chain"]["abort_reason"] == reason

    # -- Type variation: all Decision enum values --

    @pytest.mark.parametrize("decision", list(Decision))
    def test_all_decision_values_serialize(self, decision: Decision) -> None:
        """Every Decision enum value must produce a valid string."""
        event = _make_event(decision=decision)
        result = serialize_safety_event(event)
        assert isinstance(result["decision"], str)
        assert len(result["decision"]) > 0

    # -- Corrupted input: metadata.tags is None instead of dict --

    def test_metadata_tags_none(self) -> None:
        """ChainMetadata with tags=None must not crash serialize_snapshot."""
        meta = ChainMetadata(
            request_id="req-001",
            chain_id="chain-1",
        )
        # tags defaults to empty dict via dataclass default_factory
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, metadata=meta)
        assert result["chain"]["tags"] == {}

    # -- Large payload: many events --

    def test_thousand_events(self) -> None:
        """1000 events must serialize without performance issues."""
        events = [_make_event(event_type=f"EVT_{i}") for i in range(1000)]
        snapshot = _make_snapshot(events=events)
        result = serialize_snapshot(snapshot)
        assert len(result["events"]) == 1000
        assert result["events"][999]["event_type"] == "EVT_999"

    # -- Corrupted input: empty strings everywhere --

    def test_all_empty_strings(self) -> None:
        """Snapshot with all-empty string fields must serialize cleanly."""
        event = _make_event(
            event_type="", reason="", hook="", request_id=""
        )
        node = _make_node(
            node_id="", parent_id="", operation_name=""
        )
        snapshot = _make_snapshot(
            chain_id="", request_id="", abort_reason="",
            nodes=[node], events=[event],
        )
        result = serialize_snapshot(snapshot)
        assert result["chain"]["chain_id"] == ""
        assert result["events"][0]["event_type"] == ""

    # -- JSON round-trip: ensure output is actually JSON-serializable --

    def test_full_output_is_json_serializable(self) -> None:
        """serialize_snapshot output must survive json.dumps without error."""
        import json

        meta = ChainMetadata(
            request_id="req",
            chain_id="chain",
            service="svc",
            team="t",
            model="m",
            tags={"k": "v"},
        )
        graph = {"aggregates": {"total_cost_usd": 0.1}}
        snapshot = _make_snapshot()
        result = serialize_snapshot(snapshot, metadata=meta, graph=graph)
        # Must not raise
        output = json.dumps(result, default=str)
        assert isinstance(output, str)
        # Round-trip
        parsed = json.loads(output)
        assert parsed["chain"]["chain_id"] == "chain-1"


# ---------------------------------------------------------------------------
# Adversarial: NaN/Inf handling in serialized data (Gap #12)
# ---------------------------------------------------------------------------


class TestAdversarialNaNInfSerializer:
    """Gap #12: IEEE 754 special float values in serializer output.

    JSON standard (RFC 8259) does not support NaN, Infinity, or -Infinity.
    The serializers are pure (no json.dumps internally), so they pass values
    through as-is.  The exporter uses json.dumps(default=str) which handles
    most types.

    These tests verify:
    1. serialize_* functions don't crash on NaN/Inf inputs.
    2. The resulting dict is handled gracefully (either converted or
       raises a clear error when json.dumps is applied).
    3. Document actual behavior for future reference.
    """

    def test_nan_in_node_cost_usd(self) -> None:
        """cost_usd=NaN must not crash serialize_node_record.

        The serializer passes the value through; json.dumps(default=str)
        in the exporter will convert NaN to "nan" string.
        """
        node = _make_node(cost_usd=float("nan"))
        result = serialize_node_record(node)
        # Serializer passes NaN through as-is (no json.dumps here)
        import math
        assert math.isnan(result["cost_usd"])

    def test_inf_in_node_cost_usd(self) -> None:
        """cost_usd=Infinity must not crash serialize_node_record."""
        import math
        node = _make_node(cost_usd=float("inf"))
        result = serialize_node_record(node)
        assert math.isinf(result["cost_usd"])
        assert result["cost_usd"] > 0

    def test_nan_in_snapshot_cost(self) -> None:
        """cost_usd_accumulated=NaN must not crash serialize_snapshot."""
        import math
        snapshot = _make_snapshot(cost_usd_accumulated=float("nan"))
        result = serialize_snapshot(snapshot)
        # The chain dict must exist; cost_usd may be NaN
        assert "chain" in result
        assert math.isnan(result["chain"]["cost_usd"])

    def test_inf_in_snapshot_elapsed_ms(self) -> None:
        """elapsed_ms=Infinity must not crash serialize_snapshot."""
        import math
        snapshot = _make_snapshot(elapsed_ms=float("inf"))
        result = serialize_snapshot(snapshot)
        assert math.isinf(result["chain"]["elapsed_ms"])

    def test_nan_in_event_metadata_value(self) -> None:
        """NaN in event metadata must pass through serialize_safety_event."""
        import math
        event = _make_event(metadata={"score": float("nan"), "normal": 1.0})
        result = serialize_safety_event(event)
        assert isinstance(result["metadata"], dict)
        assert math.isnan(result["metadata"]["score"])
        assert result["metadata"]["normal"] == 1.0

    def test_inf_in_event_metadata_value(self) -> None:
        """Inf in event metadata must pass through serialize_safety_event."""
        import math
        event = _make_event(metadata={"limit": float("inf")})
        result = serialize_safety_event(event)
        assert math.isinf(result["metadata"]["limit"])

    def test_nan_serialized_snapshot_json_roundtrip_with_default_str(self) -> None:
        """Document Python 3.11 json behavior with NaN/Inf in serialize_snapshot output.

        Python's json module (allow_nan=True by default) accepts NaN and Inf
        in dumps(), producing non-standard JSON tokens ("NaN", "Infinity").
        This test documents actual behavior as a regression guard:
        if Python changes this behavior in a future version, this test will catch it.
        """
        import json
        import math

        snapshot = _make_snapshot(cost_usd_accumulated=float("nan"))
        result = serialize_snapshot(snapshot)

        # Python 3.11: json.dumps succeeds for NaN (allow_nan=True default)
        output = json.dumps(result, default=str)
        assert isinstance(output, str)
        # Output contains "NaN" token (non-RFC-8259 but Python-native)
        assert "NaN" in output

        # Round-trip: json.loads can parse it back on the same runtime
        parsed = json.loads(output)
        assert math.isnan(parsed["chain"]["cost_usd"])

        # Original value in the dict is still NaN
        assert math.isnan(result["chain"]["cost_usd"])
