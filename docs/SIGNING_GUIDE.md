# Policy Signing Guide

This guide explains how to sign and verify VERONICA policy files.

## Overview

VERONICA supports two signature schemes:

| Version | Algorithm | Dependency | File suffix |
|---------|-----------|------------|-------------|
| v1 | HMAC-SHA256 (symmetric) | stdlib only | `.sig` |
| v2 | ed25519 (asymmetric) | `cryptography` package | `.sig.v2` |

`PolicyEngine` checks v2 first. If `.sig.v2` exists, v1 is ignored.
If neither exists, a warning is logged and the engine continues (backward-compatible).

---

## v2 Signing (ed25519) — Recommended

### Requirements

```
pip install cryptography
```

### 1. Generate a dev keypair (one-time)

```python
from veronica_core.security.policy_signing import PolicySignerV2

priv_pem, pub_pem = PolicySignerV2.generate_dev_keypair()

# Save public key to the repo (safe to commit)
open("policies/public_key.pem", "wb").write(pub_pem)

# NEVER commit the private key
# Store it in a secrets manager or environment variable
```

### 2. Sign a policy file

```python
from pathlib import Path
from veronica_core.security.policy_signing import PolicySignerV2

priv_pem = open("your_private_key.pem", "rb").read()  # or from secrets manager
signer = PolicySignerV2(public_key_path=Path("policies/public_key.pem"))
signer.sign(Path("policies/default.yaml"), priv_pem)
# Creates: policies/default.yaml.sig.v2
```

### 3. Verify a policy file

```python
from pathlib import Path
from veronica_core.security.policy_signing import PolicySignerV2

signer = PolicySignerV2(public_key_path=Path("policies/public_key.pem"))
ok = signer.verify(
    Path("policies/default.yaml"),
    Path("policies/default.yaml.sig.v2"),
)
print("OK" if ok else "TAMPER DETECTED")
```

### 4. Files to commit

- `policies/public_key.pem` — public key (safe to commit)
- `policies/default.yaml.sig.v2` — base64-encoded signature (safe to commit)

**Never commit the private key.**

---

## v1 Signing (HMAC-SHA256) — Legacy

Used when `cryptography` is not installed or v2 files are absent.

```python
from pathlib import Path
from veronica_core.security.policy_signing import PolicySigner

signer = PolicySigner()  # uses VERONICA_POLICY_KEY env var or built-in test key
sig_hex = signer.sign(Path("policies/default.yaml"))
Path("policies/default.yaml.sig").write_text(sig_hex + "\n")
```

Set `VERONICA_POLICY_KEY` (hex-encoded 32-byte key) for production use.

---

## Checking availability

```python
from veronica_core.security.policy_signing import PolicySignerV2

print(PolicySignerV2.is_available())  # True if cryptography is installed
print(PolicySignerV2().mode)          # "ed25519" or "unavailable"
```

---

## CI / CD integration

Add a signing step to your pipeline after editing `policies/default.yaml`:

```bash
python - <<'EOF'
from pathlib import Path
import os
from veronica_core.security.policy_signing import PolicySignerV2

priv_pem = os.environ["VERONICA_SIGNING_KEY"].encode()  # PEM from secret
signer = PolicySignerV2(public_key_path=Path("policies/public_key.pem"))
signer.sign(Path("policies/default.yaml"), priv_pem)
print("[OK] policies/default.yaml.sig.v2 updated")
EOF
```

Then commit `policies/default.yaml.sig.v2` along with the policy change.

---

## Security notes

- The ed25519 private key is never persisted to disk in production.
- The public key is safe to store in the repository.
- `PolicyEngine` raises `RuntimeError` immediately on tamper detection.
- Audit events (`policy_tamper`, `policy_sig_missing`) are written to the audit log.
