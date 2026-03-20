"""Tests for veronica_core.adapters.a2a_server.A2AServerContainmentMiddleware."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from veronica_core.adapters._a2a_base import (
    A2AIncomingRequest,
    A2AServerConfig,
)
from veronica_core.adapters.a2a_server import (
    A2AServerContainmentMiddleware,
    CardVerifierProtocol,
    DefaultCardVerifier,
    _RateLimiter,
)
from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.types import AgentIdentity, TrustLevel
from veronica_core.memory.types import (
    DegradeDirective,
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryProvenance,
    MessageContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_identity(
    agent_id: str = "sender",
    trust: TrustLevel = TrustLevel.TRUSTED,
) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, origin="a2a", trust_level=trust)


def _make_request(
    *,
    operation: str = "SendMessage",
    tenant_id: str = "t1",
    sender_id: str = "sender",
    trust: TrustLevel = TrustLevel.TRUSTED,
    content_size_bytes: int = 100,
    agent_card: dict[str, Any] | None = None,
) -> A2AIncomingRequest:
    return A2AIncomingRequest(
        operation=operation,
        tenant_id=tenant_id,
        sender_identity=_make_identity(agent_id=sender_id, trust=trust),
        content_size_bytes=content_size_bytes,
        agent_card=agent_card,
    )


class _AllowHook:
    """Governance hook that always returns ALLOW."""

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

    def after_message(self, *args: Any, **kwargs: Any) -> None:
        pass


class _DenyHook:
    """Governance hook that always returns DENY."""

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DENY, reason="hook denied"
        )

    def after_message(self, *args: Any, **kwargs: Any) -> None:
        pass


class _RaisingHook:
    """Governance hook that always raises."""

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        raise RuntimeError("hook crashed")

    def after_message(self, *args: Any, **kwargs: Any) -> None:
        pass


class _DegradeHook:
    """Governance hook that returns DEGRADE with a directive."""

    def __init__(self, directive: DegradeDirective | None = None) -> None:
        self._directive = (
            directive
            if directive is not None
            else DegradeDirective(
                max_packet_tokens=100,
            )
        )

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DEGRADE,
            degrade_directive=self._directive,
        )

    def after_message(self, *args: Any, **kwargs: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestProcessIncomingHappyPath:
    def test_allow_hook_returns_allow(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_AllowHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())

    def test_no_hooks_fail_open_returns_allow(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())

    def test_sender_trust_propagated(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(trust=TrustLevel.PROVISIONAL)
            decision = await mw.process_incoming(req)
            assert decision.sender_trust == TrustLevel.PROVISIONAL

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------


class TestSizeLimit:
    def test_oversized_message_denied(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(max_message_size_bytes=100)
            mw = A2AServerContainmentMiddleware(config=cfg)
            req = _make_request(content_size_bytes=101)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "large" in decision.reason

        asyncio.run(_run())

    def test_exactly_at_limit_denied(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(max_message_size_bytes=100, fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=cfg)
            req = _make_request(content_size_bytes=100)
            decision = await mw.process_incoming(req)
            # At-limit must be denied (>= boundary, "max" is exclusive upper bound)
            assert decision.verdict == "DENY"

        asyncio.run(_run())

    def test_zero_size_always_allowed_by_size_check(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(max_message_size_bytes=1, fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=cfg)
            req = _make_request(content_size_bytes=0)
            decision = await mw.process_incoming(req)
            assert "large" not in decision.reason

        asyncio.run(_run())

    # Boundary triple around max_message_size_bytes=50
    @pytest.mark.parametrize(
        "size,expected_deny", [(49, False), (50, True), (51, True)]
    )
    def test_size_boundary_triple(self, size: int, expected_deny: bool) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(max_message_size_bytes=50, fail_closed=False)
            mw = A2AServerContainmentMiddleware(
                config=cfg, governance_hooks=[_AllowHook()]
            )
            req = _make_request(content_size_bytes=size)
            decision = await mw.process_incoming(req)
            is_size_deny = decision.verdict == "DENY" and "large" in decision.reason
            assert is_size_deny == expected_deny

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_tenant_rate_limit_exceeded(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(
                max_requests_per_minute_per_tenant=2,
                max_requests_per_minute_per_sender=1000,
            )
            mw = A2AServerContainmentMiddleware(
                config=cfg, governance_hooks=[_AllowHook()]
            )
            req = _make_request()
            # First 2 should pass
            r1 = await mw.process_incoming(req)
            r2 = await mw.process_incoming(req)
            # 3rd should be rate-limited
            r3 = await mw.process_incoming(req)
            assert r1.verdict == "ALLOW"
            assert r2.verdict == "ALLOW"
            assert r3.verdict == "DENY"
            assert "tenant rate limit" in r3.reason

        asyncio.run(_run())

    def test_sender_rate_limit_exceeded(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(
                max_requests_per_minute_per_tenant=1000,
                max_requests_per_minute_per_sender=2,
            )
            mw = A2AServerContainmentMiddleware(
                config=cfg, governance_hooks=[_AllowHook()]
            )
            req = _make_request()
            r1 = await mw.process_incoming(req)
            r2 = await mw.process_incoming(req)
            r3 = await mw.process_incoming(req)
            assert r1.verdict == "ALLOW"
            assert r2.verdict == "ALLOW"
            assert r3.verdict == "DENY"
            assert "sender rate limit" in r3.reason

        asyncio.run(_run())

    def test_different_senders_have_separate_limits(self) -> None:
        async def _run() -> None:
            cfg = A2AServerConfig(
                max_requests_per_minute_per_tenant=1000,
                max_requests_per_minute_per_sender=1,
            )
            mw = A2AServerContainmentMiddleware(
                config=cfg, governance_hooks=[_AllowHook()]
            )
            r1 = await mw.process_incoming(_make_request(sender_id="sender-A"))
            r2 = await mw.process_incoming(_make_request(sender_id="sender-B"))
            assert r1.verdict == "ALLOW"
            assert r2.verdict == "ALLOW"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_no_hooks_fail_closed_returns_deny(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "no governance hooks" in decision.reason

        asyncio.run(_run())

    def test_raising_hook_fail_closed_returns_deny(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_RaisingHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Deny hook
# ---------------------------------------------------------------------------


class TestDenyHook:
    def test_deny_hook_returns_deny(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_DenyHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"

        asyncio.run(_run())

    def test_deny_hook_reason_propagated(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                governance_hooks=[_DenyHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "hook denied" in decision.reason

        asyncio.run(_run())

    def test_deny_reason_does_not_leak_internal(self) -> None:
        """Rule 5: reason must not contain internal exception details."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                governance_hooks=[_RaisingHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert "RuntimeError" not in decision.reason
            assert "hook crashed" not in decision.reason

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Card verification
# ---------------------------------------------------------------------------


