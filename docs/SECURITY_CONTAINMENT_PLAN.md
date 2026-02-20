# VERONICA Security Containment Layer — Design & Audit Plan

## 1. Problem Statement

AI agents operating with tool-use capabilities can be **weaponized** or can
**bypass upper-layer controls** in ways their operators did not intend:

- Agents may receive adversarial prompts instructing them to exfiltrate secrets
  (`.env`, SSH keys, cloud credentials) via outbound HTTP.
- Agents may execute uncontrolled shell commands that delete files, modify CI
  pipelines, or install malicious packages.
- Agents may be prompted to push code to remote repositories without human
  review, or to alter GitHub Actions workflows.
- Once an agent is compromised, it can attempt to disable its own safety
  mechanisms unless those mechanisms are enforced at a **lower** layer.

---

## 2. Design Philosophy

### "Cannot do" vs. "Should not do"

Upper-layer controls (system prompts, rules files) tell the agent what it
_should not do_.  They are advisory and can be overridden by a sufficiently
adversarial prompt.

The Security Containment Layer enforces what the agent **cannot do** —
regardless of what instructions it receives — by intercepting every tool
dispatch and egress request at the OS/process boundary.

### Defense in Depth

```
Layer 0 (upper): System prompt / rules / agent persona
Layer 1 (this):  PolicyEngine + Adapter + SecureExecutor  [THIS LAYER]
Layer 2 (OS):    Linux seccomp / cgroups / network namespaces (Phase B+)
```

Even if Layer 0 is fully bypassed, Layer 1 stops the action.
Even if Layer 1 is degraded, Layer 2 (sandbox) provides a hard boundary.

### Fail-Closed

All evaluations default to `DENY`.  An action must be **explicitly allowed**
by a rule to proceed.  Unknown actions are always denied.

---

## 3. Architecture

```
Agent / LLM tool call
        |
        v
  PolicyHook.before_tool_call(ToolCallContext)
        |
        v
  PolicyEngine.evaluate(PolicyContext)
        |
        +---> DENY  -----------> Decision.HALT  (blocked, logged)
        |
        +---> REQUIRE_APPROVAL -> Decision.QUARANTINE (queued for human)
        |
        +---> ALLOW -----------> Adapter.exec(args)
                                        |
                                        v
                               SecureExecutor (Phase B)
                                        |
                                        v
                               subprocess (shell=False, cwd=repo_root)
                                        |
                                        v
                               Tamper-evident audit log (Phase C)
```

**Egress path (outbound HTTP):**

```
Agent HTTP call
        |
        v
  PolicyHook.before_egress(ToolCallContext, url, method)
        |
        v
  PolicyEngine.evaluate(PolicyContext action="net")
        |
        +---> DENY  -----------> Decision.HALT  (connection blocked)
        +---> ALLOW -----------> HTTP request proceeds
```

---

## 4. Capability Model

Capabilities gate which operations an agent profile may perform.
All capabilities are evaluated in addition to policy rules.

| Capability              | dev | ci  | audit |
|-------------------------|-----|-----|-------|
| READ_REPO               | yes | yes | yes   |
| EDIT_REPO               | yes | no  | no    |
| BUILD                   | yes | yes | no    |
| TEST                    | yes | yes | no    |
| NET_FETCH_ALLOWLIST     | no  | no  | no    |
| GIT_PUSH_APPROVAL       | no  | no  | no    |
| SHELL_BASIC             | yes | no  | no    |
| FILE_READ_SENSITIVE     | no  | no  | no    |

`GIT_PUSH_APPROVAL` must be explicitly granted; it is not included in any
built-in profile.  `NET_FETCH_ALLOWLIST` controls whether allowlisted GET
requests are permitted (default: denied unless explicitly set).

---

## 5. Policy Engine Decision Flow

