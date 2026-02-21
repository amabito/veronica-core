"""AST-based linter: detect forbidden raw execution calls.

Checks Python source files for calls that bypass SecureExecutor.
Exits 1 if any violations found, 0 if clean.

Usage:
    python tools/lint_no_raw_exec.py [path ...]

    Default path: src/
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Allowlist — files that are permitted to contain raw exec calls
# ---------------------------------------------------------------------------

_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/veronica_core/adapter/exec.py",
        "src/veronica_core/runner/sandbox.py",
        "src/veronica_core/runner/sandbox_windows.py",
        # SandboxProbe intentionally calls urllib.request.urlopen to test
        # whether the sandbox actually blocks outbound network access.
        "src/veronica_core/runner/attestation.py",
        "tools/lint_no_raw_exec.py",
    }
)


# ---------------------------------------------------------------------------
# Forbidden patterns
# ---------------------------------------------------------------------------

# (module_or_none, attr_or_funcname)
# module_or_none=None means a bare function call (e.g. eval, exec)
_FORBIDDEN_CALLS: tuple[tuple[str | None, str], ...] = (
    # subprocess
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_output"),
    ("subprocess", "check_call"),
    # os
    ("os", "system"),
    ("os", "popen"),
    ("os", "execv"),
    ("os", "execve"),
    # requests
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "request"),
    ("requests", "put"),
    ("requests", "delete"),
    ("requests", "patch"),
    ("requests", "head"),
    ("requests", "options"),
    # urllib
    ("urllib.request", "urlopen"),
    # bare builtins
    (None, "eval"),
    (None, "exec"),
)

_FORBIDDEN_SET: frozenset[tuple[str | None, str]] = frozenset(_FORBIDDEN_CALLS)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Violation(NamedTuple):
    """A single linter violation."""

    file: Path
    line: int
    col: int
    pattern: str  # human-readable description of the forbidden call


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------


class ForbiddenCallVisitor(ast.NodeVisitor):
    """Walk an AST and record violations for forbidden raw exec calls."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, int, str]] = []  # (line, col, pattern)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        """Inspect every function call node."""
        func = node.func

        if isinstance(func, ast.Attribute):
            # Could be module.func (e.g. subprocess.run) or
            # package.module.func (e.g. urllib.request.urlopen)
            attr_name = func.attr
            obj = func.value

            # Build dotted module path from the LHS of the attribute
            module_path = _extract_dotted_name(obj)

            if module_path is not None:
                # Try full dotted path first (e.g. urllib.request.urlopen)
                full_key: tuple[str | None, str] = (module_path, attr_name)
                if full_key in _FORBIDDEN_SET:
                    pattern = f"{module_path}.{attr_name}"
                    self.violations.append((node.lineno, node.col_offset, pattern))
                else:
                    # Try just the last component (e.g. "subprocess" from "subprocess.run")
                    last_module = module_path.rsplit(".", 1)[-1]
                    short_key: tuple[str | None, str] = (last_module, attr_name)
                    if short_key in _FORBIDDEN_SET:
                        pattern = f"{module_path}.{attr_name}"
                        self.violations.append((node.lineno, node.col_offset, pattern))

        elif isinstance(func, ast.Name):
            # Bare call: eval(), exec()
            bare_key: tuple[str | None, str] = (None, func.id)
            if bare_key in _FORBIDDEN_SET:
                # Allow eval/exec with trivial constant args (e.g. eval("1+1"))
                if not _is_trivial_eval(node):
                    self.violations.append((node.lineno, node.col_offset, func.id))

        # Continue walking children
        self.generic_visit(node)


def _extract_dotted_name(node: ast.expr) -> str | None:
    """Recursively extract a dotted name from an attribute chain or Name node.

    Returns None if the node is not a simple dotted name.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _extract_dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def _is_trivial_eval(call_node: ast.Call) -> bool:
    """Return True if the eval/exec call uses only constant arguments.

    We flag eval/exec when called with non-trivial (non-constant) args,
    as those could execute dynamically constructed code.
    """
    if not call_node.args:
        return True
    first_arg = call_node.args[0]
    return isinstance(first_arg, ast.Constant)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_file(path: Path) -> list[Violation]:
    """Parse *path* and return a list of Violations found.

    Returns an empty list for allowlisted files or parse errors.
    """
    # Normalise to forward slashes for cross-platform allowlist matching
    norm_path = str(path).replace("\\", "/")
    for allowed in _ALLOWLIST:
        # Match if the path ends with the allowlisted suffix
        if norm_path.endswith(allowed) or norm_path == allowed:
            return []

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    visitor = ForbiddenCallVisitor()
    visitor.visit(tree)

    return [
        Violation(file=path, line=line, col=col, pattern=pattern)
        for line, col, pattern in visitor.violations
    ]


def format_violation(v: Violation) -> str:
    """Format a Violation as a lint message."""
    return (
        f"{v.file}:{v.line}:{v.col}: [VERONICA-E001] "
        f"Forbidden raw exec: {v.pattern} — use SecureExecutor"
    )


def scan_paths(paths: list[Path]) -> list[Violation]:
    """Recursively scan *paths* for Python files and collect violations."""
    all_violations: list[Violation] = []
    for root in paths:
        if root.is_file() and root.suffix == ".py":
            all_violations.extend(check_file(root))
        elif root.is_dir():
            for py_file in sorted(root.rglob("*.py")):
                all_violations.extend(check_file(py_file))
    return all_violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list. Defaults to sys.argv[1:].

    Returns:
        0 if no violations found, 1 otherwise.
    """
    args = argv if argv is not None else sys.argv[1:]
    paths = [Path(a) for a in args] if args else [Path("src")]

    violations = scan_paths(paths)

    for v in violations:
        print(format_violation(v))

    if violations:
        print(f"\n{len(violations)} violation(s) found.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
