---
title: "feat: tool description pinning and schema hash verification"
labels: enhancement, security, mcp
---

## Why

MCP tools can change their description or schema between invocations. A tool that was "read a file" at registration time can silently become "execute shell command" at call time. There is no mechanism to detect this.

## Goal

Add tool description pinning: hash the tool schema at registration, verify at invocation. Reject calls where the schema has changed.

## Scope

- Tool schema hashing (SHA-256 of canonical JSON)
- Pin storage (in-memory registry per ExecutionContext)
- Verification hook in ShieldPipeline
- DENY on mismatch (fail-closed)
- MCP adapter integration

## Non-goals

- Tool content sandboxing (separate concern)
- Tool output validation (separate concern)
- Server-level trust (future: federation)

## Why now

MCP adoption is accelerating. Tool poisoning is a known attack vector with no current mitigation in VERONICA-Core.

## Acceptance criteria

- [ ] `ToolSchemaPin` dataclass with hash, tool name, registered_at
- [ ] `ToolPinRegistry` with register() and verify() methods
- [ ] ShieldPipeline hook that calls verify() before tool dispatch
- [ ] DENY decision on hash mismatch
- [ ] Unit tests for register, verify, mismatch, hash collision
- [ ] MCP adapter wired to pin on first tool call
