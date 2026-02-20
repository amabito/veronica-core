"""Secret masking utilities for VERONICA Security Containment Layer."""
from __future__ import annotations

import re
from typing import Any


# Pattern registry: (label, compiled_regex)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS access key
    ("AWS_KEY", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    # AWS secret key (40-char base64-ish after common prefixes)
    ("AWS_SECRET", re.compile(r"(?i)aws[_\-. ]?secret[_\-. ]?(?:access[_\-. ]?)?key\s*[=:]\s*([^\s,;\"']{20,})")),
    # GitHub tokens
    ("GITHUB_TOKEN", re.compile(r"\b(gh[ps]_[A-Za-z0-9]{36,})\b")),
    # Stripe live keys
    ("STRIPE_KEY", re.compile(r"\b(sk_live_[A-Za-z0-9]{24,}|pk_live_[A-Za-z0-9]{24,})\b")),
    # Firebase / Supabase service role JWTs (long JWT tokens with known header)
    ("SERVICE_ROLE_JWT", re.compile(r"\beyJ[A-Za-z0-9+/=]{40,}\.[A-Za-z0-9+/=]{40,}\.[A-Za-z0-9\-_]{20,}\b")),
    # Resend API keys
    ("RESEND_KEY", re.compile(r"\b(re_[A-Za-z0-9]{20,})\b")),
    # bitbank API keys (32-char hex-ish)
    ("BITBANK_KEY", re.compile(r"(?i)bitbank[_\-. ]?(?:api[_\-. ]?)?(?:key|secret)\s*[=:]\s*([^\s,;\"']{20,})")),
    # Generic 32+ hex strings (must be standalone to avoid false positives on hashes)
    ("HEX_SECRET", re.compile(r"(?<![A-Za-z0-9])([0-9a-fA-F]{32,64})(?![A-Za-z0-9])")),
    # password=value, passwd=value, secret=value patterns
    ("PASSWORD_KV", re.compile(
        r"(?i)(?:password|passwd|secret|token|api_key|apikey|access_key)\s*[=:]\s*([^\s,;\"'&]{4,})"
    )),
]


class SecretMasker:
    """Masks sensitive secrets in strings, dicts, and argument lists."""

    def mask(self, text: str) -> str:
        """Replace detected secrets in *text* with ``[REDACTED:<type>]``."""
        for label, pattern in _PATTERNS:
            replacement = f"[REDACTED:{label}]"

            def _replace(m: re.Match[str], _label: str = label) -> str:  # noqa: E731
                # If the pattern has a capture group, replace only the group
                if m.lastindex:
                    full = m.group(0)
                    captured = m.group(1)
                    return full.replace(captured, f"[REDACTED:{_label}]", 1)
                return f"[REDACTED:{_label}]"

            text = pattern.sub(_replace, text)
        return text

    def mask_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        """Recursively mask string values inside a dict."""
        result: dict[str, Any] = {}
        for key, value in d.items():
            if isinstance(value, str):
                result[key] = self.mask(value)
            elif isinstance(value, dict):
                result[key] = self.mask_dict(value)
            elif isinstance(value, list):
                result[key] = self.mask_args(value) if all(isinstance(v, str) for v in value) else value
            else:
                result[key] = value
        return result

    def mask_args(self, args: list[str]) -> list[str]:
        """Mask secrets in each element of a list of strings."""
        return [self.mask(arg) for arg in args]
