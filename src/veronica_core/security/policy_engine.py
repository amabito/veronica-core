"""Policy Engine for VERONICA Security Containment Layer.

Evaluates PolicyContext against ordered rules and returns PolicyDecision.
Rules are fail-closed: default verdict is DENY.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Literal

from veronica_core.security.capabilities import CapabilitySet
from veronica_core.shield.types import Decision, ToolCallContext

# Re-export all public symbols from policy_rules for backward compatibility.
from veronica_core.security.policy_rules import (  # noqa: F401
    ActionLiteral,
    ExecPolicyContext,
    ExecPolicyDecision,
    FILE_COUNT_APPROVAL_THRESHOLD,
    FILE_READ_DENY_PATTERNS,
    FILE_WRITE_APPROVAL_PATTERNS,
    FILE_WRITE_LOCKFILE_PATTERNS,
    GIT_DENY_SUBCMDS,
    NET_ALLOWLIST_HOSTS,
    NET_DENY_METHODS,
    NET_ENTROPY_MIN_LEN,
    NET_ENTROPY_THRESHOLD,
    NET_PATH_ALLOWLIST,
    NET_URL_MAX_LENGTH,
    PolicyContext,
    PolicyDecision,
    SHELL_ALLOW_COMMANDS,
    SHELL_CREDENTIAL_DENY,
    SHELL_DENY_COMMANDS,
    SHELL_DENY_EXEC_FLAGS,
    SHELL_DENY_OPERATORS,
    SHELL_PKG_INSTALL_APPROVAL,
    _eval_browser,
    _eval_file_read,
    _eval_file_write,
    _eval_git,
    _eval_net,
    _eval_shell,
    _matches_any,
    _shannon_entropy,
    _has_combined_short_flag,
    _url_host,
    _url_path,
    _check_shell_deny_commands,
    _check_shell_operators,
    _check_credentials_in_args,
    _check_python_exec_flags,
    _check_pkg_install,
    _check_host_restrictions,
    _check_protocol_rules,
    _check_data_exfil,
)


# ---------------------------------------------------------------------------
# Policy Engine
# ---------------------------------------------------------------------------

_EVALUATORS = {
    "shell": _eval_shell,
    "file_read": _eval_file_read,
    "file_write": _eval_file_write,
    "net": _eval_net,
    "git": _eval_git,
    "browser": _eval_browser,
}

_DEFAULT_DENY = PolicyDecision(
    verdict="DENY",
    rule_id="DEFAULT_DENY",
    reason="No rule matched; fail-closed policy applies",
    risk_score_delta=5,
)


class PolicyEngine:
    """Evaluates PolicyContext against security rules and returns PolicyDecision.

    Rules are evaluated in order: DENY first, then REQUIRE_APPROVAL, then ALLOW.
    Unknown actions are denied by default (fail-closed).
    """

    def __init__(
        self,
        policy_path: Path | None = None,
        public_key_path: Path | None = None,
        key_provider: Any = None,
    ) -> None:
        """Initialize the engine.

        If *policy_path* is provided, its signature is verified.
        v2 (ed25519) is checked first via ``.sig.v2``; v1 (HMAC-SHA256)
        via ``.sig`` is the fallback.  A mismatch raises ``RuntimeError``.
        A missing sig file logs a ``policy_sig_missing`` warning and
        allows the engine to continue loading (backward compatibility).

        In CI or PROD security levels:
        - A missing signature file raises ``RuntimeError`` (not just a warning).
        - If ed25519 (cryptography package) is unavailable, raises ``RuntimeError``.

        Args:
            policy_path: Optional path to a YAML policy file.
            public_key_path: Optional path to the ed25519 public key PEM
                             used for v2 verification.  Defaults to
                             ``policies/public_key.pem`` in the repo root.
        """
        from veronica_core.security.security_level import (
            SecurityLevel,
            get_security_level,
        )
        from veronica_core.security.policy_signing import _ED25519_AVAILABLE

        self._policy_path = policy_path
        self._public_key_path = public_key_path
        self._key_provider = key_provider
        self._policy: dict[str, Any] = {}
        self._audit_log = None

        level = get_security_level()
        strict = level in (SecurityLevel.CI, SecurityLevel.PROD)

        if strict and not _ED25519_AVAILABLE:
            raise RuntimeError(
                f"cryptography package is required in {level.name} environment. "
                "Install it with: pip install cryptography"
            )

        if policy_path is not None:
            if strict:
                sig_v2_path = Path(str(policy_path) + ".sig.v2")
                sig_v1_path = Path(str(policy_path) + ".sig")
                if not sig_v2_path.exists() and not sig_v1_path.exists():
                    raise RuntimeError(
                        f"Policy signature file missing in {level.name} environment: "
                        f"{policy_path}"
                    )
            self._verify_policy_signature(
                policy_path,
                public_key_path=public_key_path,
                key_provider=key_provider,
            )
            self._policy = self._load_policy(policy_path)
            self._check_rollback()

    # ------------------------------------------------------------------
    # Signature verification (G-1)
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_policy_audit(event: str, payload: dict) -> None:
        """Write a policy-related audit event (best-effort, never raises)."""
        try:
            import tempfile

            from veronica_core.audit.log import AuditLog

            audit_dir = Path(tempfile.gettempdir()) / "veronica_audit"
            audit_log = AuditLog(audit_dir / "policy.jsonl")
            audit_log.write(event, payload)
        except Exception:
            pass

    @staticmethod
    def _validate_jwk_format(
        policy_path: Path,
        sig_v2_path: Path,
        public_key_path: Path | None,
        key_provider: Any = None,
    ) -> None:
        """Verify ed25519 (v2) signature and enforce key pin.

        Raises RuntimeError on tamper or key-pin violation.
        """
        import logging as _logging

        from veronica_core.security.policy_signing import PolicySignerV2

        _log = _logging.getLogger(__name__)
        signer_v2 = PolicySignerV2(
            public_key_path=public_key_path, key_provider=key_provider
        )

        if not signer_v2.verify(policy_path, sig_v2_path):
            PolicyEngine._emit_policy_audit(
                "policy_tamper", {"policy_path": str(policy_path), "version": "v2"}
            )
            _log.error("policy_tamper (v2): signature mismatch for %s", policy_path)
            raise RuntimeError(f"Policy tamper detected (v2): {policy_path}")

        resolved_pub = public_key_path or signer_v2.public_key_path
        if not resolved_pub.exists():
            return

        try:
            from veronica_core.security.key_pin import KeyPinChecker
            import tempfile
            from veronica_core.audit.log import AuditLog

            audit_dir = Path(tempfile.gettempdir()) / "veronica_audit"
            audit_log: Any
            try:
                audit_log = AuditLog(audit_dir / "policy.jsonl")
            except Exception:
                audit_log = None
            pub_pem = resolved_pub.read_bytes()
            KeyPinChecker(audit_log).enforce(pub_pem)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            _log.warning("key_pin check failed unexpectedly: %s", exc)

    @staticmethod
    def _verify_jws_signature(
        policy_path: Path,
        sig_v1_path: Path,
    ) -> None:
        """Verify HMAC-SHA256 (v1) signature.

        Raises RuntimeError on tamper.
        """
        import logging as _logging

        from veronica_core.security.policy_signing import PolicySigner

        _log = _logging.getLogger(__name__)
        signer = PolicySigner()

        if signer.verify(policy_path, sig_v1_path):
            return

        PolicyEngine._emit_policy_audit(
            "policy_tamper",
            {
                "policy_path": str(policy_path),
                "expected": "<redacted>",
                "actual": "<redacted>",
            },
        )
        _log.error("policy_tamper: signature mismatch for %s", policy_path)
        raise RuntimeError(f"Policy tamper detected: {policy_path}")

    @staticmethod
    def _verify_policy_signature(
        policy_path: Path,
        public_key_path: Path | None = None,
        key_provider: Any = None,
    ) -> None:
        """Verify policy signature for *policy_path*.

        Checks v2 (ed25519) first if a ``.sig.v2`` file exists, then falls back
        to v1 (HMAC-SHA256) via ``.sig``.  Raises RuntimeError on tamper;
        logs warning if no signature file is found.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        sig_v2_path = Path(str(policy_path) + ".sig.v2")
        sig_v1_path = Path(str(policy_path) + ".sig")

        if sig_v2_path.exists():
            PolicyEngine._validate_jwk_format(
                policy_path, sig_v2_path, public_key_path, key_provider
            )
            return

        if sig_v1_path.exists():
            PolicyEngine._verify_jws_signature(policy_path, sig_v1_path)
            return

        PolicyEngine._emit_policy_audit(
            "policy_sig_missing", {"policy_path": str(policy_path)}
        )
        _log.warning("policy_sig_missing: no signature file found for %s", policy_path)

    @staticmethod
    def _load_policy(policy_path: Path) -> dict[str, Any]:
        """Load and parse a YAML policy file.

        Behaviour (v0.10.3 fail-closed change, R-5):

        * If the file **does not exist**: emit a warning and return ``{}``.
        * If the file **exists** but cannot be loaded: **raise RuntimeError**.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        if not policy_path.exists():
            _log.warning(
                "policy_load_failed: policy file not found: %s",
                policy_path,
            )
            return {}

        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                f"policy_load_failed: pyyaml is required to load the policy "
                f"at {policy_path}. Install it with: pip install pyyaml"
            ) from exc

        try:
            with policy_path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception as exc:
            raise RuntimeError(
                f"policy_load_failed: policy file exists but could not be "
                f"parsed: {policy_path} ({type(exc).__name__})"
            ) from exc

    def _check_rollback(self) -> None:
        """Check policy_version / min_engine_version fields if present."""
        from veronica_core.security.rollback_guard import RollbackGuard

        policy_version = self._policy.get("policy_version")
        min_engine = self._policy.get("min_engine_version")
        if policy_version is not None:
            guard = RollbackGuard(audit_log=self._audit_log)
            guard.check(int(policy_version), min_engine)

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Evaluate *ctx* and return a PolicyDecision.

        The engine delegates to a per-action evaluator function.
        If the action is unknown, DENY is returned immediately.
        """
        evaluator = _EVALUATORS.get(ctx.action)
        if evaluator is None:
            return PolicyDecision(
                verdict="DENY",
                rule_id="UNKNOWN_ACTION",
                reason=f"Action '{ctx.action}' is not recognised",
                risk_score_delta=5,
            )

        result = evaluator(ctx)
        return result if result is not None else _DEFAULT_DENY


