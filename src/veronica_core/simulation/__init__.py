"""veronica_core.simulation — Policy simulation and what-if analysis.

Replay historical execution logs against policy configurations to answer
"what would have happened if this policy had been active?" questions.

Public API:
    ExecutionLogEntry — single recorded action (LLM call, tool call, etc.)
    ExecutionLog      — collection of log entries with OTel import support
    PolicySimulator   — replays a log against a ShieldPipeline
    SimulationReport  — summary of simulation results
    SimulationEvent   — individual policy decision during simulation

Usage::

    from veronica_core.simulation import PolicySimulator, ExecutionLog
    from veronica_core.policy.loader import PolicyLoader

    loader = PolicyLoader()
    policy = loader.load("new_policy.yaml")
    log = ExecutionLog.from_file("last_month.json")
    report = PolicySimulator(policy.pipeline).simulate(log.entries)
    print(f"Would have saved ${report.cost_saved_estimate:.2f}")
"""

from veronica_core.simulation.log import ExecutionLog, ExecutionLogEntry
from veronica_core.simulation.report import SimulationEvent, SimulationReport
from veronica_core.simulation.simulator import PolicySimulator

__all__ = [
    "ExecutionLog",
    "ExecutionLogEntry",
    "PolicySimulator",
    "SimulationEvent",
    "SimulationReport",
]
