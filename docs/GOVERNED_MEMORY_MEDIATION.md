# Governed Memory Mediation

veronica-core governs memory operations. It does not implement memory storage,
retrieval, consolidation, or archival. Those responsibilities belong to the
memory layer (e.g., TriMemory).

---

## Responsibility Boundary

```
veronica-core (kernel)              Memory Layer (e.g., TriMemory)
-----------------------------       ----------------------------
Memory operation governance         Storage engine
View-based access policy            Retrieval engine
Compactness constraints             Consolidation logic
DEGRADE directive emission          Compact packet construction
Message governance hooks            Message delivery
Bridge promotion policy             Archive database
Threat-aware audit trail            Content indexing
Execution mode scoping              Replay engine
```

The kernel exposes governance types and hooks. The memory layer consumes them.
The kernel never imports from the memory layer.

---

## Memory Mediation

Memory mediation is the process of evaluating, constraining, and transforming
memory operations before they reach the storage layer.

Raw memory content (retrieved chunks, archived events, tool traces) must not
flow directly into the main execution path. The mediation pipeline:

1. Operation submitted (MemoryOperation)
2. Governance hooks evaluate (MemoryGovernor)
3. Verdict: ALLOW, DENY, DEGRADE, QUARANTINE
4. If DEGRADE: DegradeDirective specifies transformation
5. Memory layer applies transformation and delivers result

---

## DEGRADE Semantics

DEGRADE is not a soft deny. It is a correction-bearing permit.

When a governance hook returns DEGRADE, it attaches a DegradeDirective
that specifies HOW the operation should be modified:

| Directive Field | Meaning |
|----------------|---------|
| mode | Degradation hint: compact, redact, truncate, downscope |
| max_packet_tokens | Token ceiling for output packet |
| allowed_provenance | Only these provenance values may pass |
| verified_only | Restrict to verified content |
| summary_required | Raw content must be summarized |
| raw_replay_blocked | Raw replay denied; compact form required |
| namespace_downscoped_to | Use safer namespace instead of requested |
| redacted_fields | Fields to strip before delivery |
| max_content_size_bytes | Byte ceiling for content payload |

Examples of DEGRADE in practice:
- Raw replay is denied, but a compact packet is allowed
- Unverified fields are dropped, verified fields pass
- Large packet is truncated to token budget
- Requested namespace is downscoped to a safer one
- Retrieve is allowed, but archive is denied

Multiple hooks can return DEGRADE. The governor merges directives:
- Boolean fields: OR (True wins)
- Integer fields: max of non-zero values
- Float fields: min of non-1.0 values (stricter wins for ratios)
- Tuple fields: set union (sorted for determinism)
- String fields: last non-empty value wins

---

## Compactness Policy

Compactness constraints govern packet size and content shape.

| Parameter | Description |
|-----------|-------------|
| max_packet_tokens | Maximum tokens in response packet |
| max_raw_replay_ratio | Fraction of raw content vs compact |
| require_compaction_if_over_budget | Force compaction on budget overflow |
| prefer_verified_summary | Prefer verified summary over raw unverified |
| max_attributes_per_packet | Attribute count limit |
| max_payload_bytes | Byte size limit |

The CompactnessEvaluator checks these constraints and returns DENY for hard
limits (payload bytes) or DEGRADE with appropriate directives for soft limits.

Evaluation order within CompactnessEvaluator:

1. No constraints in context and no defaults -- ALLOW immediately.
2. max_payload_bytes exceeded -- DENY (hard limit).
3. max_packet_tokens exceeded -- DEGRADE with summary_required via compact mode.
4. max_attributes_per_packet exceeded -- DEGRADE.
5. raw_replay_ratio exceeds max_raw_replay_ratio -- DEGRADE with raw_replay_blocked.
6. require_compaction_if_over_budget and any limit exceeded -- DEGRADE with summary_required.
7. prefer_verified_summary and provenance != VERIFIED -- DEGRADE with verified_only.

When multiple soft limits trigger, their directives are merged into a single
DegradeDirective before returning.

