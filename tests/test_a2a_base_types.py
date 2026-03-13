"""Tests for veronica_core.adapters._a2a_base types.

Covers A2AClientConfig, A2AServerConfig, A2AIncomingRequest, A2AResult,
A2AStats, A2AMessageCost, A2AServerDecision, A2AStreamEvent.
"""

from __future__ import annotations

import pytest

from veronica_core.adapters._a2a_base import (
    A2AClientConfig,
    A2AIncomingRequest,
    A2AMessageCost,
    A2AResult,
    A2AServerConfig,
    A2AServerDecision,
    A2AStats,
    A2AStreamEvent,
)
from veronica_core.a2a.types import AgentIdentity, TrustLevel
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_identity(agent_id: str = "sender") -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, origin="a2a")


# ---------------------------------------------------------------------------
# A2AClientConfig
# ---------------------------------------------------------------------------


class TestA2AClientConfig:
    def test_default_construction(self) -> None:
        cfg = A2AClientConfig()
        assert cfg.default_cost_per_message == 0.01
        assert cfg.timeout_seconds == 30.0
        assert cfg.max_poll_attempts == 100
        assert cfg.max_state_transitions == 8

    def test_frozen_immutable(self) -> None:
        cfg = A2AClientConfig()
        with pytest.raises((TypeError, AttributeError)):
            cfg.default_cost_per_message = 0.5  # type: ignore[misc]

    def test_negative_cost_raises(self) -> None:
        with pytest.raises(ValueError, match="default_cost_per_message"):
            A2AClientConfig(default_cost_per_message=-0.01)

    def test_zero_cost_allowed(self) -> None:
        cfg = A2AClientConfig(default_cost_per_message=0.0)
        assert cfg.default_cost_per_message == 0.0

    def test_zero_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            A2AClientConfig(timeout_seconds=0.0)

    def test_negative_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            A2AClientConfig(timeout_seconds=-1.0)

    def test_none_timeout_allowed(self) -> None:
        cfg = A2AClientConfig(timeout_seconds=None)
        assert cfg.timeout_seconds is None

    def test_zero_poll_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_poll_attempts"):
            A2AClientConfig(max_poll_attempts=0)

    def test_negative_poll_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_poll_attempts"):
            A2AClientConfig(max_poll_attempts=-1)

    def test_zero_state_transitions_raises(self) -> None:
        with pytest.raises(ValueError, match="max_state_transitions"):
            A2AClientConfig(max_state_transitions=0)

    # Boundary triple: around default_cost_per_message=0
    @pytest.mark.parametrize("cost", [-0.001, 0.0, 0.001])
    def test_cost_boundary_triple(self, cost: float) -> None:
        if cost < 0:
            with pytest.raises(ValueError):
                A2AClientConfig(default_cost_per_message=cost)
        else:
            cfg = A2AClientConfig(default_cost_per_message=cost)
            assert cfg.default_cost_per_message == cost


# ---------------------------------------------------------------------------
# A2AServerConfig
# ---------------------------------------------------------------------------


class TestA2AServerConfig:
    def test_default_construction(self) -> None:
        cfg = A2AServerConfig()
        assert cfg.max_message_size_bytes == 1_048_576
        assert cfg.max_requests_per_minute_per_tenant == 600
        assert cfg.max_requests_per_minute_per_sender == 60
        assert cfg.fail_closed is True

    def test_frozen_immutable(self) -> None:
        cfg = A2AServerConfig()
        with pytest.raises((TypeError, AttributeError)):
            cfg.fail_closed = False  # type: ignore[misc]

    def test_zero_message_size_raises(self) -> None:
        with pytest.raises(ValueError, match="max_message_size_bytes"):
            A2AServerConfig(max_message_size_bytes=0)

    def test_negative_tenant_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="max_requests_per_minute_per_tenant"):
            A2AServerConfig(max_requests_per_minute_per_tenant=0)

    def test_negative_sender_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="max_requests_per_minute_per_sender"):
            A2AServerConfig(max_requests_per_minute_per_sender=0)

    def test_fail_open_allowed(self) -> None:
        cfg = A2AServerConfig(fail_closed=False)
        assert cfg.fail_closed is False

    # Boundary triple: message size around 1
    @pytest.mark.parametrize("size,should_raise", [(0, True), (1, False), (2, False)])
    def test_message_size_boundary_triple(self, size: int, should_raise: bool) -> None:
        if should_raise:
            with pytest.raises(ValueError):
                A2AServerConfig(max_message_size_bytes=size)
        else:
            cfg = A2AServerConfig(max_message_size_bytes=size)
            assert cfg.max_message_size_bytes == size


# ---------------------------------------------------------------------------
# A2AIncomingRequest
# ---------------------------------------------------------------------------


