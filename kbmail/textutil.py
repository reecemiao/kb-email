"""Small text helpers shared by every step (front matter, quoting, tokenizing, hashing)."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

import yaml

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def parse_dt(value) -> datetime | None:
    """Parse an ISO string (or pass a datetime through) into an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- front matter


def dump_frontmatter(meta: dict, body: str) -> str:
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{head}\n---\n\n{body.rstrip()}\n"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end() :]


# ---------------------------------------------------------------- names


def slugify(text: str, maxlen: int = 60) -> str:
    """Filesystem-safe slug. Keeps CJK and other letters, collapses the rest to '-'."""
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = re.sub(r"^((re|fw|fwd|aw|wg|返信|転送)\s*[:：]\s*)+", "", text, flags=re.I)
    text = re.sub(r"[^\w]+", "-", text, flags=re.U).strip("-_")
    return (text[:maxlen].rstrip("-_")) or "untitled"


def normalize_subject(subject: str) -> str:
    s = unicodedata.normalize("NFKC", subject or "")
    s = re.sub(r"^\s*((re|fw|fwd|aw|wg|返信|転送|rv)\s*(\[\d+\])?\s*[:：]\s*)+", "", s, flags=re.I)
    s = re.sub(r"^\s*\[(ext|external|社外)\]\s*", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def email_domain(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[1] if "@" in address else ""


# ---------------------------------------------------------------- quoted text

# Headers that start the quoted history of a reply/forward (English + Japanese Outlook).
_QUOTE_PATTERNS = [
    r"^[\s>*_]*-{2,}\s*(original message|forwarded message|元のメッセージ|転送されたメッセージ)\s*-{2,}",
    r"^[\s>*_]*(from|差出人|送信者)\s*[:：][*_]*\s*.+",
    r"^[\s>*_]*on .{4,200}wrote:\s*$",
    r"^[\s>*_]*_{20,}\s*$",
    r"^[\s>*_]*\d{4}[年/-]\d{1,2}[月/-]\d{1,2}.{0,40}(wrote|書きました)[:：]?\s*$",
]
_QUOTE_RE = re.compile("|".join(f"(?:{p})" for p in _QUOTE_PATTERNS), re.I | re.M)
# A "From:" line only counts when a Sent/Date/To header follows shortly after.
_HEADER_FOLLOW_RE = re.compile(r"^[\s>*_]*(sent|date|to|送信日時|日時|宛先|件名|subject)\s*[:：]", re.I | re.M)


def strip_quoted(body: str) -> str:
    """Remove quoted reply history so each message carries only its own new text."""
    if not body:
        return ""
    for m in _QUOTE_RE.finditer(body):
        line = m.group(0)
        if re.match(r"^[\s>*_]*(from|差出人|送信者)\s*[:：]", line, re.I):
            window = body[m.end() : m.end() + 400]
            if not _HEADER_FOLLOW_RE.search(window):
                continue
        if m.start() == 0:
            continue
        body = body[: m.start()]
        break
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def compact(text: str, limit: int) -> str:
    text = re.sub(r"[ \t　]+", " ", text or "")
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + " …"
    return text


# ---------------------------------------------------------------- tokenizing / BM25

_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f"  # kana, CJK, hangul, half-width kana
_TOKEN_RE = re.compile(rf"[a-z0-9][a-z0-9_.\-]*[a-z0-9]|[a-z0-9]|[{_CJK}]+", re.I)
STOPWORDS = frozenset(
    (
        "a an the and or of to in on for with at by from is are was were be been this that it as re fw fwd "
        "please thanks thank you we i our your have has will can could would should not no yes if"
    ).split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; CJK runs become character bigrams (Japanese has no spaces)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        if re.match(rf"[{_CJK}]", tok):
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        elif tok not in STOPWORDS and len(tok) > 1:
            out.append(tok)
    return out


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: dict[str, Counter] = {}
        self.len: dict[str, int] = {}
        df: Counter = Counter()
        for key, text in docs.items():
            toks = tokenize(text)
            c = Counter(toks)
            self.tf[key] = c
            self.len[key] = len(toks)
            df.update(c.keys())
        n = max(len(docs), 1)
        self.avg = (sum(self.len.values()) / n) or 1.0
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        q = tokenize(query)
        scores: dict[str, float] = {}
        for key, tf in self.tf.items():
            s = 0.0
            dl = self.len[key]
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            if s > 0:
                scores[key] = s
        return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
