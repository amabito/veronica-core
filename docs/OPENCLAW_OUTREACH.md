# OpenClaw Outreach Templates — Maintainer Communication

Templates for reaching out to OpenClaw maintainers with integration proposal.

---

## Principles

1. **Respectful**: Acknowledge OpenClaw's excellence
2. **Concise**: Keep initial contact short (< 200 words)
3. **Non-pushy**: Offer value, don't demand attention
4. **Collaborative**: Frame as partnership, not competition
5. **Professional**: Provide proof, not just claims

---

## Initial Contact (Twitter/X DM)

### Version 1: Technical Focus (< 150 words)

```
Hi [Name],

I built VERONICA Core — a failsafe state machine for autonomous systems.

It complements OpenClaw's decision-making with execution safety (circuit breakers, emergency halt, crash recovery). Battle-tested: 1000+ ops/sec, 2.6M operations, 0 data loss.

Created an integration kit showing how VERONICA can wrap OpenClaw strategies with production-grade safety guarantees. Non-breaking, optional dependency, ~100 LOC.

Would you be interested in reviewing for potential integration? Happy to adjust based on OpenClaw's architecture preferences.

Integration demo: [short link]
Destruction test proof: [short link]

No pressure — if it's not a fit, totally understand. Just wanted to share in case it helps OpenClaw users deploy to production.

Thanks for building OpenClaw!
[Your name]
```

### Version 2: Value Proposition Focus (< 150 words)

```
Hi [Name],

OpenClaw users asked me about production safety (circuit breakers, crash recovery). Built VERONICA Core to handle this layer separately from strategy logic.

Designed an optional integration showing how VERONICA complements OpenClaw:
- OpenClaw decides *what* to do (decision quality)
- VERONICA enforces *how* to execute safely (circuit breakers, emergency halt)

Non-breaking, zero overhead by default, optional dependency. ~100 LOC integration.

Integration kit ready: [short link]
Proof of guarantees: [short link]

Would you be open to reviewing? Happy to adjust based on your feedback.

If not a fit, no worries — just wanted to offer in case it helps production deployments.

Appreciate your work on OpenClaw!
[Your name]
```

### Version 3: Community-Driven (< 150 words)

```
Hi [Name],

Built VERONICA Core for production failsafe (circuit breakers, crash recovery). Several users asked about OpenClaw integration.

Created integration kit showing how to wrap OpenClaw strategies with safety layer. Non-breaking, optional, ~100 LOC.

Thought you might be interested in reviewing for official integration:
- Demo: [short link]
- Proof: [short link]

Happy to:
- Adjust to fit OpenClaw's architecture
- Maintain integration code separately if you prefer
- Answer questions about design decisions

No expectations — just offering in case it's useful for OpenClaw's production users.

Thanks for building an excellent strategy framework!
[Your name]
```

**Recommended**: Version 1 (technical focus, shows proof immediately)

---

## Initial Contact (GitHub Issue)

### Title
```
[Discussion] Optional safety layer integration (circuit breakers, crash recovery)
```

### Body

```markdown
## Summary

VERONICA Core is a production-grade failsafe state machine built to complement strategy engines like OpenClaw.

I've created an integration kit showing how VERONICA can wrap OpenClaw strategies with execution safety guarantees (circuit breakers, emergency halt, crash recovery). Thought it might be valuable for OpenClaw users deploying to production.

**Key points**:
- Non-breaking (existing code works unchanged)
- Optional dependency (not required)
- Zero overhead by default (no-op safety layer)
- ~100 LOC integration

## Integration Demo

[Link to integrations/openclaw/demo.py]

Demonstrates:
- Circuit breaker activation on repeated failures
- SAFE_MODE persistence across restart
- Crash recovery (state survives SIGKILL)

## Proof of Guarantees

[Link to docs/PROOF.md]

Reproducible destruction tests showing:
- SAFE_MODE persistence (emergency halt survives restart)
- SIGKILL survival (cooldown state persists through hard kill)
- SIGINT graceful exit (Ctrl+C saves state atomically)

Production metrics: 30 days uptime, 1000+ ops/sec, 2.6M operations, 0 data loss.

## Question

Would you be interested in reviewing this for potential integration into OpenClaw?

I'm happy to:
- Adjust to fit OpenClaw's architecture preferences
- Maintain integration code separately if you prefer
- Answer questions about design decisions

If it's not a fit, no problem — just wanted to offer in case it helps production deployments.

## Why This Matters

OpenClaw excels at decision-making. VERONICA adds execution safety:
- **Runaway execution**: Circuit breaker blocks after N consecutive fails
- **Crash recovery loops**: SAFE_MODE persists across restart (no auto-recovery)
- **Lost state**: Atomic persistence survives SIGKILL

**Design philosophy**: OpenClaw decides *what* to do. VERONICA enforces *how* to execute safely.

## Links

- VERONICA Core: https://github.com/amabito/veronica-core
- Integration guide: [link to integrations/openclaw/README.md]
- PR template: [link to docs/OPENCLAW_PR_TEMPLATE.md]
- Patch guide: [link to integrations/openclaw/PATCH.md]

Thanks for considering!
```

