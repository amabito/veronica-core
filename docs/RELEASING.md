# VERONICA Release Procedure

This document describes the steps to prepare and publish a veronica-core release.

## Prerequisites

- Python 3.10+
- `cryptography` package: `pip install cryptography`
- `pyyaml` package: `pip install pyyaml`
- Ed25519 private key for signing (see [Key Management](#key-management))
- PyPI API token in `PYPI_API_TOKEN` (for publishing)

---

## Release Steps

### 1. Bump Version

Update the version string in **two** places to match (e.g., `0.10.0`):

```
pyproject.toml          → version = "0.10.0"
src/veronica_core/__init__.py  → __version__ = "0.10.0"
```

### 2. Update CHANGELOG

Add a new section to `CHANGELOG.md` for the version being released.

### 3. Sign Policy Artifacts

Sign the default policy file with the Ed25519 private key.

**Option A — Environment variable (recommended for CI):**

```bash
export VERONICA_PRIVATE_KEY_PEM="$(cat /secure/path/to/private_key.pem)"
python tools/release_sign_policy.py
```

**Option B — Key file (local only):**

```bash
python tools/release_sign_policy.py --key-file /secure/path/to/private_key.pem
```

**Dry-run (validate without writing):**

```bash
python tools/release_sign_policy.py --key-file /path/to/private_key.pem --dry-run
```

The tool writes `policies/default.yaml.sig.v2` on success.

### 4. Verify Release Artifacts

Run the verification tool to confirm all checks pass:

```bash
python tools/verify_release.py
```

Expected output:

```
[PASS] Signature file exists: default.yaml.sig.v2
[PASS] Key pin matches: <hash prefix>...
[PASS] Ed25519 signature valid
[PASS] policy_version is set: 1

Results: 4 passed, 0 failed
Release verification PASSED.
```

All 4 checks must pass before proceeding.

### 5. Run Full Test Suite

```bash
python -m pytest tests/ -q
```

Zero failures required.

### 6. Commit Artifacts

Stage **only** the changed files explicitly (never use `git add -A`):

```bash
git add pyproject.toml \
        src/veronica_core/__init__.py \
        CHANGELOG.md \
        policies/default.yaml.sig.v2

git commit -m "chore: release v0.10.0"
```

### 7. Tag and Push

```bash
git tag v0.10.0
git push origin main
git push origin v0.10.0
```

Pushing a `v*` tag triggers the `.github/workflows/release.yml` CI workflow,
which re-runs signing (if the secret is available), verification, SBOM
generation, and artifact upload.

---

## Key Management

### Generating a Development Keypair

For local testing only — never use dev keys in production:

```python
from veronica_core.security.policy_signing import PolicySignerV2

priv_pem, pub_pem = PolicySignerV2.generate_dev_keypair()
Path("dev_private_key.pem").write_bytes(priv_pem)
Path("dev_public_key.pem").write_bytes(pub_pem)
```

### Pinning a New Public Key

After rotating or creating a new public key:

```bash
python - <<'EOF'
import hashlib
from pathlib import Path
pem = Path("policies/public_key.pem").read_bytes().strip()
pin = hashlib.sha256(pem).hexdigest()
Path("policies/key_pin.txt").write_text(pin + "\n")
print(f"New key pin: {pin}")
EOF
```

Commit both `policies/public_key.pem` and `policies/key_pin.txt`.

### Key Rotation

See `docs/KEY_ROTATION.md` for the full key rotation procedure.

---

## CI Secrets Required

| Secret | Purpose |
|--------|---------|
| `VERONICA_PRIVATE_KEY_PEM` | Signs policy artifacts in CI |
| `PYPI_API_TOKEN` | Publishes to PyPI (publish workflow only) |

---

## Troubleshooting

**Signing fails with "cryptography not installed":**

```bash
pip install cryptography
```

**Verification fails with "Key pin MISMATCH":**

The `policies/public_key.pem` no longer matches `policies/key_pin.txt`.
Re-pin the key (see [Pinning a New Public Key](#pinning-a-new-public-key))
or rotate to the correct key.

**Verification fails with "Ed25519 signature INVALID":**

The policy file was modified after signing. Re-run `release_sign_policy.py`.
