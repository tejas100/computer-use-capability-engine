"""
Redaction: strips patterns that look like secrets or raw sensitive
data before anything is written to an artifact file or an evidence
log entry.

Per section 3.4: "Never persist secrets or raw sensitive data
(credentials, tokens, full PII) into artifacts or logs. Redact
appropriately."

This mock_bank domain has no real credentials or tokens flowing
through it -- there's no login step, no API keys typed into any form
-- so there is admittedly little for this to catch in practice here.
It's included anyway, applied at the actual write boundaries (log
entries and the final artifact write), because a real deployment
against actual back-office tools would have exactly these patterns
flowing through the same code paths, and the redaction needs to sit
at the boundary regardless of whether today's demo data happens to
trigger it. This is intentionally pattern-based and conservative
(over-redact rather than under-redact) rather than an exhaustive PII
classifier, which is a much larger problem out of scope here -- see
/REPORT.md section 7 (Cuts).
"""

from __future__ import annotations

import re

# Patterns broad enough to catch common secret/PII shapes without
# needing per-field knowledge of what a given app calls things.
_PATTERNS: dict[str, re.Pattern] = {
    "password_field": re.compile(r'(?i)(password|passwd|pwd)\s*[:=]\s*["\']?[^\s"\']{4,}["\']?'),
    "bearer_token": re.compile(r'(?i)bearer\s+[a-zA-Z0-9\-_\.]{16,}'),
    "api_key_assignment": re.compile(r'(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*["\']?[a-zA-Z0-9\-_\.]{12,}["\']?'),
    "ssn": re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "credit_card": re.compile(r'\b(?:\d[ -]*?){13,16}\b'),
}

_REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    """Replace any matched pattern in `text` with a redaction marker.
    Safe to call on arbitrary strings (log messages, step descriptions,
    reasoning text) before they're written anywhere persistent."""
    if not text:
        return text
    result = text
    for name, pattern in _PATTERNS.items():
        result = pattern.sub(f"{_REDACTED}<{name}>", result)
    return result


def redact_dict(data: dict) -> dict:
    """Recursively redact string values in a dict/list structure --
    used on log entries and artifact dicts before they're serialized,
    so redaction happens once at the boundary rather than needing to
    be threaded through every call site that builds these structures."""
    if isinstance(data, dict):
        return {k: redact_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_dict(v) for v in data]
    if isinstance(data, str):
        return redact_text(data)
    return data