---

## Follow-Up (If No Response After 1 Week)

### Version 1: Gentle Reminder (< 100 words)

```
Hi [Name],

Following up on my previous message about VERONICA integration.

I know you're busy — no worries if it's not a priority right now.

If you'd prefer I maintain this integration externally (separate repo), happy to do that instead. Just wanted to check if official integration was of interest.

Either way, appreciate your work on OpenClaw!

[Your name]
```

### Version 2: Offer Alternative (< 100 words)

```
Hi [Name],

Re: VERONICA integration proposal.

If official integration isn't a fit, I can:
1. Maintain externally (separate repo)
2. Link from VERONICA docs to OpenClaw (credit your work)
3. Support OpenClaw users who want safety layer

No pressure on your side. Just wanted to offer options.

Thanks again for OpenClaw!

[Your name]
```

**Recommended**: Version 2 (shows flexibility, removes pressure)

---

## If Declined (Graceful Exit)

### Version 1: Acknowledge Decision (< 75 words)

```
Hi [Name],

Thanks for taking the time to review!

Totally understand — not every integration is a good fit.

I'll maintain VERONICA integration externally and link to OpenClaw docs (with credit).

If you ever want to revisit, or if OpenClaw users ask about it, feel free to reach out.

Appreciate your consideration!

[Your name]
```

### Version 2: Offer Future Collaboration (< 75 words)

```
Hi [Name],

No problem — thanks for considering!

I'll keep VERONICA integration available for users who want it (external repo).

If OpenClaw's roadmap changes, or if you want to collaborate on production safety in the future, I'm happy to discuss.

Thanks for building OpenClaw — it's excellent work!

[Your name]
```

**Recommended**: Version 1 (clean exit, leaves door open)

---

## If Interested (Next Steps Response)

### Version 1: PR Readiness (< 100 words)

```
Hi [Name],

Great to hear you're interested!

I have a PR ready with:
- Integration code (~100 LOC)
- Unit tests
- Documentation
- Examples

PR template: [link to docs/OPENCLAW_PR_TEMPLATE.md]

Should I submit PR now, or would you prefer to review integration kit first?

Also happy to:
- Adjust API to fit OpenClaw's style
- Add more tests
- Update documentation format

What works best for your review process?

[Your name]
```

### Version 2: Collaborative Approach (< 100 words)

```
Hi [Name],

Excited to collaborate!

Before submitting PR, would you prefer:
1. Review integration kit first (integrations/openclaw/)
2. Discuss API changes (if needed)
3. Walk through design decisions (call/video)

I want to make sure it fits OpenClaw's architecture cleanly.

Available for:
- Adjusting to your code style
- Adding requested tests
- Updating based on feedback

What would be most helpful?

[Your name]
```

**Recommended**: Version 2 (shows collaboration, not just code dump)

---

## Answering Common Questions

### Q: "Why not build this into OpenClaw directly?"

```
Great question!

Separation of concerns:
- OpenClaw should focus on decision quality (your core strength)
- VERONICA focuses on execution safety (our core strength)

Mixing concerns leads to:
- Bloated strategy engines (harder to optimize/test)
- Duplicated effort (every engine reimplements circuit breakers)

With integration approach:
- OpenClaw stays lean and focused
- Users get production-proven safety (battle-tested code)
- Optional (zero overhead if not used)

That said, if you prefer built-in approach, happy to discuss!
```

### Q: "What's the maintenance burden?"

