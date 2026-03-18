"""veronica_core.adapters.a2a_server -- A2A server containment middleware.

A2AServerContainmentMiddleware governs incoming A2A requests before they
reach the agent handler. It enforces:

- Message size limits
- Per-tenant and per-sender rate limits (sliding window)
- Agent Card verification (signature check, SHA-256 fingerprint)
- Trust level resolution via TrustEscalationTracker
- Message governance hooks (MessageGovernanceHook protocol)
- Fail-closed mode when no hooks are configured

Usage::

    from veronica_core.adapters.a2a_server import A2AServerContainmentMiddleware
    from veronica_core.adapters._a2a_base import A2AServerConfig, A2AIncomingRequest
    from veronica_core.a2a.types import AgentIdentity, TrustLevel

    middleware = A2AServerContainmentMiddleware(
        config=A2AServerConfig(fail_closed=True),
    )
    request = A2AIncomingRequest(
        operation='SendMessage',
        tenant_id='my-tenant',
        sender_identity=AgentIdentity(agent_id='sender', origin='a2a'),
    )
    decision = await middleware.process_incoming(request)
    if decision.verdict == 'DENY':
        raise PermissionError(decision.reason)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Protocol, runtime_checkable

from veronica_core.a2a.escalation import TrustEscalationTracker
from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.router import TrustBasedPolicyRouter
from veronica_core.adapter_capabilities import AdapterCapabilities
from veronica_core.adapters._a2a_base import (
    A2AIncomingRequest,
    A2AServerConfig,
    A2AServerDecision,
    _STATS_WARN_LIMIT,
)
from veronica_core.memory.message_governance import MessageGovernanceHook
from veronica_core.memory.types import GovernanceVerdict, MemoryProvenance, MessageContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CardVerifierProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CardVerifierProtocol(Protocol):
    """Protocol for Agent Card verification.

    Implementations verify an A2A Agent Card dict and return provenance
    metadata. Callers must not rely on exception types -- any error from
    verify() should be treated as unverified.
    """

    def verify(self, card: dict[str, Any]) -> A2AIdentityProvenance:
        """Verify an A2A Agent Card and return provenance metadata.

        Args:
            card: A2A Agent Card dict from the incoming request.

        Returns:
            A2AIdentityProvenance with card_verified and card_fingerprint set.
        """
        ...


class DefaultCardVerifier:
    """Default card verifier -- computes fingerprint only; never grants verified.

    card_verified is always False because this verifier performs NO cryptographic
    signature validation. Production deployments must supply a real
    CardVerifierProtocol implementation (e.g. JWS/JWK) to grant VERIFIED
    provenance.

    card_fingerprint is the hex-encoded SHA-256 of the sorted JSON representation.
    """

    def verify(self, card: dict[str, Any]) -> A2AIdentityProvenance:
        """Verify an A2A Agent Card.

        Args:
            card: A2A Agent Card dict. May contain any keys.

        Returns:
            A2AIdentityProvenance with card_verified=False (no crypto check)
            and a fingerprint computed from the card contents.
        """
        try:
            canonical = json.dumps(card, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        except (TypeError, ValueError):
            fingerprint = None

        return A2AIdentityProvenance(
            card_verified=False,
            card_fingerprint=fingerprint,
        )


# ---------------------------------------------------------------------------
# Internal rate limiter (sliding window)
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Thread-safe sliding window rate limiter.

    Tracks request timestamps per key. Keys beyond the cardinality cap
    are denied (fail-closed) to prevent DoS via attacker-controlled key
    generation.

    Args:
        window_seconds: Length of the sliding window in seconds. Default 60.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str, max_per_window: int) -> bool:
        """Return True if the request is within the rate limit.

        Side effect: records this request timestamp if allowed.

        Args:
            key: Rate limit bucket key (e.g. 'tenant1:tenant').
            max_per_window: Maximum requests allowed in the sliding window.

        Returns:
            True if the request is allowed; False if rate-limited or the
            cardinality cap has been reached.
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= _STATS_WARN_LIMIT:
                    logger.warning(
                        "_RateLimiter: cardinality cap (%d) reached, "
                        "denying key %r (fail-closed)",
                        _STATS_WARN_LIMIT, key,
                    )
                    return False
                bucket = deque()
                self._buckets[key] = bucket

            # Prune expired timestamps from this bucket.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            # B3-M1: evict the key entirely when its bucket is now empty so
            # the dict does not grow without bound for identities that are no
            # longer active.  This also reclaims the cardinality slot so a
            # formerly-active tenant/sender can re-register after going quiet.
            if not bucket and key in self._buckets:
                del self._buckets[key]
                bucket = deque()
                self._buckets[key] = bucket

            if len(bucket) >= max_per_window:
                logger.warning(
                    "_RateLimiter: rate limit exceeded for key %r "
                    "(%d/%d in %.0fs window)",
                    key, len(bucket), max_per_window, self._window,
                )
                return False

            bucket.append(now)
            return True


