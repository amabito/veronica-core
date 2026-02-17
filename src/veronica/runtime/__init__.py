"""VERONICA Runtime — Run/Session/Step execution model with state machine and event log."""
from __future__ import annotations

from typing import TYPE_CHECKING

from veronica.runtime.events import Event, EventBus, EventTypes
from veronica.runtime.models import (
    Budget,
    Labels,
    Run,
    RunStatus,
    Session,
    SessionCounters,
    SessionStatus,
    Step,
    StepError,
    StepKind,
    StepStatus,
)
from veronica.runtime.sinks import (
    CompositeSink,
    EventSink,
    JsonlFileSink,
    NullSink,
    ReporterBridgeSink,
    StdoutSink,
)
from veronica.runtime.state_machine import (
    InvalidTransitionError,
    transition_run,
    transition_session,
)

if TYPE_CHECKING:
    from veronica.runtime.hooks import MaxStepsExceeded, RuntimeContext

__all__ = [
    "Budget",
    "CompositeSink",
    "Event",
    "EventBus",
    "EventSink",
    "EventTypes",
    "InvalidTransitionError",
    "JsonlFileSink",
    "Labels",
    "MaxStepsExceeded",
    "NullSink",
    "ReporterBridgeSink",
    "Run",
    "RunStatus",
    "RuntimeContext",
    "Session",
    "SessionCounters",
    "SessionStatus",
    "Step",
    "StepError",
    "StepKind",
    "StepStatus",
    "StdoutSink",
    "transition_run",
    "transition_session",
]
