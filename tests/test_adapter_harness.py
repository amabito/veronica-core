"""Generic adapter test harness -- parameterized across all framework adapters.

Issue #68: Every adapter in src/veronica_core/adapters/ must pass this suite.
Issue #69: Every adapter must declare a non-default supported_versions range.

Each adapter is tested via a thin factory wrapper so that optional framework
dependencies are skipped gracefully with pytest.importorskip.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import dataclass
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest

from veronica_core.adapter_capabilities import AdapterCapabilities, UNCONSTRAINED_VERSIONS
from veronica_core.inject import GuardConfig, VeronicaHalt


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_metrics() -> MagicMock:
    """Return a fresh mock ContainmentMetricsProtocol."""
    m = MagicMock()
    m.record_decision = MagicMock()
    m.record_tokens = MagicMock()
    m.record_cost = MagicMock()
    return m


def _unlimited_config() -> GuardConfig:
    return GuardConfig(max_cost_usd=100.0, max_steps=100, max_retries_total=5)


def _exhausted_config() -> GuardConfig:
    return GuardConfig(max_cost_usd=0.0, max_steps=0, max_retries_total=0)


# ---------------------------------------------------------------------------
# AdapterFixture: thin wrapper around each adapter instance
# ---------------------------------------------------------------------------


@dataclass
class AdapterFixture:
    """Thin wrapper that normalises the call interface across adapter types.

    Attributes:
        name: Human-readable adapter name (matches capabilities().framework_name).
        agent_id: The agent_id string passed when constructing the adapter.
        make: Factory that accepts a GuardConfig and optional metrics kwarg,
              and returns an adapter instance.
        invoke_allow: Call the adapter once with an ALLOW policy (no exception).
        invoke_halt: Call the adapter so a HALT policy is triggered.
        has_container: Whether the adapter exposes a .container property.
    """

    name: str
    agent_id: str
    make: Callable[..., Any]
    invoke_allow: Callable[[Any], None]
    invoke_halt: Callable[[Any], None]
    has_container: bool = True


# ---------------------------------------------------------------------------
# Stub injection helpers
# ---------------------------------------------------------------------------


def _ensure_langchain_stubs() -> None:
    """Inject minimal langchain_core stubs if the real package is absent."""
    if "langchain_core" in sys.modules:
        return

    lc_core = types.ModuleType("langchain_core")
    lc_callbacks = types.ModuleType("langchain_core.callbacks")
    lc_outputs = types.ModuleType("langchain_core.outputs")

    class FakeBaseCallbackHandler:
        def __init__(self) -> None:
            pass

    class FakeLLMResult:
        def __init__(self, llm_output: Optional[dict] = None) -> None:
            self.llm_output = llm_output

    lc_callbacks.BaseCallbackHandler = FakeBaseCallbackHandler
    lc_outputs.LLMResult = FakeLLMResult
    lc_core.callbacks = lc_callbacks
    lc_core.outputs = lc_outputs
    sys.modules.update(
        {
            "langchain_core": lc_core,
            "langchain_core.callbacks": lc_callbacks,
            "langchain_core.outputs": lc_outputs,
        }
    )


def _ensure_langgraph_stubs() -> None:
    """Inject minimal langgraph stub if the real package is absent."""
    if "langgraph" not in sys.modules:
        sys.modules["langgraph"] = types.ModuleType("langgraph")


def _ensure_ag2_stubs() -> None:
    """Inject minimal autogen/ag2 stubs if the real packages are absent."""
    if "autogen" in sys.modules:
        return

    autogen = types.ModuleType("autogen")

    class FakeConversableAgent:
        def __init__(self, name: str, **kwargs: Any) -> None:
            self.name = name

        def generate_reply(
            self, messages: Any = None, sender: Any = None, **kwargs: Any
        ) -> str:
            return "reply"

        def register_reply(
            self, trigger: Any, reply_func: Any, position: int = 0
        ) -> None:
            pass

    autogen.ConversableAgent = FakeConversableAgent
    sys.modules["autogen"] = autogen


def _ensure_crewai_stubs() -> None:
    """Inject minimal crewai stubs if the real package is absent."""
    if "crewai" in sys.modules and "crewai.events" in sys.modules:
        return

    crewai_pkg = types.ModuleType("crewai")
    crewai_events = types.ModuleType("crewai.events")
    crewai_types = types.ModuleType("crewai.events.types")
    crewai_llm_events = types.ModuleType("crewai.events.types.llm_events")

    # Minimal event stubs
    class _FakeEvent:
        def __init__(self) -> None:
            self.response = None

    LLMCallStartedEvent = type("LLMCallStartedEvent", (_FakeEvent,), {})
    LLMCallCompletedEvent = type("LLMCallCompletedEvent", (_FakeEvent,), {})
    LLMCallFailedEvent = type(
        "LLMCallFailedEvent", (_FakeEvent,), {"error": "fake-error"}
    )

    class FakeBaseEventListener:
        def __init__(self) -> None:
            self._fake_bus = _FakeBus()
            self.setup_listeners(self._fake_bus)

        def setup_listeners(self, bus: Any) -> None:  # pragma: no cover
            pass

    class _FakeBus:
        def on(self, event_type: Any) -> Callable:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

    crewai_llm_events.LLMCallStartedEvent = LLMCallStartedEvent
    crewai_llm_events.LLMCallCompletedEvent = LLMCallCompletedEvent
    crewai_llm_events.LLMCallFailedEvent = LLMCallFailedEvent
    crewai_events.BaseEventListener = FakeBaseEventListener
    crewai_pkg.events = crewai_events

    sys.modules.update(
        {
            "crewai": crewai_pkg,
            "crewai.events": crewai_events,
            "crewai.events.types": crewai_types,
            "crewai.events.types.llm_events": crewai_llm_events,
        }
    )


def _ensure_llamaindex_stubs() -> None:
    """Inject minimal llama_index stubs if the real package is absent."""
    if "llama_index" in sys.modules or "llama_index.core" in sys.modules:
        return

    li_core = types.ModuleType("llama_index.core")
    li_callbacks = types.ModuleType("llama_index.core.callbacks")
    li_schema = types.ModuleType("llama_index.core.callbacks.schema")

    class FakeCBH:
        event_starts_to_ignore: list = []
        event_ends_to_ignore: list = []

        def __init__(
            self,
            event_starts_to_ignore: list,
            event_ends_to_ignore: list,
        ) -> None:
            self.event_starts_to_ignore = event_starts_to_ignore
            self.event_ends_to_ignore = event_ends_to_ignore

    class FakeCBEventType:
        LLM = "llm"

    li_callbacks.BaseCallbackHandler = FakeCBH
    li_schema.CBEventType = FakeCBEventType
    li_core.callbacks = li_callbacks

    sys.modules.update(
        {
            "llama_index": types.ModuleType("llama_index"),
            "llama_index.core": li_core,
            "llama_index.core.callbacks": li_callbacks,
            "llama_index.core.callbacks.schema": li_schema,
        }
    )


# ---------------------------------------------------------------------------
# Adapter fixture factories
# ---------------------------------------------------------------------------


def _make_langchain_fixture() -> AdapterFixture:
    _ensure_langchain_stubs()
    # Force fresh import so stubs are picked up
    if "veronica_core.adapters.langchain" in sys.modules:
        del sys.modules["veronica_core.adapters.langchain"]
    from veronica_core.adapters.langchain import VeronicaCallbackHandler

    def make(config: GuardConfig, metrics: Any = None) -> Any:
        return VeronicaCallbackHandler(config, metrics=metrics, agent_id="harness-lc")

    def invoke_allow(adapter: Any) -> None:
        adapter.on_llm_start({}, ["hello"])

    def invoke_halt(adapter: Any) -> None:
        adapter.on_llm_start({}, ["hello"])

    return AdapterFixture(
        name="LangChain",
        agent_id="harness-lc",
        make=make,
        invoke_allow=invoke_allow,
        invoke_halt=invoke_halt,
    )


def _make_langgraph_fixture() -> AdapterFixture:
    _ensure_langchain_stubs()
    _ensure_langgraph_stubs()
    if "veronica_core.adapters.langgraph" in sys.modules:
        del sys.modules["veronica_core.adapters.langgraph"]
    from veronica_core.adapters.langgraph import VeronicaLangGraphCallback

    def make(config: GuardConfig, metrics: Any = None) -> Any:
        return VeronicaLangGraphCallback(config, metrics=metrics, agent_id="harness-lg")

    def invoke_allow(adapter: Any) -> None:
        adapter.on_llm_start({}, ["hello"])

    def invoke_halt(adapter: Any) -> None:
        adapter.on_llm_start({}, ["hello"])

    return AdapterFixture(
        name="LangGraph",
        agent_id="harness-lg",
        make=make,
        invoke_allow=invoke_allow,
        invoke_halt=invoke_halt,
    )


def _make_ag2_fixture() -> AdapterFixture:
    _ensure_ag2_stubs()
    if "veronica_core.adapters.ag2" in sys.modules:
        del sys.modules["veronica_core.adapters.ag2"]
    from veronica_core.adapters.ag2 import VeronicaConversableAgent

    def make(config: GuardConfig, metrics: Any = None) -> Any:
        return VeronicaConversableAgent(
            "harness-bot", config, metrics=metrics, agent_id="harness-ag2"
        )

    def invoke_allow(adapter: Any) -> None:
        adapter.generate_reply()

    def invoke_halt(adapter: Any) -> None:
        adapter.generate_reply()

    return AdapterFixture(
        name="AG2",
        agent_id="harness-ag2",
        make=make,
        invoke_allow=invoke_allow,
        invoke_halt=invoke_halt,
    )


def _make_crewai_fixture() -> AdapterFixture:
    _ensure_crewai_stubs()
    if "veronica_core.adapters.crewai" in sys.modules:
        del sys.modules["veronica_core.adapters.crewai"]
    from veronica_core.adapters.crewai import VeronicaCrewAIListener

    def make(config: GuardConfig, metrics: Any = None) -> Any:
        return VeronicaCrewAIListener(config, metrics=metrics, agent_id="harness-crew")

    def invoke_allow(adapter: Any) -> None:
        adapter.check_or_raise()

    def invoke_halt(adapter: Any) -> None:
        adapter.check_or_raise()

    return AdapterFixture(
        name="CrewAI",
        agent_id="harness-crew",
        make=make,
        invoke_allow=invoke_allow,
        invoke_halt=invoke_halt,
    )


def _make_llamaindex_fixture() -> AdapterFixture:
    _ensure_llamaindex_stubs()
    if "veronica_core.adapters.llamaindex" in sys.modules:
        del sys.modules["veronica_core.adapters.llamaindex"]
    from veronica_core.adapters.llamaindex import VeronicaLlamaIndexHandler

    # After stubs are injected, LLM CBEventType is available as a module-global.
    # Import via sys.modules to avoid a direct llama_index import here.
    CBEventType = sys.modules["llama_index.core.callbacks.schema"].CBEventType

    def make(config: GuardConfig, metrics: Any = None) -> Any:
        return VeronicaLlamaIndexHandler(
            config, metrics=metrics, agent_id="harness-li"
        )

    def invoke_allow(adapter: Any) -> None:
        adapter.on_event_start(CBEventType.LLM, {})

    def invoke_halt(adapter: Any) -> None:
        adapter.on_event_start(CBEventType.LLM, {})

    return AdapterFixture(
        name="LlamaIndex",
        agent_id="harness-li",
        make=make,
        invoke_allow=invoke_allow,
        invoke_halt=invoke_halt,
    )


# ---------------------------------------------------------------------------
# Build adapter fixture list
# ---------------------------------------------------------------------------


def _build_fixtures() -> list[AdapterFixture]:
    """Build all adapter fixtures, skipping any that fail to import."""
    fixtures: list[AdapterFixture] = []
    builders = [
        _make_langchain_fixture,
        _make_langgraph_fixture,
        _make_ag2_fixture,
        _make_crewai_fixture,
        _make_llamaindex_fixture,
    ]
    for builder in builders:
        try:
            fixtures.append(builder())
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Skipping fixture {builder.__name__}: {exc}")
    return fixtures


_FIXTURES = _build_fixtures()
_FIXTURE_IDS = [f.name for f in _FIXTURES]


# ---------------------------------------------------------------------------
# Parameterized harness
# ---------------------------------------------------------------------------


class TestAdapterHarness:
    """Generic harness: every registered adapter must pass all test cases."""

    # ------------------------------------------------------------------
    # TC-01: Instantiation
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_instantiation_with_unlimited_config(self, fixture: AdapterFixture) -> None:
        """Adapter must instantiate without error given an unlimited config."""
        adapter = fixture.make(_unlimited_config())
        assert adapter is not None

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_instantiation_with_exhausted_config(self, fixture: AdapterFixture) -> None:
        """Adapter must instantiate without error given an exhausted config."""
        adapter = fixture.make(_exhausted_config())
        assert adapter is not None

    # ------------------------------------------------------------------
    # TC-02: ALLOW path -- no exception raised
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_allow_does_not_raise(self, fixture: AdapterFixture) -> None:
        """Adapter with budget remaining must not raise VeronicaHalt."""
        adapter = fixture.make(_unlimited_config())
        fixture.invoke_allow(adapter)  # must not raise

    # ------------------------------------------------------------------
    # TC-03: HALT path -- VeronicaHalt raised
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_halt_raises_veronica_halt(self, fixture: AdapterFixture) -> None:
        """Adapter with exhausted budget/steps must raise VeronicaHalt."""
        adapter = fixture.make(_exhausted_config())
        with pytest.raises(VeronicaHalt):
            fixture.invoke_halt(adapter)

    # ------------------------------------------------------------------
    # TC-04: record_decision metrics -- ALLOW path
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_allow_emits_record_decision(self, fixture: AdapterFixture) -> None:
        """record_decision('ALLOW') must be emitted when policy passes."""
        m = _make_metrics()
        adapter = fixture.make(_unlimited_config(), metrics=m)
        fixture.invoke_allow(adapter)
        m.record_decision.assert_called_once_with(fixture.agent_id, "ALLOW")

    # ------------------------------------------------------------------
    # TC-05: record_decision metrics -- HALT path
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_halt_emits_record_decision(self, fixture: AdapterFixture) -> None:
        """record_decision('HALT') must be emitted when policy blocks."""
        m = _make_metrics()
        adapter = fixture.make(_exhausted_config(), metrics=m)
        with pytest.raises(VeronicaHalt):
            fixture.invoke_halt(adapter)
        m.record_decision.assert_called_once_with(fixture.agent_id, "HALT")

    # ------------------------------------------------------------------
    # TC-06: record_tokens metrics emission
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_record_tokens_emission(self, fixture: AdapterFixture) -> None:
        """record_tokens must not crash when called on adapter internals."""
        m = _make_metrics()
        # Adapter is constructed to exercise the full make() path even though
        # record_tokens is verified via safe_emit directly below.
        fixture.make(_unlimited_config(), metrics=m)
        # Verify safe_emit works for record_tokens via the shared helper
        from veronica_core.adapters._shared import safe_emit

        safe_emit(m, "record_tokens", fixture.agent_id, 100, 50)
        m.record_tokens.assert_called_once_with(fixture.agent_id, 100, 50)

    # ------------------------------------------------------------------
    # TC-07: capabilities() returns AdapterCapabilities
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_capabilities_returns_adapter_capabilities(
        self, fixture: AdapterFixture
    ) -> None:
        """capabilities() must return an AdapterCapabilities instance."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        assert isinstance(caps, AdapterCapabilities)

    # ------------------------------------------------------------------
    # TC-08: capabilities().framework_name is non-empty
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_capabilities_framework_name_is_set(
        self, fixture: AdapterFixture
    ) -> None:
        """capabilities().framework_name must be a non-empty string."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        assert isinstance(caps.framework_name, str)
        assert caps.framework_name != ""

    # ------------------------------------------------------------------
    # TC-09: capabilities().framework_name matches fixture.name
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_capabilities_framework_name_matches_fixture(
        self, fixture: AdapterFixture
    ) -> None:
        """capabilities().framework_name must equal the expected adapter name."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        assert caps.framework_name == fixture.name

    # ------------------------------------------------------------------
    # TC-10: Error handling -- None metrics does not crash
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_none_metrics_does_not_crash(self, fixture: AdapterFixture) -> None:
        """Adapter must not crash when metrics=None (default path)."""
        adapter = fixture.make(_unlimited_config(), metrics=None)
        fixture.invoke_allow(adapter)  # must not raise

    # ------------------------------------------------------------------
    # TC-11: Error handling -- exception raised by metrics.record_decision
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_metrics_exception_swallowed(self, fixture: AdapterFixture) -> None:
        """Exceptions from metrics.record_decision must not propagate."""
        m = _make_metrics()
        m.record_decision.side_effect = RuntimeError("metrics-boom")
        adapter = fixture.make(_unlimited_config(), metrics=m)
        fixture.invoke_allow(adapter)  # must not raise

    # ------------------------------------------------------------------
    # TC-12: Concurrent calls -- 3 threads, ALLOW config
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_concurrent_allow_calls(self, fixture: AdapterFixture) -> None:
        """Adapter must survive 3 concurrent calls under ALLOW config."""
        adapter = fixture.make(_unlimited_config())
        errors: list[BaseException] = []

        def call() -> None:
            try:
                fixture.invoke_allow(adapter)
            except VeronicaHalt:
                # Step limit may trigger after several calls -- acceptable
                pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent calls raised: {errors}"

    # ------------------------------------------------------------------
    # TC-13: container property is accessible (where supported)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_container_property_accessible(self, fixture: AdapterFixture) -> None:
        """Adapter must expose .container for introspection."""
        adapter = fixture.make(_unlimited_config())
        assert hasattr(adapter, "container"), (
            f"{fixture.name} adapter is missing .container property"
        )
        assert adapter.container is not None

    # ------------------------------------------------------------------
    # TC-14 (Issue #69): supported_versions is non-default
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_supported_versions_is_non_default(self, fixture: AdapterFixture) -> None:
        """Every adapter must declare a specific supported_versions range.

        The default ("0.0.0", "99.99.99") means the adapter has not been
        updated for Issue #69 yet. All registered adapters must declare
        a real version range.
        """
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        assert caps.supported_versions != UNCONSTRAINED_VERSIONS, (
            f"{fixture.name} adapter still uses the default supported_versions tuple. "
            "Update capabilities() to declare a real version range."
        )

    # ------------------------------------------------------------------
    # TC-15 (Issue #69): is_version_compatible works for min boundary
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_is_version_compatible_min_boundary(
        self, fixture: AdapterFixture
    ) -> None:
        """is_version_compatible(min_version) must return True."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        min_ver, _ = caps.supported_versions
        assert caps.is_version_compatible(min_ver), (
            f"{fixture.name}: is_version_compatible({min_ver!r}) returned False -- "
            "min_version itself must be compatible."
        )

    # ------------------------------------------------------------------
    # TC-16 (Issue #69): is_version_compatible works for max boundary
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_is_version_compatible_max_boundary(
        self, fixture: AdapterFixture
    ) -> None:
        """is_version_compatible(max_version) must return True."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        _, max_ver = caps.supported_versions
        assert caps.is_version_compatible(max_ver), (
            f"{fixture.name}: is_version_compatible({max_ver!r}) returned False -- "
            "max_version itself must be compatible."
        )

    # ------------------------------------------------------------------
    # TC-17 (Issue #69): is_version_compatible returns False below min
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
    def test_is_version_compatible_below_min_returns_false(
        self, fixture: AdapterFixture
    ) -> None:
        """is_version_compatible('0.0.0') must return False for any adapter
        whose min_version > 0.0.0 (i.e. all adapters in this harness)."""
        adapter = fixture.make(_unlimited_config())
        caps = adapter.capabilities()
        min_ver, _ = caps.supported_versions
        # Only test this when min_ver is actually > 0.0.0
        if min_ver == "0.0.0":
            pytest.skip(f"{fixture.name} has min_version=0.0.0 -- cannot test below-min")
        assert not caps.is_version_compatible("0.0.0"), (
            f"{fixture.name}: is_version_compatible('0.0.0') should be False "
            f"because min_version is {min_ver!r}."
        )