VALID_OPERATIONS = [
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "CancelTask",
    "ListTasks",
    "SubscribeToTask",
]


class TestA2AIncomingRequest:
    def test_valid_send_message(self) -> None:
        req = A2AIncomingRequest(
            operation="SendMessage",
            tenant_id="t1",
            sender_identity=_make_identity(),
        )
        assert req.operation == "SendMessage"
        assert req.tenant_id == "t1"

    @pytest.mark.parametrize("op", VALID_OPERATIONS)
    def test_all_valid_operations(self, op: str) -> None:
        req = A2AIncomingRequest(
            operation=op,
            tenant_id="t1",
            sender_identity=_make_identity(),
        )
        assert req.operation == op

    def test_invalid_operation_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            A2AIncomingRequest(
                operation="HackAgent",
                tenant_id="t1",
                sender_identity=_make_identity(),
            )

    def test_empty_tenant_id_raises(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="",
                sender_identity=_make_identity(),
            )

    def test_frozen_immutable(self) -> None:
        req = A2AIncomingRequest(
            operation="SendMessage",
            tenant_id="t1",
            sender_identity=_make_identity(),
        )
        with pytest.raises((TypeError, AttributeError)):
            req.tenant_id = "t2"  # type: ignore[misc]

    # Adversarial: garbage operation must not produce silent success
    @pytest.mark.parametrize("bad_op", ["", "sendmessage", "SEND_MESSAGE", "\x00", None])
    def test_corrupted_operation_raises(self, bad_op: object) -> None:
        with pytest.raises((ValueError, TypeError)):
            A2AIncomingRequest(
                operation=bad_op,  # type: ignore[arg-type]
                tenant_id="t1",
                sender_identity=_make_identity(),
            )


# ---------------------------------------------------------------------------
# A2AResult
# ---------------------------------------------------------------------------


class TestA2AResult:
    def test_success_construction(self) -> None:
        result = A2AResult(success=True)
        assert result.success is True
        assert result.error is None
        assert result.decision == Decision.ALLOW
        assert result.cost_usd == 0.0

    def test_failure_construction(self) -> None:
        result = A2AResult(
            success=False,
            error="Agent call failed",
            decision=Decision.HALT,
        )
        assert result.success is False
        assert result.error == "Agent call failed"
        assert result.decision == Decision.HALT

    def test_frozen_immutable(self) -> None:
        result = A2AResult(success=True)
        with pytest.raises((TypeError, AttributeError)):
            result.success = False  # type: ignore[misc]

    def test_error_does_not_leak_exception_type(self) -> None:
        """External error strings must never contain exception class names."""
        result = A2AResult(success=False, error="Agent call failed")
        assert "Error" not in (result.error or "")
        assert "Exception" not in (result.error or "")


# ---------------------------------------------------------------------------
# A2AStats
# ---------------------------------------------------------------------------


class TestA2AStats:
    def test_construction(self) -> None:
        stats = A2AStats(agent_id="agent-1")
        assert stats.agent_id == "agent-1"
        assert stats.message_count == 0
        assert stats.total_cost_usd == 0.0
        assert stats.error_count == 0
        assert stats.trust_level == TrustLevel.UNTRUSTED

    def test_mutable_fields(self) -> None:
        stats = A2AStats(agent_id="agent-1")
        stats.message_count = 5
        assert stats.message_count == 5

    def test_internal_field_not_in_repr(self) -> None:
        stats = A2AStats(agent_id="agent-1")
        assert "_total_latency_ms" not in repr(stats)
        assert "_latency_sample_count" not in repr(stats)


# ---------------------------------------------------------------------------
# A2AMessageCost
# ---------------------------------------------------------------------------


class TestA2AMessageCost:
    def test_construction(self) -> None:
        cost = A2AMessageCost(agent_id="agent-1", cost_per_message=0.05)
        assert cost.agent_id == "agent-1"
        assert cost.cost_per_message == 0.05
        assert cost.cost_per_token == 0.0

    def test_frozen_immutable(self) -> None:
        cost = A2AMessageCost(agent_id="agent-1")
        with pytest.raises((TypeError, AttributeError)):
            cost.cost_per_message = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# A2AServerDecision
# ---------------------------------------------------------------------------


