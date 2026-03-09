# Reproducibility Guide

This document describes how to reproduce the evaluation results reported in the
[technical paper](paper/veronica_runtime_containment_draft.md).

---

## Environment

- Python 3.10 or later
- No GPU required (all benchmarks use stub LLM implementations)
- No API keys required (no network calls during benchmarks)
- OS-agnostic (Linux, macOS, Windows)

### Install

```bash
pip install veronica-core==2.0.0
```

or from source:

```bash
git clone https://github.com/amabito/veronica-core
cd veronica-core
pip install -e ".[dev]"
```

Development dependencies (for running the test suite):

```bash
pip install -e ".[dev]"
# installs: pytest, fakeredis, lupa, ruff, coverage
```

---

## Benchmark Commands and Expected Output

### 1. Retry Amplification (paper Section 6.2, row 1)

```bash
python benchmarks/bench_retry_amplification.py
```

Expected summary:

```
Scenario              Total Calls  Theoretical   Elapsed ms  Contained
--------------------------------------------------------------------
baseline                       27           27         <1ms         No
veronica                        3          N/A         <5ms        Yes

Call reduction vs theoretical: 88.9%
```

**Paper claim:** 88.9% reduction (27 -> 3 calls). The baseline runs 3 retry layers
x 3 retries = 27 worst-case calls. The contained run enforces `max_retries_total=5`,
bounding total calls to 1+5=6; the stub LLM succeeds after 2 failures, so 3 tasks
x 1 successful call each = 3 actual calls.

---

### 2. Recursive Tool Calls (paper Section 6.2, row 2)

```bash
python benchmarks/bench_recursive_tools.py
```

Expected summary:

```
Scenario                Calls    Depth           Stopped By   Elapsed ms
----------------------------------------------------------------------
baseline                   20       19          natural_end         <1ms
veronica                    5        4           step_limit         <5ms

Depth reduction: 75.0%
```

**Paper claim:** 75.0% reduction (20 -> 5 calls). Baseline runs to depth 20.
VERONICA enforces `max_steps=5`, halting at depth 5.

---

### 3. Multi-Agent Loop (paper Section 6.2, row 3)

```bash
python benchmarks/bench_multi_agent_loop.py
```

Expected summary:

```
Scenario              Total Calls   Iterations                Halted By
----------------------------------------------------------------------
baseline                       60           30           max_iterations
veronica                       28            7         agent_step_guard

Call reduction: 53.3%
```

**Paper claim:** 53.3% reduction (60 -> 28 calls). The planner/critic loop never
converges; baseline runs 30 iterations (60 plan+critique calls). VERONICA enforces
`AgentStepGuard(max_steps=8)`, halting at 8 guard steps (~7 loop iterations, 28 calls).

---

### 4. WebSocket Runaway (paper Section 6.2, row 4)

```bash
python benchmarks/bench_websocket_runaway.py
```

Expected summary:

```
Scenario                  Ops   Bytes Sent   Close Code   Latency ms
------------------------------------------------------------------
baseline                  100         9000          N/A          N/A
veronica                   10          900         1008      <1ms

Operation reduction: 90.0%
```

**Paper claim:** 90.0% reduction (100 -> 10 ops). Baseline runs 50 send+receive
pairs = 100 operations. VERONICA enforces `max_steps=10`, sending `close(1008)` on
limit breach. Containment latency < 1 ms.

---

### 5. Baseline Comparison (paper Section 6.4 table, four scenarios)

```bash
python benchmarks/bench_baseline_comparison.py
```

Expected: comparison table across scenarios A--D showing average 78.8% call reduction
and 84.4% cost reduction.

---

### 6. Ablation Study

```bash
python benchmarks/bench_ablation_study.py
```

Expected: seven treatment conditions (CONFIG-0 to CONFIG-6) showing marginal
contribution of each primitive. Full Veronica halts at 9 calls (vs 50 baseline,
82% reduction).

---

### 7. Real Incident Reproductions

