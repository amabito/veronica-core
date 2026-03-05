"""Tests for A2A budget isolation (Step 1 of v3.0).

Covers:
- ExecutionContext.agent_identity field
- ContextSnapshot.agent_identity propagation
- identity_from_a2a_card() utility
- Integration: router + context + tenant wiring
- Adversarial: corrupted cards, type mismatches, concurrent access
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from veronica_core.a2a import (
    AgentIdentity,
    TrustBasedPolicyRouter,
    TrustLevel,
    identity_from_a2a_card,
)
from veronica_core.containment import (
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> ExecutionConfig:
    defaults = dict(max_cost_usd=10.0, max_steps=100, max_retries_total=5)
    defaults.update(overrides)
    return ExecutionConfig(**defaults)


def _make_identity(
    agent_id: str = "agent-1",
    origin: str = "a2a",
    trust_level: TrustLevel = TrustLevel.UNTRUSTED,
) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, origin=origin, trust_level=trust_level)


# ---------------------------------------------------------------------------
# ExecutionContext.agent_identity
# ---------------------------------------------------------------------------


class TestExecutionContextAgentIdentity:
    """agent_identity field on ExecutionContext."""

    def test_default_is_none(self) -> None:
        ctx = ExecutionContext(config=_make_config())
        assert ctx.agent_identity is None
        ctx.close()

    def test_set_at_construction(self) -> None:
        identity = _make_identity()
        ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
        assert ctx.agent_identity is identity
        assert ctx.agent_identity.agent_id == "agent-1"
        ctx.close()

    def test_identity_immutable_via_property(self) -> None:
        """Property is read-only; cannot be assigned."""
        ctx = ExecutionContext(config=_make_config(), agent_identity=_make_identity())
        with pytest.raises(AttributeError):
            ctx.agent_identity = _make_identity("other")  # type: ignore[misc]
        ctx.close()

    def test_all_trust_levels(self) -> None:
        for level in TrustLevel:
            identity = _make_identity(trust_level=level)
            ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
            assert ctx.agent_identity.trust_level == level
            ctx.close()

    def test_identity_survives_wrap_call(self) -> None:
        identity = _make_identity(trust_level=TrustLevel.TRUSTED)
        ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
        ctx.wrap_llm_call(
            fn=lambda: "ok",
            options=WrapOptions(operation_name="test", cost_estimate_hint=0.01),
        )
        assert ctx.agent_identity is identity
        ctx.close()


# ---------------------------------------------------------------------------
# ContextSnapshot.agent_identity
# ---------------------------------------------------------------------------


class TestContextSnapshotAgentIdentity:
    """agent_identity propagation into snapshots."""

    def test_snapshot_includes_identity(self) -> None:
        identity = _make_identity()
        with ExecutionContext(config=_make_config(), agent_identity=identity) as ctx:
            snap = ctx.get_snapshot()
        assert snap.agent_identity is identity

    def test_snapshot_none_when_no_identity(self) -> None:
        with ExecutionContext(config=_make_config()) as ctx:
            snap = ctx.get_snapshot()
        assert snap.agent_identity is None

    def test_snapshot_identity_after_abort(self) -> None:
        identity = _make_identity()
        ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
        ctx.abort("test")
        snap = ctx.get_snapshot()
        assert snap.agent_identity is identity
        ctx.close()


# ---------------------------------------------------------------------------
# identity_from_a2a_card()
# ---------------------------------------------------------------------------


class TestIdentityFromA2ACard:
    """Conversion from A2A Agent Card dict to AgentIdentity."""

    def test_minimal_card(self) -> None:
        card = {"name": "agent-alpha"}
        identity = identity_from_a2a_card(card)
        assert identity.agent_id == "agent-alpha"
        assert identity.origin == "a2a"
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_card_with_trust_level(self) -> None:
        card = {"name": "trusted-bot", "trust_level": "trusted"}
        identity = identity_from_a2a_card(card)
        assert identity.trust_level == TrustLevel.TRUSTED

    def test_card_with_url(self) -> None:
        card = {"name": "remote-agent", "url": "https://agent.example.com"}
        identity = identity_from_a2a_card(card)
        assert identity.metadata["url"] == "https://agent.example.com"

    def test_card_without_url_has_empty_metadata(self) -> None:
        card = {"name": "local-agent"}
        identity = identity_from_a2a_card(card)
        assert identity.metadata == {}

    def test_unknown_trust_level_defaults_to_untrusted(self) -> None:
        card = {"name": "agent", "trust_level": "superadmin"}
        identity = identity_from_a2a_card(card)
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_empty_trust_level_defaults_to_untrusted(self) -> None:
        card = {"name": "agent", "trust_level": ""}
        identity = identity_from_a2a_card(card)
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_all_valid_trust_levels(self) -> None:
        for level in TrustLevel:
            card = {"name": "agent", "trust_level": level.value}
            identity = identity_from_a2a_card(card)
            assert identity.trust_level == level


# ---------------------------------------------------------------------------
# Integration: Router + ExecutionContext
# ---------------------------------------------------------------------------


class TestRouterContextIntegration:
    """End-to-end: agent card -> identity -> router -> pipeline -> context."""

    def test_router_selects_pipeline_for_identity(self) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        untrusted_pipeline = ShieldPipeline()
        trusted_pipeline = ShieldPipeline()
        router = TrustBasedPolicyRouter(
            policies={
                TrustLevel.UNTRUSTED: untrusted_pipeline,
                TrustLevel.TRUSTED: trusted_pipeline,
            }
        )

        identity = _make_identity(trust_level=TrustLevel.TRUSTED)
        pipeline = router.route(identity)
        assert pipeline is trusted_pipeline

        ctx = ExecutionContext(
            config=_make_config(),
            pipeline=pipeline,
            agent_identity=identity,
        )
        assert ctx.agent_identity.trust_level == TrustLevel.TRUSTED
        ctx.close()

    def test_full_card_to_context_flow(self) -> None:
        card = {"name": "remote-agent", "trust_level": "provisional"}
        identity = identity_from_a2a_card(card)

        ctx = ExecutionContext(
            config=_make_config(),
            agent_identity=identity,
        )
        assert ctx.agent_identity.agent_id == "remote-agent"
        assert ctx.agent_identity.trust_level == TrustLevel.PROVISIONAL
        snap = ctx.get_snapshot()
        assert snap.agent_identity is identity
        ctx.close()


# ---------------------------------------------------------------------------
# Adversarial: identity_from_a2a_card
# ---------------------------------------------------------------------------


class TestAdversarialCardParsing:
    """Adversarial tests for identity_from_a2a_card -- attacker mindset."""

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({})

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({"name": ""})

    def test_none_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({"name": None})

    def test_int_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({"name": 42})

    def test_list_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({"name": ["agent"]})

    def test_bytes_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            identity_from_a2a_card({"name": b"agent"})

    def test_trust_level_injection_attempt(self) -> None:
        """Injecting a dict as trust_level must not crash."""
        card = {"name": "evil", "trust_level": {"admin": True}}
        identity = identity_from_a2a_card(card)
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_trust_level_none(self) -> None:
        card = {"name": "agent", "trust_level": None}
        identity = identity_from_a2a_card(card)
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_url_non_string_ignored(self) -> None:
        card = {"name": "agent", "url": 12345}
        identity = identity_from_a2a_card(card)
        assert "url" not in identity.metadata

    def test_url_empty_string_ignored(self) -> None:
        card = {"name": "agent", "url": ""}
        identity = identity_from_a2a_card(card)
        assert "url" not in identity.metadata

    def test_extra_fields_not_leaked(self) -> None:
        """Extra card fields must not appear in metadata (only 'url' is extracted)."""
        card = {"name": "agent", "api_key": "secret123", "password": "hunter2"}
        identity = identity_from_a2a_card(card)
        assert "api_key" not in identity.metadata
        assert "password" not in identity.metadata

    def test_extremely_long_name(self) -> None:
        """Long names must not cause errors (they are passed through)."""
        card = {"name": "a" * 100_000}
        identity = identity_from_a2a_card(card)
        assert len(identity.agent_id) == 100_000

    def test_unicode_name(self) -> None:
        card = {"name": "agent-\u0000\u200b\uffff"}
        identity = identity_from_a2a_card(card)
        assert identity.agent_id == "agent-\u0000\u200b\uffff"


# ---------------------------------------------------------------------------
# Adversarial: ExecutionContext agent_identity
# ---------------------------------------------------------------------------


class TestAdversarialContextIdentity:
    """Adversarial tests for ExecutionContext.agent_identity -- attacker mindset."""

    def test_concurrent_reads_same_identity(self) -> None:
        """Multiple threads reading agent_identity must see the same object."""
        identity = _make_identity()
        ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
        results: list[AgentIdentity | None] = []

        def read_identity() -> None:
            results.append(ctx.agent_identity)

        threads = [threading.Thread(target=read_identity) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is identity for r in results)
        ctx.close()

    def test_snapshot_concurrent_with_wrap(self) -> None:
        """Snapshot and wrap_llm_call in parallel must not corrupt identity."""
        identity = _make_identity()
        ctx = ExecutionContext(config=_make_config(), agent_identity=identity)
        errors: list[str] = []

        def do_wraps() -> None:
            for _ in range(10):
                ctx.wrap_llm_call(
                    fn=lambda: "ok",
                    options=WrapOptions(operation_name="t", cost_estimate_hint=0.001),
                )

        def do_snapshots() -> None:
            for _ in range(10):
                snap = ctx.get_snapshot()
                if snap.agent_identity is not identity:
                    errors.append("identity mismatch in snapshot")

        t1 = threading.Thread(target=do_wraps)
        t2 = threading.Thread(target=do_snapshots)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        ctx.close()

    def test_child_context_independent_identity(self) -> None:
        """Parent and child can have different agent identities."""
        parent_id = _make_identity("parent-agent")
        child_id = _make_identity("child-agent", trust_level=TrustLevel.TRUSTED)

        parent = ExecutionContext(config=_make_config(), agent_identity=parent_id)
        child = ExecutionContext(
            config=_make_config(),
            parent=parent,
            agent_identity=child_id,
        )

        assert parent.agent_identity.agent_id == "parent-agent"
        assert child.agent_identity.agent_id == "child-agent"
        assert child.agent_identity.trust_level == TrustLevel.TRUSTED
        child.close()
        parent.close()

    def test_child_context_without_identity(self) -> None:
        """Child context does not inherit parent's agent_identity."""
        parent_id = _make_identity("parent-agent")
        parent = ExecutionContext(config=_make_config(), agent_identity=parent_id)
        child = ExecutionContext(config=_make_config(), parent=parent)

        assert parent.agent_identity is parent_id
        assert child.agent_identity is None
        child.close()
        parent.close()
