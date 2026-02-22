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
    # GitHub new fine-grained PAT format: github_pat_... (must be BEFORE GITHUB_TOKEN)
    ("GITHUB_FINE_GRAINED", re.compile(r"\b(github_pat_[A-Za-z0-9_]{82,})\b")),
    # GitHub CLI OAuth tokens: gho_... (must be BEFORE GITHUB_TOKEN to avoid gh[o] overlap)
    ("GITHUB_CLI_TOKEN", re.compile(r"\b(gho_[A-Za-z0-9]{36,})\b")),
    # GitHub tokens (classic PAT + server; excludes gho_ which is caught above)
    ("GITHUB_TOKEN", re.compile(r"\b(gh[psua]_[A-Za-z0-9]{36,})\b")),
    # Stripe live keys
    ("STRIPE_KEY", re.compile(r"\b(sk_live_[A-Za-z0-9]{24,}|pk_live_[A-Za-z0-9]{24,})\b")),
    # Firebase / Supabase service role JWTs (long JWT tokens with known header)
    ("SERVICE_ROLE_JWT", re.compile(r"\beyJ[A-Za-z0-9+/=]{40,}\.[A-Za-z0-9+/=]{40,}\.[A-Za-z0-9\-_]{20,}\b")),
    # Resend API keys
    ("RESEND_KEY", re.compile(r"\b(re_[A-Za-z0-9]{20,})\b")),
    # bitbank API keys (32-char hex-ish)
    ("BITBANK_KEY", re.compile(r"(?i)bitbank[_\-. ]?(?:api[_\-. ]?)?(?:key|secret)\s*[=:]\s*([^\s,;\"']{20,})")),
    # Anthropic API keys — must come BEFORE OPENAI_KEY (sk-ant- is a subset of sk-)
    ("ANTHROPIC_KEY", re.compile(r"\b(sk-ant-[A-Za-z0-9\-_]{20,})\b")),
    # OpenAI API keys (sk-proj-* and legacy sk-*)
    ("OPENAI_KEY", re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9\-_]{20,})\b")),
    # Slack bot/user/webhook tokens
    ("SLACK_TOKEN", re.compile(r"\b(xox[bposa]-[A-Za-z0-9\-]{10,})\b")),
    # Slack webhook URLs
    ("SLACK_WEBHOOK", re.compile(r"(https://hooks\.slack\.com/services/[A-Za-z0-9/]+)")),
    # Discord bot tokens
    ("DISCORD_TOKEN", re.compile(r"\b([MNO][A-Za-z0-9_\-]{23})\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b")),
    # Twilio account SIDs and auth tokens
    ("TWILIO_SID", re.compile(r"\b(AC[0-9a-f]{32})\b")),
    ("TWILIO_TOKEN", re.compile(r"(?i)twilio[_\-. ]?(?:auth[_\-. ]?)?token\s*[=:]\s*([^\s,;\"']{20,})")),
    # SendGrid API keys
    ("SENDGRID_KEY", re.compile(r"\b(SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43})\b")),
    # Google API keys
    ("GOOGLE_API_KEY", re.compile(r"\b(AIza[0-9A-Za-z\-_]{35})\b")),
    # Google OAuth2 client secrets
    ("GOOGLE_OAUTH", re.compile(r"\b(GOCSPX-[A-Za-z0-9_\-]{28,})\b")),
    # Azure SAS tokens and connection strings
    ("AZURE_SAS", re.compile(r"(?i)(?:sig|SharedAccessSignature)[=&]([A-Za-z0-9%+/=]{20,})")),
    # PGP private key blocks
    ("PGP_PRIVATE_KEY", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    # SSH / RSA / EC / DSA / OPENSSH private key blocks (expanded to cover all OpenSSH formats)
    ("SSH_PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    # .netrc password field — "password <value>" on its own line or inline
    ("NETRC_PASSWORD", re.compile(r"(?i)\bpassword\s+([^\s\r\n]{4,})")),
    # npm tokens
    ("NPM_TOKEN", re.compile(r"\b(npm_[A-Za-z0-9]{36,})\b")),
    # PyPI API tokens (real tokens are 50+ chars after pypi-)
    ("PYPI_TOKEN", re.compile(r"\b(pypi-[A-Za-z0-9_\-]{50,})\b")),
    # Polymarket API keys (common in this project)
    ("POLYMARKET_KEY", re.compile(r"(?i)polymarket[_\-. ]?(?:api[_\-. ]?)?(?:key|secret|token)\s*[=:]\s*([^\s,;\"']{20,})")),
    # Generic 32+ hex strings (must be standalone to avoid false positives on hashes).
    # No upper bound: 65+ char secrets (SHA-512 digests, long API tokens) are also redacted.
    ("HEX_SECRET", re.compile(r"(?<![A-Za-z0-9])([0-9a-fA-F]{32,})(?![A-Za-z0-9])")),
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