# ---------------------------------------------------------------------------
# Standalone tests for AdapterCapabilities.is_version_compatible
# ---------------------------------------------------------------------------


class TestIsVersionCompatible:
    """Unit tests for AdapterCapabilities.is_version_compatible()."""

    def _caps(self, min_ver: str, max_ver: str) -> AdapterCapabilities:
        return AdapterCapabilities(
            framework_name="Test",
            supported_versions=(min_ver, max_ver),
        )

    def test_exact_min_is_compatible(self) -> None:
        assert self._caps("0.4.0", "0.6.99").is_version_compatible("0.4.0")

    def test_exact_max_is_compatible(self) -> None:
        assert self._caps("0.4.0", "0.6.99").is_version_compatible("0.6.99")

    def test_mid_range_is_compatible(self) -> None:
        assert self._caps("0.4.0", "0.6.99").is_version_compatible("0.5.3")

    def test_below_min_is_not_compatible(self) -> None:
        assert not self._caps("0.4.0", "0.6.99").is_version_compatible("0.3.99")

    def test_above_max_is_not_compatible(self) -> None:
        assert not self._caps("0.4.0", "0.6.99").is_version_compatible("0.7.0")

    def test_empty_version_is_not_compatible(self) -> None:
        """Empty string parses as (0,) -- below any real min."""
        assert not self._caps("0.4.0", "0.6.99").is_version_compatible("")

    def test_default_range_accepts_any_version(self) -> None:
        """Default ("0.0.0", "99.99.99") must accept arbitrary versions."""
        caps = AdapterCapabilities()
        assert caps.is_version_compatible("1.2.3")
        assert caps.is_version_compatible("50.0.0")
        assert caps.is_version_compatible("0.0.0")

    def test_non_numeric_segment_treated_as_zero(self) -> None:
        """Non-numeric version segments are normalised to 0."""
        caps = self._caps("0.0.0", "99.99.99")
        # "1.alpha.0" -> (1, 0, 0) which is within [0.0.0, 99.99.99]
        assert caps.is_version_compatible("1.alpha.0")