```bash
python benchmarks/real_incidents/incident_01_openai_loop.py
python benchmarks/real_incidents/incident_02_cost_spike.py
python benchmarks/real_incidents/incident_03_websocket_ddos.py
python benchmarks/real_incidents/incident_04_semantic_echo.py
python benchmarks/real_incidents/incident_05_multi_tool.py
```

Each script is self-contained and prints a comparison table: scenario, baseline calls,
VERONICA calls, contained (bool), cost saved percentage.

---

### 8. Scale Simulation (10 to 1000 agents)

```bash
python benchmarks/scale_simulation.py
```

Expected: throughput table across fleet sizes 1, 10, 50, 100, 500, 1000 chains.
Expected per-chain containment overhead: ~12.63 microseconds. Expected average call
reduction: ~83.1%.

---

## Policy Pipeline Overhead Measurement (paper Section 6.3)

To reproduce the overhead figures in Table 2 of the paper:

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig, WrapOptions
from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.runtime_policy import PolicyContext
import time

# BudgetEnforcer.spend() overhead
be = BudgetEnforcer(limit_usd=1e9)
N = 100_000
start = time.perf_counter()
for _ in range(N):
    be.spend(0.000001)
elapsed = time.perf_counter() - start
print(f"BudgetEnforcer.spend(): {(elapsed/N)*1e6:.3f} us/call")
# Expected: ~0.191 us/call

# CircuitBreaker.check() overhead
cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
ctx = PolicyContext()
N = 100_000
start = time.perf_counter()
for _ in range(N):
    cb.check(ctx)
elapsed = time.perf_counter() - start
print(f"CircuitBreaker.check(): {(elapsed/N)*1e6:.3f} us/call")
# Expected: ~0.528 us/call

# ExecutionContext full round-trip overhead
config = ExecutionConfig(max_cost_usd=100.0, max_steps=10000, max_retries_total=10000)
N = 10_000
start = time.perf_counter()
with ExecutionContext(config=config) as ctx:
    for _ in range(N):
        ctx.wrap_llm_call(fn=lambda: None, options=WrapOptions(cost_estimate_hint=0.0))
elapsed = time.perf_counter() - start
print(f"ExecutionContext.wrap_llm_call(): {(elapsed/N)*1e6:.2f} us/call")
# Expected: ~11.43 us/call
```

---

## Test Suite

Run the full test suite:

```bash
pytest tests/ -q
```

Expected: 2232 tests, 92% coverage (v1.8.1). Adversarial tests:

```bash
pytest tests/adversarial/ -v
```

Coverage report:

```bash
pytest tests/ --cov=src/veronica_core --cov-report=term-missing -q
```

---

## Verification Against Paper Claims

| Paper Claim | Command | Verify |
|------------|---------|--------|
| 88.9% retry reduction | `bench_retry_amplification.py` | `"reduction_pct": 88.9` in JSON output |
| 75.0% recursive tool reduction | `bench_recursive_tools.py` | `"depth_reduction_pct": 75.0` |
| 53.3% multi-agent loop reduction | `bench_multi_agent_loop.py` | `"call_reduction_pct": 53.3` |
| 90.0% WebSocket reduction | `bench_websocket_runaway.py` | `"operation_reduction_pct": 90.0` |
| 11.43 us full overhead | inline script above | measure within 2x of reported |
| 0.191 us BudgetEnforcer | inline script above | measure within 2x of reported |
| 0.528 us CircuitBreaker | inline script above | measure within 2x of reported |
| 2232 tests, 92% coverage | `pytest tests/ -q` | test count in summary line |

Overhead measurements are hardware-dependent; values within 2x of reported are
consistent with the claim that overhead is sub-microsecond to sub-millisecond, well
below typical LLM API latency of 500--5000 ms.

---

## Notes on Reproducibility

- All benchmark LLM calls are stubs (no network, no API keys required).
- Timing results depend on hardware; relative comparisons (baseline vs contained) are
  hardware-independent.
- Jitter in `RetryContainer` uses `random.random()`; individual run results may vary
  slightly; aggregate reduction percentages are deterministic at the design level.
- Redis-backend tests require `fakeredis` and `lupa` (installed with `.[dev]`).