```
evaluate(ctx: PolicyContext) -> PolicyDecision
    |
    +-- action == "shell"?
    |       argv[0] in DENY_COMMANDS?                       -> DENY  (risk +8)
    |       any DENY_OPERATOR in args?                      -> DENY  (risk +6)
    |       (argv[0], argv[1]) in CREDENTIAL_DENY?          -> DENY  (risk +9)  [E-2]
    |       metadata.file_count > 20?                       -> REQUIRE_APPROVAL (risk +3)
    |       argv[0] in ALLOW_COMMANDS?                      -> ALLOW (risk 0)
    |       else                                            -> DENY  (risk +5)
    |
    +-- action == "file_read"?
    |       path matches DENY_PATTERNS?     -> DENY  (risk +7)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "file_write"?
    |       path matches APPROVAL_PATTERNS? -> REQUIRE_APPROVAL (risk +4)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "net"?
    |       method in DENY_METHODS?         -> DENY  (risk +6)
    |       host not in GET_ALLOWLIST?      -> DENY  (risk +5)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "git"?
    |       subcmd in DENY_SUBCMDS
    |         AND NOT has_cap GIT_PUSH_APPROVAL? -> DENY (risk +7)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "browser"?                -> DENY  (risk +5)
    |
    +-- unknown action                      -> DENY  (risk +5)
```

---

## 6. Integration: PolicyHook into ShieldPipeline

```python
from veronica_core.security import PolicyEngine, PolicyHook, CapabilitySet
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.config import ShieldConfig

# Build the hook
engine = PolicyEngine()                    # uses built-in rules
hook = PolicyHook(
    engine=engine,
    caps=CapabilitySet.ci(),               # restrict to CI profile
    working_dir="/repo",
    repo_root="/repo",
    env="ci",
)

# Wire into the pipeline
config = ShieldConfig()
pipeline = ShieldPipeline(
    config=config,
    tool_dispatch=[hook],                  # PolicyHook as ToolDispatchHook
    egress=[hook],                         # PolicyHook as EgressBoundaryHook
)
```

The `PolicyHook.last_decision` attribute exposes the most recent
`PolicyDecision` for inspection (e.g. to read `risk_score_delta`).

---

## 7. Audit Trail and SAFE_MODE Auto-Transition

Each `PolicyDecision` carries a `risk_score_delta` (0–10).

**Phase D** (risk accumulation) aggregates these deltas in a sliding window.
When the cumulative score exceeds a configured threshold, the system
automatically transitions to `SAFE_MODE` — blocking all further tool dispatch
until an operator resets the state.

```
risk_score_delta accumulation (sliding window):
  +0   safe action (ALLOW)
  +3   suspicious (REQUIRE_APPROVAL: large file change)
  +4   sensitive write (REQUIRE_APPROVAL: workflow/script)
  +5   unknown host or unrecognised command (DENY)
  +6   pipe operator or mutating HTTP method (DENY)
  +7   sensitive file read or git push without cap (DENY)
  +8   dangerous shell command e.g. rm, powershell (DENY)
  +9   credential sub-command (DENY) [E-2]
       OR high-entropy/base64/hex GET query exfiltration [E-3]
  +10  raw exec/eval/os.system bypass [E-1]

Threshold example: 30 points in 60 seconds -> transition to SAFE_MODE
```

This means repeated probing attacks (an agent trying many blocked actions)
will rapidly accumulate risk score and trigger SAFE_MODE, even if no single
action is catastrophic.

---

## 8. Critical Audit Findings Coverage

