"""Policy rollback protection for VERONICA Security Containment Layer."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.audit.log import AuditLog

ENGINE_VERSION = "0.1.0"


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted/hyphenated version string into a tuple of ints.

    Args:
        v: Version string such as "1.2.3" or "0.1.0-alpha".

    Returns:
        Tuple of ints, e.g. (1, 2, 3).
    """
    return tuple(int(x) for x in re.split(r"[.\-]", v)[:3] if x.isdigit())


class RollbackGuard:
    """Guard that prevents policy rollback attacks.

    Compares the incoming policy version against the last accepted version
    recorded in the audit log.  Raises RuntimeError on rollback or engine
    version mismatch.

    Args:
        audit_log: Optional AuditLog instance.  When None all checks are
                   skipped (no persistent state available).
    """

    def __init__(self, audit_log: AuditLog | None = None) -> None:
        self._audit_log = audit_log

    def check(
        self,
        policy_version: int,
        min_engine_version: str | None = None,
    ) -> None:
        """Validate *policy_version* against rollback and engine constraints.

        Steps:
        1. Retrieve the last accepted version from the audit log.
        2. If *policy_version* < last accepted → log rollback + raise.
        3. Check *min_engine_version* against ENGINE_VERSION → raise if not met.
        4. Log version accepted + write checkpoint.

        Args:
            policy_version: Version integer from the policy file.
            min_engine_version: Optional minimum engine version string the
                                policy requires (e.g. "0.1.0").

        Raises:
            RuntimeError: On rollback detection or engine version mismatch.
        """
        last_seen = (
            self._audit_log.get_last_policy_version()
            if self._audit_log is not None
            else None
        )

        # Rollback check
        if last_seen is not None and policy_version < last_seen:
            if self._audit_log is not None:
                self._audit_log.log_policy_rollback(policy_version, last_seen)
            raise RuntimeError(
                f"Policy rollback detected: {policy_version} < {last_seen}"
            )

        # Engine version check
        if min_engine_version:
            if parse_version(ENGINE_VERSION) < parse_version(min_engine_version):
                raise RuntimeError(
                    f"Engine {ENGINE_VERSION} < required {min_engine_version}"
                )

        # Accept the policy version
        if self._audit_log is not None:
            self._audit_log.log_policy_version_accepted(
                policy_version, "policies/default.yaml"
            )
            self._audit_log.write_policy_checkpoint(policy_version)