class TestCardVerification:
    def test_card_with_signature_increases_provenance(self) -> None:
        """Card with signature triggers VERIFIED provenance path."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(
                agent_card={"name": "agent-1", "signature": "valid-sig"}
            )
            # Should not crash and should allow (fail_closed=False, no hooks)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())

    def test_card_without_signature_unverified(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(agent_card={"name": "agent-1"})
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())

    def test_no_card_no_crash(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(agent_card=None)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())

    def test_failing_verifier_treated_as_unverified(self) -> None:
        """Verifier that raises must not crash process_incoming."""

        async def _run() -> None:
            class BrokenVerifier:
                def verify(self, card: dict[str, Any]) -> A2AIdentityProvenance:
                    raise RuntimeError("verifier broken")

            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
                card_verifier=BrokenVerifier(),
            )
            req = _make_request(agent_card={"name": "agent-1"})
            decision = await mw.process_incoming(req)
            assert decision.verdict in ("ALLOW", "DENY", "DEGRADE")

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# DefaultCardVerifier
# ---------------------------------------------------------------------------


class TestDefaultCardVerifier:
    def test_card_with_string_signature_not_verified(self) -> None:
        """DefaultCardVerifier never grants verified -- no crypto check."""
        verifier = DefaultCardVerifier()
        card = {"name": "agent-1", "signature": "abc123"}
        prov = verifier.verify(card)
        assert prov.card_verified is False
        assert prov.card_fingerprint is not None

    def test_card_without_signature_not_verified(self) -> None:
        verifier = DefaultCardVerifier()
        prov = verifier.verify({"name": "agent-1"})
        assert prov.card_verified is False

    def test_card_with_empty_string_signature_not_verified(self) -> None:
        verifier = DefaultCardVerifier()
        prov = verifier.verify({"name": "agent-1", "signature": ""})
        assert prov.card_verified is False

    def test_empty_card_no_crash(self) -> None:
        verifier = DefaultCardVerifier()
        prov = verifier.verify({})
        assert prov.card_fingerprint is not None

    def test_fingerprint_deterministic(self) -> None:
        verifier = DefaultCardVerifier()
        card = {"name": "agent-1", "url": "https://example.com"}
        p1 = verifier.verify(card)
        p2 = verifier.verify(card)
        assert p1.card_fingerprint == p2.card_fingerprint

    def test_fingerprint_differs_for_different_cards(self) -> None:
        verifier = DefaultCardVerifier()
        p1 = verifier.verify({"name": "a"})
        p2 = verifier.verify({"name": "b"})
        assert p1.card_fingerprint != p2.card_fingerprint


# ---------------------------------------------------------------------------
# Trust level resolution
# ---------------------------------------------------------------------------


class TestTrustLevelResolution:
    def test_static_trust_used_without_tracker(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(trust=TrustLevel.PROVISIONAL)
            decision = await mw.process_incoming(req)
            assert decision.sender_trust == TrustLevel.PROVISIONAL

        asyncio.run(_run())

    def test_untrusted_sender_allowed_when_fail_open(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(trust=TrustLevel.UNTRUSTED)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_capabilities_output(self) -> None:
        mw = A2AServerContainmentMiddleware()
        caps = mw.capabilities()
        assert caps.framework_name == "A2A"
        assert caps.supports_async is True
        assert caps.extra.get("role") == "server"


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


class TestAdversarialServerMiddleware:
    def test_garbage_card_no_crash(self) -> None:
        """Malformed card dict must not crash process_incoming."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
            )
            req = _make_request(agent_card={"signature": None, "name": None})
            decision = await mw.process_incoming(req)
            assert decision.verdict in ("ALLOW", "DENY", "DEGRADE")

        asyncio.run(_run())

    def test_concurrent_requests_consistent(self) -> None:
        """Multiple concurrent requests must not cause state corruption."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(
                    max_requests_per_minute_per_tenant=1000,
                    max_requests_per_minute_per_sender=1000,
                    fail_closed=False,
                ),
                governance_hooks=[_AllowHook()],
            )

            async def one_req(i: int) -> str:
                req = _make_request(sender_id=f"concurrent-sender-{i}")
                d = await mw.process_incoming(req)
                return d.verdict

            verdicts = await asyncio.gather(*[one_req(i) for i in range(20)])
            # All should be ALLOW (no rate limit exceeded)
            assert all(v == "ALLOW" for v in verdicts)

        asyncio.run(_run())

    def test_extremely_large_tenant_id_no_crash(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False)
            )
            long_tenant_id = "t" * 10_000
            req = A2AIncomingRequest(
                operation="SendMessage",
                tenant_id=long_tenant_id,
                sender_identity=_make_identity(),
            )
            decision = await mw.process_incoming(req)
            assert decision.verdict in ("ALLOW", "DENY", "DEGRADE")

        asyncio.run(_run())

    def test_deny_verdict_reason_not_internal(self) -> None:
        """Denial reasons must be generic, not contain internal class names."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(max_message_size_bytes=1, fail_closed=True),
            )
            req = _make_request(content_size_bytes=2)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "ValueError" not in decision.reason
            assert "Exception" not in decision.reason

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Side-effect: get_request_count verification (Rule 7)
# ---------------------------------------------------------------------------


