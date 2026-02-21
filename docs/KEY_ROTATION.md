# Key Rotation Guide

This guide describes when and how to rotate the ed25519 public key used for
policy signing verification.  Key rotation also updates `policies/key_pin.txt`
so that the `KeyPinChecker` can detect unauthorized key substitutions.

---

## When to Rotate

- Suspected private key compromise
- Scheduled rotation policy (e.g., annual)
- Team member departure with access to the private key
- CI/CD secrets manager rotation policy triggers

---

## Rotation Steps

### 1. Generate a new keypair

Run this in a secure, offline environment.  Never store the private key on disk
in production — use a secrets manager.

```python
from veronica_core.security.policy_signing import PolicySignerV2

priv_pem, pub_pem = PolicySignerV2.generate_dev_keypair()

# Write the public key to the repository (safe to commit)
open("policies/public_key.pem", "wb").write(pub_pem)

# Store the private key in your secrets manager, NOT on disk
print("Private key (store in secrets manager):")
print(priv_pem.decode())
```

### 2. Update the key pin

```python
import hashlib

pub_pem = open("policies/public_key.pem", "rb").read().strip()
pin = hashlib.sha256(pub_pem).hexdigest()
open("policies/key_pin.txt", "w").write(pin)
print(f"New pin: {pin}")
```

Or with the CLI shortcut:

```bash
python -c "
import hashlib
data = open('policies/public_key.pem', 'rb').read().strip()
open('policies/key_pin.txt', 'w').write(hashlib.sha256(data).hexdigest())
print('key_pin.txt updated')
"
```

### 3. Re-sign the policy

```python
from pathlib import Path
from veronica_core.security.policy_signing import PolicySignerV2

signer = PolicySignerV2()
private_key_pem = open("private_key.pem", "rb").read()  # from secrets manager
signer.sign(Path("policies/default.yaml"), private_key_pem)
# Writes policies/default.yaml.sig.v2
```

See `docs/SIGNING_GUIDE.md` for the full signing workflow.

### 4. Update CI/CD secrets

Update the following environment variables in all CI/CD environments:

- `VERONICA_SIGNING_KEY` — new private key PEM (for re-signing in CI)
- `VERONICA_KEY_PIN` — new SHA-256 hex pin (or rely on `policies/key_pin.txt`)

### 5. Commit the updated files

```bash
git add policies/public_key.pem policies/key_pin.txt policies/default.yaml.sig.v2
git commit -m "chore: rotate ed25519 public key and update key pin"
```

### 6. Verify

Deploy and confirm that VERONICA starts without `key_pin_mismatch` audit events:

```bash
grep key_pin_mismatch /path/to/audit.jsonl
# Should be empty
```

---

## Emergency Rotation (Key Compromise)

If the private key is believed to be compromised:

1. **Immediately** revoke the key from all secrets managers.
2. Follow steps 1–6 above.
3. Audit the audit log for any `policy_tamper` or `key_pin_mismatch` events
   that may indicate unauthorized policy modifications.
4. Review all deployments that used the compromised key.

---

## Verification After Rotation

```python
from veronica_core.security.key_pin import KeyPinChecker
from pathlib import Path

pub_pem = Path("policies/public_key.pem").read_bytes()
checker = KeyPinChecker()
assert checker.check(pub_pem), "Key pin check failed after rotation!"
print("Key pin OK")
```

---

## Related Files

| File | Purpose |
|------|---------|
| `policies/public_key.pem` | ed25519 public key (committed, safe) |
| `policies/key_pin.txt` | SHA-256 hash of the public key (committed, safe) |
| `policies/default.yaml.sig.v2` | ed25519 signature of the policy file |
| `docs/SIGNING_GUIDE.md` | Full policy signing workflow |
| `src/veronica_core/security/key_pin.py` | KeyPinChecker implementation |
| `src/veronica_core/security/policy_signing.py` | PolicySignerV2 implementation |