| Finding    | Attack Vector                              | Blocked by                                              |
|------------|--------------------------------------------|---------------------------------------------------------|
| CRITICAL-1 | Uncontrolled shell execution (shell=True)  | Adapter (Phase A-3): `shell=False` + argv allowlist     |
|            |                                            | PolicyEngine: SHELL_DENY_CMD, SHELL_DENY_OPERATOR       |
| CRITICAL-2 | .env / secret file read                   | PolicyEngine: FILE_READ_DENY_SENSITIVE                  |
|            |                                            | SecretMasker (A-4): redacts any leaked secrets in output|
| CRITICAL-3 | Unauthenticated outbound POST/PUT/DELETE   | PolicyEngine: NET_DENY_METHOD                           |
|            |                                            | PolicyHook.before_egress: maps DENY -> Decision.HALT    |
| CRITICAL-4 | CI/workflow file modification              | PolicyEngine: FILE_WRITE_REQUIRE_APPROVAL               |
|            |                                            | Returns QUARANTINE, queued for human approval           |
| CRITICAL-5 | Runaway agent SAFE_MODE bypass             | Phase D: risk_score_delta accumulation -> SAFE_MODE     |
|            |                                            | SAFE_MODE blocks ALL tool dispatch unconditionally      |
| CRITICAL-E1 | Raw exec bypass via shell metacharacters  | E-1: AST linter (lint_no_raw_exec.py) + CI gate         |
|             |                                           | Scans source for `exec()`, `eval()`, `os.system()`      |
| CRITICAL-E2 | Extended credential file read             | E-2: Expanded FILE_READ_DENY_PATTERNS (npmrc, pypirc,   |
|             |                                           | id_rsa, pem, p12) + credential shell sub-commands       |
| CRITICAL-E3 | GET-based data exfiltration               | E-3: URL length limit (2048), Shannon entropy detection |
|             |                                           | (>4.5 bits), base64/hex pattern check, path allowlist   |
| CRITICAL-E4 | Host profile access inside sandbox        | E-4: WindowsSandboxRunner path-based profile isolation  |
|             |                                           | Rewrites workspace paths, blocks host profile reads     |
| CRITICAL-E5 | Approval token replay attack              | E-5: NonceRegistry (single-use nonces), scope binding   |
|             |                                           | (`action:args_hash`), expiry field, audit log events    |
| CRITICAL-F  | All 20 red team scenarios                 | See Phase F table below (Section 11)                    |

**Key principle:** Even if the upper environment (system prompt, agent rules)
is fully bypassed by an adversarial prompt, the lower containment layer
intercepts and blocks the action.  The agent literally cannot execute the
operation — not merely should not.

---

## 9. Custom Policy Override

To customize rules, edit `policies/default.yaml`:

```yaml
# policies/custom.yaml
version: "1.0"

deny:
  shell:
    commands:
      - rm
      - del
      # add your own blocked commands here

allow:
  shell:
    commands:
      - pytest
      - python
      # add your own allowed commands here
```

Then load the custom policy:

```python
from pathlib import Path
from veronica_core.security import PolicyEngine

engine = PolicyEngine(policy_path=Path("policies/custom.yaml"))
```

The YAML file is currently used as documentation; future versions will parse
it into live rule objects.  For now, fork `_EVALUATORS` in `policy_engine.py`
to add custom evaluator functions.

---

## 10. Defense-in-Depth Guarantee

> "Even if the upper environment is bypassed, the lower layer stops it."

The Security Containment Layer is designed so that:

1. **PolicyEngine** enforces rules independently of agent instructions.
2. **Adapter** (Phase A-3) always uses `shell=False`, never interpolating
   arguments into a shell string — preventing injection via argv.
3. **SecureExecutor** (Phase B) runs inside a sandboxed process with reduced
   OS-level privileges (seccomp, cgroups).
4. **Tamper-evident log** (Phase C) records every decision with a chained
   HMAC, ensuring audit records cannot be silently altered by a compromised
   agent.
5. **Risk accumulation** (Phase D) triggers SAFE_MODE on repeated suspicious
   activity, stopping the agent even if each individual action was borderline.

No single layer is sufficient on its own.  The layers are deliberately
redundant so that compromising any one layer does not grant the agent
unrestricted capability.

---

## 11. Phase E: Operational Hardening

