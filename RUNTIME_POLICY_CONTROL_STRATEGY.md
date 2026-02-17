# VERONICA -- Runtime Policy Control Strategy

---

## Section 1: Category Definition

### 1.1 What is Runtime Policy Control

Runtime Policy Control is the enforcement of operational constraints on LLM execution **during** the call lifecycle -- not before (configuration), not after (observation).

```
Timeline of an LLM call:

  [Config]          [Runtime Policy Control]          [Observation]
  model=gpt-4       budget.spend() -> bool            trace.log()
  temperature=0.7   guard.step() -> bool              cost.record()
  max_tokens=4096   circuit.can_proceed() -> bool     latency.measure()
                    retry.execute() -> result|raise
       |                    |                              |
   Before call      During call lifecycle            After call returns
   (static)         (dynamic, enforced)              (passive, recorded)
```

Three systems exist in the LLM stack. Two have categories. One does not.

| System | Category | When | Verb | Example Products |
|--------|----------|------|------|------------------|
| Configuration | Prompt Engineering | Before | Configure | LangChain, DSPy, Instructor |
| **Policy Control** | **Runtime Policy Control** | **During** | **Enforce** | **VERONICA (only)** |
| Observation | Observability | After | Record | LangSmith, Helicone, Datadog LLM |

Runtime Policy Control is the missing middle layer. Configuration decides what to do. Observation records what happened. Policy Control decides **whether to allow it to continue**.

### 1.2 Why Not Observability

Observability answers: "What happened?"
Runtime Policy Control answers: "Should this be allowed to continue?"

| Dimension | Observability | Runtime Policy Control |
|-----------|---------------|----------------------|
| **Timing** | Post-execution (trace write) | Mid-execution (policy check) |
| **Effect** | None (passive recording) | Halt, throttle, redirect (active) |
| **Latency** | Acceptable (async trace flush) | Must be sub-millisecond (in critical path) |
| **Data** | Rich (prompts, responses, tokens, latency) | Minimal (cost, step_count, entity_id) |
| **Architecture** | Sidecar / proxy / callback | Inline / wrapper / middleware |
| **Failure mode** | "We saw the $12K bill on Monday" | "The $12K bill never happened" |
| **Revenue model** | Per-trace (data volume) | Per-org (policy value) |

Adding tracing to VERONICA would make it a worse LangSmith. Adding policy control to LangSmith would require rewriting their architecture from async-observer to sync-enforcer. They can't do it without degrading trace throughput.

### 1.3 Why Not Evaluation

Evaluation answers: "Was the output good?"
Runtime Policy Control answers: "Should execution continue?"

Evaluation is inherently subjective (requires human judgment or ML scoring). Policy Control is deterministic (spent > limit = stop). There is no false positive in a budget check.

| | Evaluation | Runtime Policy Control |
|--|-----------|----------------------|
| **Input** | LLM output (text, structured) | Operational metric (cost, steps, errors) |
| **Logic** | ML model, human rating, heuristic | Arithmetic comparison |
| **Certainty** | Probabilistic ("87% quality score") | Deterministic ("budget exceeded: true") |
| **Dependencies** | Embedding model, scoring rubric | None (pure arithmetic) |
| **Products** | Braintrust, W&B Weave, Ragas | VERONICA |

### 1.4 Why 2025 Needs This Category

Three forces converge:

**Force 1: Autonomous agents are shipping to production.**
LangGraph, CrewAI, AutoGen, PydanticAI -- agent frameworks went from demos to production in 2024-2025. GitHub stars: LangChain 90K+, CrewAI 20K+, AutoGen 30K+. Production agents execute unbounded loops with real-money API calls. The failure mode changed from "bad output" to "infinite cost." OpenAI reported 200M+ weekly active users in 2025, with enterprise API usage growing 300%+ YoY.

**Force 2: Observability tools cannot prevent damage.**
LangSmith ($16M ARR, 130M+ downloads), Helicone ($79/mo), Datadog LLM Observability, Langfuse -- all trace calls. None stop calls. When a production agent enters a retry loop at 3am on Saturday, observability tells you about it Monday morning. The bill is already $12,000. Every trace is a post-mortem, not a prevention.

**Force 3: No standard exists for runtime constraints.**
Every team writes ad-hoc budget checks, retry limits, and circuit breakers inside their application code. There is no shared vocabulary, no reusable library, no specification. Teams reinvent the same 4 patterns and get them wrong in the same 4 ways:
- Budget check with race condition (not thread-safe)
- Retry limit per-call instead of per-chain (3 retries x 5 calls = 15 LLM calls)
- Agent step limit without partial result preservation (work lost on timeout)
- Circuit breaker without persistence (resets on restart, same failure repeats)

**Force 4: LLM API costs are unpredictable by nature.**
GPT-4o: $2.50/1M input tokens, $10.00/1M output tokens. A single agent chain can generate 50K+ tokens. An agent loop that runs 100 iterations = $50+ per user action. At 1000 concurrent users with runaway agents, costs hit $50K/hour. No engineering team budgets for this. No existing tool prevents it.

Runtime Policy Control is the category name for solving all four forces.

---

## Section 2: OSS Role

### 2.1 Abstract Interface: The Policy Protocol

The SDK's public interface is not the 4 concrete classes. It is the **Policy** abstraction that all 4 implement.

```python
from typing import Protocol, Any, runtime_checkable

@runtime_checkable
class RuntimePolicy(Protocol):
    """A runtime constraint that can halt LLM execution.

    All VERONICA primitives implement this protocol.
    Third-party policies can implement it to integrate
    with the VERONICA execution pipeline.
    """

    def check(self, context: PolicyContext) -> PolicyDecision:
        """Evaluate whether execution should continue.

        Args:
            context: Current execution state (cost, step, entity, etc.)

        Returns:
            PolicyDecision with allow/deny and reason
        """
        ...

    def reset(self) -> None:
        """Reset policy state (e.g., new budget period)."""
        ...

    @property
    def policy_type(self) -> str:
        """Machine-readable policy type identifier."""
        ...


@dataclass(frozen=True)
class PolicyContext:
    """Immutable snapshot of execution state for policy evaluation."""
    cost_usd: float = 0.0
    step_count: int = 0
    entity_id: str = ""
    chain_id: str = ""
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a policy evaluation."""
    allowed: bool
    policy_type: str
    reason: str = ""
    # For partial results
    partial_result: Any = None
```

