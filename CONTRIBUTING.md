# Contributing to VERONICA Core

## Development Setup

```bash
git clone https://github.com/amabito/veronica-core.git
cd veronica-core
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running Tests

```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=veronica --cov-report=term-missing

# Single file
pytest tests/test_runtime_events.py -v
```

Coverage target: 90% minimum. CI will fail below this threshold.

## Code Style

We use ruff for linting and formatting:

```bash
# Check
ruff check src/ tests/
ruff format --check src/ tests/

# Fix
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch from `master`
3. Write tests for new functionality
4. Ensure all tests pass and coverage >= 90%
5. Run `ruff check` and `ruff format` with no errors
6. Submit a PR with a clear description

### PR Checklist

- [ ] Tests added or updated
- [ ] All tests passing (`pytest tests/ -v`)
- [ ] Coverage >= 90% (`pytest --cov=veronica --cov-fail-under=90`)
- [ ] Lint passing (`ruff check src/ tests/`)
- [ ] Format passing (`ruff format --check src/ tests/`)
- [ ] No new external dependencies added
- [ ] CHANGELOG.md updated (if user-facing change)

## Versioning

We follow Semantic Versioning (SemVer):

- **PATCH** (0.1.x): Bug fixes, no API changes
- **MINOR** (0.x.0): New features, backward compatible
- **MAJOR** (x.0.0): Breaking API changes

## Release Process

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md` with release notes
3. Commit: `git commit -m "release: vX.Y.Z"`
4. Tag: `git tag vX.Y.Z`
5. Push: `git push origin master --tags`
6. CI automatically publishes to PyPI on tag push

## Code Review

All PRs require at least one review before merge. Reviewers check:

- Correctness: Does the code do what it claims?
- Tests: Are edge cases covered?
- Safety: Does this maintain enforcement guarantees?
- Dependencies: Zero external dependencies is a hard constraint
- Documentation: Are public APIs documented?

## Reporting Issues

- **Bugs**: Use the bug report template
- **Features**: Use the feature request template
- **Security**: See SECURITY.md (do NOT open a public issue)

## Zero Dependencies Policy

VERONICA Core uses only Python standard library. This is a deliberate architectural decision to eliminate supply chain risk. PRs that add external dependencies will not be merged.

If you need functionality from an external package, implement it using stdlib or propose it as an optional integration in a separate package.