class TestA2AServerDecision:
    def test_allow_decision(self) -> None:
        decision = A2AServerDecision(
            verdict="ALLOW",
            reason="Trust level sufficient",
            sender_trust=TrustLevel.TRUSTED,
        )
        assert decision.verdict == "ALLOW"
        assert decision.degrade_directive is None

    def test_deny_decision(self) -> None:
        decision = A2AServerDecision(
            verdict="DENY",
            reason="Size limit exceeded",
            sender_trust=TrustLevel.UNTRUSTED,
        )
        assert decision.verdict == "DENY"

    def test_reason_does_not_leak_internal_details(self) -> None:
        """Reason must be human-readable, not internal exception details."""
        decision = A2AServerDecision(
            verdict="DENY",
            reason="Request denied",
            sender_trust=TrustLevel.UNTRUSTED,
        )
        assert "RuntimeError" not in decision.reason
        assert "Traceback" not in decision.reason


# ---------------------------------------------------------------------------
# A2AStreamEvent
# ---------------------------------------------------------------------------


class TestA2AStreamEvent:
    def test_construction(self) -> None:
        event = A2AStreamEvent(event_type="status_update", payload={"status": "ok"})
        assert event.event_type == "status_update"
        assert event.chunk_index == 0
        assert event.cumulative_bytes == 0
        assert event.decision == Decision.ALLOW

    def test_frozen_immutable(self) -> None:
        event = A2AStreamEvent(event_type="message", payload="hello")
        with pytest.raises((TypeError, AttributeError)):
            event.chunk_index = 5  # type: ignore[misc]

    def test_invalid_event_type_raises(self) -> None:
        with pytest.raises(ValueError, match="event_type"):
            A2AStreamEvent(event_type="hack", payload={})

    @pytest.mark.parametrize("val", [-1, True, False])
    def test_invalid_chunk_index(self, val: object) -> None:
        with pytest.raises((ValueError, TypeError)):
            A2AStreamEvent(event_type="message", payload={}, chunk_index=val)  # type: ignore[arg-type]

    @pytest.mark.parametrize("val", [-1, True, False])
    def test_invalid_cumulative_bytes(self, val: object) -> None:
        with pytest.raises((ValueError, TypeError)):
            A2AStreamEvent(event_type="message", payload={}, cumulative_bytes=val)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# NaN/Inf validation (Rule 17)
# ---------------------------------------------------------------------------


class TestNaNInfValidation:
    """NaN and Inf must be rejected by all numeric config fields."""

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_a2a_message_cost_nan_inf_cost_per_message(self, val: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            A2AMessageCost(agent_id="a", cost_per_message=val)

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_a2a_message_cost_nan_inf_cost_per_token(self, val: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            A2AMessageCost(agent_id="a", cost_per_token=val)

    def test_a2a_message_cost_negative_cost_per_message_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AMessageCost(agent_id="a", cost_per_message=-0.01)

    def test_a2a_message_cost_negative_cost_per_token_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AMessageCost(agent_id="a", cost_per_token=-0.001)

    @pytest.mark.parametrize("field,kwargs", [
        ("cost_per_message", {"agent_id": "a", "cost_per_message": True}),
        ("cost_per_message", {"agent_id": "a", "cost_per_message": False}),
        ("cost_per_token", {"agent_id": "a", "cost_per_token": True}),
    ])
    def test_a2a_message_cost_bool_rejected(self, field: str, kwargs: dict) -> None:
        """bool is subclass of int but must not be accepted as a cost value."""
        with pytest.raises(TypeError, match="finite"):
            A2AMessageCost(**kwargs)

    def test_client_config_bool_default_cost_rejected(self) -> None:
        """bool must not be accepted as default_cost_per_message."""
        with pytest.raises(TypeError, match="finite"):
            A2AClientConfig(default_cost_per_message=True)

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_client_config_nan_inf_default_cost(self, val: float) -> None:
        with pytest.raises((ValueError, TypeError), match="finite"):
            A2AClientConfig(default_cost_per_message=val)

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_client_config_nan_inf_timeout(self, val: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            A2AClientConfig(timeout_seconds=val)

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_client_config_nan_inf_stream_duration(self, val: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            A2AClientConfig(max_stream_duration_s=val)

    @pytest.mark.parametrize("field,kwargs", [
        ("timeout_seconds", {"timeout_seconds": True}),
        ("timeout_seconds", {"timeout_seconds": False}),
        ("max_stream_duration_s", {"max_stream_duration_s": True}),
        ("max_stream_duration_s", {"max_stream_duration_s": False}),
    ])
    def test_client_config_bool_float_fields_rejected(
        self, field: str, kwargs: dict
    ) -> None:
        """bool must not be silently accepted as timeout or duration."""
        with pytest.raises(TypeError, match="finite"):
            A2AClientConfig(**kwargs)


# ---------------------------------------------------------------------------
# Stream/stats field validation (Guard scope - Rule 19)
# ---------------------------------------------------------------------------


class TestClientConfigStreamStatsValidation:
    """Verify stream and stats config fields are validated."""

    def test_zero_max_stream_chunks_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AClientConfig(max_stream_chunks=0)

    def test_negative_max_stream_bytes_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AClientConfig(max_stream_bytes=-1)

    def test_zero_max_stream_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AClientConfig(max_stream_duration_s=0.0)

    def test_zero_stats_cap_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AClientConfig(stats_cap=0)

    def test_negative_stats_cap_rejected(self) -> None:
        with pytest.raises(ValueError):
            A2AClientConfig(stats_cap=-1)


# ---------------------------------------------------------------------------
# A2AIncomingRequest content_size_bytes enforcement (Rule 17/18)
# ---------------------------------------------------------------------------


class TestIncomingRequestContentSizeEnforcement:
    """content_size_bytes must be int and >= 0."""

    def test_float_content_size_rejected(self) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="t1",
                sender_identity=_make_identity(),
                content_size_bytes=3.14,  # type: ignore[arg-type]
            )

    def test_nan_content_size_rejected(self) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="t1",
                sender_identity=_make_identity(),
                content_size_bytes=float("nan"),  # type: ignore[arg-type]
            )

    def test_negative_content_size_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="t1",
                sender_identity=_make_identity(),
                content_size_bytes=-1,
            )

    def test_whitespace_tenant_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="   ",
                sender_identity=_make_identity(),
            )


