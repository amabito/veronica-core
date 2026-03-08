"""veronica_core.cli -- Command-line utilities for veronica-core.

Currently provided commands:

new-adapter
    Generate adapter and test boilerplate for a new framework integration.

    Python API::

        from pathlib import Path
        from veronica_core.cli.new_adapter import generate_adapter

        paths = generate_adapter("myframework", Path("./output"))

    CLI (via __main__)::

        python -m veronica_core.cli new-adapter myframework --output-dir ./output
"""

from __future__ import annotations

from veronica_core.cli.new_adapter import generate_adapter

__all__ = ["generate_adapter"]


def main() -> None:
    """Entry point for the ``veronica`` CLI.

    Usage::

        python -m veronica_core.cli new-adapter <framework_name> [--output-dir DIR]
    """
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="veronica",
        description="veronica-core command-line utilities",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_adapter_parser = subparsers.add_parser(
        "new-adapter",
        help="Generate adapter and test boilerplate for a new framework",
    )
    new_adapter_parser.add_argument(
        "framework_name",
        help="Short lowercase framework identifier (e.g. myframework)",
    )
    new_adapter_parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write generated files (default: current directory)",
    )

    args = parser.parse_args()

    if args.command == "new-adapter":
        try:
            paths = generate_adapter(args.framework_name, Path(args.output_dir))
            for path in paths:
                print(f"[OK] Created: {path}")
        except (ValueError, FileExistsError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
