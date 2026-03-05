"""Internal utilities shared across veronica_core modules.

Do not import from this module in public API; it is private.
"""

from __future__ import annotations

import re


def redact_exc(exc: BaseException) -> str:
    """Return exception type and message with Redis URLs redacted.

    Prevents credential leakage when ``redis://user:password@host/...``
    appears in exception strings (e.g. ``ConnectionError``).

    Handles ``redis://``, ``rediss://``, ``redis+ssl://``, ``rediss+ssl://``
    (case-insensitive), and passwords containing literal ``@`` characters.
    """
    msg = str(exc)
    # Redact user:password in Redis URLs.
    # - ``rediss?`` matches redis:// and rediss://
    # - ``(?:\\+ssl)?`` matches optional +ssl suffix
    # - ``\\S+@`` greedy match handles passwords with literal '@' (backtracks to last @)
    # - ``(?=\\S)`` ensures the @ is followed by a hostname, not trailing whitespace
    msg = re.sub(
        r"(rediss?(?:\+ssl)?://)\S+@(?=\S)",
        r"\1***@",
        msg,
        flags=re.IGNORECASE,
    )
    return f"{type(exc).__name__}: {msg}"
