# VERONICA Manifesto

---

LLM systems are not reliable by default.

They are probabilistic, cost-generating, recursively-invocable components
embedded inside production systems that are expected to behave reliably.
That contradiction is structural. It does not resolve itself through better prompts,
better models, or better orchestration.

It requires a containment layer.

---

## What we believe

**Unbounded execution is a structural defect, not a configuration problem.**

An agent that can run indefinitely, spend arbitrarily, retry infinitely,
or recurse without termination is not a feature awaiting configuration.
It is a system without a resource model.
Every production system requires a resource model.
LLM systems are not exempt.

**Observability is not containment.**

Knowing that a runaway execution occurred is not the same as preventing it.
Dashboards, traces, and cost alerts operate after the fact.
A containment layer operates before the call.
These are different problems. Solving one does not solve the other.

**The execution environment must be explicit.**

Every call an agent makes is a node in an execution graph.
That graph has a cost, a depth, an amplification factor, and a set of containment conditions.
Making those properties implicit — invisible until something breaks — is an architectural choice.
It is the wrong choice.

**Containment is not safety theater.**

Containment is not a list of guardrails bolted onto an existing system.
It is a constraint layer that enforces bounded properties on execution:
bounded cost, bounded retries, bounded recursion, bounded wait states, bounded failure domains.
These constraints are enforced at call time, not evaluated in post-incident review.

**The infrastructure layer should not require model-level trust.**

A containment layer does not trust the model to be predictable.
It does not trust the orchestrator to respect resource limits.
It does not trust the caller to set correct bounds.
It enforces bounds unconditionally, as a structural guarantee of the execution environment.

---

## What we are building

VERONICA is an execution OS for LLM systems.

It manages what an operating system manages for processes:
resource allocation, execution bounds, failure isolation, structured termination.

It does not manage what an OS does not manage:
the correctness of what runs inside the execution environment,
the quality of decisions made by the agent,
or the content of prompts and completions.

The scope is deliberate.
A layer that tries to manage everything manages nothing reliably.

---

## What we are not building

We are not building:

- A prompt management system
- An evaluation framework
- An observability product
- A guardrail layer that inspects content
- A model router
- A workflow orchestrator

Each of those is a legitimate product.
None of them is a containment layer.
VERONICA is a containment layer.

---

## On structural maturity

LLM infrastructure is young.
The current state resembles distributed systems infrastructure circa 2005:
capable of extraordinary things, structurally incomplete,
and missing the constraint and coordination layers that production reliability requires.

Circuit breakers, rate limiters, bulkheads, resource quotas —
these did not emerge because distributed systems were poorly designed.
They emerged because unbounded execution at scale fails in predictable ways,
and building the infrastructure to prevent those failures is engineering, not overhead.

The same is true for LLM systems.
Runtime containment is not optional infrastructure.
It is the missing layer.

---

*VERONICA v0.9.0*