### 2.2 Current Primitives as Policy Implementations

Each existing primitive maps to a `RuntimePolicy` implementation:

| Primitive | `policy_type` | `check()` Logic | Context Fields Used |
|-----------|--------------|-----------------|---------------------|
| `BudgetEnforcer` | `"budget"` | `spent + cost <= limit` | `cost_usd` |
| `AgentStepGuard` | `"step_limit"` | `current_step < max_steps` | `step_count` |
| `RetryContainer` | `"retry_budget"` | `attempts < max_retries` | `entity_id` (per-call) |
| `CircuitBreaker` | `"circuit_breaker"` | `state != OPEN` | `entity_id` |

### 2.3 Policy Pipeline (Execution Model)

```python
class PolicyPipeline:
    """Evaluates multiple policies in sequence. First denial wins."""

    def __init__(self, policies: list[RuntimePolicy]):
        self.policies = policies

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """Run all policies. Return first denial, or allow."""
        for policy in self.policies:
            decision = policy.check(context)
            if not decision.allowed:
                return decision  # First denial wins. No override.
        return PolicyDecision(allowed=True, policy_type="pipeline", reason="all policies passed")
```

**Design principle**: Policies compose via AND logic. If any policy denies, execution stops. There is no "override" mechanism. This is intentional -- an override defeats the purpose of enforcement.

### 2.4 Future Policy Types (Designed, NOT Implemented)

These interfaces exist in the `RuntimePolicy` protocol. Implementations ship when demand proves the need.

| Policy Type | `policy_type` | What It Controls | When to Ship |
|-------------|--------------|-----------------|--------------|
| `TokenCap` | `"token_cap"` | Max tokens per chain (input + output) | When 3+ users ask |
| `LatencyCap` | `"latency_cap"` | Max wall-clock time per chain | When agent P99 latency matters |
| `ContentGate` | `"content_gate"` | Block execution if input/output matches rule | When compliance teams ask |
| `RateLimiter` | `"rate_limit"` | Max calls per time window per entity | When multi-tenant apps need it |
| `CostRate` | `"cost_rate"` | Max $/minute burn rate (not total, but velocity) | When streaming agents ship |
| `ModelGate` | `"model_gate"` | Restrict which models an agent can call | When model governance is needed |

**Rule**: A policy type is added to veronica-core ONLY when:
1. 3+ independent users request it (GitHub issues)
2. The implementation fits the `RuntimePolicy` protocol without changes
3. It requires zero external dependencies

### 2.5 What the SDK Does NOT Do

| Capability | Why Excluded | Who Does It |
|-----------|-------------|-------------|
| Trace/log LLM calls | Observability (different architecture) | LangSmith, Helicone |
| Score output quality | Evaluation (requires ML/heuristics) | Braintrust, Ragas |
| Route between models | Orchestration (different concern) | LiteLLM, Portkey |
| Store prompts/responses | Data management (storage cost) | LangSmith, Langfuse |
| Dashboard/visualization | Analytics (scope creep) | Grafana, Datadog |
| Authentication/AuthZ | Security (orthogonal concern) | Auth0, Ory |

The SDK is a **decision engine** with one output: `{allowed: bool}`. Everything else is someone else's job.

---

## Section 3: Cloud Minimal Configuration

### 3.1 Cloud = Org-Level Runtime Decision Authority

The Cloud does exactly what the SDK does -- evaluate policies and return `{allowed: bool}` -- but at organization scope instead of process scope.

| Capability | SDK (Process) | Cloud (Organization) |
|-----------|---------------|---------------------|
| Budget enforcement | Single chain | All chains across all services |
| Policy evaluation | Local `PolicyPipeline` | Fleet-wide `PolicyPipeline` |
| Kill switch | `sys.exit()` | API halt across N connected SDKs |
| State | In-memory / local file | Centralized database |

### 3.2 Cloud Features (Complete, Final List)

| Feature | Endpoint | What It Does |
|---------|----------|-------------|
| **Org Budget** | `POST /v1/spend` | Aggregate cost across fleet. Return `{halt: bool}` when ceiling hit. |
| **Fleet Policy Sync** | `GET /v1/policies` | SDK polls for org-level policy configuration (60s interval). |
| **Global Kill Switch** | `POST /v1/kill` | Set `safe_mode=true`. All connected SDKs halt on next poll. |
| **Webhook Alert** | (outbound) | Slack/Discord notification on budget threshold, kill activation, policy breach. |

### 3.3 What Cloud Does NOT Have

| Feature | Status | Reason |
|---------|--------|--------|
| Dashboard | **Prohibited** | Analytics territory. Use Grafana/Datadog. |
| Cost analytics | **Prohibited** | Observability territory. Use LangSmith/Helicone. |
| Prompt/response logging | **Prohibited** | Data storage liability. Privacy risk. |
| Evaluation scoring | **Prohibited** | Different problem. Use Braintrust. |
| Usage charts | **Prohibited** | Becomes analytics the moment you add a chart. |
| Team management UI | **Deferred** | YAGNI until MRR > $10K. API key per org. |
| Audit log search | **Deferred** | Export CSV. No search UI. |

**The Cloud has no frontend.** API-only. SDKs poll it. Humans interact via Slack alerts and `curl`.

### 3.4 Architecture

```
veronica-core SDK (in customer's process)
  |
  | POST /v1/spend  {cost_usd, chain_id, org_api_key}
  | GET  /v1/policies  (every 60s)
  |
  v
Cloudflare Worker (stateless, free tier)
  |
  +--> Validate API key (SHA-256 hash lookup)
  +--> UPDATE org SET spent = spent + $cost
  +--> SELECT spent, limit, safe_mode FROM org
  |      spent >= limit?  --> {halt: true}
  |      safe_mode?       --> {halt: true}
  |      else             --> {halt: false, policies: [...]}
  |
  v
Neon PostgreSQL (free tier, 0.5GB)
```

### 3.5 Data Model (3 Tables, No More)

