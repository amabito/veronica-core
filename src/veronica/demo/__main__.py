"""VERONICA demo -- entry point for ``python -m veronica.demo``."""
from __future__ import annotations

import sys

from veronica.demo.runner import run_demo


def main() -> None:
    """Parse optional CLI argument for JSONL path and run the demo."""
    jsonl_path = sys.argv[1] if len(sys.argv) > 1 else "./veronica-demo-events.jsonl"
    run_demo(jsonl_path=jsonl_path)


if __name__ == "__main__":
    main()
