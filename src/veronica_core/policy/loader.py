"""Policy loader for VERONICA Declarative Policy Layer.

Parses YAML/JSON policy files and builds ShieldPipeline instances.
JSON: stdlib only. YAML: requires pyyaml (optional).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from veronica_core.policy.registry import PolicyRegistry
from veronica_core.policy.schema import PolicySchema, RuleSchema, PolicyValidationError
from veronica_core.shield.hooks import (
    BudgetBoundaryHook,
    EgressBoundaryHook,
    PreDispatchHook,
    RetryBoundaryHook,
    ToolDispatchHook,
)
from veronica_core.shield.pipeline import ShieldPipeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _parse_json(content: str) -> dict[str, Any]:
    """Parse JSON content string, raising a clear error on failure."""
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PolicyValidationError(
            [f"Invalid JSON: {exc}"],
            field_name="content",
        ) from exc
    if not isinstance(result, dict):
        raise PolicyValidationError(
            ["Policy must be a JSON object at the top level"],
            field_name="content",
        )
    return result


def _parse_yaml(content: str) -> dict[str, Any]:
    """Parse YAML content string. Requires pyyaml."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "pyyaml is required to load YAML policies. "
            "Install with: pip install pyyaml  (or: pip install veronica-core[yaml])"
        ) from None
    result = yaml.safe_load(content)
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise PolicyValidationError(
            ["Policy must be a YAML mapping at the top level"],
            field_name="content",
        )
    return result


def _parse_content(content: str, fmt: str) -> dict[str, Any]:
    """Dispatch to the correct parser based on *fmt*."""
    if fmt == "json":
        return _parse_json(content)
    return _parse_yaml(content)


def _fmt_for_path(file_path: Path) -> Literal["json", "yaml"]:
    """Infer format from file extension; default to JSON."""
    suffix = file_path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    return "json"


def _parse_to_schema(path: str | Path) -> PolicySchema:
    """Read *path*, parse, and return a validated PolicySchema.

    Shared by load() and validate() to avoid duplicating parse logic.
    """
    file_path = Path(path)
    content = file_path.read_text(encoding="utf-8")
    fmt = _fmt_for_path(file_path)
    data = _parse_content(content, fmt)
    return PolicySchema.from_dict(data)


# ---------------------------------------------------------------------------
# LoadedPolicy -- wraps ShieldPipeline + schema + hooks (fix #3)
# ---------------------------------------------------------------------------


@dataclass
class LoadedPolicy:
    """Result of loading a policy file.

    Wraps the built ShieldPipeline together with the source schema and the
    list of instantiated (rule, component) pairs for introspection.

    Attributes:
        pipeline: The configured ShieldPipeline ready to use.
        schema:   The parsed PolicySchema that produced this pipeline.
        hooks:    Ordered list of (RuleSchema, component) pairs, one per rule.
                  Each component is the SAME instance wired into the pipeline.
    """

    pipeline: ShieldPipeline
    schema: PolicySchema
    hooks: list[tuple[RuleSchema, Any]] = field(default_factory=list)

    # Convenience proxy: make LoadedPolicy usable wherever ShieldPipeline is
    # expected by delegating attribute access to the inner pipeline.
    def __getattr__(self, name: str) -> Any:
        # Avoid infinite recursion for dataclass fields accessed before __init__.
        if name in ("pipeline", "schema", "hooks"):
            raise AttributeError(name)
        return getattr(self.pipeline, name)


# ---------------------------------------------------------------------------
# Internal pipeline builder (fix #1 + #2 + #3)
# ---------------------------------------------------------------------------


def _build_pipeline(
    schema: PolicySchema,
    registry: PolicyRegistry,
) -> LoadedPolicy:
    """Build a LoadedPolicy from a validated PolicySchema.

    Single loop: each factory is called ONCE per rule.  The same component
    instance is used for both the ShieldPipeline slot and the hooks list.
    Protocol membership is detected via isinstance for all five hook types
    (fix #2 extended: previously only PreDispatchHook was wired).
    Last matching rule per slot wins (consistent with append-order iteration).
    """
    pre_dispatch: PreDispatchHook | None = None
    egress: EgressBoundaryHook | None = None
    retry: RetryBoundaryHook | None = None
    budget: BudgetBoundaryHook | None = None
    tool_dispatch: ToolDispatchHook | None = None
    hooks: list[tuple[RuleSchema, Any]] = []

    for rule in schema.rules:
        factory = registry.get_rule_type(rule.type)
        component = factory(rule.params)
        hooks.append((rule, component))

        if isinstance(component, PreDispatchHook):
            pre_dispatch = component
        if isinstance(component, EgressBoundaryHook):
            egress = component
        if isinstance(component, RetryBoundaryHook):
            retry = component
        if isinstance(component, BudgetBoundaryHook):
            budget = component
        if isinstance(component, ToolDispatchHook):
            tool_dispatch = component

    pipeline = ShieldPipeline(
        pre_dispatch=pre_dispatch,
        egress=egress,
        retry=retry,
        budget=budget,
        tool_dispatch=tool_dispatch,
    )
    return LoadedPolicy(pipeline=pipeline, schema=schema, hooks=hooks)


# ---------------------------------------------------------------------------
# WatchHandle -- cancellable hot-reload handle (fix #5)
# ---------------------------------------------------------------------------