```sql
-- Organization and its active policy configuration
CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    plan TEXT DEFAULT 'free',
    -- Budget policy (org-level)
    budget_limit_usd DECIMAL(10,2),
    budget_spent_usd DECIMAL(10,6) DEFAULT 0,
    budget_period TEXT DEFAULT 'monthly',
    budget_reset_at TIMESTAMPTZ,
    -- Fleet control
    safe_mode BOOLEAN DEFAULT false,
    -- Notification
    webhook_url TEXT,
    webhook_type TEXT DEFAULT 'slack',
    -- Auth
    api_key_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Spend events (append-only, auto-pruned)
CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id),
    cost_usd DECIMAL(10,6) NOT NULL,
    chain_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_events_org_period ON events(org_id, created_at);
-- Retention: 7 days (free), 30 days (pro). Cron DELETE daily.

-- Kill switch audit log
CREATE TABLE kill_log (
    id BIGSERIAL PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id),
    reason TEXT,
    activated_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

**Data stored per event: 3 fields.** `org_id`, `cost_usd`, `created_at`. No prompts. No responses. No tokens. No model names. This is not observability data. This is policy enforcement data.

### 3.6 Cost Structure

| Component | Provider | Tier | Monthly Cost |
|-----------|----------|------|-------------|
| API | Cloudflare Workers | Free (100K req/day) | $0 |
| Database | Neon PostgreSQL | Free (0.5GB) | $0 |
| Domain | veronica.dev | Annual | ~$1 |
| **Total (0-100 orgs)** | | | **~$1/month** |
| **At 300 Pro orgs** | Neon Launch + CF Paid | | **~$50/month** |
| **At 1,000 orgs** | | | **~$150/month** |

**Why so cheap**: No prompt data storage. No response data storage. No embeddings. No ML inference. Each event is ~50 bytes. 10M events/month = 500MB. Observability tools store 100x more data per event.

---

## Section 4: 3-Year Category Creation Strategy

### 4.1 The Category Creation Playbook

Category creation follows a 5-phase pattern. Every successful DevTools category followed this arc:

**Verified precedents** (sourced from company filings, CNCF records, TechCrunch, Crunchbase):

| Category | Who Coined It | Key Moment | Timeline | Revenue / Outcome |
|----------|--------------|------------|----------|-------------------|
| **Observability** | Charity Majors (Honeycomb, 2016) | Wikipedia lookup of control theory term -> blog evangelism -> Monitorama 2017 keynote -> Gartner Magic Quadrant 2020 -> "Observability: A Manifesto" (Jul 2021) | 4-5 years (2016-2021) | Honeycomb: Series D $50M (2023), 160% NRR, 600+ customers. Datadog (category king): $2.7B ARR (2024) |
| **Infrastructure as Code** | Mitchell Hashimoto (HashiCorp, 2012) | "Tao of HashiCorp" manifesto -> Terraform 0.1 (Jul 2014, only AWS+DigitalOcean) -> bottom-up adoption -> IPO Dec 2021 ($14B, $212M revenue) | 5-9 years (2012-2021) | HashiCorp: $600M ARR -> IBM $6.4B (10.7x). TAM: $2.1B -> $12B (5.7x in 5 years) |
| **Service Mesh** | William Morgan (Buoyant, 2016) | Coined "service mesh" with Linkerd release -> CNCF accepted Jan 2017 -> Rewritten in Rust 2018 -> CNCF Graduated Jul 2021 | 5 years (2016-2021) | Buoyant: $24M+ raised, profitable. Linkerd = first service mesh to graduate CNCF |
| **DevSecOps** | Neil MacDonald (Gartner, 2012) | Analyst coined "DevOpsSec" (renamed "DevSecOps" to avoid "DOS") -> Snyk founded 2015 -> $100K ARR Aug 2017 -> Unicorn 2020 (5 years!) | 3-5 years to mainstream | Snyk: $344M revenue (2024), $8.5B valuation, $1.32B total raised |
| **Policy as Code** | Styra/OPA (2016) | OPA created -> CNCF accepted Mar 2018 -> Incubating Apr 2019 -> Graduated Jan 2021 -> 120M+ downloads | 5 years (2016-2021) | Styra: $15M ARR (2025), $67.5M raised. Slow monetization despite CNCF standard |
| **AI Engineering** | swyx (Latent Space, Jun 2023) | Blog post "The Rise of the AI Engineer" -> Endorsed by Andrej Karpathy -> Summit Oct 2023 (500 attendees) -> World's Fair 2024 (2,000 attendees) | 1-2 years (emerging) | Conference: 500 -> 2,000 attendees (4x YoY). 500K+ online views |

**Critical insights from the data**:

1. **76% rule** (Chris Lochhead, "Play Bigger"): Category kings capture 76%+ of their market's value. Datadog ($2.7B ARR) vs Honeycomb (<$150M raised). **Being the category creator doesn't guarantee being the category king.** Winning requires execution speed, not just naming rights.

2. **Evangelize 12-24 months BEFORE product traction**: Charity Majors blogged/spoke for 2 years (2016-2018) before Honeycomb gained real traction. swyx published for 4 months before Summit. **Content first, product second.**

3. **Manifesto at inflection point**: Not Day 1, but after community validation (12-24 months). Honeycomb's "Observability: A Manifesto" came in 2021 (5 years after founding). Too early = nobody cares. Too late = someone else defines it.

4. **CNCF graduation = 3-5 year commitment**: Linkerd (2017->2021), OPA (2018->2021). Enterprise legitimacy but slow.

5. **OSS != fast revenue**: Styra: 9 years to $15M ARR. Snyk: 9 years to $344M. **Security positioning monetizes 20x faster than infrastructure positioning.** VERONICA must be security, not infrastructure.

6. **Conference = proof of category**: 500+ attendees = critical mass. AI Engineer Summit proved this in 18 months.

**Common pattern across all 6**:
1. **Name it** -- one person/company coins the term and evangelizes relentlessly
2. **Blog about it** -- 3-5 defining blog posts over 12-24 months
3. **Manifesto** -- formal document that crystallizes the concept (at inflection point)
4. **Ship the reference implementation** -- OSS project becomes the default
5. **Foundation/standard** -- CNCF, Gartner, or O'Reilly validation
6. **Competitors adopt the term** -- the moment competitors use your term, the category is real

```
VERONICA's plan:
Phase 1: Name it       (Month 0-3)    "Runtime Policy Control"
Phase 2: Ship it       (Month 0-6)    veronica-core on PyPI, framework PRs
Phase 3: Standardize   (Month 6-18)   Specification, CNCF, conference talks
Phase 4: Ecosystem     (Month 12-24)  Others implement the spec
Phase 5: Own it        (Month 18-36)  "VERONICA" = "Runtime Policy Control"
```

**Why VERONICA can compress the timeline to 2-3 years** (vs 3-5 year historical average):
- LLM ecosystem moves 3x faster than infrastructure (782 AI M&A deals in 2025, $146B+)
- The pain is measurable in dollars (not abstract "security risk")
- Zero competitors in the specific category (Observability had Honeycomb vs Datadog vs New Relic from day 1)
- MIT license + zero dependencies = zero friction to adopt

### 4.2 Phase 1: Name the Category (Month 0-3)

**Term**: "Runtime Policy Control" (RPC)

**Why this name**:
- "Runtime" -- distinguishes from config-time and post-hoc. Implies live, in-process.
- "Policy" -- borrows authority from "Policy as Code" (OPA). Implies governance, compliance.
- "Control" -- implies active intervention, not passive observation. Stronger than "management."

**Term seeding locations** (first 90 days):

| Location | Action | Content |
|----------|--------|---------|
| PyPI description | Ship | "Runtime Policy Control for LLM execution" |
| GitHub repo description | Ship | "Runtime Policy Control SDK -- budget, step, retry, circuit breaker" |
| README.md H1 | Ship | "VERONICA -- Runtime Policy Control for LLM Applications" |
| HN Show post | Publish | "Show HN: Runtime Policy Control for LLM Agents (MIT, zero-dep)" |
| Dev.to article | Publish | "Why Your LLM Stack Needs Runtime Policy Control (and What That Means)" |
| Twitter/X thread | Publish | "I coined 'Runtime Policy Control.' Here's why Observability isn't enough." |
| LiteLLM PR description | Submit | "Add Runtime Policy Control via VERONICA integration" |

**Name validation test**: Search "Runtime Policy Control LLM" on Google. If zero results, the term is unclaimed. VERONICA defines it.

**Manifesto strategy** (based on Honeycomb/HashiCorp pattern):

Publish **"The Case for Runtime Policy Control"** at Month 3-6 (not Day 1). This is the defining document.

Structure (modeled on Honeycomb's "Observability: A Manifesto" and HashiCorp's "Tao"):
1. **The Problem**: LLM execution has no runtime constraints. Observability is rear-view. Evaluation is post-hoc.
2. **The Definition**: Runtime Policy Control = enforcement of operational constraints during the call lifecycle.
3. **The Principles**: Synchronous. Deterministic. Composable. Zero-dependency.
4. **The Specification**: 4 required policy types + extension protocol.
5. **The Call to Action**: Implement the spec. Build on it. Critique it.

License: Apache-2.0 (same as spec). Publish on personal blog, cross-post to HN, Dev.to, Medium.

**Timing**: After 500+ GitHub stars and 1+ framework integration merged. This proves the concept has traction before formalizing it. Publishing too early (Day 1) = "nobody cares." Publishing at inflection point = "this is real."

### 4.3 Phase 2: Ship and Integrate (Month 0-12)

**Framework integration = distribution.** Each merged PR creates a permanent install base.

| Framework | Users | Integration Type | Timeline |
|-----------|-------|-----------------|----------|
| LiteLLM | 25K+ GitHub stars | `VeronicaBudgetLogger` extending `CustomLogger` | Month 1-3 |
| Instructor | 10K+ stars | Decorator: `@with_budget(limit=100)` | Month 3-4 |
| PydanticAI | 5K+ stars | Standalone package `pydantic-ai-veronica` | Month 4-6 |
| CrewAI | 20K+ stars | Agent-level policy pipeline | Month 6-8 |
| LangChain | 90K+ stars | `VeronicaCallbackHandler` | Month 8-10 |
| AutoGen | 30K+ stars | Runtime policy middleware | Month 10-12 |

**Integration design principle**: Each integration is a thin adapter (<100 LOC) that maps the framework's callback/middleware pattern to `RuntimePolicy.check()`. The policy logic stays in veronica-core. The adapter is framework-specific glue.

### 4.4 Phase 3: Standardize (Month 6-18)

**Step 1: Publish the Specification (Month 6-8)**

```yaml
# LLM Runtime Policy Control Specification v1.0
# License: Apache-2.0
# URL: https://github.com/veronica-project/rpc-spec

