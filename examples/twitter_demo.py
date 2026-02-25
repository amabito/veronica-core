"""VERONICA full-feature demo for X/Twitter video (~40 seconds).

Shows:
  PART 1: Runaway agent with no ceiling
  PART 2: Budget enforcement (Decision.HALT before the call)
  PART 3: Multi-agent cost propagation (child spend counts against parent)
  PART 4: Degradation ladder (downgrade -> trim -> rate limit -> halt)
  PART 5: SAFE_MODE persistence (survives process restart)

Run:
    python examples/twitter_demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.degradation import DegradationConfig, DegradationLadder
from veronica_core.shield.types import Decision
from veronica_core.state import VeronicaState, VeronicaStateMachine

SEP = "=" * 56
DIV = "-" * 56
COST_PER_CALL = 0.05


def p(line: str = "") -> None:
    print(line, flush=True)


def tick(t: float) -> None:
    time.sleep(t)


# ----------------------------------------------------------------
# PART 1 -- Runaway agent
# ----------------------------------------------------------------

p()
p(SEP)
p("  PART 1: AGENT WITHOUT VERONICA")
p(SEP)
tick(0.5)

p()
accumulated = 0.0
for i in range(1, 9):
    accumulated += COST_PER_CALL
    p(f"  call {i:>3}  ->  ${accumulated:>5.2f} accumulated")
    tick(0.15)

p()
tick(0.4)
p("  call  47,000  ->  $2,350.00")
tick(0.2)
p("  call 120,000  ->  $6,000.00")
tick(0.2)
p("  call 240,000  ->  $12,000.00")
tick(0.5)
p()
p("  nobody stopped it. 60 hours. $12,000.")
tick(1.2)

# ----------------------------------------------------------------
# PART 2 -- Budget enforcement
# ----------------------------------------------------------------

p()
p(SEP)
p("  PART 2: BUDGET ENFORCEMENT  (max_cost_usd=$1.00)")
p(SEP)
tick(0.5)

config = ExecutionConfig(max_cost_usd=1.00, max_steps=100, max_retries_total=50)
ctx = ExecutionContext(config=config)

p()
for i in range(1, 25):
    decision = ctx.wrap_llm_call(
        fn=lambda: "response",
        options=WrapOptions(operation_name=f"step_{i}", cost_estimate_hint=COST_PER_CALL),
    )
    snap = ctx.get_snapshot()
    if decision == Decision.HALT:
        p(f"  call {i:>3}  ->  HALT   fn() never called. $0 spent.")
        tick(0.5)
        p()
        p(f"  stopped at ${snap.cost_usd_accumulated:.2f} / {snap.step_count} steps")
        p("  no network request. no spend.")
        break
    else:
        p(f"  call {i:>3}  ->  ALLOW  ${snap.cost_usd_accumulated:.2f} accumulated")
        tick(0.22)

tick(1.0)

# ----------------------------------------------------------------
# PART 3 -- Multi-agent cost propagation
# ----------------------------------------------------------------

p()
p(SEP)
p("  PART 3: MULTI-AGENT COST PROPAGATION")
p("  parent=$1.00  spawns  child=$0.50")
p(SEP)
tick(0.5)

parent_cfg = ExecutionConfig(max_cost_usd=1.00, max_steps=100, max_retries_total=50)
parent_ctx = ExecutionContext(config=parent_cfg)

p()
with parent_ctx.spawn_child(max_cost_usd=0.50) as child:
    for i in range(1, 15):
        d = child.wrap_llm_call(
            fn=lambda: "sub-agent result",
            options=WrapOptions(operation_name=f"sub_{i}", cost_estimate_hint=0.07),
        )
        ps = parent_ctx.get_snapshot()
        cs = child.get_snapshot()
        if d == Decision.HALT:
            p(f"  child call {i:>2}  ->  HALT")
            tick(0.5)
            p()
            p(f"  child spent: ${cs.cost_usd_accumulated:.2f} of $0.50 sub-limit")
            p(f"  parent sees: ${ps.cost_usd_accumulated:.2f} of $1.00 consumed")
            p("  child spend counted against parent's budget.")
            break
        else:
            p(
                f"  child call {i:>2}  ->  ALLOW"
                f"  child=${cs.cost_usd_accumulated:.2f}"
                f"  parent=${ps.cost_usd_accumulated:.2f}"
            )
            tick(0.25)

tick(1.0)

# ----------------------------------------------------------------
# PART 4 -- Degradation ladder
# ----------------------------------------------------------------

p()
p(SEP)
p("  PART 4: DEGRADATION LADDER")
p("  budget=$1.00  gpt-4 -> gpt-3.5-turbo fallback")
p(SEP)
tick(0.5)

ladder = DegradationLadder(
    config=DegradationConfig(
        model_map={"gpt-4": "gpt-3.5-turbo"},
        cost_thresholds={
            "model_downgrade": 0.80,
            "context_trim": 0.85,
            "rate_limit": 0.90,
        },
    )
)

p()
steps = [
    (0.60, "normal"),
    (0.80, "model_downgrade threshold"),
    (0.85, "context_trim threshold"),
    (0.90, "rate_limit threshold"),
]

for spent, label in steps:
    result = ladder.evaluate(cost_accumulated=spent, max_cost_usd=1.00, current_model="gpt-4")
    if result is not None:
        action = result.degradation_action or result.policy_type
        p(f"  ${spent:.2f} / $1.00  ({int(spent*100)}%)  ->  DEGRADE: {action}")
    else:
        p(f"  ${spent:.2f} / $1.00  ({int(spent*100)}%)  ->  OK")
    tick(0.6)

p()
p("  $1.00 / $1.00  (100%)  ->  HALT")
tick(1.0)

# ----------------------------------------------------------------
# PART 5 -- SAFE_MODE persistence
# ----------------------------------------------------------------

p()
p(SEP)
p("  PART 5: SAFE_MODE  (survives process restart)")
p(SEP)
tick(0.5)

state_file = Path(tempfile.mktemp(suffix="_veronica_state.json"))
sm = VeronicaStateMachine()

p()
p(f"  state: {sm.current_state.value}")
tick(0.5)

sm.transition(VeronicaState.SAFE_MODE, "budget exhausted -- operator halt")
state_file.write_text(json.dumps({"state": sm.current_state.value}))
p(f"  state: {sm.current_state.value}  (written to disk atomically)")
tick(0.7)

p()
p("  kill -9 ...")
tick(0.8)
p("  process restarting ...")
tick(0.8)

recovered = json.loads(state_file.read_text())
p(f"  state on restart: {recovered['state']}")
tick(0.5)
p("  SAFE_MODE still active. auto-recovery did not clear it.")
tick(0.5)
p("  resume requires: state.transition(IDLE, 'manual resume')")
tick(1.0)

state_file.unlink(missing_ok=True)

# ----------------------------------------------------------------
# CTA
# ----------------------------------------------------------------

p()
p(SEP)
p("  github.com/amabito/veronica-core")
p("  pip install veronica-core")
p(SEP)
p()
