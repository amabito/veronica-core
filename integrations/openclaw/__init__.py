"""OpenClaw + VERONICA integration kit.

.. warning::
    **EXPERIMENTAL / UNSUPPORTED**

    This integration is provided as a demonstration and starting point only.
    It is **not** part of veronica-core's supported public API.

    - No stability guarantees: The interface may change or be removed without notice.
    - Not tested in CI: This integration has no automated tests in the main test suite.
    - No issue support: Bug reports for this integration may not be prioritized.
    - OpenClaw compatibility: Tested only against internal OpenClaw builds; may not
      work with other versions.

    For production use, consider implementing a custom integration using veronica-core's
    stable public API (``AIcontainer``, ``ExecutionContext``, ``VeronicaGuard``, etc.).

See also:
    ``integrations/openclaw/README.md`` — full usage guide and examples.
    ``integrations/openclaw/adapter.py`` — ``SafeOpenClawExecutor`` implementation.
    ``integrations/openclaw/demo.py`` — end-to-end demonstration script.
"""

import warnings

warnings.warn(
    "integrations.openclaw is experimental and unsupported. "
    "It is not part of veronica-core's stable public API and may change "
    "or be removed without notice. See integrations/openclaw/README.md for details.",
    UserWarning,
    stacklevel=2,
)

from integrations.openclaw.adapter import SafeOpenClawExecutor, wrap_openclaw_strategy

__all__ = ["SafeOpenClawExecutor", "wrap_openclaw_strategy"]
