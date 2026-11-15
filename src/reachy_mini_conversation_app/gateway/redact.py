"""Remove device secrets from text that may be printed or stored."""

from __future__ import annotations


def redact(message: str, secrets: list[str]) -> str:
    text = message or ""
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:500]
