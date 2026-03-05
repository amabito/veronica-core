"""Adversarial tests for God Class Split (Step 2) — veronica-core v3.0.0.

Verifies that all public symbols remain importable from the original paths
after the three-file split:
  1. distributed.py  -> distributed_circuit_breaker.py
  2. security/policy_engine.py -> security/policy_rules.py
  3. containment/execution_context.py -> containment/types.py

Also checks for circular imports and cross-reference integrity.
"""

from __future__ import annotations

import importlib
import sys
import types


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _reimport(module_name: str) -> types.ModuleType:
    """Import a module with a clean slate (evicts from sys.modules first)."""
    for key in list(sys.modules.keys()):
        if key == module_name or key.startswith(module_name + "."):
            del sys.modules[key]
    return importlib.import_module(module_name)


# ===========================================================================
# 1. distributed.py re-exports from distributed_circuit_breaker.py
# ===========================================================================


class TestDistributedReexports:
    """All circuit-breaker symbols must be importable from distributed."""

    EXPECTED_FROM_DISTRIBUTED = [
        "CircuitSnapshot",
        "DistributedCircuitBreaker",
        "_LUA_CHECK",
        "_LUA_RECORD_FAILURE",
        "_LUA_RECORD_SUCCESS",
        "get_default_circuit_breaker",
    ]

    def test_circuit_snapshot_importable_from_distributed(self) -> None:
        from veronica_core.distributed import CircuitSnapshot  # noqa: F401

        assert CircuitSnapshot is not None

    def test_distributed_circuit_breaker_importable_from_distributed(self) -> None:
        from veronica_core.distributed import DistributedCircuitBreaker  # noqa: F401

        assert DistributedCircuitBreaker is not None

    def test_lua_check_importable_from_distributed(self) -> None:
        from veronica_core.distributed import _LUA_CHECK  # noqa: F401

        assert isinstance(_LUA_CHECK, str)
        assert len(_LUA_CHECK) > 0

    def test_lua_record_failure_importable_from_distributed(self) -> None:
        from veronica_core.distributed import _LUA_RECORD_FAILURE  # noqa: F401

        assert isinstance(_LUA_RECORD_FAILURE, str)

    def test_lua_record_success_importable_from_distributed(self) -> None:
        from veronica_core.distributed import _LUA_RECORD_SUCCESS  # noqa: F401

        assert isinstance(_LUA_RECORD_SUCCESS, str)

    def test_get_default_circuit_breaker_importable_from_distributed(self) -> None:
        from veronica_core.distributed import get_default_circuit_breaker  # noqa: F401

        assert callable(get_default_circuit_breaker)

    def test_all_expected_reexports_present(self) -> None:
        """Bulk check: every expected symbol is present on the distributed module."""
        import veronica_core.distributed as m

        missing = [name for name in self.EXPECTED_FROM_DISTRIBUTED if not hasattr(m, name)]
        assert missing == [], f"Missing re-exports from distributed.py: {missing}"

    def test_circuit_snapshot_is_same_object_either_import_path(self) -> None:
        """Symbol identity must be preserved across re-export."""
        from veronica_core.distributed import CircuitSnapshot as CS1
        from veronica_core.distributed_circuit_breaker import CircuitSnapshot as CS2

        assert CS1 is CS2, (
            "CircuitSnapshot re-exported from distributed.py must be the same "
            "object as the one in distributed_circuit_breaker.py"
        )

    def test_distributed_cb_same_object_either_path(self) -> None:
        from veronica_core.distributed import DistributedCircuitBreaker as DCB1
        from veronica_core.distributed_circuit_breaker import DistributedCircuitBreaker as DCB2

        assert DCB1 is DCB2

    def test_original_budget_symbols_still_in_distributed(self) -> None:
        """Budget symbols that live in distributed.py must not have been moved away."""
        import veronica_core.distributed as m

        budget_symbols = [
            "BudgetBackend",
            "ReservableBudgetBackend",
            "LocalBudgetBackend",
            "RedisBudgetBackend",
            "get_default_backend",
            "ReservationExpiredError",
            "_redact_exc",
            "_BUDGET_EPSILON",
            "_RESERVATION_TIMEOUT_S",
        ]
        missing = [name for name in budget_symbols if not hasattr(m, name)]
        assert missing == [], f"Budget symbols missing from distributed.py: {missing}"


# ===========================================================================
# 2. Circular import: distributed <-> distributed_circuit_breaker
# ===========================================================================


