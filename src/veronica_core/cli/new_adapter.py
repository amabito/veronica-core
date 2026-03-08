"""veronica_core.cli.new_adapter -- Adapter scaffold generator.

Generates boilerplate adapter code for a new framework integration.

Usage::

    from pathlib import Path
    from veronica_core.cli.new_adapter import generate_adapter

    paths = generate_adapter("myframework", Path("./output"))
    # Creates:
    #   output/adapters/myframework.py
    #   output/tests/test_myframework_adapter.py
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["generate_adapter"]

# Valid identifier: lowercase letters, digits, underscores/hyphens, starting with
# a lowercase letter.  Uppercase is rejected to avoid silent PascalCase mismatches
# (e.g. "MyFramework" -> "Myframework" after lowercasing).
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_ADAPTER_TEMPLATE = '''\
"""veronica_core.adapters.{name} -- {pascal_name} adapter.

Integrates VERONICA policy enforcement into {pascal_name} pipelines.

Replace the stubs below with {pascal_name}-specific logic.

Usage::

    from veronica_core.adapters.{name} import {pascal_name}Adapter
    from veronica_core import ExecutionConfig

    adapter = {pascal_name}Adapter(config=ExecutionConfig(max_cost_usd=1.0, max_steps=50))
    adapter.check_and_halt()  # raises VeronicaHalt if a policy denies
"""

from __future__ import annotations

from typing import Any

from veronica_core.adapter_capabilities import AdapterCapabilities
from veronica_core.adapters._shared import build_adapter_container, safe_emit
from veronica_core.adapters._shared import check_and_halt as _shared_check_and_halt
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig

__all__ = ["{pascal_name}Adapter"]

# Replace with the actual minimum supported version of {pascal_name}.
_SUPPORTED_VERSIONS = ">=0.1"


class {pascal_name}Adapter:
    """{pascal_name} adapter for VERONICA containment.

    Args:
        config: GuardConfig or ExecutionConfig specifying budget/step limits.
        execution_context: Optional chain-level ExecutionContext.
        metrics: Optional ContainmentMetricsProtocol for observability.
        agent_id: Identifier forwarded to metrics calls.
    """

    _TAG = "[VERONICA_{upper_name}]"

    def __init__(
        self,
        config: GuardConfig | ExecutionConfig | None = None,
        execution_context: Any | None = None,
        metrics: Any | None = None,
        agent_id: str = "{name}",
    ) -> None:
        self._config = config or ExecutionConfig()
        self._container = build_adapter_container(self._config, execution_context)
        self._metrics = metrics
        self._agent_id = agent_id

    # ------------------------------------------------------------------
    # FrameworkAdapterProtocol
    # ------------------------------------------------------------------

    def capabilities(self) -> AdapterCapabilities:
        """Return static capability descriptor for this adapter."""
        return AdapterCapabilities(
            framework_name="{pascal_name}",
            framework_version_constraint=_SUPPORTED_VERSIONS,
            supports_streaming=False,
            supports_cost_extraction=False,
            supports_token_extraction=False,
            supports_async=False,
        )

    # ------------------------------------------------------------------
    # Containment hooks -- call these from your framework integration
    # ------------------------------------------------------------------

    def check_and_halt(self) -> None:
        """Check active policies and raise VeronicaHalt if any deny.

        Call this before each LLM invocation in your framework hook.

        Raises:
            VeronicaHalt: If budget, step, or retry limits are exceeded.
        """
        _shared_check_and_halt(
            self._container,
            tag=self._TAG,
            metrics=self._metrics,
            agent_id=self._agent_id,
        )

    def record_decision(self, decision: str) -> None:
        """Emit a containment decision event to metrics.

        Args:
            decision: Decision label, e.g. ``"ALLOW"`` or ``"HALT"``.
        """
        safe_emit(self._metrics, "record_decision", self._agent_id, decision)

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Emit token usage to metrics.

        Args:
            input_tokens: Number of input/prompt tokens consumed.
            output_tokens: Number of output/completion tokens produced.
        """
        safe_emit(
            self._metrics,
            "record_tokens",
            self._agent_id,
            input_tokens,
            output_tokens,
        )

    # ------------------------------------------------------------------
    # Optional: framework-specific cost/token extraction stubs
    # ------------------------------------------------------------------

    def extract_cost(self, result: Any) -> float:
        """Return USD cost from a framework response object.

        Replace with {pascal_name}-specific extraction logic.
        Return 0.0 if cost cannot be determined.
        """
        return 0.0

    def extract_tokens(self, result: Any) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) from a framework response.

        Replace with {pascal_name}-specific extraction logic.
        Return (0, 0) if token counts cannot be determined.
        """
        return (0, 0)
'''

_TEST_TEMPLATE = '''\
"""Tests for {pascal_name}Adapter -- generated by veronica new-adapter.

