"""Filter-on-ingest: strip secrets before anything reaches raw/ or the wiki."""

from __future__ import annotations

import re

_PATTERNS = [
    (r"(?i)\b(password|passwd|pwd|パスワード|暗証番号)\s*[:：=]\s*\S+", r"\1: [REDACTED]"),
    (r"(?i)\b(api[_-]?key|secret|token|client[_-]?secret)\s*[:：=]\s*['\"]?[A-Za-z0-9_\-./+=]{8,}", r"\1: [REDACTED]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED-AWS-KEY]"),
    (r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b", "[REDACTED-GITHUB-TOKEN]"),
    (r"\bsk-[A-Za-z0-9_\-]{20,}\b", "[REDACTED-API-KEY]"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED-SLACK-TOKEN]"),
    (r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{20,}", "Bearer [REDACTED]"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED-PRIVATE-KEY]"),
    (r"\b(?:\d[ -]?){13,16}\b(?=\D|$)", None),  # card-like numbers: checked with Luhn below
]


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 16:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
    return s % 10 == 0


def redact(text: str, extra_patterns: list[str] | None = None) -> tuple[str, int]:
    count = 0

    def sub(pattern, repl, s, flags=re.S):
        nonlocal count
        if repl is None:
            def card(m):
                nonlocal count
                if _luhn(m.group(0)):
                    count += 1
                    return "[REDACTED-CARD]"
                return m.group(0)
            return re.sub(pattern, card, s)
        new, n = re.subn(pattern, repl, s, flags=flags)
        count += n
        return new

    for pattern, repl in _PATTERNS:
        text = sub(pattern, repl, text)
    for pattern in extra_patterns or []:
        text = sub(pattern, "[REDACTED]", text)
    return text, count