metadata:
  name: "LLM Runtime Policy Control Specification"
  version: "1.0.0"
  status: "Draft"
  license: "Apache-2.0"

definitions:
  runtime_policy:
    description: >
      A stateful constraint that evaluates execution context
      and returns an allow/deny decision in the critical path
      of an LLM call.
    interface:
      check(context) -> {allowed: bool, reason: str}
      reset() -> void
    properties:
      - Synchronous (must not block on I/O)
      - Deterministic (same input = same output)
      - Composable (policies chain via AND logic)
      - Zero external dependencies

  policy_types:
    required:
      - budget: "Cumulative cost ceiling"
      - step_limit: "Agent iteration cap with partial result preservation"
      - retry_budget: "Chain-level retry containment"
      - circuit_breaker: "Per-entity failure threshold with cooldown"
    optional:
      - token_cap: "Total token ceiling"
      - latency_cap: "Wall-clock time limit"
      - rate_limit: "Calls per time window"
      - content_gate: "Input/output content filter"
      - model_gate: "Allowed model list"

  policy_pipeline:
    description: "Sequential evaluation. First denial wins. No override."
    composition: "AND (all must pass)"

  fleet_control:
    kill_switch:
      description: "Unconditional halt across all connected processes"
      api: "POST /v1/kill {reason: str}"
      propagation: "Polling (60s max) or WebSocket (instant)"
    safe_mode:
      description: "Persistent halt flag. Survives process restart."
      deactivation: "Explicit API call only. No auto-recovery."