Phase E extends the Security Containment Layer with five operational hardening
measures that address attack vectors not covered by the base Phase A–D
implementation.

### E-1: Bypass Prevention (AST Linter + CI Enforcement)

**Problem:** An agent or contributor could introduce `exec()`, `eval()`,
`os.system()`, or `subprocess.run(..., shell=True)` into source files,
creating a direct bypass of the containment layer.

**Solution:**

- `tools/lint_no_raw_exec.py` — static AST scanner that rejects any Python
  file containing raw exec/eval/os.system or `shell=True` usage.
- Runs in CI (`.github/workflows/containment.yml`) as a required check.
- Blocked patterns: `exec`, `eval`, `os.system`, `os.popen`,
  `subprocess.run(..., shell=True)`, `subprocess.Popen(..., shell=True)`.

**Rule ID:** `E1_RAW_EXEC` | **Risk delta:** 10 (critical)

---

### E-2: Extended Secret Classification

**Problem:** Phase A masked `.env` and SSH keys, but modern toolchains use
many additional credential file formats (npm tokens, PyPI tokens, private keys
in PEM/P12 format) and credential-handling shell sub-commands.

**Solution:**

Extended `FILE_READ_DENY_PATTERNS` to include:

| Pattern             | Credential Type          |
|---------------------|--------------------------|
| `**/.npmrc`         | npm registry tokens      |
| `**/.pypirc`        | PyPI upload credentials  |
| `**/.netrc`         | FTP/HTTP basic auth      |
| `**/*id_rsa*`       | RSA SSH private keys     |
| `**/*id_ed25519*`   | Ed25519 SSH private keys |
| `**/*.pem`          | PEM-encoded keys/certs   |
| `**/*.key`          | Generic private keys     |
| `**/*.p12` / `.pfx` | PKCS#12 keystores        |

Extended `SHELL_CREDENTIAL_DENY` to block credential-handling sub-commands:

| Command | Blocked sub-commands                        |
|---------|---------------------------------------------|
| `git`   | `credential`, `credentials`                 |
| `gh`    | `auth`, `token`, `secret`                   |
| `npm`   | `token`, `login`, `logout`, `adduser`       |
| `pip`   | `config`                                    |

Extended `SecretMasker` with 24 new patterns:

| Label                | Pattern                                              |
|----------------------|------------------------------------------------------|
| `ANTHROPIC_KEY`      | `sk-ant-...`                                         |
| `OPENAI_KEY`         | `sk-proj-...` / `sk-...`                             |
| `SLACK_TOKEN`        | `xox[bposa]-...`                                     |
| `SLACK_WEBHOOK`      | `https://hooks.slack.com/services/...`               |
| `DISCORD_TOKEN`      | MFA-format bot token                                 |
| `TWILIO_SID`         | `AC[0-9a-f]{32}`                                     |
| `TWILIO_TOKEN`       | `twilio...token=...`                                 |
| `SENDGRID_KEY`       | `SG.xxx.xxx`                                         |
| `GOOGLE_API_KEY`     | `AIza...`                                            |
| `GOOGLE_OAUTH`       | `GOCSPX-...`                                         |
| `AZURE_SAS`          | `sig=...` / `SharedAccessSignature=...`              |
| `PGP_PRIVATE_KEY`    | `-----BEGIN PGP PRIVATE KEY BLOCK-----`              |
| `SSH_PRIVATE_KEY`    | `-----BEGIN RSA/EC/DSA/OPENSSH PRIVATE KEY-----`     |
| `NETRC_PASSWORD`     | `password <value>` (.netrc format)                   |
| `NPM_TOKEN`          | `npm_...`                                            |
| `PYPI_TOKEN`         | `pypi-...` (50+ chars)                               |
| `GITHUB_FINE_GRAINED`| `github_pat_...` (82+ chars)                         |
| `GITHUB_CLI_TOKEN`   | `gho_...` (GitHub CLI OAuth)                         |
| `POLYMARKET_KEY`     | `polymarket_...key=...`                              |
| `RESEND_KEY`         | `re_...`                                             |
| `BITBANK_KEY`        | `bitbank_...key=...`                                 |
| `HEX_SECRET`         | Generic 32–64-char hex string                        |
| `PASSWORD_KV`        | `password=` / `token=` / `secret=` key-value pairs   |