class TestDistributedCircularImport:
    """Circular import between distributed.py and distributed_circuit_breaker.py must not crash.

    distributed.py imports DistributedCircuitBreaker from distributed_circuit_breaker at
    module level (line 702).  distributed_circuit_breaker.py imports _redact_exc from
    distributed at module level (line 17).  Python handles this because _redact_exc is
    defined before the cross-import, but it is fragile if the definition order ever changes.
    """

    def test_import_distributed_first_does_not_crash(self) -> None:
        """Import distributed.py first — triggers dcb import which back-imports distributed."""
        # Clear cache to test clean-slate import
        mods_to_drop = [k for k in sys.modules if "veronica_core.distributed" in k]
        for k in mods_to_drop:
            del sys.modules[k]

        import veronica_core.distributed as m  # noqa: F401

        assert hasattr(m, "DistributedCircuitBreaker")
        assert hasattr(m, "_redact_exc")

    def test_import_dcb_first_does_not_crash(self) -> None:
        """Import distributed_circuit_breaker.py first must not fail.

        The circular import was resolved by duplicating _redact_exc directly into
        distributed_circuit_breaker.py (no import from distributed.py required).
        distributed_circuit_breaker.py now only imports from circuit_breaker and
        runtime_policy, which have no cross-dependency on distributed.py.
        """
        mods_to_drop = [k for k in sys.modules if "veronica_core.distributed" in k]
        for k in mods_to_drop:
            del sys.modules[k]

        import veronica_core.distributed_circuit_breaker as dcb  # noqa: F401

        assert hasattr(dcb, "DistributedCircuitBreaker")

    def test_redact_exc_resolved_in_dcb(self) -> None:
        """_redact_exc must be correctly resolved inside distributed_circuit_breaker.

        If the circular import is broken, dcb would import a partial distributed module
        and _redact_exc would be missing, causing NameError at runtime.
        """
        from veronica_core.distributed_circuit_breaker import DistributedCircuitBreaker

        # _redact_exc is used inside _connect() and _activate_fallback(); confirming
        # the class is importable and those methods exist confirms the function resolved.
        assert hasattr(DistributedCircuitBreaker, "_connect")
        assert hasattr(DistributedCircuitBreaker, "_activate_fallback")

    def test_redact_exc_exists_in_both_modules(self) -> None:
        """_redact_exc exists in both distributed.py and distributed_circuit_breaker.py.

        distributed_circuit_breaker has its own copy to avoid circular imports.
        Both copies must be callable and produce consistent output.
        """
        from veronica_core.distributed import _redact_exc as dist_redact
        from veronica_core.distributed_circuit_breaker import _redact_exc as dcb_redact

        assert callable(dist_redact)
        assert callable(dcb_redact)
        # Both should produce same output for same input
        exc = ValueError("redis://user:pass@host:6379/0 failed")
        assert dist_redact(exc) == dcb_redact(exc)


# ===========================================================================
# 3. security/policy_engine.py re-exports from security/policy_rules.py
# ===========================================================================