```

**Step 2: CNCF Landscape Submission (Month 8-10)**

CNCF Landscape listing requires a PR to `cncf/landscape` repo modifying `landscape.yml`.

**Hard requirements**:
- 300+ GitHub stars (strict filter) [Target: Month 6]
- OSI-approved license (MIT) [DONE]
- SVG logo (centered, optimized, no embedded bitmaps) [TODO]
- Single category assignment

**Target category**: Two options:
1. **Security > Policy Management** (alongside OPA, Kyverno) -- proven category, faster merge
2. **Cloud Native AI > Agentic AI Platforms** (new subcategory added 2025) -- newer, closer to LLM

**New subcategory proposal** ("Runtime Policy Control"):
- Requires GitHub issue in `cncf/landscape`
- Must list 3+ distinct projects with 300+ stars (VERONICA + 2 others that implement the spec)
- TAG Security or TAG App Delivery buy-in needed
- TOC sign-off for major category changes
- Timeline: 3-6 months from proposal to addition
- Precedent: "Agentic AI Platforms" was added in 2025, "Wasm" in Sep 2023

**Realistic path**: List under **Security > Policy Management** first (Month 8). Propose new subcategory when 3+ implementations exist (Month 18+).

**Review timeline**: 1-4 weeks. Simple PRs (correct format, >300 stars) merge within 1 week.

**Step 3: CNCF Sandbox Application (Month 15-18)**

Requirements:
- 2+ maintainers from different organizations
- Documented governance (GOVERNANCE.md, CODE_OF_CONDUCT.md)
- Adopted by 3+ organizations in production
- TAG sponsorship (TAG Security for policy enforcement)
- Clear differentiation from existing CNCF projects (OPA = general policy, VERONICA = LLM runtime policy)

**TAG Security involvement**:
- VERONICA maps to TAG Security scope (runtime protection, policy enforcement)
- Request TAG Security white paper review of LLM Runtime Policy Control specification
- Present at TAG Security meeting (bi-weekly, open attendance)

**Fastest Sandbox-to-Graduated precedents**: Kubernetes (anomaly, pre-dates process), Helm (~2 years), Argo (~2.5 years). Typical: 3-5 years.

**Step 4: Conference Talks (Month 6-24)**

| Conference | CFP Window | Talk Title | Audience | Category Creation Angle |
|-----------|-----------|-----------|----------|------------------------|
| **KubeCon NA 2026** | Apr-May (for Nov) | "Runtime Policy Control: The Missing Layer in Your LLM Stack" | 12,000+ CNCF/infra engineers | Defines category to standards body audience |
| **AI Engineer Summit** | Rolling (next: Jun/Oct) | "Why Observability Can't Save Your Agent Fleet" | 5,000+ AI engineers | Positions against observability incumbents |
| **PyCon US 2026** | Oct-Dec 2025 (for May 2026) | "4 Patterns Every LLM App Gets Wrong" | 3,500+ Python devs | Developer education, hands-on demo |
| **QCon London/SF** | 6mo before | "From Monitoring to Control: Runtime Policy for AI" | Enterprise architects | Enterprise adoption narrative |
| **FOSDEM** | Oct-Nov (for Feb) | "Building an Open Standard for LLM Safety" | OSS purists | Standards-first messaging |
| **PyCon JP** | Mar-Apr (for Sep) | "LLM Runtime Policy Control" | Japanese developer community | Local market seeding |

**Blog post sequence (category-defining)**:

| Month | Title | Purpose | Target |
|-------|-------|---------|--------|
| 0 | "Introducing Runtime Policy Control" | Define the term | HN, Dev.to |
| 1 | "Your LLM Stack Has a Missing Layer" | Problem awareness | Medium, personal blog |
| 2 | "Observability vs Policy Control: Complementary, Not Competing" | Position alongside, not against | Dev.to, LangChain blog |
| 4 | "The $12K Weekend: Why Every Agent Needs a Budget Ceiling" | Pain point narrative | HN |
| 6 | "LLM Runtime Policy Control Specification v1.0" | Standard announcement | GitHub, HN |
| 8 | "VERONICA + LiteLLM: Runtime Policy Control in 3 Lines" | Integration tutorial | Dev.to |
| 12 | "Year 1: How Runtime Policy Control Became a Category" | Retrospective | Personal blog, HN |

**Talk structure that creates categories**:
1. Name the pain (unbounded LLM costs, runaway agents)
2. Name what exists (observability, evaluation)
3. Name what's missing (runtime policy control) -- **coin the term on stage**
4. Show the solution (VERONICA live demo, 3 lines of code)
5. Show the specification (anyone can implement -- this isn't a product pitch, it's a standard)

### 4.5 Phase 4: Ecosystem Formation (Month 12-24)

**Validation signal**: Someone else implements the RPC specification.

How to accelerate ecosystem formation:

| Action | Timeline | Expected Outcome |
|--------|----------|-----------------|
| Publish RPC spec as standalone repo | Month 8 | Clear reference for implementors |
| "Awesome Runtime Policy Control" list | Month 10 | Aggregation point for ecosystem |
| Certification program (badge) | Month 14 | "RPC-compliant" badge on README |
| Reference implementation in Go | Month 16 | Expand beyond Python |
| Reference implementation in TypeScript | Month 18 | JS/TS ecosystem coverage |

**When a competitor ships "policy control"**: This is a WIN, not a threat. When LangSmith adds "budget limits," they validate the category. VERONICA wins because:
1. VERONICA defined the specification they're implementing
2. VERONICA is policy-native; they're bolting it onto observability
3. The specification is Apache-2.0 -- they must credit it

**The Styra vs Snyk lesson** (critical for VERONICA):
- Styra (OPA, Policy as Code): 9 years, $15M ARR. **Infrastructure positioning.**
- Snyk (DevSecOps): 9 years, $344M revenue. **Security positioning.**
- Same age. Same category creation strategy. 23x revenue difference.
- **Root cause**: Styra positioned as infrastructure plumbing. Snyk positioned as security (CISO budget, compliance requirement, fear-driven purchase).
- **VERONICA must be Snyk, not Styra.** Vocabulary, buyer persona, pricing, and content must all signal "security" not "developer convenience."

### 4.6 Phase 5: Category Ownership (Month 18-36)

**Category is "owned" when**:
- [ ] "Runtime Policy Control" returns 10,000+ Google results
- [ ] 3+ competitor READMEs use the term
- [ ] 1+ analyst report (Gartner/Forrester) mentions the category
- [ ] CNCF Landscape lists "Runtime Policy Control" as a subcategory
- [ ] Stack Overflow questions tagged [runtime-policy-control]
- [ ] "What is Runtime Policy Control?" has a Wikipedia/developer-wiki entry

### 4.7 Term Penetration Method

**Repetition in every touchpoint**:

| Touchpoint | Term Usage |
|-----------|-----------|
| PyPI | "Runtime Policy Control for LLM execution" |
| npm (future) | "Runtime Policy Control for LLM execution" |
| GitHub Topics | `runtime-policy-control`, `llm-safety`, `budget-enforcement` |
| README | "VERONICA is a Runtime Policy Control SDK" |
| Every PR description | "Adds Runtime Policy Control via VERONICA" |
| Every blog post title | Contains "Runtime Policy Control" |
| Every conference talk | Defines "Runtime Policy Control" in first 2 minutes |
| Slack/Discord community | Channel name: `#runtime-policy-control` |
| Specification name | "LLM Runtime Policy Control Specification" |
| CNCF submission | Category: "Runtime Policy Control" |