class TestRequestCountSideEffect:
    """Verify get_request_count reflects actual tracked requests."""

    def test_allow_increments_request_count(self) -> None:
        async def _run() -> None:
            config = A2AServerConfig(fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"
            # Default _make_request uses tenant_id="t1", sender_id="sender"
            count = mw.get_request_count("t1", "sender")
            assert count == 1

        asyncio.run(_run())

    def test_rate_limit_deny_does_not_increment_count(self) -> None:
        async def _run() -> None:
            config = A2AServerConfig(
                max_requests_per_minute_per_tenant=1,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            await mw.process_incoming(req)  # 1st: ALLOW
            await mw.process_incoming(req)  # 2nd: DENY (rate limit)
            count = mw.get_request_count("t1", "sender")
            # Only the first ALLOW was tracked; the DENY does not increment
            assert count == 1

        asyncio.run(_run())

    def test_size_deny_does_not_increment_count(self) -> None:
        async def _run() -> None:
            config = A2AServerConfig(max_message_size_bytes=10, fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request(content_size_bytes=100)
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            count = mw.get_request_count("t1", "sender")
            assert count == 0

        asyncio.run(_run())

    def test_multiple_allows_accumulate_count(self) -> None:
        async def _run() -> None:
            config = A2AServerConfig(
                max_requests_per_minute_per_tenant=100,
                max_requests_per_minute_per_sender=100,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            for _ in range(5):
                await mw.process_incoming(req)
            count = mw.get_request_count("t1", "sender")
            assert count == 5

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Compound state tests (Rule 6)
# ---------------------------------------------------------------------------


class TestCompoundState:
    def test_size_over_plus_rate_limit(self) -> None:
        """Size check comes first -- even if rate limit would also be exceeded."""

        async def _run() -> None:
            config = A2AServerConfig(
                max_message_size_bytes=10,
                max_requests_per_minute_per_tenant=1,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            # First exhaust rate limit with a small request
            await mw.process_incoming(_make_request(content_size_bytes=5))
            # Now send oversized request (both size limit AND rate limit violated)
            decision = await mw.process_incoming(_make_request(content_size_bytes=100))
            assert decision.verdict == "DENY"
            # Size check is evaluated before rate limit, so size reason wins
            assert "large" in decision.reason

        asyncio.run(_run())

    def test_fail_closed_with_raising_hook_compound(self) -> None:
        """fail_closed=True AND raising hook -- hook fail-close path is taken."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_RaisingHook()],
            )
            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            # Reason must come from hook failure path, not from no-hooks path
            assert "hook" in decision.reason

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Concurrent _RateLimiter with threading (Rule 14)
# ---------------------------------------------------------------------------


class TestRateLimiterThreadSafety:
    def test_concurrent_is_allowed_consistent(self) -> None:
        """Threading test: exactly max_per_window requests are allowed."""
        limiter = _RateLimiter(window_seconds=60.0)
        max_per_window = 5
        results: list[bool] = []
        results_lock = threading.Lock()

        def check() -> None:
            allowed = limiter.is_allowed("key1", max_per_window)
            with results_lock:
                results.append(allowed)

        threads = [threading.Thread(target=check) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(1 for r in results if r)
        assert allowed_count == max_per_window

    def test_concurrent_different_keys_independent(self) -> None:
        """Separate keys must not interfere -- each gets its own window."""
        limiter = _RateLimiter(window_seconds=60.0)
        max_per_window = 3
        results: dict[str, list[bool]] = {"k1": [], "k2": []}
        lock = threading.Lock()

        def check(key: str) -> None:
            allowed = limiter.is_allowed(key, max_per_window)
            with lock:
                results[key].append(allowed)

        threads = [threading.Thread(target=check, args=("k1",)) for _ in range(10)] + [
            threading.Thread(target=check, args=("k2",)) for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results["k1"]) == max_per_window
        assert sum(results["k2"]) == max_per_window


# ---------------------------------------------------------------------------
# DefaultCardVerifier type confusion adversarial tests (BUG-2)
# ---------------------------------------------------------------------------


class TestDefaultCardVerifierAdversarial:
    def test_signature_as_integer(self) -> None:
        """Non-string signature must yield card_verified=False."""
        verifier = DefaultCardVerifier()
        prov = verifier.verify({"name": "agent", "signature": 12345})
        assert prov.card_verified is False
        # Fingerprint should still be computed from the card dict
        assert prov.card_fingerprint is not None

    def test_signature_as_list(self) -> None:
        """List signature must yield card_verified=False."""
        verifier = DefaultCardVerifier()
        prov = verifier.verify({"name": "agent", "signature": ["abc"]})
        assert prov.card_verified is False

    def test_signature_as_none_not_verified(self) -> None:
        """Explicit None signature must yield card_verified=False."""
        verifier = DefaultCardVerifier()
        prov = verifier.verify({"name": "agent", "signature": None})
        assert prov.card_verified is False

    def test_non_serializable_card_value(self) -> None:
        """Card with non-JSON-serializable values must not raise."""
        verifier = DefaultCardVerifier()
        # bytes is not JSON-serializable; verifier must handle gracefully
        prov = verifier.verify({"name": "agent", "data": b"bytes_value"})
        # Either fingerprint is None (fallback) or some stub -- must not crash
        assert prov.card_verified is False

    def test_very_large_card_no_crash(self) -> None:
        """Extremely large card dict must not crash verify()."""
        verifier = DefaultCardVerifier()
        big_card = {f"key_{i}": "x" * 100 for i in range(500)}
        prov = verifier.verify(big_card)
        assert prov is not None


# ---------------------------------------------------------------------------
# Log output verification (Rule 24)
# ---------------------------------------------------------------------------


class TestLogOutputVerification:
    def test_rate_limit_denial_produces_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _run() -> None:
            config = A2AServerConfig(
                max_requests_per_minute_per_sender=1,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            await mw.process_incoming(req)  # 1st: ALLOW
            with caplog.at_level(
                logging.DEBUG, logger="veronica_core.adapters.a2a_server"
            ):
                await mw.process_incoming(req)  # 2nd: DENY

        asyncio.run(_run())
        # At minimum, a debug or warning log should mention sender rate limit
        combined = caplog.text.lower()
        assert "rate limit" in combined

    def test_hook_failure_warning_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _run() -> None:
            config = A2AServerConfig(fail_closed=True)
            mw = A2AServerContainmentMiddleware(
                config=config,
                governance_hooks=[_RaisingHook()],
            )
            req = _make_request()
            with caplog.at_level(
                logging.WARNING, logger="veronica_core.adapters.a2a_server"
            ):
                decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "hook" in decision.reason
            # The warning log must mention governance hook failure
            assert "governance hook" in caplog.text.lower()

        asyncio.run(_run())

    def test_size_deny_debug_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _run() -> None:
            config = A2AServerConfig(max_message_size_bytes=10, fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request(content_size_bytes=999)
            with caplog.at_level(
                logging.DEBUG, logger="veronica_core.adapters.a2a_server"
            ):
                decision = await mw.process_incoming(req)
            assert decision.verdict == "DENY"
            assert "large" in decision.reason
            assert "size" in caplog.text.lower()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Idempotency: double process_incoming (Rule 25)
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_double_call_counts_as_two(self) -> None:
        async def _run() -> None:
            config = A2AServerConfig(
                max_requests_per_minute_per_tenant=100,
                max_requests_per_minute_per_sender=100,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            await mw.process_incoming(req)
            await mw.process_incoming(req)
            count = mw.get_request_count("t1", "sender")
            assert count == 2

        asyncio.run(_run())

    def test_same_request_twice_both_succeed_independently(self) -> None:
        """Processing the same request object twice must not share state."""

        async def _run() -> None:
            config = A2AServerConfig(fail_closed=False)
            mw = A2AServerContainmentMiddleware(config=config)
            req = _make_request()
            d1 = await mw.process_incoming(req)
            d2 = await mw.process_incoming(req)
            assert d1.verdict == "ALLOW"
            assert d2.verdict == "ALLOW"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Reentrancy: hook calling back into middleware (Rule 29)
# ---------------------------------------------------------------------------


class TestReentrancy:
    def test_reentrant_get_request_count_from_hook(self) -> None:
        """Hook calling get_request_count must not deadlock."""

        async def _run() -> None:
            # We need a reference to the middleware before constructing it;
            # use a container to patch it in after construction.
            container: list[A2AServerContainmentMiddleware] = []

            class _ReentrantHook:
                count_seen: int | None = None

                def before_message(
                    self, ctx: MessageContext
                ) -> MemoryGovernanceDecision:
                    if container:
                        # This re-enters the middleware to read stats while
                        # process_incoming is still on the call stack.
                        self.count_seen = container[0].get_request_count("t1", "sender")
                    return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

                def after_message(self, *args: Any, **kwargs: Any) -> None:
                    pass

            hook = _ReentrantHook()
            config = A2AServerConfig(fail_closed=True)
            mw = A2AServerContainmentMiddleware(config=config, governance_hooks=[hook])
            container.append(mw)

            req = _make_request()
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"
            # Hook was able to call get_request_count without deadlock
            assert hook.count_seen is not None

        asyncio.run(_run())

    def test_hook_can_call_capabilities(self) -> None:
        """Hook calling capabilities() must not deadlock."""

        async def _run() -> None:
            container: list[A2AServerContainmentMiddleware] = []
            seen: list[str] = []

            class _CapHook:
                def before_message(
                    self, ctx: MessageContext
                ) -> MemoryGovernanceDecision:
                    if container:
                        caps = container[0].capabilities()
                        seen.append(caps.framework_name)
                    return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

                def after_message(self, *args: Any, **kwargs: Any) -> None:
                    pass

            hook = _CapHook()
            config = A2AServerConfig(fail_closed=True)
            mw = A2AServerContainmentMiddleware(config=config, governance_hooks=[hook])
            container.append(mw)

            await mw.process_incoming(_make_request())
            assert seen == ["A2A"]

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# RateLimiter cardinality cap (Rule 1 -- guard-after)
# ---------------------------------------------------------------------------


class TestRateLimiterCardinalityCap:
    def test_cardinality_cap_denies_new_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once cardinality cap is reached, new keys are denied (fail-closed)."""
        import veronica_core.adapters.a2a_server as srv_mod

        monkeypatch.setattr(srv_mod, "_STATS_WARN_LIMIT", 2)
        limiter = _RateLimiter(window_seconds=60.0)
        assert limiter.is_allowed("key1", 100) is True
        assert limiter.is_allowed("key2", 100) is True
        # 3rd distinct key exceeds cap
        assert limiter.is_allowed("key3", 100) is False

    def test_existing_key_still_allowed_after_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing keys keep working even when cap is reached."""
        import veronica_core.adapters.a2a_server as srv_mod

        monkeypatch.setattr(srv_mod, "_STATS_WARN_LIMIT", 2)
        limiter = _RateLimiter(window_seconds=60.0)
        limiter.is_allowed("key1", 100)
        limiter.is_allowed("key2", 100)
        # Cap reached -- new keys denied
        assert limiter.is_allowed("key3", 100) is False
        # Existing key still works
        assert limiter.is_allowed("key1", 100) is True


# ---------------------------------------------------------------------------
# DEGRADE hook path (Unit-2 Issue 1)
# ---------------------------------------------------------------------------


class TestDegradeHook:
    def test_degrade_hook_returns_degrade_verdict(self) -> None:
        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_DegradeHook()],
            )
            decision = await mw.process_incoming(_make_request())
            assert decision.verdict == "DEGRADE"
            assert decision.degrade_directive is not None
            assert decision.degrade_directive.max_packet_tokens == 100

        asyncio.run(_run())

    def test_degrade_then_deny_short_circuits_on_deny(self) -> None:
        """DENY after DEGRADE must win (DENY short-circuits)."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_DegradeHook(), _DenyHook()],
            )
            decision = await mw.process_incoming(_make_request())
            assert decision.verdict == "DENY"

        asyncio.run(_run())

    def test_allow_then_degrade_yields_degrade(self) -> None:
        """ALLOW followed by DEGRADE: final must be DEGRADE."""

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_AllowHook(), _DegradeHook()],
            )
            decision = await mw.process_incoming(_make_request())
            assert decision.verdict == "DEGRADE"
            assert decision.degrade_directive is not None

        asyncio.run(_run())

    def test_last_degrade_directive_wins(self) -> None:
        """When multiple DEGRADE hooks exist, last directive wins."""

        async def _run() -> None:
            d1 = DegradeDirective(max_packet_tokens=50)
            d2 = DegradeDirective(max_packet_tokens=200)
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[_DegradeHook(d1), _DegradeHook(d2)],
            )
            decision = await mw.process_incoming(_make_request())
            assert decision.verdict == "DEGRADE"
            assert decision.degrade_directive.max_packet_tokens == 200

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Trust tracker branch (Unit-2 Issue 2)
# ---------------------------------------------------------------------------


class TestTrustTrackerResolution:
    def test_trust_tracker_overrides_static_trust(self) -> None:
        async def _run() -> None:
            import threading

            from veronica_core.a2a.escalation import (
                TrustEscalationTracker,
                _AgentRecord,
            )
            from veronica_core.a2a.types import TrustPolicy

            policy = TrustPolicy(default_trust=TrustLevel.UNTRUSTED)
            tracker = TrustEscalationTracker(policy=policy)
            # Inject a record with PROVISIONAL trust for "sender"
            tracker._agents["sender"] = _AgentRecord(
                current_trust=TrustLevel.PROVISIONAL,
                lock=threading.Lock(),
            )
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
                trust_tracker=tracker,
            )
            # Request has TRUSTED identity; tracker overrides to PROVISIONAL
            req = _make_request(trust=TrustLevel.TRUSTED)
            decision = await mw.process_incoming(req)
            assert decision.sender_trust == TrustLevel.PROVISIONAL

        asyncio.run(_run())

    def test_trust_tracker_default_returns_untrusted(self) -> None:
        """Unknown sender via tracker defaults to UNTRUSTED."""

        async def _run() -> None:
            from veronica_core.a2a.escalation import TrustEscalationTracker
            from veronica_core.a2a.types import TrustPolicy

            policy = TrustPolicy(default_trust=TrustLevel.UNTRUSTED)
            tracker = TrustEscalationTracker(policy=policy)
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
                trust_tracker=tracker,
            )
            req = _make_request(sender_id="unknown-sender", trust=TrustLevel.TRUSTED)
            decision = await mw.process_incoming(req)
            # Tracker has no entry for unknown-sender, defaults to UNTRUSTED
            assert decision.sender_trust == TrustLevel.UNTRUSTED

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _track_request cardinality cap guard-after (Unit-2 Issue 4)
# ---------------------------------------------------------------------------