Replace/extend these stubs with {pascal_name}-specific test logic.
"""

from __future__ import annotations

from typing import Any

import pytest
from veronica_core.adapter_capabilities import AdapterCapabilities
from veronica_core.adapters.{name} import {pascal_name}Adapter
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import VeronicaHalt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**kwargs: Any) -> {pascal_name}Adapter:
    """Return a {pascal_name}Adapter with default or custom config."""
    return {pascal_name}Adapter(**kwargs)


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_returns_adapter_capabilities_instance(self) -> None:
        caps = _make_adapter().capabilities()
        assert isinstance(caps, AdapterCapabilities)

    def test_framework_name(self) -> None:
        caps = _make_adapter().capabilities()
        assert caps.framework_name == "{pascal_name}"

    def test_version_constraint_is_string(self) -> None:
        caps = _make_adapter().capabilities()
        assert isinstance(caps.framework_version_constraint, str)


# ---------------------------------------------------------------------------
# check_and_halt()
# ---------------------------------------------------------------------------


class TestCheckAndHalt:
    def test_allows_within_budget(self) -> None:
        adapter = _make_adapter(config=ExecutionConfig(max_cost_usd=10.0, max_steps=100))
        # Should not raise
        adapter.check_and_halt()

    def test_halts_when_budget_exceeded(self) -> None:
        adapter = _make_adapter(config=ExecutionConfig(max_cost_usd=0.0, max_steps=100))
        # max_cost_usd=0.0 means any spend triggers a halt; pre-check should deny
        # because spent (0) >= limit (0) -- budget exhausted immediately.
        with pytest.raises(VeronicaHalt):
            adapter.check_and_halt()

    def test_halts_when_step_limit_exceeded(self) -> None:
        adapter = _make_adapter(config=ExecutionConfig(max_cost_usd=100.0, max_steps=0))
        with pytest.raises(VeronicaHalt):
            adapter.check_and_halt()


# ---------------------------------------------------------------------------
# record_decision() / record_tokens()
# ---------------------------------------------------------------------------


class TestMetricsStubs:
    def test_record_decision_no_metrics(self) -> None:
        adapter = _make_adapter()
        # Must not raise when no metrics attached
        adapter.record_decision("ALLOW")
        adapter.record_decision("HALT")

    def test_record_tokens_no_metrics(self) -> None:
        adapter = _make_adapter()
        # Must not raise when no metrics attached
        adapter.record_tokens(100, 50)

    def test_record_decision_with_metrics(self) -> None:
        calls: list[tuple[str, ...]] = []

        class FakeMetrics:
            def record_decision(self, agent_id: str, decision: str) -> None:
                calls.append((agent_id, decision))

        adapter = _make_adapter(metrics=FakeMetrics(), agent_id="test-agent")
        adapter.record_decision("ALLOW")
        assert ("test-agent", "ALLOW") in calls

    def test_record_tokens_with_metrics(self) -> None:
        calls: list[tuple[str, ...]] = []

        class FakeMetrics:
            def record_tokens(
                self, agent_id: str, inp: int, out: int
            ) -> None:
                calls.append((agent_id, str(inp), str(out)))

        adapter = _make_adapter(metrics=FakeMetrics(), agent_id="test-agent")
        adapter.record_tokens(42, 13)
        assert ("test-agent", "42", "13") in calls


# ---------------------------------------------------------------------------
# extract_cost() / extract_tokens() stubs
# ---------------------------------------------------------------------------


class TestExtractionStubs:
    def test_extract_cost_returns_zero(self) -> None:
        adapter = _make_adapter()
        assert adapter.extract_cost(object()) == 0.0

    def test_extract_tokens_returns_zero_pair(self) -> None:
        adapter = _make_adapter()
        assert adapter.extract_tokens(object()) == (0, 0)
'''


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _to_pascal_case(name: str) -> str:
    """Convert a snake_case or kebab-case name to PascalCase.

    Examples:
        "myframework"  -> "Myframework"
        "my_framework" -> "MyFramework"
        "my-framework" -> "MyFramework"
    """
    return "".join(part.capitalize() for part in re.split(r"[_-]", name))


def generate_adapter(framework_name: str, output_dir: Path) -> list[Path]:
    """Generate adapter and test boilerplate for a new framework integration.

    Creates two files under *output_dir*:
    - ``adapters/{framework_name}.py`` -- adapter class skeleton
    - ``tests/test_{framework_name}_adapter.py`` -- test skeleton

    Args:
        framework_name: Short lowercase identifier for the framework,
            e.g. ``"myframework"`` or ``"my-framework"``. Must match
            ``^[a-z][a-z0-9_-]*$``.
        output_dir: Directory under which the files are written.
            Created automatically if it does not exist.

    Returns:
        List of Path objects for the generated files (adapter, test).

    Raises:
        ValueError: If *framework_name* is empty or contains invalid characters.
        FileExistsError: If either output file already exists.
    """
    if not framework_name:
        raise ValueError("framework_name must not be empty")
    if not _VALID_NAME_RE.match(framework_name):
        raise ValueError(
            f"Invalid framework_name {framework_name!r}. "
            "Must start with a lowercase letter and contain only lowercase "
            "letters, digits, underscores, or hyphens."
        )

    name = framework_name.lower().replace("-", "_")
    pascal_name = _to_pascal_case(name)
    upper_name = name.upper()

    output_dir = Path(output_dir)
    adapters_dir = output_dir / "adapters"
    tests_dir = output_dir / "tests"
    adapters_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)

    adapter_path = adapters_dir / f"{name}.py"
    test_path = tests_dir / f"test_{name}_adapter.py"

    for path in (adapter_path, test_path):
        if path.exists():
            raise FileExistsError(
                f"Output file already exists and will not be overwritten: {path}"
            )

    adapter_source = _ADAPTER_TEMPLATE.format(
        name=name,
        pascal_name=pascal_name,
        upper_name=upper_name,
    )
    test_source = _TEST_TEMPLATE.format(
        name=name,
        pascal_name=pascal_name,
    )

    adapter_path.write_text(adapter_source, encoding="utf-8")
    test_path.write_text(test_source, encoding="utf-8")

    return [adapter_path, test_path]