**SEO strategy**: Own the term before anyone else writes about it. First 10 blog posts that define "Runtime Policy Control" should all link back to VERONICA.

---

## Section 5: Revenue and Exit

### 5.1 Pricing (2 Tiers Only)

| | **Free** | **Pro** |
|--|----------|---------|
| **Price** | $0 | $49/month |
| **Org Budget** | 1 budget, $100 ceiling | Unlimited budgets, any ceiling |
| **Events** | 10,000/month | 500,000/month |
| **Kill Switch** | 1/day | Unlimited |
| **Fleet Policy Sync** | Basic (budget only) | Full (all policy types) |
| **Webhook Alerts** | None | Slack, Discord, custom |
| **Retention** | 7 days | 30 days |
| **Support** | GitHub Issues | Email (48h SLA) |

**Enterprise tier ships when** MRR > $10K AND 3+ inbound requests with named budgets. Not before.

Enterprise ($499/month, later):
- SSO (Okta/Auth0)
- SLA (99.9% uptime)
- Audit log API
- Custom retention (90 days)
- Dedicated support channel
- SOC2 compliance report

### 5.2 Security Positioning Reinforcement

VERONICA must be perceived as **security infrastructure**, not developer tooling. This determines M&A multiples.

**Security = loss prevention. VERONICA prevents financial loss.**

| Positioning | Perception | M&A Multiple | Example |
|-------------|-----------|--------------|---------|
| "DevTool" | Convenience | 5-8x ARR | Prettier, ESLint |
| "Observability" | Visibility | 7-10x ARR | New Relic (8.1x), Splunk (7.0x) |
| "Infrastructure" | Plumbing | 10-15x ARR | HashiCorp (10.7x) |
| **"Security/Enforcement"** | **Loss prevention** | **25-64x ARR** | **Wiz (64x), CyberArk (25x)** |

**How to reinforce security positioning**:

1. **Vocabulary**: Always say "policy enforcement" not "budget management." Say "runtime protection" not "cost tracking." Say "threat mitigation" not "monitoring."

2. **Compliance mapping**: Map VERONICA primitives to SOC2/ISO 27001 controls:
   - Budget Enforcer -> SOC2 CC6.1 (Logical and Physical Access Controls)
   - Kill Switch -> SOC2 CC7.4 (Incident Response)
   - Circuit Breaker -> SOC2 CC8.1 (Change Management, graceful degradation)

3. **Security-adjacent content**: Blog posts titled "Preventing LLM Cost Breaches," not "Managing LLM Budgets."

4. **Buyer persona**: Sell to CISO/Security team, not just engineering manager. Security budgets are 10x developer tool budgets.

5. **Certifications**: SOC2 Type II (Year 2-3). Signals "we are security infrastructure."

### 5.3 NDR Strategy: Free -> Pro -> Enterprise -> Expansion

**Target: NDR 120%+**

NDR = (Revenue from existing customers at end of period) / (Revenue from those same customers at start of period)

**Industry benchmarks** (verified 2024-2025):

| Company | NDR/NRR | Period | Source |
|---------|---------|--------|--------|
| Snowflake | 131% | FY 2024 | Nasdaq |
| Snowflake | 138% | IPO | June.so |
| GitLab | 123% | Q4 FY 2025 | GitLab IR |
| Datadog | Mid-110%s | 9M 2024 | Nasdaq |
| Twilio | 155% | IPO | June.so |
| **Median B2B SaaS** | **106%** | 2024 | Wudpecker |
| **75th percentile** | **111%** | 2023 | High Alpha |
| **Top quartile DevTools** | **120%+** | 2025 | Burkland |
| **$1M-$10M ARR (early stage)** | **98%** | | High Alpha |
| **$100M+ ARR** | **115%** | | High Alpha |

**NDR impact on valuation multiples** (verified 2024-2025, SaaS Rise M&A Report):

| NDR Range | Median ARR Multiple | Source |
|-----------|---------------------|--------|
| Industry median | 5.6x | SaaS Rise 2025 |
| **NDR > 120%** | **11.7x** (2x industry) | SaaS Rise 2025 |
| Private SaaS typical | 4x-8x | Jackim Woods 2025 |
| Gross margin >80% | 7.6x | SaaS Rise Q4 2024 |
| Gross margin <80% | 5.5x | SaaS Rise Q4 2024 |

**Each 1% monthly churn reduction = +15-25% valuation increase** (PayPro Global).

**Key insight**: NDR alone gives 11.7x (not 30x). The 30x requires **NDR + security positioning + category ownership + hypergrowth**. See Section 5.4.

**VERONICA's expansion path per customer**:

```
Month 1:  Free ($0)         -- Single dev, testing with 1 chain
Month 3:  Pro ($49)         -- Team decides: "we need this in production"
Month 8:  Pro ($49)         -- Multiple services, Pro covers it
Month 12: Pro x2 ($98)      -- Second team onboards, needs separate org
Month 18: Enterprise ($499) -- SOC2 audit demands documented policy enforcement
Month 24: Enterprise ($499) -- Stable renewal. Lock-in via 6+ integrations.
```

**Expansion triggers** (each increases ARPU):

| Trigger | Mechanism | Revenue Impact | Probability |
|---------|-----------|----------------|-------------|
| **More services** | Each microservice needs budget policy | +$49/service | 40% of Pro customers |
| **More teams** | Each team wants own policy org | +$49/team | 25% of Pro customers |
| **Compliance** | SOC2 requires documented enforcement | Pro -> Enterprise ($499) | 10% of Pro/year |
| **Incident** | Cost breach triggers emergency purchase | Free -> Pro (same day) | 5% of Free/month |
| **Multi-region** | EU data residency, separate org | +$49-499/region | 5% of Enterprise |
| **Custom policies** | Enterprise needs custom policy types | Pro -> Enterprise | 8% of Pro/year |