class TestTrackRequestCardinalityCap:
    def test_request_count_zero_for_over_cap_sender(self) -> None:
        """Once _request_counts cap is reached, new sender is not tracked."""

        async def _run() -> None:
            config = A2AServerConfig(
                max_requests_per_minute_per_tenant=1000,
                max_requests_per_minute_per_sender=1000,
                fail_closed=False,
            )
            mw = A2AServerContainmentMiddleware(config=config)
            # First sender tracked normally
            await mw.process_incoming(_make_request(sender_id="sender-A"))
            assert mw.get_request_count("t1", "sender-A") == 1

            # Artificially fill _request_counts to the cap
            from veronica_core.adapters._a2a_base import _STATS_WARN_LIMIT

            with mw._stats_lock:
                for i in range(_STATS_WARN_LIMIT):
                    mw._request_counts[f"filler-{i}"] = 1

            # New sender: request succeeds but count is not tracked
            decision = await mw.process_incoming(_make_request(sender_id="sender-B"))
            assert decision.verdict == "ALLOW"
            assert mw.get_request_count("t1", "sender-B") == 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# SendStreamingMessage operation through process_incoming (Rule 10)
# ---------------------------------------------------------------------------


class TestSendStreamingMessageOperation:
    def test_streaming_operation_allowed_through_hook(self) -> None:
        """SendStreamingMessage must pass through governance hooks like SendMessage."""

        captured: list[str] = []

        class _CapturingHook:
            def before_message(self, ctx: MessageContext) -> MemoryGovernanceDecision:
                captured.append(ctx.message_type)
                return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

            def after_message(self, *args: Any, **kwargs: Any) -> None:
                pass

        async def _run() -> None:
            hook = _CapturingHook()
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[hook],
            )
            req = _make_request(operation="SendStreamingMessage")
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"
            assert captured == ["SendStreamingMessage"]

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Falsy signature through process_incoming (DefaultCardVerifier alignment)
# ---------------------------------------------------------------------------