# ---------------------------------------------------------------------------
# A2AServerDecision verdict validation (Rule 22)
# ---------------------------------------------------------------------------


class TestServerDecisionVerdictValidation:
    def test_invalid_verdict_rejected(self) -> None:
        with pytest.raises(ValueError, match="verdict"):
            A2AServerDecision(
                verdict="HACK",
                reason="test",
                sender_trust=TrustLevel.TRUSTED,
            )

    def test_valid_verdicts_accepted(self) -> None:
        for v in ("ALLOW", "DENY", "DEGRADE"):
            d = A2AServerDecision(verdict=v, reason="test", sender_trust=TrustLevel.TRUSTED)
            assert d.verdict == v

    def test_degrade_with_directive(self) -> None:
        d = A2AServerDecision(
            verdict="DEGRADE",
            reason="rate limit",
            sender_trust=TrustLevel.TRUSTED,
            degrade_directive={"max_tokens": 100},
        )
        assert d.degrade_directive == {"max_tokens": 100}


# ---------------------------------------------------------------------------
# Bool-as-int type confusion (Rule 18)
# ---------------------------------------------------------------------------


class TestBoolAsIntTypeConfusion:
    """bool is subclass of int -- must be rejected for int-typed config fields."""

    @pytest.mark.parametrize("field,val", [
        ("max_poll_attempts", True),
        ("max_state_transitions", False),
        ("max_stream_chunks", True),
        ("max_stream_bytes", False),
        ("stats_cap", True),
    ])
    def test_client_config_bool_rejected(self, field: str, val: object) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AClientConfig(**{field: val})

    def test_incoming_request_bool_content_size_rejected(self) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AIncomingRequest(
                operation="SendMessage",
                tenant_id="t1",
                sender_identity=_make_identity(),
                content_size_bytes=True,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Stats cap boundary triple (Rule 11)
# ---------------------------------------------------------------------------


class TestClientConfigStatsCap:
    @pytest.mark.parametrize("cap,should_raise", [
        (0, True),    # at boundary -- rejected
        (1, False),   # boundary + 1 -- accepted
        (2, False),   # above boundary -- accepted
    ])
    def test_stats_cap_boundary_triple(self, cap: int, should_raise: bool) -> None:
        if should_raise:
            with pytest.raises(ValueError):
                A2AClientConfig(stats_cap=cap)
        else:
            cfg = A2AClientConfig(stats_cap=cap)
            assert cfg.stats_cap == cap


# ---------------------------------------------------------------------------
# A2AServerConfig type/NaN/bool guards (Rule 17/18)
# ---------------------------------------------------------------------------


class TestA2AServerConfigTypeGuards:
    """A2AServerConfig int fields must reject bool, float, NaN."""

    @pytest.mark.parametrize("field", [
        "max_message_size_bytes",
        "max_requests_per_minute_per_tenant",
        "max_requests_per_minute_per_sender",
    ])
    def test_bool_rejected(self, field: str) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AServerConfig(**{field: True})

    @pytest.mark.parametrize("field", [
        "max_message_size_bytes",
        "max_requests_per_minute_per_tenant",
        "max_requests_per_minute_per_sender",
    ])
    @pytest.mark.parametrize("val", [float("nan"), float("inf"), 3.14])
    def test_float_nan_inf_rejected(self, field: str, val: float) -> None:
        with pytest.raises(TypeError, match="int"):
            A2AServerConfig(**{field: val})