# ---------------------------------------------------------------------------
# PolicyHook -- ToolDispatchHook + EgressBoundaryHook integration
# ---------------------------------------------------------------------------


class PolicyHook:
    """Implements ToolDispatchHook and EgressBoundaryHook protocols.

    Wraps PolicyEngine to intercept tool calls and egress requests.

    Attributes:
        last_decision: The most recent PolicyDecision evaluated.
    """

    def __init__(
        self,
        engine: PolicyEngine | None = None,
        caps: CapabilitySet | None = None,
        working_dir: str = ".",
        repo_root: str = ".",
        env: str = "unknown",
    ) -> None:
        self._engine = engine or PolicyEngine()
        self._caps = caps or CapabilitySet.dev()
        self._working_dir = working_dir
        self._repo_root = repo_root
        self._env = env
        self._last_decision: PolicyDecision | None = None
        self._last_decision_lock = threading.Lock()

    @property
    def last_decision(self) -> PolicyDecision | None:
        """The most recent PolicyDecision evaluated (thread-safe read)."""
        with self._last_decision_lock:
            return self._last_decision

    @last_decision.setter
    def last_decision(self, value: PolicyDecision | None) -> None:
        with self._last_decision_lock:
            self._last_decision = value

    def _verdict_to_decision(
        self, verdict: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    ) -> Decision:
        if verdict == "ALLOW":
            return Decision.ALLOW
        if verdict == "REQUIRE_APPROVAL":
            return Decision.QUARANTINE
        return Decision.HALT

    def before_tool_call(self, ctx: ToolCallContext) -> Decision | None:
        """Intercept tool dispatch. Extract action from ctx.metadata."""
        meta = ctx.metadata or {}
        action: str = meta.get("action", "shell")
        args: list[str] = meta.get("args", [])

        policy_ctx = PolicyContext(
            action=action,  # type: ignore[arg-type]
            args=args,
            working_dir=meta.get("working_dir", self._working_dir),
            repo_root=meta.get("repo_root", self._repo_root),
            user=ctx.user_id,
            caps=self._caps,
            env=self._env,
            metadata=meta,
        )
        decision = self._engine.evaluate(policy_ctx)
        self.last_decision = decision
        return self._verdict_to_decision(decision.verdict)

    def before_egress(
        self, ctx: ToolCallContext, url: str, method: str
    ) -> Decision | None:
        """Intercept outbound HTTP request."""
        policy_ctx = PolicyContext(
            action="net",
            args=[url, method],
            working_dir=self._working_dir,
            repo_root=self._repo_root,
            user=ctx.user_id,
            caps=self._caps,
            env=self._env,
            metadata=ctx.metadata or {},
        )
        decision = self._engine.evaluate(policy_ctx)
        self.last_decision = decision
        return self._verdict_to_decision(decision.verdict)
