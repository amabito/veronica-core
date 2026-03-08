"""Audit enrichment helpers for policy-attested execution (VERONICA Core v3.2+).

These helpers add policy provenance information to audit entry data dicts
so that every audited event can be traced back to the policy bundle that
governed it.

No external dependencies are required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veronica_core.policy.frozen_view import FrozenPolicyView


def enrich_audit_with_policy(
    data: dict[str, Any],
    policy_view: "FrozenPolicyView | None",
) -> dict[str, Any]:
    """Add policy metadata to an audit entry's data dict.

    Returns a new dict (the original is never mutated) with a ``"policy"``
    key appended.  If *policy_view* is None the key is set to None, which
    signals to reviewers that no active policy governed this event
    (fail-open awareness).

    Args:
        data: The existing audit entry payload dict.
        policy_view: The active FrozenPolicyView, or None if no policy
                     is currently loaded.

    Returns:
        A new dict with the same contents as *data* plus the ``"policy"`` key.
    """
    if policy_view is None:
        return {**data, "policy": None}
    return {**data, "policy": policy_view.to_audit_dict()}