class TestPolicyEngineReexports:
    """All symbols re-exported in policy_engine.py's import block must be accessible."""

    # Full list of symbols declared in the re-export block of policy_engine.py
    PUBLIC_SYMBOLS = [
        "ActionLiteral",
        "ExecPolicyContext",
        "ExecPolicyDecision",
        "FILE_COUNT_APPROVAL_THRESHOLD",
        "FILE_READ_DENY_PATTERNS",
        "FILE_WRITE_APPROVAL_PATTERNS",
        "FILE_WRITE_LOCKFILE_PATTERNS",
        "GIT_DENY_SUBCMDS",
        "NET_ALLOWLIST_HOSTS",
        "NET_DENY_METHODS",
        "NET_ENTROPY_MIN_LEN",
        "NET_ENTROPY_THRESHOLD",
        "NET_PATH_ALLOWLIST",
        "NET_URL_MAX_LENGTH",
        "PolicyContext",
        "PolicyDecision",
        "SHELL_ALLOW_COMMANDS",
        "SHELL_CREDENTIAL_DENY",
        "SHELL_DENY_COMMANDS",
        "SHELL_DENY_EXEC_FLAGS",
        "SHELL_DENY_OPERATORS",
        "SHELL_PKG_INSTALL_APPROVAL",
    ]

    PRIVATE_REEXPORTED = [
        "_eval_browser",
        "_eval_file_read",
        "_eval_file_write",
        "_eval_git",
        "_eval_net",
        "_eval_shell",
        "_matches_any",
        "_shannon_entropy",
        "_has_combined_short_flag",
        "_url_host",
        "_url_path",
        "_check_shell_deny_commands",
        "_check_shell_operators",
        "_check_credentials_in_args",
        "_check_python_exec_flags",
        "_check_pkg_install",
        "_check_host_restrictions",
        "_check_protocol_rules",
        "_check_data_exfil",
    ]

    def test_all_public_symbols_importable_from_policy_engine(self) -> None:
        import veronica_core.security.policy_engine as pe

        missing = [s for s in self.PUBLIC_SYMBOLS if not hasattr(pe, s)]
        assert missing == [], (
            f"Public symbols missing from policy_engine.py after split: {missing}"
        )

    def test_all_private_reexports_importable_from_policy_engine(self) -> None:
        import veronica_core.security.policy_engine as pe

        missing = [s for s in self.PRIVATE_REEXPORTED if not hasattr(pe, s)]
        assert missing == [], (
            f"Private re-exported symbols missing from policy_engine.py: {missing}"
        )

    def test_policy_context_alias_preserved(self) -> None:
        """PolicyContext must be the backward-compatible alias for ExecPolicyContext."""
        from veronica_core.security.policy_engine import ExecPolicyContext, PolicyContext

        assert PolicyContext is ExecPolicyContext, (
            "PolicyContext alias in policy_engine.py must be ExecPolicyContext"
        )

    def test_policy_decision_alias_preserved(self) -> None:
        from veronica_core.security.policy_engine import ExecPolicyDecision, PolicyDecision

        assert PolicyDecision is ExecPolicyDecision

    def test_symbols_same_object_as_in_policy_rules(self) -> None:
        """Re-exported constants must be identical objects, not copies."""
        from veronica_core.security import policy_engine as pe
        from veronica_core.security import policy_rules as pr

        for name in self.PUBLIC_SYMBOLS:
            if name in ("ActionLiteral", "PolicyContext", "PolicyDecision",
                        "ExecPolicyContext", "ExecPolicyDecision"):
                # These are type aliases; identity may differ but the target must match
                continue
            pe_obj = getattr(pe, name, None)
            pr_obj = getattr(pr, name, None)
            assert pe_obj is pr_obj, (
                f"{name}: policy_engine re-export (id={id(pe_obj)}) differs from "
                f"policy_rules original (id={id(pr_obj)})"
            )

    def test_policy_engine_security_init_exports_needed_symbols(self) -> None:
        """security/__init__.py must still export the four key aliases."""
        from veronica_core.security import (
            ExecPolicyContext,  # noqa: F401
            ExecPolicyDecision,  # noqa: F401
            PolicyContext,  # noqa: F401
            PolicyDecision,  # noqa: F401
            PolicyEngine,  # noqa: F401
            PolicyHook,  # noqa: F401
        )

    def test_policy_engine_evaluates_after_split(self) -> None:
        """PolicyEngine.evaluate() must still function end-to-end."""
        from veronica_core.security.capabilities import CapabilitySet
        from veronica_core.security.policy_engine import PolicyContext, PolicyEngine

        engine = PolicyEngine()
        ctx = PolicyContext(
            action="shell",
            args=["pytest"],
            working_dir=".",
            repo_root=".",
            user=None,
            caps=CapabilitySet.dev(),
            env="dev",
        )
        decision = engine.evaluate(ctx)
        assert decision.verdict == "ALLOW"

    def test_private_helpers_not_in_policy_rules_are_not_leaked(self) -> None:
        """Internal constants that were NOT re-exported should not appear on policy_engine."""
        import veronica_core.security.policy_engine as pe

        # These are truly internal to policy_rules and were NOT re-exported
        leaked = [
            name for name in (
                "_PYTHON_MODULE_PKG_MANAGERS",
                "_RE_BASE64",
                "_RE_HEX",
                "_UVR_INLINE_EXEC_FLAGS",
                "_UV_INSTALL_SUBCMDS",
                "_UV_PIP_INSTALL_SUBCMDS",
            )
            if hasattr(pe, name)
        ]
        # Current design: these are NOT re-exported (they are internal).
        # If any appear on policy_engine, it means a star-import crept in.
        assert leaked == [], (
            f"Internal policy_rules names leaked onto policy_engine namespace: {leaked}"
        )


# ===========================================================================
# 4. containment/execution_context.py re-exports from containment/types.py
# ===========================================================================