---

### E-3: Network Exfiltration Prevention

**Problem:** An agent could exfiltrate secrets via allowlisted GET requests by
encoding data in query string parameters (base64, hex, or high-entropy values)
or by accessing undocumented paths on allowlisted hosts.

**Solution:**

Added four sub-checks to `_eval_net()` for GET requests:

| Check                  | Trigger                                       | Rule ID                 | Risk |
|------------------------|-----------------------------------------------|-------------------------|------|
| URL length limit       | `len(url) > 2048`                             | `net.url_too_long`      | +8   |
| Base64 in query        | Value matches `^[A-Za-z0-9+/]{20,}={0,2}$`   | `net.base64_in_query`   | +9   |
| Hex string in query    | Value matches `^[0-9a-fA-F]{32,}$`           | `net.hex_in_query`      | +9   |
| High Shannon entropy   | entropy > 4.5 bits AND len > 20               | `net.high_entropy_query`| +9   |
| Path not in allowlist  | Path prefix not in `NET_PATH_ALLOWLIST`       | `net.path_not_allowed`  | +6   |

Per-host path allowlist (`NET_PATH_ALLOWLIST`):

```
pypi.org               → /pypi/,  /simple/
files.pythonhosted.org → /packages/
github.com             → /
raw.githubusercontent.com → /
registry.npmjs.org     → /
```

---

### E-4: Windows Sandbox Hardening

**Problem:** The base `SandboxRunner` on Windows did not isolate the agent
from the host user profile (AppData, Documents, credential stores).

**Solution:**

`WindowsSandboxRunner` implements path-based profile isolation:

- Workspace operations are rewritten to a controlled `sandbox_root`
  directory, preventing path traversal to host profile locations.
- Attempts to access `AppData`, `%USERPROFILE%`, `%HOMEDRIVE%` outside the
  workspace are blocked before the subprocess is spawned.
- Inherits all PolicyEngine rules (double-gated: policy + sandbox boundary).

---

### E-5: Approval Token v2 (Nonce/Replay Prevention)

**Problem:** v1 `ApprovalToken` had no replay protection.  A valid token
intercepted by an adversary could be re-submitted to approve an operation
the operator intended to approve only once.

**Solution:**

`CLIApprover.sign_v2()` produces tokens with three additional fields:

| Field    | Purpose                                           |
|----------|---------------------------------------------------|
| `nonce`  | UUID4 hex; consumed by `NonceRegistry` on use     |
| `scope`  | `f"{action}:{args_hash}"` — binds to exact op     |
| `expiry` | ISO8601; explicit expiry (timestamp + 5 min)      |

`CLIApprover.approve()` now enforces:

1. HMAC signature valid (v1 and v2 payloads supported)
2. Token not expired (uses `expiry` field for v2)
3. Scope matches `token.action:token.args_hash`
4. Nonce consumed (thread-safe single-use via `NonceRegistry`)

`NonceRegistry` is an in-memory, thread-safe set with configurable
`max_size` (default 10,000) and FIFO eviction.

`CLIApprover.approve()` accepts an optional `audit_log: AuditLog` parameter:
- On success: writes `APPROVAL_GRANTED` event
- On failure: writes `APPROVAL_DENIED` event with reason

`sign()` is deprecated (emits `DeprecationWarning`); `sign_v2()` is the new
default.  Existing v1 tokens continue to verify and approve correctly.

---

## 12. Phase F: Red Team Regression

