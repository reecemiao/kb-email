"""Filter-on-ingest: strip secrets before anything reaches raw/ or the wiki."""

from __future__ import annotations

import re

# Password-like keywords. ASCII words need a word boundary on the left; Japanese ones must not have
# one, because `\b` never matches between two CJK characters (初期パスワード, 仮パスワード).
_PW_WORDS = r"(?:(?<![A-Za-z0-9_])(?:password|passwd|passcode|pwd|pw|pin)|パスワード|暗証番号|パスコード)"
# A value is ASCII (passwords practically always are), so Japanese text after the keyword is kept.
_QUOTED = r"""(?:'[^'\n]+'|"[^"\n]+"|「[^」\n]+」)"""
_VALUE = rf"(?:{_QUOTED}|[!-:<-~]+)"  # quoted, or printable ASCII except `;`

_PATTERNS = [
    # `password: x`, `パスワード：x`, `Password=x;` (connection strings)
    (rf"(?i)({_PW_WORDS})(\s*[:：=]\s*){_VALUE}", r"\1\2[REDACTED]"),
    # `password is x`, `パスワードは x です`: weaker separators, so an unquoted value must contain a
    # digit or symbol ("the password is required" keeps its text)
    (
        rf"(?i)({_PW_WORDS})(\s+is\s+|\s*(?:は|が)\s*)(?:{_QUOTED}|(?=[!-~]*[0-9!-/:-@\[-`{{-~])[!-:<-~]+)",
        r"\1\2[REDACTED]",
    ),
    (r"(?i)\b(api[_-]?key|secret|token|client[_-]?secret)\s*[:：=]\s*['\"]?[A-Za-z0-9_\-./+=]{8,}", r"\1: [REDACTED]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED-AWS-KEY]"),
    (r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b", "[REDACTED-GITHUB-TOKEN]"),
    (r"\bsk-[A-Za-z0-9_\-]{20,}\b", "[REDACTED-API-KEY]"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED-SLACK-TOKEN]"),
    (r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{20,}", "Bearer [REDACTED]"),
    (r"(?i)\b(authorization\s*[:=]\s*basic)\s+[A-Za-z0-9+/]{8,}={0,2}", r"\1 [REDACTED]"),
    # credentials in URLs: https://user:secret@host
    (r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^/\s@]+@", r"\1:[REDACTED]@"),
    # Azure storage / service bus connection strings and SAS signatures
    (r"(?i)\b(AccountKey|SharedAccessKey)\s*=\s*[A-Za-z0-9+/]{20,}={0,2}", r"\1=[REDACTED]"),
    (r"(?i)([?&]sig=)[A-Za-z0-9%+/=]{10,}", r"\1[REDACTED]"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED-PRIVATE-KEY]"),
]

# Card numbers: 13-19 digits, optionally grouped by spaces or dashes. The match never includes the
# characters around it, so "card 4111 ... 1111 exp" keeps its spaces.
_CARD = re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d])")
# Issuer prefixes: Diners 30/36/38, Amex 34/37, JCB 35, Visa 4, Mastercard 2/5, Discover/UnionPay 6.
_CARD_PREFIX = re.compile(r"^(?:3[0-8]|[2456])")
# An ungrouped run of digits is only a card when the text around it says so. Order numbers, tracking
# ids and ticket numbers pass the Luhn check about one time in ten and must not be lost.
_CARD_CONTEXT = re.compile(
    r"(?i)(card|credit|visa|master|amex|american express|jcb|diners|discover|unionpay|"
    r"カード|クレジット|クレカ)"
)
_CARD_CONTEXT_CHARS = 40


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
    return s % 10 == 0


def _is_card(m: re.Match, text: str) -> bool:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if not _luhn(digits) or not _CARD_PREFIX.match(digits):
        return False
    if raw != digits:  # written in groups (4111 1111 1111 1111, 3782-822463-10005)
        return True
    return bool(_CARD_CONTEXT.search(text, max(0, m.start() - _CARD_CONTEXT_CHARS), m.start()))


def redact(text: str, extra_patterns: list[str] | None = None) -> tuple[str, int]:
    count = 0

    def sub(pattern, repl, s, flags=re.S):
        nonlocal count
        new, n = re.subn(pattern, repl, s, flags=flags)
        count += n
        return new

    for pattern, repl in _PATTERNS:
        text = sub(pattern, repl, text)

    def card(m: re.Match) -> str:
        nonlocal count
        if _is_card(m, text):
            count += 1
            return "[REDACTED-CARD]"
        return m.group(0)

    text = _CARD.sub(card, text)
    for pattern in extra_patterns or []:
        text = sub(pattern, "[REDACTED]", text)
    return text, count
