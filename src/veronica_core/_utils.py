"""Internal utilities shared across veronica_core modules.

Do not import from this module in public API; it is private.
"""

from __future__ import annotations

import re

# Sentinel hash used as prev_hash for the first entry in any audit chain.
# 64 hex zeros -- chosen to be visually distinct from a real SHA-256 digest.
GENESIS_HASH: str = "0" * 64


def freeze_mapping(obj: object, field_name: str) -> None:
    """Freeze a dict field on a frozen dataclass to MappingProxyType.

    Replaces the named attribute with an immutable MappingProxyType so that
    callers cannot mutate the dict after construction.  Must be called from
    __post_init__ of a frozen dataclass.

    Args:
        obj: The frozen dataclass instance.
        field_name: Name of the dict attribute to freeze.
    """
    import types as _types

    val = getattr(obj, field_name)
    object.__setattr__(obj, field_name, _types.MappingProxyType(dict(val)))


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
