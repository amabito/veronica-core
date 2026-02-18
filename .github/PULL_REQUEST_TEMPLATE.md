## Summary
<!-- 1-3 bullet points describing the change -->

## Release checklist
<!-- Check all that apply. CI enforces version + exports + README via tools/release_check.py -->

- [ ] Version bump: `pyproject.toml` and `src/veronica_core/__init__.py` match
- [ ] README: Ship Readiness section references current version
- [ ] Docs: new features documented (or existing docs updated)
- [ ] Examples: at least one demo updated or added
- [ ] Exports: public API symbols in `__init__.py` files
- [ ] `python tools/release_check.py --mode=pr` passes locally

## Test plan
<!-- How was this tested? -->