---

## Memory Views

Memory access is not a single global state read. Views partition memory
into governed regions:

| View | Description |
|------|-------------|
| agent_private | Owner agent only |
| local_working | Any trust level, current session |
| team_shared | Provisional+ read, trusted+ write |
| session_state | Trusted+ read; privileged write (consolidation: trusted+) |
| verified_archive | Trusted+ read-only (consolidation mode: trusted+ write) |
| provisional_archive | Provisional+ read, trusted+ write |
| quarantined | Privileged read-only (audit_review: trusted+) |

The ViewPolicyEvaluator combines memory_view, trust_level, execution_mode,
and agent identity to determine access. When context is None, defaults are
LOCAL_WORKING view, LIVE mode, and untrusted trust level.

---

## Scoped Execution Modes

Runtime mode determines memory access posture:

| Mode | Read scope | Write scope |
|------|-----------|-------------|
| live_execution | agent_private, local_working; untrusted denied verified_archive | agent_private, local_working |
| replay | all views (broader read) | denied |
| simulation | all views | provisional only (verified_archive and session_state denied) |
| consolidation | all views | copy-on-write; trusted+ may write verified_archive and session_state |
| audit_review | quarantined + verified (trusted+) | denied |

Default mode is live_execution (most conservative).
Fail-closed: unknown views return DENY.

---

## Message Governance

Multi-agent systems produce messages that may enter the memory layer.
Message governance hooks evaluate messages before delivery or archival.

```
Agent A --[message]--> MessageGovernanceHook --> Agent B
                              |
                              +--> Memory Layer (if bridge policy permits)
```

Hooks available:
- DefaultMessageGovernanceHook: fail-open, allows all messages
- DenyOversizedMessageHook: DENY above max_bytes; DEGRADE above degrade_threshold fraction
- MessageBridgeHook: controls message-to-memory promotion via BridgePolicy

MessageGovernanceHook is a separate protocol from MemoryGovernanceHook. Message
hooks receive MessageContext; memory hooks receive MemoryOperation. They are
evaluated in separate pipelines.

---

## Message-to-Memory Bridge

Messages do not automatically become memory. The BridgePolicy controls
promotion:

| Field | Description |
|-------|-------------|
| allow_archive | Whether archiving is permitted at all |
| require_signature | Require verified provenance for archive eligibility |
| max_promotion_level | Highest view a message can reach |
| quarantine_untrusted | Route untrusted to quarantined view |
| write_once_scratch | Allow temporary scratch writes |

Promotion examples:
- Agent-to-agent chat enters provisional_archive only
- Tool result with VERIFIED provenance is eligible for verified_archive
- Untrusted message (trust_level="" or "untrusted") is quarantined when quarantine_untrusted is True
- Message with unrecognized type is denied when allowed_message_types is configured

MessageBridgeHook evaluation order:
1. allow_archive=False -- DENY
2. require_signature and provenance != VERIFIED -- DENY
3. message_type not in allowed_message_types -- DENY
4. quarantine_untrusted and trust_level in ("untrusted", "") -- QUARANTINE
5. Otherwise -- ALLOW

---

## Threat-Aware Audit

Governance decisions carry ThreatContext so post-hoc audit can reconstruct
the decision rationale:

| Field | Description |
|-------|-------------|
| threat_hypothesis | What threat this decision guards against |
| mitigation_applied | What action was taken |
| degrade_reason | Why degradation was chosen |
| degraded_fields | Which fields were modified |
| effective_scope | Scope that was actually applied |
| effective_view | View that was actually used |
| compactness_enforced | Whether compaction was applied |
| source_trust | Trust level at decision time |
| source_provenance | Provenance of source content |

ThreatContext is attached to MemoryGovernanceDecision. When the verdict is
ALLOW, threat_context is None by default. When the verdict is DEGRADE or
QUARANTINE, threat_context propagates from the worst-verdict hook.
The to_audit_dict() method serializes all ThreatContext fields under
threat_* prefixed keys for flat log emission.

---