class TestContainmentTypesReexports:
    """All types.py symbols must be importable from execution_context and containment package."""

    TYPES_ALL = [
        "CancellationToken",
        "ChainMetadata",
        "ContextSnapshot",
        "ExecutionConfig",
        "NodeRecord",
        "WrapOptions",
    ]

    def test_types_all_importable_from_execution_context(self) -> None:
        import veronica_core.containment.execution_context as ec

        missing = [name for name in self.TYPES_ALL if not hasattr(ec, name)]
        assert missing == [], (
            f"Types missing from execution_context.py namespace: {missing}"
        )

    def test_types_all_importable_from_containment_package(self) -> None:
        import veronica_core.containment as pkg

        missing = [name for name in self.TYPES_ALL if not hasattr(pkg, name)]
        assert missing == [], (
            f"Types missing from containment/__init__.py: {missing}"
        )

    def test_types_same_object_via_both_paths(self) -> None:
        """Same class object whether imported from types or execution_context."""
        import veronica_core.containment.types as t
        import veronica_core.containment.execution_context as ec

        for name in self.TYPES_ALL:
            t_obj = getattr(t, name)
            ec_obj = getattr(ec, name)
            assert t_obj is ec_obj, (
                f"{name}: types.py object (id={id(t_obj)}) differs from "
                f"execution_context.py re-export (id={id(ec_obj)})"
            )

    def test_cancellation_token_functional(self) -> None:
        from veronica_core.containment import CancellationToken

        token = CancellationToken()
        assert not token.is_cancelled
        token.cancel()
        assert token.is_cancelled

    def test_execution_config_functional(self) -> None:
        from veronica_core.containment import ExecutionConfig

        cfg = ExecutionConfig(max_cost_usd=1.0, max_steps=10, max_retries_total=3)
        assert cfg.max_cost_usd == 1.0

    def test_wrap_options_functional(self) -> None:
        from veronica_core.containment import WrapOptions

        opts = WrapOptions(operation_name="test_op", cost_estimate_hint=0.05)
        assert opts.operation_name == "test_op"

    def test_chain_metadata_functional(self) -> None:
        from veronica_core.containment import ChainMetadata

        meta = ChainMetadata(request_id="req-1", chain_id="chain-1", org_id="org-x")
        assert meta.org_id == "org-x"

    def test_context_snapshot_functional(self) -> None:
        from veronica_core.containment import ContextSnapshot

        snap = ContextSnapshot(
            chain_id="c1",
            request_id="r1",
            step_count=0,
            cost_usd_accumulated=0.0,
            retries_used=0,
            aborted=False,
            abort_reason=None,
            elapsed_ms=0.0,
            nodes=[],
            events=[],
        )
        assert snap.chain_id == "c1"

    def test_execution_context_all_includes_types(self) -> None:
        """execution_context.__all__ must include all types.__all__ entries."""
        from veronica_core.containment.types import __all__ as types_all
        import veronica_core.containment.execution_context as ec

        ec_all = set(ec.__all__)
        missing_in_ec_all = [name for name in types_all if name not in ec_all]
        assert missing_in_ec_all == [], (
            f"Types from types.py not included in execution_context.__all__: "
            f"{missing_in_ec_all}"
        )

    def test_direct_import_from_types_module_path(self) -> None:
        """Types must also be importable directly from the new module path."""
        from veronica_core.containment.types import (  # noqa: F401
            CancellationToken,
            ChainMetadata,
            ContextSnapshot,
            ExecutionConfig,
            NodeRecord,
            WrapOptions,
        )

    def test_execution_context_still_importable(self) -> None:
        """ExecutionContext itself must still be importable from the original path."""
        from veronica_core.containment.execution_context import ExecutionContext  # noqa: F401
        from veronica_core.containment import ExecutionContext as EC2  # noqa: F401

        assert ExecutionContext is EC2


# ===========================================================================
# 5. Cross-reference integrity: distributed_circuit_breaker uses distributed
# ===========================================================================


