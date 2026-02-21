# VERONICA Threat Model

## Overview

VERONICA is a runtime containment layer for LLM-agent systems.
This document defines the threat model for veronica-core's security containment subsystem (Phase A–J).

## Assets

| Asset | Description | Criticality |
|-------|-------------|-------------|
| Policy file (default.yaml) | Defines what LLM agents are allowed to do | CRITICAL |
| Public key (public_key.pem) | Verifies policy signatures | HIGH |
| Key pin (key_pin.txt) | Prevents key substitution | HIGH |
| Audit log | Tamper-evident record of all actions | HIGH |
| Approval tokens | Authorizes sensitive operations | HIGH |
| SBOM (sbom.json) | Tracks dependency provenance | MEDIUM |
| Environment fingerprint | Detects process/container escape | MEDIUM |

## Trust Assumptions

1. The host OS and Python runtime are trusted.
2. The audit log file path is writable only by the veronica process.
3. CI secrets (VERONICA_SIGNING_KEY, VERONICA_KEY_PIN) are protected by the CI platform.
4. The developer generating keypairs follows the SIGNING_GUIDE.
5. git history is trusted (committed artifacts: public_key.pem, key_pin.txt, policies/).

## Attacker Model

### In Scope

- **LLM agent**: an LLM model that generates tool calls attempting to escape containment
- **Prompt injection**: malicious user input that manipulates the LLM to call forbidden tools
- **Compromised dependency**: a malicious package injected via supply chain
- **Policy rollback**: an attacker (with file access) replaces default.yaml with an older, weaker version
- **Key substitution**: an attacker replaces public_key.pem with their own key to sign a weak policy
- **Approval fatigue**: flooding the approval system to exhaust the operator
- **Exfiltration**: LLM agent attempting to send secrets/data out via HTTP, shell, or file

### Out of Scope

- Kernel-level exploits, hypervisor escape
- Compromise of the CI/CD platform secrets store
- Physical access to the developer machine
- Zero-day vulnerabilities in Python stdlib

## Threat Categories & Phase Mitigations

### THREAT-1: Unrestricted Shell Execution

**Risk**: LLM calls subprocess.run() / os.system() directly
**Mitigations**:
- Phase A: PolicyEngine DENY for shell commands (rm, curl, powershell, cmd...)
- Phase E-1: AST linter blocks raw subprocess in source (lint_no_raw_exec.py)
- Phase B: SecureExecutor enforces shell=False

### THREAT-2: Sensitive File Access

**Risk**: LLM reads .env, .ssh/id_rsa, Chrome cookies, etc.
**Mitigations**:
- Phase A: PolicyEngine DENY for sensitive path patterns
- Phase E-4: WindowsSandboxRunner blocks C:\Users\, APPDATA, System32
- Phase I-2: AttestationProbe verifies restrictions are enforced

### THREAT-3: Data Exfiltration

**Risk**: LLM sends secrets via HTTP POST, base64/hex encoded
**Mitigations**:
- Phase E-3: Shannon entropy check on URLs (>4.5 bits → DENY)
- Phase E-3: Base64/hex detection in query params
- Phase E-3: Domain allowlist for outbound HTTP
- Phase A: PolicyEngine DENY for POST/PUT/DELETE

### THREAT-4: Secret Leakage in Logs

**Risk**: API keys, tokens appear in audit logs or output
**Mitigations**:
- Phase A: SecretMasker applied to all logged output
- Phase E-2: 16+ regex patterns (OpenAI, Anthropic, Slack, SSH, PGP, npm, PyPI...)

### THREAT-5: Credential Harvesting

**Risk**: LLM uses git credential, gh auth token, npm token commands
**Mitigations**:
- Phase E-2: Credential subcommands in DENY list
- Phase A: PolicyEngine DENY for .ssh, .pem, .p12 file reads

### THREAT-6: CI/CD Workflow Poisoning

**Risk**: LLM modifies .github/workflows/**, package.json, git hooks
**Mitigations**:
- Phase A: .github/workflows/**, git hooks → REQUIRE_APPROVAL
- Phase F: Red team regression 20 scenarios (workflow-poison/persistence)

### THREAT-7: Supply Chain Injection

**Risk**: Malicious package installed silently
**Mitigations**:
- Phase G-2: pip/npm/cargo install → REQUIRE_APPROVAL
- Phase G-2: Lock file changes → REQUIRE_APPROVAL
- Phase G-2: SBOM generation in CI (dependency tracking)
- Phase I-3: SBOM diff gate (changes require approval token)

### THREAT-8: Policy Tampering

**Risk**: Attacker modifies default.yaml to weaken rules
**Mitigations**:
- Phase G-1: HMAC-SHA256 signature (v1)
- Phase I-1: Ed25519 signature (v2, asymmetric — private key not present at runtime)
- Phase J-1: CI/PROD: unsigned policy → RuntimeError
- Phase J-3: Policy version → REQUIRE_APPROVAL

### THREAT-9: Key Substitution

**Risk**: Attacker replaces public_key.pem with their own key
**Mitigations**:
- Phase J-2: SHA-256 key pin (policies/key_pin.txt committed)
- Phase J-2: CI/PROD: pin mismatch → RuntimeError

### THREAT-10: Policy Rollback

**Risk**: Attacker replaces default.yaml with older, weaker version
**Mitigations**:
- Phase J-3: policy_version monotonically increasing
- Phase J-3: last_seen_version derived from hash-chained audit log
- Phase J-3: Rollback detected → RuntimeError (all security levels)

### THREAT-11: Process/Container Escape

**Risk**: Agent escapes sandbox to access host resources
**Mitigations**:
- Phase G-3: EnvironmentFingerprint baseline on startup
- Phase I-2: SandboxProbe active verification
- Phase G-3: Anomaly → SAFE_MODE

### THREAT-12: Approval Fatigue

**Risk**: Flooding approval requests to exhaust operator attention
**Mitigations**:
- Phase H: ApprovalBatcher deduplicates requests
- Phase H: ApprovalRateLimiter token bucket → SAFE_MODE on flood

### THREAT-13: Audit Log Manipulation

**Risk**: Attacker tampers with audit log to hide activity
**Mitigations**:
- Phase C: SHA-256 hash chain (each entry includes prev_hash)
- Phase C: verify_chain() detects any modification
- Phase J-3: policy_version history derived from trusted audit log

## Residual Risks

| Risk | Level | Reason |
|------|-------|---------|
| Kernel exploit | HIGH | Out of scope — requires OS hardening |
| CI secrets compromise | MEDIUM | CI platform responsibility |
| Developer OPSEC | MEDIUM | Policy + SIGNING_GUIDE mitigates |
| Log volume (slow startup) | LOW | policy_checkpoint events mitigate |
| Approval token replay | LOW | Nonce registry + expiry mitigate |

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-21 | Initial threat model (Phase A–J) |
