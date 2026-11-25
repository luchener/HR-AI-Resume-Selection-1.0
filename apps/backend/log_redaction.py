"""Email redaction helpers for application logs."""
from __future__ import annotations

import logging
import re
from typing import Any


_REDACTED = "<redacted>"
_EMAIL_RE = re.compile(
    # Keep the full local part: auth.py accepts any non-whitespace text before @.
    r"(?<![\w@])"
    r"[^@\s]+"
    r"@"
    r"(?:[\w\-]+\.)+[\w\-]+"
    r"(?![\w\-@])",
    re.UNICODE,
)


def mask_email(value: Any) -> str:
    """Return a log-safe email representation without exposing the local part."""
    text = "" if value is None else str(value)
    if not _EMAIL_RE.fullmatch(text):
        return _REDACTED
    local, domain = text.rsplit("@", 1)
    if local.startswith('"'):
        return f'"***"@{domain}'
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def redact_email_addresses(value: Any) -> str:
    """Redact every email-shaped token in arbitrary text such as exceptions."""
    return _EMAIL_RE.sub(lambda match: mask_email(match.group(0)), str(value or ""))


class RedactingFormatter(logging.Formatter):
    """Redact emails from final log output, including exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_email_addresses(super().format(record))