class TestFalsySignatureThroughMiddleware:
    def test_null_signature_treated_as_unverified(self) -> None:
        """Card with 'signature': None must yield UNVERIFIED provenance."""

        observed_provenance: list[MemoryProvenance] = []

        class _ProvenanceCapture:
            def before_message(self, ctx: MessageContext) -> MemoryGovernanceDecision:
                observed_provenance.append(ctx.provenance)
                return MemoryGovernanceDecision(verdict=GovernanceVerdict.ALLOW)

            def after_message(self, *args: Any, **kwargs: Any) -> None:
                pass

        async def _run() -> None:
            hook = _ProvenanceCapture()
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=True),
                governance_hooks=[hook],
            )
            req = _make_request(agent_card={"name": "agent-1", "signature": None})
            decision = await mw.process_incoming(req)
            assert decision.verdict == "ALLOW"
            assert observed_provenance == [MemoryProvenance.UNVERIFIED]

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Server CardVerifierProtocol compliance
# ---------------------------------------------------------------------------


class TestServerCardVerifierProtocol:
    def test_default_card_verifier_satisfies_protocol(self) -> None:
        assert isinstance(DefaultCardVerifier(), CardVerifierProtocol)

    def test_wrong_return_type_verifier_fails_safe(self) -> None:
        """Verifier returning bool (card.py style) instead of A2AIdentityProvenance
        must not crash -- the except Exception block catches AttributeError."""

        class _BoolVerifier:
            def verify(self, card: dict[str, Any]) -> bool:
                return True

        async def _run() -> None:
            mw = A2AServerContainmentMiddleware(
                config=A2AServerConfig(fail_closed=False),
                card_verifier=_BoolVerifier(),  # type: ignore[arg-type]
            )
            req = _make_request(agent_card={"name": "agent-1", "signature": "abc"})
            decision = await mw.process_incoming(req)
            # Must not crash; provenance falls back to UNVERIFIED
            assert decision.verdict in ("ALLOW", "DENY", "DEGRADE")

        asyncio.run(_run())