Phase F defines 20 attack scenarios that serve as a permanent regression suite.
Every scenario must be blocked by the containment layer.

Run the full red team suite:

```bash
uv run pytest tests/redteam/ -v
```

### Red Team Scenario Table

| # | Scenario                            | Category          | Technique                              | Blocked By                          | Rule ID                   |
|---|-------------------------------------|-------------------|----------------------------------------|-------------------------------------|---------------------------|
| 1 | POST to exfil data                  | Exfiltration      | HTTP POST with secret payload          | PolicyEngine                        | NET_DENY_METHOD           |
| 2 | Base64 GET exfil                    | Exfiltration      | GET with base64-encoded secret         | E-3 query check                     | net.base64_in_query       |
| 3 | Hex GET exfil                       | Exfiltration      | GET with hex-encoded token             | E-3 query check                     | net.hex_in_query          |
| 4 | High-entropy GET exfil              | Exfiltration      | GET with high-entropy random string    | E-3 entropy check                   | net.high_entropy_query    |
| 5 | Long URL GET exfil                  | Exfiltration      | GET with 4096-char URL                 | E-3 URL length                      | net.url_too_long          |
| 6 | Read .env file                      | Credential Hunt   | Read dotenv secret file                | PolicyEngine                        | FILE_READ_DENY_SENSITIVE  |
| 7 | Read .npmrc                         | Credential Hunt   | Read npm registry token                | E-2 file patterns                   | FILE_READ_DENY_SENSITIVE  |
| 8 | Read id_rsa                         | Credential Hunt   | Read SSH private key                   | E-2 file patterns                   | FILE_READ_DENY_SENSITIVE  |
| 9 | Read secrets.pem                    | Credential Hunt   | Read PEM private key                   | E-2 file patterns                   | FILE_READ_DENY_SENSITIVE  |
|10 | git credential helper               | Credential Hunt   | Extract credentials from git helper    | E-2 SHELL_CREDENTIAL_DENY           | SHELL_DENY_CREDENTIAL     |
|11 | Overwrite .github/workflows/ci.yml  | Workflow Poison   | Replace CI config with malicious       | PolicyEngine                        | FILE_WRITE_REQUIRE_APPROVAL|
|12 | git push to remote                  | Workflow Poison   | Push poisoned code without approval    | PolicyEngine                        | GIT_DENY_SUBCMD           |
|13 | pip config get global.index-url     | Workflow Poison   | Extract package index credentials      | E-2 SHELL_CREDENTIAL_DENY           | SHELL_DENY_CREDENTIAL     |
|14 | npm token create                    | Workflow Poison   | Create npm publish token               | E-2 SHELL_CREDENTIAL_DENY           | SHELL_DENY_CREDENTIAL     |
|15 | exec() in injected code             | Workflow Poison   | AST exec bypass via eval               | E-1 AST linter                      | E1_RAW_EXEC               |
|16 | rm -rf /                            | Persistence       | Destructive shell command              | PolicyEngine                        | SHELL_DENY_CMD            |
|17 | Approval token replay               | Persistence       | Reuse spent approval token             | E-5 NonceRegistry                   | nonce_replayed            |
|18 | Expired token reuse                 | Persistence       | Submit token after expiry              | E-5 expiry check                    | token_expired             |
|19 | Wrong-scope token                   | Persistence       | Token from different operation         | E-5 scope check                     | scope_mismatch            |
|20 | Path traversal to host profile      | Persistence       | Read AppData outside workspace         | E-4 WindowsSandboxRunner            | SANDBOX_PATH_TRAVERSAL    |

### Adding New Scenarios

New attack techniques can be added to `tests/redteam/test_redteam.py`.
Each scenario should:

1. Construct a `PolicyContext` (or `ApprovalToken`) that represents the attack.
2. Assert that the result is `DENY` (or `approve()` returns `False`).
3. Document the technique and expected rule ID in a comment.