## Data Flow

```
Agent Request
    |
    v
MemoryOperation(action, agent_id, namespace, provenance, content_size_bytes)
    |
    v
MemoryPolicyContext(operation, trust_level, memory_view, execution_mode, compactness)
    |
    v
MemoryGovernor.evaluate()
    |
    +---> CompactnessEvaluator.before_op()
    +---> ViewPolicyEvaluator.before_op()
    +---> MemoryBoundaryHook.before_op()
    +---> [custom hooks...]
    |
    |  First DENY terminates evaluation immediately (fail-closed).
    |  Hook exception treated as DENY (fail-closed).
    |  QUARANTINE > DEGRADE > ALLOW (worst non-DENY verdict wins).
    |  DEGRADE directives from all DEGRADE hooks are merged.
    |
    v
MemoryGovernanceDecision(verdict, degrade_directive, threat_context)
    |
    +---> ALLOW: proceed to memory layer
    +---> DENY: reject, emit audit
    +---> DEGRADE: proceed with directive, memory layer applies transformation
    +---> QUARANTINE: proceed, mark for review
    |
    v
MemoryGovernor.notify_after()
    (calls after_op on all hooks, never raises)
```

---

## MemoryBoundaryHook Integration

MemoryBoundaryHook implements both PostDispatchHook (shield pipeline) and
MemoryGovernanceHook (governor pipeline). It provides two integration paths:

**PostDispatchHook path**: intercepts memory_read and memory_write tool calls
from ToolCallContext metadata after LLM response. Raises PermissionError on deny.

**MemoryGovernanceHook path**: called by MemoryGovernor.evaluate() for READ
and WRITE actions. Other actions (RETRIEVE, ARCHIVE, etc.) pass through as ALLOW.

Trust-level isolation (when trust_tracker and trusted_namespaces are configured):

| Trust Level | Trusted Namespace Access |
|-------------|--------------------------|
| UNTRUSTED | deny read and write |
| PROVISIONAL | allow read, deny write |
| TRUSTED | full access, fall through to rules |
| PRIVILEGED | full access, fall through to rules |
| None (unknown) | deny (fail-closed) |

Rule specificity scoring (highest score wins when multiple rules match):
- Exact agent_id + exact namespace: score 3
- Exact agent_id + wildcard namespace: score 2
- Wildcard agent_id + exact namespace: score 1
- Wildcard agent_id + wildcard namespace: score 0
- No match: default_allow determines outcome

---

## Governor Aggregation Rules

When no hooks are registered:
- fail_closed=True (default): DENY
- fail_closed=False: ALLOW

During hook chain evaluation:
- First DENY from any hook: stops evaluation, returns DENY
- Hook raises exception: treated as DENY (fail-closed)
- Unknown verdict returned by hook: treated as DENY (fail-closed)
- QUARANTINE / DEGRADE: accumulate (worst verdict propagates: QUARANTINE > DEGRADE > ALLOW)
- DegradeDirective: merged from all hooks that returned DEGRADE
- ThreatContext: taken from the hook that produced the worst verdict

Maximum registered hooks: 100. Exceeding this raises RuntimeError.

---

## What veronica-core Does NOT Do

- Does not store or retrieve memory content
- Does not implement consolidation or archival logic
- Does not construct compact packets (emits directives; memory layer constructs)
- Does not deliver messages (hooks gate; delivery is external)
- Does not implement replay or simulation engines
- Does not manage memory indices or embeddings

These are memory layer responsibilities. veronica-core provides the governance
surface -- types, hooks, evaluators, audit -- so that a memory layer can be
governed deterministically.

---

## TriMemory Bridge

A TriMemory integration would:

1. Wrap memory operations in MemoryOperation
2. Submit to MemoryGovernor.evaluate()
3. Read DegradeDirective to apply compaction/redaction
4. Respect view boundaries from ViewPolicyEvaluator
5. Apply CompactnessConstraints to packet construction
6. Use MessageBridgeHook for message-to-memory promotion
7. Emit audit events with ThreatContext