class WatchHandle:
    """Cancellable handle returned by PolicyLoader.watch().

    Tracks the currently-armed threading.Timer and cancels it on request.
    Calling cancel() is idempotent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: threading.Timer | None = None
        self._cancelled = False

    def _arm(self, timer: threading.Timer) -> None:
        """Replace the tracked timer with a newly-armed one."""
        with self._lock:
            self._current = timer

    def cancel(self) -> None:
        """Stop polling. Safe to call multiple times."""
        with self._lock:
            self._cancelled = True
            if self._current is not None:
                self._current.cancel()
                self._current = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


# ---------------------------------------------------------------------------
# PolicyLoader
# ---------------------------------------------------------------------------


class PolicyLoader:
    """Loads policy files and builds LoadedPolicy (ShieldPipeline) instances.

    Supports JSON (stdlib) and YAML (pyyaml optional).

    Parameters
    ----------
    registry:
        Custom rule registry. Defaults to PolicyRegistry.default().
    policy_root:
        If set, all file paths passed to load() / validate() / watch()
        are resolved and checked to be within this directory. Prevents
        path traversal when file paths originate from untrusted input.
    """

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        policy_root: Path | None = None,
    ) -> None:
        self._registry = registry or PolicyRegistry.default()
        self._policy_root = policy_root.resolve() if policy_root is not None else None

    def _check_path(self, path: str | Path) -> Path:
        """Resolve *path* and verify it stays within policy_root (if set)."""
        resolved = Path(path).resolve()
        if self._policy_root is not None:
            try:
                resolved.relative_to(self._policy_root)
            except ValueError:
                raise PolicyValidationError(
                    ["Path traversal denied: path resolves outside policy_root"],
                    field_name="path",
                ) from None
        return resolved

    def load(self, path: str | Path) -> LoadedPolicy:
        """Load a policy from *path* and return a LoadedPolicy.

        The file format is determined by the file extension:
          .json        -> JSON (stdlib)
          .yaml / .yml -> YAML (requires pyyaml)
        """
        checked = self._check_path(path)
        schema = _parse_to_schema(checked)
        return _build_pipeline(schema, self._registry)

    def load_from_string(
        self,
        content: str,
        format: Literal["json", "yaml", "yml"] = "yaml",  # noqa: A002
    ) -> LoadedPolicy:
        """Parse *content* as the given *format* and return a LoadedPolicy.

        Args:
            content: Policy document text.
            format:  "json", "yaml", or "yml".

        Raises:
            PolicyValidationError: if the content is invalid.
            RuntimeError: if format is "yaml"/"yml" and pyyaml is not installed.
        """
        fmt_lower = format.lower()
        if fmt_lower in ("yaml", "yml"):
            fmt_lower = "yaml"
        elif fmt_lower != "json":
            raise PolicyValidationError(
                [f"Unsupported format {format!r}; use 'json' or 'yaml'"],
                field_name="format",
            )
        data = _parse_content(content, fmt_lower)
        schema = PolicySchema.from_dict(data)
        return _build_pipeline(schema, self._registry)

    def validate(self, path: str | Path) -> list[PolicyValidationError]:
        """Validate *path* without building a pipeline.

        Returns a list of validation errors (empty list = valid).
        Does not raise; all errors are collected and returned.
        Uses the shared _parse_to_schema() helper (fix #4).
        """
        errors: list[PolicyValidationError] = []
        try:
            checked = self._check_path(path)
            schema = _parse_to_schema(checked)
            # Also validate that all rule types are known.
            for rule in schema.rules:
                try:
                    self._registry.get_rule_type(rule.type)
                except PolicyValidationError as exc:
                    errors.append(exc)
        except PolicyValidationError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(
                PolicyValidationError([f"Unexpected error during validation: {exc}"])
            )
        return errors

    def watch(
        self,
        path: str | Path,
        callback: Callable[[LoadedPolicy], None],
        poll_interval: float = 5.0,
    ) -> WatchHandle:
        """Poll *path* every *poll_interval* seconds; call *callback* on change.

        Uses threading.Timer for polling (no watchdog dependency).
        Returns a WatchHandle whose cancel() method stops polling (fix #5).

        Args:
            path:          File to watch.
            callback:      Called with the new LoadedPolicy on file change.
            poll_interval: Polling interval in seconds (default 5.0).

        Returns:
            WatchHandle -- call handle.cancel() to stop watching.
        """
        file_path = self._check_path(path)
        handle = WatchHandle()

        try:
            last_mtime: float | None = file_path.stat().st_mtime
        except OSError:
            last_mtime = None

        def _poll() -> None:
            nonlocal last_mtime
            if handle.cancelled:
                return
            try:
                mtime = file_path.stat().st_mtime
                if mtime != last_mtime:
                    try:
                        loaded = self.load(file_path)
                        callback(loaded)
                        # Update mtime only on success so that a failed callback
                        # is retried on the next poll cycle rather than silently
                        # skipped forever.
                        last_mtime = mtime
                    except Exception as exc:
                        logger.warning(
                            "PolicyLoader.watch: reload failed for %s: %s",
                            file_path,
                            exc,
                        )
            except OSError:
                pass  # File temporarily unavailable; retry next poll.

            if not handle.cancelled:
                next_timer = threading.Timer(poll_interval, _poll)
                next_timer.daemon = True
                handle._arm(next_timer)
                next_timer.start()

        first_timer = threading.Timer(poll_interval, _poll)
        first_timer.daemon = True
        handle._arm(first_timer)
        first_timer.start()
        return handle
