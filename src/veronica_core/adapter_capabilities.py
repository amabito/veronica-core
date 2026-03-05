"""Capability declarations for framework adapters.

Each adapter declares its capabilities via AdapterCapabilities so that
orchestrators can discover adapter features at runtime without instantiation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdapterCapabilities:
    """Static capability descriptor for a framework adapter.

    All fields default to False/empty so that new capabilities can be added
    without breaking existing adapters.

    Attributes:
        supports_streaming: Adapter can handle streaming LLM responses.
        supports_cost_extraction: Adapter can extract USD cost from responses.
        supports_token_extraction: Adapter can extract token counts.
        supports_async: Adapter provides async wrappers.
        supports_reserve_commit: Adapter uses two-phase budget reservation.
        supports_agent_identity: Adapter propagates A2A agent identity.
        framework_name: Human-readable framework name (e.g. "LangChain").
        framework_version_constraint: Optional version constraint string.
        extra: Arbitrary extension metadata.
    """

    supports_streaming: bool = False
    supports_cost_extraction: bool = False
    supports_token_extraction: bool = False
    supports_async: bool = False
    supports_reserve_commit: bool = False
    supports_agent_identity: bool = False
    framework_name: str = ""
    framework_version_constraint: str = ""
    extra: dict[str, object] = field(default_factory=dict)