**NDR math** (100-customer Pro cohort, 12-month period):

| Flow | Customers | Revenue Change |
|------|-----------|---------------|
| Start: 100 Pro @ $49 | 100 | $4,900/mo |
| Churn (8%) | -8 | -$392/mo |
| Downgrade (2%) | -2 | -$98/mo |
| Stay Pro | 60 | $0 |
| Expand (add org) | 12 | +$588/mo |
| Upgrade Enterprise | 8 | +$3,600/mo |
| Expand Enterprise (multi-region) | 2 | +$998/mo |
| **End** | **92 (+ 8 Enterprise)** | **$9,598/mo** |
| **NDR** | | **$9,598 / $4,900 = 196%** |

This is optimistic. Conservative scenario (4% churn, 5% Enterprise upgrade, 10% expansion):

| Conservative | Revenue |
|--------------|---------|
| Start | $4,900/mo |
| Churn (4%) | -$196 |
| Expansion (10% add org) | +$490 |
| Enterprise upgrade (5%) | +$2,250 |
| **End** | **$7,444/mo** |
| **NDR** | **152%** |

Even conservatively, NDR > 120% is achievable because:
1. **Enterprise tier is 10x Pro** ($499 vs $49). Even 5% conversion = massive expansion.
2. **Integration stickiness** reduces churn below industry average (more frameworks = harder to rip out).
3. **Incident-driven conversion** is free acquisition (the $12K weekend sells itself).

**Pricing model insight** (Monetizely 2025 benchmark):
- **Usage-based pricing** is 23% more likely to achieve NDR >120% than pure flat pricing.
- VERONICA's Pro is flat ($49), but **expansion is usage-driven** (more orgs, more events). This hybrid model captures the NDR benefit of usage-based without the billing complexity.
- 80% of enterprise SaaS customers report better value alignment with usage-based elements (BetterCloud 2025).

**Churn reduction tactics** (target: <8% annual for Pro, <3% for Enterprise):

| Tactic | Mechanism | Estimated Impact |
|--------|-----------|-----------------|
| Framework integration depth | 3+ framework integrations = 2x stickier | -3% churn |
| Policy-as-code definitions | Policies defined in YAML, version-controlled | -2% churn (data lock-in) |
| Slack/Discord alerts | Daily visibility = daily reminder of value | -1% churn |
| Annual billing discount | $470/year vs $588 ($49x12) = 20% off | -2% churn (commitment) |

**DevTools churn benchmarks**: Typical SaaS at $49/month = 5-10% monthly churn. Infrastructure/security tools = 2-5% annual churn (they're embedded in CI/CD and can't be easily removed). VERONICA's target: infrastructure-grade stickiness.

### 5.4 ARR $500K at 30x: The Narrative Design

**Target**: ARR $500K. Valuation $15M (30x). Timeline: Month 24-30.

**Revenue composition at $500K ARR**:

| Tier | Customers | ARPU/month | MRR Contribution | % of MRR |
|------|-----------|-----------|------------------|----------|
| Free | 10,000 | $0 | $0 | 0% |
| Pro | 600 | $49 | $29,400 | 71% |
| Enterprise | 24 | $499 | $11,976 | 29% |
| **Total** | **10,624** | | **$41,376 MRR ($496K ARR)** | 100% |

**Path to $500K ARR** (month-by-month):

| Month | Free | Pro | Enterprise | MRR | ARR | Event |
|-------|------|-----|-----------|-----|-----|-------|
| 3 | 200 | 5 | 0 | $245 | $2.9K | HN launch |
| 6 | 800 | 20 | 0 | $980 | $11.8K | LiteLLM merge |
| 9 | 1,500 | 50 | 1 | $2,949 | $35.4K | Cloud GA |
| 12 | 3,000 | 100 | 3 | $6,397 | $76.8K | 5 integrations |
| 15 | 5,000 | 200 | 8 | $13,792 | $165.5K | First conference talk |
| 18 | 7,000 | 350 | 14 | $24,136 | $289.6K | Spec v1.0 |
| 21 | 8,500 | 480 | 20 | $33,500 | $402K | CNCF listed |
| 24 | 10,000 | 600 | 24 | $41,376 | **$496K** | Category established |

**Conversion funnel**: Free -> Pro = 5-6% (industry: 2-5%). Pro -> Enterprise = 4% (incident/compliance driven).

**Why 30x is achievable (reality check included)**:

**Base case** (SaaS M&A data 2024-2025):
- Private SaaS typical: **4-8x ARR** (Jackim Woods 2025)
- NDR > 120%: **11.7x median** (SaaS Rise 2025)
- 30x requires: security positioning + NDR 120%+ + growth >100% YoY + category ownership

**Multiplier stack** (how 11.7x becomes 30x):

| Factor | Base Multiple | Multiplier | Result | Evidence |
|--------|-------------|-----------|--------|----------|
| NDR > 120% | 11.7x | 1.0x (baseline) | 11.7x | SaaS Rise 2025 median |
| + Security positioning | | 1.5-2.0x | 17.5-23.4x | Security tools: CyberArk 25x, Wiz 64x vs Observability 7-10x |
| + Category creator | | 1.2-1.5x | 21-35x | Snyk (created DevSecOps) 24x, Auth0 (created developer AuthZ) 32x |
| + OSS ecosystem moat | | 1.1-1.3x | 23-45x | HashiCorp OSS -> $6.4B (10.7x, but infrastructure, not security) |

**Conservative estimate**: 11.7x (NDR) x 1.5x (security) x 1.2x (category) = **21x = $10.5M**
**Optimistic estimate**: 11.7x x 2.0x x 1.5x x 1.2x = **42x = $21M**
**Target**: **30x = $15M** (midpoint)

**Critical caveat**: 30x+ multiples (Wiz 64x, Auth0 32x) were at $200M-$500M ARR, not $500K. At $500K ARR, the acquisition is strategic (buy the category/team), not financial (buy the revenue). Strategic acquisitions can command premiums that don't follow ARR multiple logic -- Datadog paid $100M+ for Sqreen at <$10M ARR because they needed security capability.

