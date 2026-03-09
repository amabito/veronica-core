# Evaluation

veronica-core includes reproducible evaluation of runtime containment across four
canonical runaway failure modes (retry amplification, recursive tools, multi-agent
loops, WebSocket runaway):

- [Technical paper](paper/veronica_runtime_containment_draft.md) -- system design, threat model, formal safety guarantees (G1-G6), evaluation
- [Baseline comparison](../benchmarks/bench_baseline_comparison.py) -- no containment vs veronica across four scenarios (avg 78.8% call reduction)
- [Ablation study](../benchmarks/bench_ablation_study.py) -- incremental component contribution (BudgetEnforcer, AgentStepGuard, CircuitBreaker, RetryContainer)
- [Real incident reproduction](../benchmarks/real_incidents/) -- five real-world failure scenarios with before/after comparison
- [Scale simulation](../benchmarks/scale_simulation.py) -- 1 to 1000 concurrent agent chains (~83.1% reduction, ~12.63 us/chain overhead)
- [Reproducibility guide](reproducibility.md) -- environment, commands, expected output, verification against paper claims

Supporting theory:

- [Amplification model](theory/amplification_model.md) -- formal model of retry and agent amplification with worked examples
- [Safety guarantees](security/safety_guarantees.md) -- cost bound, termination, retry budget, failure isolation proofs
