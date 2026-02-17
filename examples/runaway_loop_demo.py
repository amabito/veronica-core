"""VERONICA runaway loop demo -- budget enforcement stops infinite retries.

Run:
    pip install -e .
    PYTHONPATH=src python examples/runaway_loop_demo.py
"""
from veronica.runtime.hooks import RuntimeContext
from veronica.runtime.events import EventBus
from veronica.budget.enforcer import BudgetEnforcer, BudgetExceeded
from veronica.budget.policy import BudgetPolicy, WindowLimit
from veronica.budget.ledger import BudgetLedger
from veronica.runtime.models import Labels, Budget, RunStatus


def main() -> None:
    # Hard limit: $0.05 per minute at org level
    policy = BudgetPolicy(org_limits=WindowLimit(minute_usd=0.05))
    bus = EventBus()  # No sinks -- quiet output
    enforcer = BudgetEnforcer(policy=policy, ledger=BudgetLedger(), bus=bus)
    ctx = RuntimeContext(sinks=[], enforcer=enforcer)

    labels = Labels(org="demo-org", team="demo-team")
    run = ctx.create_run(labels=labels, budget=Budget(limit_usd=0.10))
    session = ctx.create_session(run)

    print("Starting runaway loop...")
    print("Each call costs $0.01. Budget limit: $0.05/minute.")
    print()

    call_count = 0
    try:
        while True:
            with ctx.llm_call(
                session, model="gpt-4", labels=labels, run=run
            ) as step:
                call_count += 1
                step.cost_usd = 0.01
                print(f"  Call {call_count}: ${step.cost_usd:.2f}")
    except BudgetExceeded as exc:
        print()
        print(f"HALTED after {call_count} calls: {exc}")
        print()
        print("Without VERONICA: infinite retries, $12,000 bill.")
        print("With VERONICA: hard stop, zero damage.")

    ctx.finish_session(session, labels=labels)
    if run.status in (RunStatus.HALTED, RunStatus.DEGRADED):
        ctx.finish_run(run, status=RunStatus.FAILED, error_summary="budget_exceeded")
    else:
        ctx.finish_run(run)


if __name__ == "__main__":
    main()
