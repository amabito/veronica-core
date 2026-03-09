# VERONICA OS Roadmap

**Date**: 2026-03-09
**Scope**: Control plane completion. Not feature expansion.

---

## 1. Current Position

### veronica-core (kernel) -- stable, shipped

v3.4.2. PyPI published. 4844 tests, 94% coverage. Zero required dependencies. 4 rounds of independent security audit (130+ findings, all fixed). 8 framework adapters (OpenAI, Anthropic, LangChain, LangGraph, AG2, CrewAI, LlamaIndex, MCP). AG2 integration merged upstream (PR #2430). ASGI/WSGI middleware. Quickstart runs in 3 lines.

The kernel enforces execution boundaries: cost, steps, retries, timeouts, circuit state. It does not decide policy -- it executes policy. It sits on the call path between the application and the LLM provider. A HALT blocks the call before it reaches the model.

The kernel is done. It does not need new features to be useful.

### veronica (control plane) -- alpha, incomplete

v0.7.1. Python API exists. 232 tests, 94% coverage. 7 swappable protocols (Collector, Analyzer, CostModel, Planner, Arbiter, Store, EventEmitter). OrgPolicy validation and clamping. Thread-safe pipeline with per-stage time budgets. Prometheus metrics exporter. Redis arbiter for cross-service coordination.

The control plane decides what limits to set. It takes StepIntent as input, runs a pipeline (analyze history, estimate cost, plan ceilings, arbitrate across chains), and produces PolicyConfig. The PolicyConfig converts to ExecutionConfig via `to_exec_config()` -- the sole bridge between OS and kernel.

What the control plane does not have: HTTP API, Web UI, alerts, deploy guide, E2E integration test with the kernel, documentation for external users.

### VERONICA OS -- vision, not product

The name "VERONICA OS" describes the complete system: kernel + control plane + deployment shape. It is not a product today. It becomes a product when a user can deploy the kernel with policy management, see what is happening, and change policy without writing Python.

---

## 2. Goal Definition

VERONICA OS is real when all five conditions hold:

1. **E2E enforcement**: An application calls the kernel, the control plane sets policy, and HALT fires when the policy says HALT. This works without the user writing glue code.
2. **HTTP API**: Policy can be read, written, and updated over HTTP. No Python required for management.
3. **Visibility**: A dashboard shows active chains, budget consumption, circuit state, and safety events in real time.
4. **Single-org deploy**: One Docker Compose command starts the kernel, control plane, API, and dashboard. Works for one organization with one policy set.
5. **Design partner validation**: At least one external team has deployed it, hit a HALT, and confirmed the system did what they expected.

Federation, multi-org, marketplace, SaaS -- none of these are required. They are Phase 6.

---

## 3. Roadmap

### Phase 1: Kernel-Control Plane E2E (4 weeks)

**Purpose**: Prove the two components work together. The control plane sets policy, the kernel enforces it, events flow back to the control plane.

**Do**:
- Write an E2E integration test: VeronicaOS.step() -> ExecutionContext.wrap_llm_call() -> HALT -> SafetyEvent propagates back to Store.
- Create `veronica-e2e/` test directory with 5 scenarios: budget exhaustion, step limit, circuit open, org policy denial, adaptive ceiling tightening.
- Wire MetricsSubscriber to emit Prometheus counters for step_completed, step_denied, halt, degrade.
- Document the data flow: StepIntent -> PolicyConfig -> ExecutionConfig -> Decision -> StepOutcome -> AnalysisResult -> next PolicyConfig.
- Pin veronica's dependency on veronica-core to `>=3.4.0,<4`.

**Do not**:
- Add new kernel features.
- Build HTTP API yet.
- Change veronica-core's public API.

**Artifacts**:
- `tests/e2e/` with 5 passing scenarios.
- `docs/data-flow.md` showing the full cycle.
- Prometheus metrics confirmed in Grafana.

**Done when**: `pytest tests/e2e/ -v` passes. A Grafana screenshot shows step_completed and halt counters incrementing in real time.

---

### Phase 2: Minimal HTTP API (3 weeks)

**Purpose**: Policy management without Python. External systems can read and write policy over HTTP.

**Do**:
- FastAPI app in `veronica/api/`. 6 endpoints:
  - `GET /health` -- liveness.
  - `GET /policy` -- current OrgPolicy.
  - `PUT /policy` -- update OrgPolicy (validated, fail-closed on invalid input).
  - `GET /chains` -- active chain summaries (chain_id, cost_usd, step_count, status).
  - `GET /chains/{chain_id}` -- chain detail with recent StepOutcomes.
  - `GET /events` -- recent SafetyEvents (paginated).
- OpenAPI spec auto-generated.
- Auth: API key header (`X-Veronica-Key`). Single key, no user model yet.
- `veronica serve` CLI entry point.
- Rate limit: 100 req/s per key (in-process, no Redis).

**Do not**:
- Build multi-tenant API.
- Implement user/role management.
- Add WebSocket streaming.
- Build the UI (that is Phase 3).

**Artifacts**:
- `veronica/api/app.py` with 6 endpoints.
- `veronica/api/auth.py` with API key check.
- OpenAPI spec at `/docs`.
- `tests/api/` with endpoint tests.
- `veronica serve --port 8400` runs the server.

**Done when**: `curl -H "X-Veronica-Key: test" localhost:8400/chains` returns JSON. `PUT /policy` with invalid ceiling_usd returns 422.

---

### Phase 3: Minimal UI (3 weeks)

**Purpose**: See what the system is doing. Change policy without curl.

**Do**:
- Single-page app (HTML + vanilla JS or htmx). No React, no build step.
- 3 pages:
  - **Dashboard**: Active chains table. Total cost. HALT count. Circuit states. Auto-refresh every 5 seconds via API polling.
  - **Policy editor**: Current OrgPolicy as form. Save button calls `PUT /policy`. Validation errors shown inline.
  - **Event log**: Recent SafetyEvents table with filters (chain_id, event_type, time range).
- Served by the same FastAPI process (`/ui/` static mount).
- No authentication beyond API key (same as API).

**Do not**:
- Build a design system.
- Add real-time WebSocket push.
- Implement user accounts or RBAC.
- Support multi-org views.

**Artifacts**:
- `veronica/ui/` with index.html, app.js, styles.css.
- Dashboard screenshot suitable for README/HN.
- Policy editor that saves and takes effect within 5 seconds.

**Done when**: Open browser, see active chains and cost. Edit max_ceiling_usd, save, see next HALT fire at the new limit.

---

### Phase 4: Single-Org Production Shape (3 weeks)

**Purpose**: One command deploys the full system. Works for a real team.

**Do**:
- `docker-compose.yml` with 4 services: veronica API+UI, Redis (for distributed circuit breaker and arbiter), Prometheus, Grafana.
- Pre-built Grafana dashboard with panels: cost/time, HALT rate, circuit states, step latency, budget utilization.
- `pip install veronica[server]` extras with FastAPI, uvicorn dependencies.
- Deploy guide: `docs/deploy.md` with prerequisites, env vars, ports, health checks.
- Backup/restore for Store state (JSON export/import).
- Graceful shutdown: drain active steps, flush store, close connections.

**Do not**:
- Support multi-org or tenant isolation.
- Add TLS termination (rely on reverse proxy).
- Build Kubernetes manifests.
- Implement high availability.

**Artifacts**:
- `docker-compose.yml` that starts with `docker compose up`.
- `docs/deploy.md` with copy-paste setup.
- Grafana dashboard JSON.
- Smoke test script that starts the stack, sends 10 requests, and checks HALT fires.

**Done when**: Clone repo, `docker compose up`, send requests, see dashboard, change policy, see new policy enforced. Total setup time under 5 minutes.

---

### Phase 5: First Design Partners (ongoing, starts at Phase 3)

**Purpose**: Validate that the system solves a real problem for someone who is not the author.

**Do**:
- Identify 2-3 candidates: AG2 users (from PR #2430 interest), LangChain users running multi-agent systems, teams with LLM cost incidents.
- Provide: private deploy guide, 30-minute setup call, dedicated issue label.
- Collect: what worked, what broke, what was missing, what they expected.
- Ship fixes within 48 hours of reported issues.
- Write up each deployment as a case study (with permission).

**Do not**:
- Add features they do not ask for.
- Offer SLA or production support.
- Build features for hypothetical users.

**Artifacts**:
- 2-3 design partner deployments.
- Issue tracker with partner-reported bugs (label: `design-partner`).
- At least 1 case study or testimonial.

**Done when**: At least 1 partner has run the system for 7+ days, hit at least 1 HALT, and confirmed it prevented a cost overrun they would have otherwise incurred.

---

### Phase 6: Federation -- NOT SCHEDULED

**Purpose**: Multiple VERONICA instances share budget state and policy across organizations.

This phase exists in the architecture documents (docs/V4_ARCHITECTURE.md, docs/EVOLUTION_ROADMAP.md Phase G). It will be built when the first concrete cross-organization use case appears. No timeline commitment.

Prerequisites: Phase 1-5 complete. At least 3 single-org deployments stable.

---

## 4. 30/60/90 Day Plan

### Days 1-30: E2E + API foundation

- Week 1-2: Phase 1 E2E integration tests. 5 scenarios passing.
- Week 3-4: Phase 2 API. 6 endpoints. OpenAPI spec. `veronica serve` works.
- Deliverable: `PUT /policy` changes enforcement behavior within 5 seconds.

### Days 31-60: UI + deploy shape

- Week 5-6: Phase 3 UI. Dashboard + policy editor + event log.
- Week 7-8: Phase 4 Docker Compose. Grafana dashboard. Deploy guide.
- Deliverable: `docker compose up` -> browser shows dashboard -> edit policy -> HALT fires.

### Days 61-90: Design partners + public launch

- Week 9-10: Reach out to AG2/LangChain communities. Offer private access.
- Week 11-12: Fix partner-reported issues. Write case study. Prepare HN/Reddit launch.
- Deliverable: 1 external deployment confirmed. Launch post published.

---

## 5. KPIs

### Technical KPIs

| Metric | Target | Measurement |
|--------|--------|-------------|
| E2E test scenarios | 5 passing | pytest tests/e2e/ |
| API endpoint count | 6 | OpenAPI spec |
| API response time p99 | < 50ms | Prometheus histogram |
| Docker Compose startup | < 30 seconds | Smoke test |
| veronica test count | 300+ (from 232) | pytest |
| veronica coverage | >= 90% | pytest-cov |

### Product KPIs

| Metric | Target | Measurement |
|--------|--------|-------------|
| Time to first HALT | < 5 minutes from `pip install` | Manual test |
| Design partners deployed | >= 1 by day 90 | Issue tracker |
| Partner-reported bugs fixed | 100% within 48h | Issue tracker |
| HN launch upvotes | >= 50 | Post URL |
| GitHub stars (veronica-core) | >= 100 by day 90 | GitHub |
| PyPI downloads/week | >= 200 | PyPI stats |

---

## 6. Now / Not Now

### Now (Phase 1-5)

- E2E integration tests between kernel and control plane.
- FastAPI HTTP API for policy management.
- Minimal dashboard UI (HTML + JS, no framework).
- Docker Compose single-org deploy.
- Design partner outreach and onboarding.
- HN/Reddit launch (kernel first, then OS).
- Technical paper publication.

### Not Now

- Federation (v4.0). No demand. Build when asked.
- ExecutionContext God Class split. Important for maintainability but does not block users. Defer to post-launch.
- Multi-tenant API. Single-org is enough for Phase 4.
- Kubernetes manifests. Docker Compose is sufficient.
- SaaS / hosted offering. Too early.
- New kernel features. The kernel is stable. Do not touch it.
- adapter/ vs adapters/ directory unification. Cosmetic. Defer.
- nogil Python readiness. Python 3.14 is not shipping yet.
- Memory governance real-system integration. No memory system to integrate with.
- New framework adapters. 8 is enough.

---

## 7. External Positioning

`veronica-core` is a runtime containment layer for LLM agent systems. It enforces hard limits on cost, steps, retries, and circuit state -- evaluated before the call reaches the model. It works with any LLM provider, requires zero dependencies, and integrates with 8 agent frameworks including AG2 (merged upstream). 4844 tests, 94% coverage, 4 rounds of security audit.

`veronica` is the control plane. It decides what limits to set based on execution history, cost estimation, and organization policy. It produces PolicyConfig; veronica-core enforces it.

VERONICA OS is the vision: kernel + control plane + dashboard + single-command deployment. The kernel is shipped. The control plane is in alpha. The goal is a system where a team can deploy runtime containment for their LLM agents in 5 minutes, see what is happening, and change policy without writing code.

The kernel is not a guardrail. Guardrails inspect LLM output content. VERONICA does not look at prompts or completions. It governs resource consumption -- the same way Linux cgroups govern container resources. Containment, not observability.

---

## 8. Conclusion

The kernel is the strong asset. It is tested, audited, integrated, and published. The control plane is the gap. Without HTTP API, UI, and deploy shape, the kernel is a library that only Python developers can use. The roadmap closes that gap in 90 days, validates with design partners, and launches publicly.

The risk is not technical. The kernel works. The risk is that no one cares -- that LLM cost overruns are not painful enough to justify a runtime containment layer. Design partners answer that question. Everything else is execution.

VERONICA_OS_ROADMAP_FIXED
