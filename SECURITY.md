# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.x     | Current development |

Security fixes are applied to the latest release only.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use [GitHub Security Advisories](https://github.com/amabito/veronica-core/security/advisories/new) (Settings > Security > Advisories) with:

1. Description of the vulnerability
2. Steps to reproduce
3. Affected versions
4. Impact assessment (if known)

### Response Timeline

- **Acknowledgment**: within 48 hours
- **Initial assessment**: within 5 business days
- **Fix timeline**: depends on severity, communicated in initial assessment

### Disclosure Process

We follow coordinated disclosure:

1. Reporter submits vulnerability privately
2. We confirm and assess severity
3. We develop and test a fix
4. We release the fix and publish a security advisory
5. Reporter is credited (unless they prefer anonymity)

We ask reporters to allow 90 days before public disclosure.

## Scope

VERONICA Core enforces runtime execution safety:

- Budget limits (USD and token caps)
- Concurrency gating (org/team level admission control)
- Degradation control (progressive response to failure signals)
- Loop detection hooks (caller-invoked session halt on detected loops)

### Explicitly Out of Scope

VERONICA does **not** provide and is **not** responsible for:

- **Content safety**: Prompt injection detection, output filtering, toxicity screening
- **Authentication or authorization**: User identity, API key management, RBAC
- **Network security**: TLS, firewall rules, DDoS protection
- **Model security**: Model weight protection, inference tampering
- **Data privacy**: PII detection, data masking, GDPR compliance

Vulnerabilities in out-of-scope areas should be reported to the appropriate upstream project.

## Threat Model

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full threat model, including:
- Assets protected
- Attack surface analysis
- Failure mode catalog
- Security invariants and assumptions

## Dependencies

VERONICA Core has **zero external dependencies** (pure Python stdlib). This eliminates supply chain attack surface from third-party packages.

## Security Design Principles

1. **Fail-closed (when configured)**: With BudgetEnforcer active, budget violations raise `BudgetExceeded` before the call executes. Default `RuntimeContext` without enforcement components passes all calls through.
2. **In-memory state**: All enforcement state is held in-memory. No state files are written by veronica-core. For persistent state that survives crashes, use veronica-cloud.
3. **No secrets in state**: VERONICA never stores API keys, tokens, or credentials.
4. **Minimal surface**: Zero dependencies means zero transitive vulnerabilities.