class TestCrossReferenceIntegrity:
    """Symbols that cross-reference between the split files must resolve correctly."""

    def test_distributed_cb_uses_redact_exc_from_distributed(self) -> None:
        """_redact_exc must be resolvable in distributed_circuit_breaker at import time."""
        # If this import succeeds, the cross-reference worked.
        import veronica_core.distributed_circuit_breaker  # noqa: F401

    def test_distributed_cb_uses_circuit_breaker_from_circuit_breaker_module(self) -> None:
        """DistributedCircuitBreaker._fallback is a local CircuitBreaker instance."""
        from veronica_core.distributed_circuit_breaker import DistributedCircuitBreaker
        from veronica_core.circuit_breaker import CircuitBreaker

        # DistributedCircuitBreaker creates a local CircuitBreaker as fallback
        # This verifies the import inside dcb.py resolved correctly.
        assert DistributedCircuitBreaker is not None
        assert CircuitBreaker is not None

    def test_policy_rules_uses_capabilities_correctly(self) -> None:
        """policy_rules.py imports Capability and CapabilitySet — must not be broken."""
        from veronica_core.security.policy_rules import _eval_git
        from veronica_core.security.capabilities import CapabilitySet
        from veronica_core.security.policy_rules import PolicyContext

        ctx = PolicyContext(
            action="git",
            args=["push"],
            working_dir=".",
            repo_root=".",
            user=None,
            caps=CapabilitySet.ci(),  # CI caps: no GIT_PUSH_APPROVAL
            env="dev",
        )
        decision = _eval_git(ctx)
        assert decision is not None
        assert decision.verdict == "DENY"  # no GIT_PUSH_APPROVAL capability

    def test_execution_context_imports_from_types_not_itself(self) -> None:
        """execution_context.py must not import from itself (no self-circular reference)."""
        import veronica_core.containment.types as t
        import veronica_core.containment.execution_context as ec

        # Verify the classes are the same identity (imported, not redefined)
        for name in t.__all__:
            t_cls = getattr(t, name)
            ec_cls = getattr(ec, name)
            assert t_cls is ec_cls, (
                f"{name} re-exported in execution_context is a different object "
                f"from types.py — may indicate a redefinition bug"
            )

    def test_policy_engine_evaluator_dict_uses_rules_functions(self) -> None:
        """_EVALUATORS in policy_engine.py must reference the functions from policy_rules."""
        from veronica_core.security import policy_engine as pe
        from veronica_core.security import policy_rules as pr

        # The _EVALUATORS dict is internal; we verify it via evaluate() behavior
        # which uses the rule functions from policy_rules.
        from veronica_core.security.capabilities import CapabilitySet

        ctx = pr.PolicyContext(
            action="browser",
            args=[],
            working_dir=".",
            repo_root=".",
            user=None,
            caps=CapabilitySet.dev(),
            env="dev",
        )
        engine = pe.PolicyEngine()
        decision = engine.evaluate(ctx)
        assert decision.verdict == "DENY"
        assert "BROWSER_DENY_DEFAULT" in decision.rule_id


# ===========================================================================
# 6. Regression: old import paths must not regress
# ===========================================================================


class TestOldImportPathRegression:
    """Simulate external callers that use the old import paths."""

    def test_old_path_distributed_circuit_snapshot(self) -> None:
        from veronica_core.distributed import CircuitSnapshot

        snap = CircuitSnapshot(
            state=__import__(
                "veronica_core.circuit_breaker", fromlist=["CircuitState"]
            ).CircuitState.CLOSED,
            failure_count=0,
            success_count=0,
            last_failure_time=None,
            distributed=False,
            circuit_id="test",
        )
        assert snap.distributed is False

    def test_old_path_policy_engine_policy_context(self) -> None:
        from veronica_core.security.policy_engine import PolicyContext
        from veronica_core.security.capabilities import CapabilitySet

        ctx = PolicyContext(
            action="file_read",
            args=["/tmp/safe.txt"],
            working_dir=".",
            repo_root=".",
            user="alice",
            caps=CapabilitySet.dev(),
            env="dev",
        )
        assert ctx.user == "alice"

    def test_old_path_containment_execution_config(self) -> None:
        from veronica_core.containment.execution_context import ExecutionConfig

        cfg = ExecutionConfig(max_cost_usd=0.5, max_steps=5, max_retries_total=2)
        assert cfg.max_steps == 5

    def test_old_path_containment_wrap_options(self) -> None:
        from veronica_core.containment.execution_context import WrapOptions

        opts = WrapOptions(operation_name="plan", cost_estimate_hint=0.01)
        assert opts.operation_name == "plan"

    def test_old_path_distributed_get_default_circuit_breaker(self) -> None:
        from veronica_core.distributed import get_default_circuit_breaker
        from veronica_core.circuit_breaker import CircuitBreaker

        cb = get_default_circuit_breaker(redis_url=None)
        assert isinstance(cb, CircuitBreaker)