**The narrative acquirers need to hear**:

> "VERONICA created the Runtime Policy Control category. They own the specification.
> Their SDK is integrated into 6 frameworks with 200K+ monthly downloads.
> NDR is 125%. They ARE the standard. Building would take 2 years and wouldn't have
> the ecosystem. Buying gives us the category, the standard, and the community."

**Comparable precedents (category creators, early-stage)**:

| Company | What They Created | Exit / Valuation | Multiple | Key Differentiator |
|---------|-------------------|------------------|----------|-------------------|
| Styra (OPA) | "Policy as Code" | $67.5M raised, pre-exit | N/A | CNCF standard ownership |
| Auth0 | Developer-friendly AuthZ | Okta $6.5B (32x ARR) | 32x | Developer adoption + enterprise |
| Vanta | Continuous compliance | $4.15B valuation ($220M ARR) | 19x | Compliance enforcement SaaS |
| Snyk | Developer security | $7.4B peak valuation | 24x | OSS + security positioning |
| Wiz | Cloud security posture | Google $32B ($500M ARR) | 64x | Security + hyper-growth |

VERONICA's unique advantages over all of these:
1. **MIT OSS distribution** (Vanta has no OSS. Auth0 had limited OSS.)
2. **Zero competition** (All others had competitors from day 1. VERONICA has none.)
3. **Security positioning** (Snyk/Wiz multiples, not Datadog/New Relic multiples)
4. **Specification ownership** (OPA-level standard, not just a product)

### 5.5 Alternative: Stay Bootstrapped

| Year | MRR | ARR | Team | Net Profit |
|------|-----|-----|------|------------|
| 2 | $41K | $496K | 2 | $350K |
| 3 | $80K | $960K | 4 | $500K |
| 4 | $150K | $1.8M | 6 | $800K |
| 5 | $250K | $3.0M | 8 | $1.2M |

At $3M ARR with 8 people, the company is highly profitable. Founder retains 100%.

**Decision framework**:
- Offer < $10M: Reject.
- Offer $10-20M: Consider if acquirer provides massive distribution (e.g., Datadog integration).
- Offer > $20M: Serious evaluation. $20M post-tax = ~$14M.
- Offer > $50M: Accept unless ARR trajectory clearly reaches $5M+ within 2 years.

### 5.6 Exit Scenarios by Acquirer Type

| Acquirer | Why VERONICA | Price Signal | Trigger |
|----------|-------------|-------------|---------|
| **Datadog** | Completes LLM Observability stack with enforcement layer | $30-80M | When they launch "LLM Runtime Controls" and it fails |
| **Wiz/Palo Alto** | LLM security = next attack surface. Policy control = defensive layer | $50-100M | When first LLM cost breach makes headlines |
| **LangChain** | Native policy control = premium enterprise feature | $15-40M | When enterprise customers demand governance |
| **AWS (Bedrock)** | Managed policy control for Bedrock customers | $40-100M | When 3+ Bedrock enterprise customers request it |
| **Salesforce** | Agent-level budget enforcement for Einstein agents | $30-60M | When Einstein agent costs spiral |
| **CrowdStrike** | AI runtime protection (extension of endpoint security) | $40-80M | When AI-specific threats emerge |

---

## Appendix A: Competitive Positioning

**Category map (VERONICA's position)**:

```
                    Passive                    Active
                  (observe)                  (control)
                     |                          |
Pre-execution   Config tools               Guardrails
                (DSPy, prompts)            (NeMo Guardrails)
                     |                          |
During          Observability          ** Runtime Policy **
execution       (LangSmith,            ** Control         **
                 Helicone)             ** (VERONICA)       **
                     |                          |
Post-execution  Analytics              Compliance
                (cost reports)          (Vanta, Drata)
```

**One-liner**: "LangSmith traces. VERONICA enforces."

**Elevator pitch** (30 seconds):
Every LLM observability tool records what happened. None of them stop it from happening. VERONICA is Runtime Policy Control -- budget ceilings, step limits, circuit breakers, and kill switches for your LLM fleet. Zero dependencies, MIT licensed, works with every framework. The category didn't exist until now.

## Appendix B: SDK Interface Evolution Path

```
v0.1 (NOW):     4 concrete classes (BudgetEnforcer, AgentStepGuard, etc.)
                 Direct instantiation. No abstraction.

v0.2 (Month 3):  Add RuntimePolicy protocol.
                 Existing classes implement it. Backward compatible.
                 Add PolicyPipeline for composition.

v0.3 (Month 6):  Add PolicyContext with extensible metadata.
                 Framework adapters use PolicyContext.

v1.0 (Month 12): Stable API. RuntimePolicy protocol is the public interface.
                 Concrete classes are "reference implementations."
                 Third-party policies can plug in.
```

**Migration guarantee**: v0.1 API (`BudgetEnforcer(limit_usd=100)`, `budget.spend(cost)`) will work forever. The `RuntimePolicy` protocol is additive, not breaking.

## Appendix C: Glossary (Term Control)

| Term | Definition | Why This Term |
|------|-----------|---------------|
| **Runtime Policy Control** | The enforcement of operational constraints on LLM execution during the call lifecycle | Category name. Must appear in every external communication. |
| **Policy** | A stateful constraint that returns allow/deny | Borrows authority from "Policy as Code" (OPA). |
| **Policy Pipeline** | Sequential evaluation of multiple policies. First denial wins. | Implies composability and order. |
| **Fleet Control** | Org-level policy enforcement across multiple processes | Implies scale. "Fleet" > "organization." |
| **Kill Switch** | Unconditional, immediate halt of all execution | Clear, visceral, memorable. |
| **SAFE_MODE** | Persistent halt flag that survives process restart | Familiar from hardware (safe mode). |
| **Policy Decision** | The output of a policy evaluation: `{allowed: bool}` | Neutral, technical. Not "block" (negative). |
| **Runtime Protection** | Security-oriented synonym for Runtime Policy Control | Use in security/compliance contexts. |

**Terms to NEVER use**:
- "Monitoring" (implies passive)
- "Tracking" (implies observation)
- "Analytics" (implies dashboards)
- "Management" (implies CRUD UI)
- "Governance" (too abstract, too enterprise)
