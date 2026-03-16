---
title: "docs: README positioning refresh"
labels: documentation
---

## Why

The current README mixes implemented features with future roadmap items in a way that is not obvious to a first-time reader. Some metrics (test counts, coverage percentage) may be stale. Sections describing "control plane" or "enterprise-ready" capabilities overstate the current state of the project.

External credibility depends on honest, accurate documentation. A README that claims more than the library does creates trust problems when users discover the gap.

## Goal

A README that accurately describes what VERONICA-Core is today: a runtime containment library for LLM agents. No roadmap items presented as current features. No stale numbers.

## Scope

- Rewrite the header section (one-sentence description, no marketing adjectives)
- Add "What it does not do" section (currently absent)
- Update test count and coverage metrics to match current `pytest` output
- Add MCP scope limits (what the MCP adapter covers and does not cover)
- Replace "control plane" framing with accurate "runtime containment library" framing
- Remove or clearly label roadmap items that are not yet implemented

## Non-goals

- Marketing copy
- Feature announcements for unimplemented items
- Changing the structure of sections that are accurate

## Why now

The project is at a stage where external contributors and early adopters are reading it. Inaccurate positioning is a liability. A clear, honest README is the highest-leverage documentation change.

## Acceptance criteria

- [ ] Header: one sentence, describes what the library does, no adjectives like "powerful" or "enterprise-ready"
- [ ] "What it does not do" section present
- [ ] Test count matches `pytest --co -q | tail -1` output
- [ ] Coverage percentage matches `pytest --cov` output
- [ ] No roadmap items listed as current features without a `(planned)` label
- [ ] MCP adapter scope limits noted
- [ ] README passes `grep -c '!'` check (target: 0 exclamation marks)
- [ ] README passes banned-word check: no "game-changer", "revolutionary", "powerful", "seamlessly"