# ---------------------------------------------------------------------------
# A2AServerContainmentMiddleware
# ---------------------------------------------------------------------------


class A2AServerContainmentMiddleware:
    """Server-side A2A containment middleware.

    Governs incoming A2A requests before they reach the agent handler.
    All evaluation is fail-closed by default: when no governance hooks are
    configured, all requests are denied.

    Thread-safe for concurrent async callers (the internal rate limiter
    and stats counter use threading.Lock).

    Args:
        config: Server configuration. Defaults to A2AServerConfig().
        trust_tracker: Optional trust escalation tracker. When provided,
            the sender's dynamic trust level is used instead of the static
            trust level from AgentIdentity.
        trust_router: Optional trust-based policy router. Currently unused
            directly in process_incoming but available for subclasses and
            future shield pipeline integration.
        governance_hooks: List of MessageGovernanceHook instances evaluated
            in order. First DENY verdict short-circuits. Last DEGRADE
            directive wins.
        card_verifier: Card verifier implementation. Defaults to
            DefaultCardVerifier.
    """

    def __init__(
        self,
        config: A2AServerConfig | None = None,
        trust_tracker: TrustEscalationTracker | None = None,
        trust_router: TrustBasedPolicyRouter | None = None,
        governance_hooks: list[MessageGovernanceHook] | None = None,
        card_verifier: CardVerifierProtocol | None = None,
    ) -> None:
        self._config = config if config is not None else A2AServerConfig()
        self._trust_tracker = trust_tracker
        # TODO: integrate trust_router into process_incoming() routing logic.
        # Currently stored but unused -- kept for API compatibility.
        self._trust_router = trust_router
        self._hooks: list[MessageGovernanceHook] = list(governance_hooks or [])
        self._card_verifier: CardVerifierProtocol = (
            card_verifier if card_verifier is not None else DefaultCardVerifier()
        )
        self._rate_limiter = _RateLimiter(window_seconds=60.0)

        # Per-key request counters for stats. Protected by _stats_lock.
        self._request_counts: dict[str, int] = {}
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_incoming(self, request: A2AIncomingRequest) -> A2AServerDecision:
        """Evaluate an incoming A2A request and return a governance decision.

        Evaluation order:
        1. Message size check
        2. Tenant rate limit
        3. Sender rate limit
        4. Agent Card verification (if card provided)
        5. Trust level resolution
        6. Governance hooks
        7. Fail-closed check (when no hooks are configured)

        Args:
            request: Typed incoming request envelope.

        Returns:
            A2AServerDecision with verdict, reason, sender_trust, and an
            optional degrade_directive.
        """
        config = self._config
        sender = request.sender_identity

        # Step 1: size check
        if request.content_size_bytes >= config.max_message_size_bytes:
            logger.debug(
                "[A2AServer] DENY size %d >= %d for tenant=%r sender=%r",
                request.content_size_bytes,
                config.max_message_size_bytes,
                request.tenant_id,
                sender.agent_id,
            )
            return A2AServerDecision(
                verdict="DENY",
                reason="message too large",
                sender_trust=sender.trust_level,
            )

        # Step 2: tenant rate limit
        tenant_key = f"T:{request.tenant_id}"
        if not self._rate_limiter.is_allowed(tenant_key, config.max_requests_per_minute_per_tenant):
            return A2AServerDecision(
                verdict="DENY",
                reason="tenant rate limit exceeded",
                sender_trust=sender.trust_level,
            )

        # Step 3: sender rate limit
        sender_key = f"S:{request.tenant_id}:{sender.agent_id}"
        if not self._rate_limiter.is_allowed(sender_key, config.max_requests_per_minute_per_sender):
            return A2AServerDecision(
                verdict="DENY",
                reason="sender rate limit exceeded",
                sender_trust=sender.trust_level,
            )

        # Track request for stats
        self._track_request(sender_key)

        # Step 4: Card verification
        provenance = MemoryProvenance.UNVERIFIED
        if request.agent_card is not None:
            try:
                card_provenance = self._card_verifier.verify(request.agent_card)
                if card_provenance.card_verified:
                    provenance = MemoryProvenance.VERIFIED
            except Exception:
                # Treat any verifier failure as unverified -- never crash on bad card
                logger.warning(
                    "[A2AServer] card verifier raised for tenant=%r sender=%r, "
                    "treating as unverified",
                    request.tenant_id,
                    sender.agent_id,
                )
                provenance = MemoryProvenance.UNVERIFIED

        # Step 5: Trust resolution
        if self._trust_tracker is not None:
            resolved_trust = self._trust_tracker.get_trust_level(sender.agent_id)
        else:
            resolved_trust = sender.trust_level

        logger.debug(
            "[A2AServer] tenant=%r sender=%r trust=%s provenance=%s op=%s",
            request.tenant_id,
            sender.agent_id,
            resolved_trust.value,
            provenance.value,
            request.operation,
        )

        # Step 6: Governance hooks
        degrade_directive: Any = None
        if self._hooks:
            msg_ctx = MessageContext(
                message_type=request.operation,
                content_size_bytes=request.content_size_bytes,
                trust_level=resolved_trust.value,
                provenance=provenance,
            )
            for hook in self._hooks:
                try:
                    decision = hook.before_message(msg_ctx)
                except Exception:
                    # Hook failure is fail-closed -- deny to be safe
                    logger.warning(
                        "[A2AServer] governance hook raised, failing closed "
                        "for tenant=%r sender=%r",
                        request.tenant_id,
                        sender.agent_id,
                    )
                    return A2AServerDecision(
                        verdict="DENY",
                        reason="governance hook failed",
                        sender_trust=resolved_trust,
                    )

                if decision.verdict == GovernanceVerdict.DENY:
                    logger.debug(
                        "[A2AServer] DENY from hook for tenant=%r sender=%r reason=%r",
                        request.tenant_id,
                        sender.agent_id,
                        decision.reason,
                    )
                    return A2AServerDecision(
                        verdict="DENY",
                        reason=decision.reason or "denied by governance hook",
                        sender_trust=resolved_trust,
                    )

                if decision.verdict == GovernanceVerdict.DEGRADE:
                    degrade_directive = decision.degrade_directive
                elif decision.verdict != GovernanceVerdict.ALLOW:
                    logger.warning(
                        "[A2AServer] unrecognised verdict %r from hook "
                        "for tenant=%r sender=%r, treating as DENY",
                        decision.verdict,
                        request.tenant_id,
                        sender.agent_id,
                    )
                    return A2AServerDecision(
                        verdict="DENY",
                        reason="unrecognised governance hook verdict",
                        sender_trust=resolved_trust,
                    )

            # All hooks passed
            if degrade_directive is not None:
                return A2AServerDecision(
                    verdict="DEGRADE",
                    reason="governance hook requested degradation",
                    sender_trust=resolved_trust,
                    degrade_directive=degrade_directive,
                )
            return A2AServerDecision(
                verdict="ALLOW",
                reason="all governance hooks passed",
                sender_trust=resolved_trust,
            )

        # Step 7: Fail-closed when no hooks configured
        if config.fail_closed:
            logger.debug(
                "[A2AServer] DENY fail_closed=True, no hooks for tenant=%r sender=%r",
                request.tenant_id,
                sender.agent_id,
            )
            return A2AServerDecision(
                verdict="DENY",
                reason="no governance hooks configured",
                sender_trust=resolved_trust,
            )

        # No hooks + fail_closed=False -- allow
        return A2AServerDecision(
            verdict="ALLOW",
            reason="no governance hooks, fail_closed=False",
            sender_trust=resolved_trust,
        )

    def capabilities(self) -> AdapterCapabilities:
        """Return the capability descriptor for this middleware.

        Returns:
            AdapterCapabilities with A2A server role metadata.
        """
        return AdapterCapabilities(
            framework_name="A2A",
            supports_async=True,
            supports_agent_identity=True,
            extra={"role": "server", "protocol_version": "1.0"},
        )

    def get_request_count(self, tenant_id: str, sender_id: str) -> int:
        """Return the total request count for a given tenant+sender pair.

        Args:
            tenant_id: Tenant scope identifier.
            sender_id: Sender agent identifier.

        Returns:
            Number of requests recorded since middleware creation.
        """
        key = f"S:{tenant_id}:{sender_id}"
        with self._stats_lock:
            return self._request_counts.get(key, 0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _track_request(self, key: str) -> None:
        """Increment the request counter for *key*.

        Silently drops new keys once the cardinality cap is reached.
        """
        with self._stats_lock:
            if key in self._request_counts:
                self._request_counts[key] += 1
                return
            if len(self._request_counts) >= _STATS_WARN_LIMIT:
                logger.warning(
                    "[A2AServer] stats cardinality cap (%d) reached, "
                    "dropping count for key %r",
                    _STATS_WARN_LIMIT, key,
                )
                return
            self._request_counts[key] = 1