```
Minimal for OpenClaw:
- Integration code: ~100 LOC (stable, unlikely to change)
- VERONICA Core: We maintain separately (no burden on OpenClaw)
- Tests: We can contribute (if you want them in OpenClaw's test suite)

If maintenance is a concern, we can:
- Maintain integration externally (separate repo)
- Only link from OpenClaw docs (no code changes)
- Support users directly (via VERONICA issues)

Your call — we're flexible!
```

### Q: "Performance impact?"

```
Zero by default (no-op safety layer is literally no-op).

When enabled (opt-in):
- < 5% overhead (measured in production: 1050 → 1000 ops/sec)
- Per-op overhead: ~1-5ms (atomic file write)
- Bottleneck is usually external systems (APIs), not safety layer

Users who need maximum performance can disable (zero overhead).
Users who need reliability can enable (< 5% cost).

Benchmarks: [link to integration guide performance section]
```

### Q: "License compatibility?"

```
Yes! VERONICA is MIT licensed (permissive, no restrictions).

MIT is compatible with most open-source licenses. No CLA, no strings attached.

Users can:
- Use commercially (no fees)
- Modify (no approval needed)
- Distribute (no restrictions)

If OpenClaw has specific license requirements, let me know — we can adjust if needed.
```

---

## Conversation Guidelines

### DO:
- Acknowledge OpenClaw's excellence
- Provide proof (links to demos, destruction tests)
- Show flexibility (offer multiple integration options)
- Be patient (maintainers are busy)
- Thank them for their time

### DON'T:
- Criticize OpenClaw's current approach
- Pressure for quick decision
- Make unsubstantiated claims
- Use adversarial language ("better than", "superior to")
- Spam with follow-ups (1 week minimum between messages)

---

## Timeline Expectations

**Initial contact → Response**: 1-2 weeks (maintainers are busy)
**Response → Decision**: 2-4 weeks (review takes time)
**Decision → PR merge**: 1-2 months (if accepted)

**If no response after 2 weeks**: Send single follow-up
**If no response after 4 weeks**: Assume declined, proceed with external integration

---

## Success Metrics

### Primary (Integration Accepted)
- PR merged into OpenClaw
- VERONICA listed in OpenClaw docs
- Integration code maintained by OpenClaw

### Secondary (External Integration)
- External integration repo created
- Link from VERONICA docs to OpenClaw
- Support OpenClaw users via separate channel

### Tertiary (Awareness)
- OpenClaw maintainers aware of VERONICA
- Users know integration exists (even if external)
- Positive relationship with OpenClaw team

**All outcomes are wins** — we're building for the community, not for credit.

---

## Post-Integration (If Accepted)

### Maintenance Commit

```
Hi [Name],

Integration merged — thank you!

I'm committed to maintaining this long-term:
- Fix bugs in integration code (via PRs)
- Update when VERONICA Core API changes
- Support users via OpenClaw issues (if tagged)

If you ever want to remove integration (for any reason), just let me know — no hard feelings.

Appreciate the collaboration!

[Your name]
```

### Community Support

- Monitor OpenClaw issues for VERONICA-related questions
- Respond within 24-48 hours
- Fix bugs within 1 week
- Coordinate releases (if breaking changes)

---

## Templates Summary

| Template | Use Case | Length | Tone |
|----------|----------|--------|------|
| **Initial Contact v1** | Technical focus | < 150 words | Professional |
| **Initial Contact v2** | Value proposition | < 150 words | Collaborative |
| **Initial Contact v3** | Community-driven | < 150 words | Friendly |
| **GitHub Issue** | Public discussion | ~ 300 words | Formal |
| **Follow-Up v1** | Gentle reminder | < 100 words | Patient |
| **Follow-Up v2** | Offer alternative | < 100 words | Flexible |
| **Declined v1** | Graceful exit | < 75 words | Respectful |
| **Interested v1** | PR readiness | < 100 words | Professional |
| **Interested v2** | Collaborative | < 100 words | Enthusiastic |

---

## Final Notes

**Remember**: OpenClaw maintainers are doing you no favors by integrating. You're offering value, but they decide what's best for their project.

**Be grateful**: If accepted, express appreciation. If declined, thank them for their time.

**Be professional**: Maintain positive relationship regardless of outcome. We're all building for the community.
