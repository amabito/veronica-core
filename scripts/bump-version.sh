#!/usr/bin/env bash
# Usage: bash scripts/bump-version.sh 3.7.9 "Short release title"
#
# Does everything in version-bump.md atomically:
# 1. Updates pyproject.toml version
# 2. Updates __init__.py __version__
# 3. Updates __init__.py version comments
# 4. Updates README.md version references
# 5. Verifies no old version remains
# 6. Refuses to proceed if any check fails
#
# Does NOT: create CHANGELOG entry, commit, tag, push.
# Those are manual (CHANGELOG needs human-written content).

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: bash scripts/bump-version.sh <NEW_VERSION> <TITLE>"
    echo "Example: bash scripts/bump-version.sh 3.7.9 'Security Patch'"
    exit 1
fi

NEW="$1"
TITLE="$2"

# Detect current version from pyproject.toml
OLD=$(grep 'version = "' pyproject.toml | head -1 | sed 's/.*version = "\([^"]*\)".*/\1/')
if [[ -z "$OLD" ]]; then
    echo "[NG] Cannot detect current version from pyproject.toml"
    exit 1
fi

echo "Bumping: $OLD -> $NEW ($TITLE)"
echo ""

# --- Step 1: pyproject.toml ---
sed -i "s/version = \"$OLD\"/version = \"$NEW\"/" pyproject.toml
echo "[OK] pyproject.toml: $OLD -> $NEW"

# --- Step 2: __init__.py __version__ ---
INIT="src/veronica_core/__init__.py"
sed -i "s/__version__ = \"$OLD\"/__version__ = \"$NEW\"/" "$INIT"
echo "[OK] __init__.py __version__: $OLD -> $NEW"

# --- Step 3: __init__.py comments (vX.Y.Z) ---
sed -i "s/(v$OLD)/(v$NEW)/g" "$INIT"
echo "[OK] __init__.py comments: (v$OLD) -> (v$NEW)"

# --- Step 4: README.md ---
# Update "stable at vX.Y.Z"
sed -i "s/stable at v$OLD/stable at v$NEW/" README.md

# Add version note if not already present
if ! grep -q "v$NEW" README.md; then
    # Append to the Stats line
    sed -i "s/Python 3.10+\./v$NEW $TITLE. Python 3.10+./" README.md
fi
echo "[OK] README.md: references updated to v$NEW"

# --- Step 5: Verify no old version remains in key files ---
echo ""
echo "--- Verification ---"
FAIL=0

# Old version residue check (only meaningful when OLD != NEW)
if [[ "$OLD" != "$NEW" ]]; then
    if grep -q "version = \"$OLD\"" pyproject.toml; then
        echo "[NG] pyproject.toml still contains $OLD"
        FAIL=1
    else
        echo "[OK] pyproject.toml: no $OLD residue"
    fi
    if grep -q "__version__ = \"$OLD\"" "$INIT"; then
        echo "[NG] __init__.py still contains $OLD"
        FAIL=1
    else
        echo "[OK] __init__.py: no $OLD residue"
    fi
else
    echo "[SKIP] OLD == NEW ($OLD), residue check skipped"
fi

# Check README references new version
if ! grep -q "v$NEW" README.md; then
    echo "[NG] README.md does not reference v$NEW"
    FAIL=1
else
    echo "[OK] README.md: references v$NEW"
fi

# Check README stable line
if ! grep -q "stable at v$NEW" README.md; then
    echo "[NG] README.md 'stable at' not updated to v$NEW"
    FAIL=1
else
    echo "[OK] README.md: 'stable at v$NEW'"
fi

# Check new version is in pyproject.toml
if ! grep -q "version = \"$NEW\"" pyproject.toml; then
    echo "[NG] pyproject.toml does not have version = \"$NEW\""
    FAIL=1
else
    echo "[OK] pyproject.toml: version = \"$NEW\""
fi

echo ""
if [[ $FAIL -eq 1 ]]; then
    echo "[NG] Version bump FAILED. Fix the issues above."
    exit 1
fi

echo "[OK] Version bump complete: $OLD -> $NEW"
echo ""
echo "Next steps (manual):"
echo "  1. Add CHANGELOG.md entry for [$NEW]"
echo "  2. uv sync  (update lockfile)"
echo "  3. uv run pytest tests/ -q  (verify tests pass)"
echo "  4. git add pyproject.toml $INIT README.md CHANGELOG.md uv.lock"
echo "  5. git commit -m 'chore: bump version to v$NEW'"
echo "  6. git push origin main"
echo "  7. git tag -a v$NEW -m 'v$NEW -- $TITLE'"
echo "  8. git push origin v$NEW"
echo "  9. gh release create v$NEW --generate-notes (or manual)"
