"""Quoted history in replies and forwards.

A reply or forward carries older messages below its own text ("From: ... Sent: ... To: ...",
"-----Original Message-----", "On ... wrote:"). Dropping that history loses information when the
older message is not in the export ("FYI" + a forwarded mail from someone outside the mailbox);
keeping it everywhere repeats the same text in every reply. So:

- `split()` cuts a body into its own text and the quoted messages (sender, date, recipients, text).
- `History` keeps a small fingerprint of every exported message (`state/history.json`) and checks
  whether a quoted message is one of them: same sender, a date that differs only by a time zone,
  and the exported message's text found inside the quote.
- `History.render()` gives a message's own text plus only the quoted messages that are NOT already
  exported (in the given scope), with a one-line pointer for the others, and who was added to the
  conversation compared with the quoted message it replies to.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kbmail.records import fmt_people, fmt_person, message_index, read_message
from kbmail.textutil import normalize_subject, parse_dt, short_hash
from kbmail.workspace import Workspace

STATE = "state/history.json"
_VERSION = 1

# ---------------------------------------------------------------- parsing

_FIELDS = {
    "from": "from|差出人|送信者|発信者|发件人|發件人|寄件者",
    "sent": "sent|date|送信日時|日時|日付|发送时间|發送時間|发送日期|时间|時間",
    "to": "to|宛先|收件人|收件者",
    "cc": "cc|bcc|抄送|副本",
    "subject": "subject|件名|主题|主旨|主題",
    "other": "importance|attachments|重要度|添付ファイル|附件",
}
_FIELD_RE = re.compile(
    r"^[\s>*_]*(?P<k>"
    + "|".join(f"(?P<{k}>{v})" for k, v in _FIELDS.items())
    + r")\s*[*_]*\s*[:：]\s*[*_]*\s*(?P<v>.*)$",
    re.I,
)
_INLINE_FIELD_RE = re.compile(
    r"\s+(?=[*_]*(?:" + "|".join(_FIELDS[k] for k in ("sent", "to", "cc", "subject")) + r")[*_]*\s*[:：])", re.I
)
_SEPARATOR_RE = re.compile(
    r"^[\s>*_]*(?:-{2,}\s*(?:original message|forwarded message|元のメッセージ|転送されたメッセージ|原始邮件|原始郵件)"
    r"\s*-{2,}|_{20,})\s*$",
    re.I,
)
_WROTE_RE = re.compile(
    r"^[\s>*_]*(?:on .{4,200}wrote:|\d{4}[年/-]\d{1,2}[月/-]\d{1,2}.{0,80}(?:wrote|書きました|写道)[:：]?|"
    r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}.{0,80}<[^>]+@[^>]+>\s*[:：])\s*$",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class Segment:
    """One quoted message inside a reply or forward."""

    sender: dict = field(default_factory=dict)
    sent: str = ""
    to: list[dict] = field(default_factory=list)
    cc: list[dict] = field(default_factory=list)
    subject: str = ""
    text: str = ""

    @property
    def dates(self) -> list[datetime]:
        return parse_loose_dt(self.sent)


def _person(value: str) -> dict:
    value = re.sub(r"\[([^\]]*)\]\((?:mailto:)?[^)]*\)", r"\1", value or "")  # markdown links
    m = EMAIL_RE.search(value)
    email = m.group(0).lower() if m else ""
    name = EMAIL_RE.sub("", value)
    name = re.sub(r"(?i)\bmailto:|[<>\[\]()\"'*_\\]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" ;,")
    return {"name": name, "email": email}


def _people(value: str) -> list[dict]:
    value = (value or "").strip()
    if not value:
        return []
    parts = [p for p in re.split(r";", value) if p.strip()]
    if len(parts) == 1 and len(EMAIL_RE.findall(value)) > 1:
        parts = re.findall(r"[^,<]*<[^>]+>|[^,\s]+@[^,\s]+", value)
    return [p for p in (_person(x) for x in parts) if p["name"] or p["email"]]


def _parse_header(lines: list[str], i: int) -> tuple[dict, int] | None:
    """Read 'From/Sent/To/Cc/Subject' lines from line i. Returns (fields, next line) or None."""
    fields: dict[str, str] = {}
    last, blanks, j = None, 0, i
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            blanks += 1
            if blanks > 3 or (blanks > 1 and "subject" in fields):
                break
            j += 1
            continue
        m = _FIELD_RE.match(line)
        if m:
            key = next(k for k in _FIELDS if m.group(k))
            if key == "from" and "from" in fields:
                break  # the next quoted message
            if key != "other":
                fields.setdefault(key, m.group("v").strip())
            last, blanks = key, 0
        elif last in ("to", "cc") and blanks == 0 and not fields.get("subject"):
            fields[last] += " " + line.strip()  # wrapped recipient list
        else:
            break
        j += 1
    if "from" in fields and fields.keys() & {"sent", "to", "subject"}:
        return fields, j
    return None


def _split_inline_header(line: str) -> list[str]:
    """'**From:** A **Sent:** B **To:** C' on one line (HTML headers) -> one field per line."""
    if _FIELD_RE.match(line) and len(_INLINE_FIELD_RE.findall(line)) >= 1:
        m = _FIELD_RE.match(line)
        if m and m.group("from"):
            return _INLINE_FIELD_RE.split(line)
    return [line]


def _wrote_header(line: str) -> dict:
    p = _person(line)
    m = re.search(r",\s*([^,<]+?)\s*(?:<[^>]*>)?\s*(?:wrote|書きました|写道)?\s*[:：]?\s*$", line)
    name = "" if p["email"] or not m else m.group(1).strip()
    return {"from": "", "sender": {"name": name, "email": p["email"]}, "sent": line}


def split(body: str) -> tuple[str, list[Segment]]:
    """Own text of a message and the quoted messages below it (newest first)."""
    if not body:
        return "", []
    lines: list[str] = []
    for ln in body.replace("\r\n", "\n").split("\n"):
        lines.extend(_split_inline_header(ln))
    starts: list[tuple[int, int, dict]] = []  # (first line, first body line, header)
    i = 0
    while i < len(lines):
        line = lines[i]
        hdr = None
        if _SEPARATOR_RE.match(line):  # a separator without a header we can read still starts a quote
            hdr = _parse_header(lines, i + 1) or ({"from": ""}, i + 1)
        elif _FIELD_RE.match(line) and _FIELD_RE.match(line).group("from"):
            hdr = _parse_header(lines, i)
        elif _WROTE_RE.match(line):
            hdr = (_wrote_header(line), i + 1)
        if hdr:
            starts.append((i, hdr[1], hdr[0]))
            i = max(hdr[1], i + 1)
            continue
        i += 1
    own_end = starts[0][0] if starts else len(lines)
    own = [ln for ln in lines[:own_end] if not ln.lstrip().startswith(">")]
    segs = []
    for n, (_, body_start, hdr) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        text = "\n".join(re.sub(r"^(\s*>)+ ?", "", ln) for ln in lines[body_start:end])
        segs.append(
            Segment(
                sender=hdr.get("sender") or _person(hdr.get("from", "")),
                sent=hdr.get("sent", ""),
                to=_people(hdr.get("to", "")),
                cc=_people(hdr.get("cc", "")),
                subject=re.sub(r"[*_\\]+", "", hdr.get("subject", "")).strip(),
                text=_tidy(text),
            )
        )
    return _tidy("\n".join(own)), segs


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in text.split("\n"))).strip()


# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_WEEKDAY_RE = re.compile(
    r"(?i)\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?|[(（][月火水木金土日一二三四五六天][)）]|"
    r"[月火水木金土日]曜日?|(星期|周|週|礼拜|禮拜)[一二三四五六日天]"
)


def parse_loose_dt(text: str) -> list[datetime]:
    """Dates as shown in quoted headers ('Thursday, July 30, 2026 3:48 PM', '2026年7月30日 15:48').

    Returns naive local times; two candidates when day and month are ambiguous (07/08/2026)."""
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(
        r"(午前|午後|上午|下午|早上|晚上)\s*(\d{1,2}:\d{2})",
        lambda m: f"{m.group(2)} " + ("AM" if m.group(1) in ("午前", "上午", "早上") else "PM"),
        s,
    )
    s = _WEEKDAY_RE.sub(" ", s)
    s = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", r"\1-\2-\3 ", s)
    s = re.sub(r"\bat\b|,", " ", s, flags=re.I)
    tm = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap])?\.?\s*m?\.?", s, re.I)
    if not tm:
        return []
    hour, minute = int(tm.group(1)), int(tm.group(2))
    if tm.group(3):
        hour = hour % 12 + (12 if tm.group(3).lower() == "p" else 0)
    s = s[: tm.start()] + " " + s[tm.end() :]
    days: list[tuple[int, int, int]] = []
    if m := re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s):
        days.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    elif m := re.search(r"(\d{1,2})\s+([a-z]{3,})\.?\s+(\d{4})", s, re.I):
        if mon := _MONTHS.get(m.group(2)[:3].lower()):
            days.append((int(m.group(3)), mon, int(m.group(1))))
    elif m := re.search(r"([a-z]{3,})\.?\s+(\d{1,2})\s+(\d{4})", s, re.I):
        if mon := _MONTHS.get(m.group(1)[:3].lower()):
            days.append((int(m.group(3)), mon, int(m.group(2))))
    elif m := re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        days += [(y, b, a)] if a > 12 else [(y, a, b)] if b > 12 else [(y, b, a), (y, a, b)]
    out = []
    for y, mo, d in days:
        try:
            out.append(datetime(y, mo, d, hour, minute))
        except ValueError:
            pass
    return out


def same_moment(quoted: list[datetime], actual: str | None) -> bool | None:
    """Could a quoted local time be this message's time in some time zone? None = unknown."""
    dt = parse_dt(actual)
    if not quoted or not dt:
        return None
    utc = dt.astimezone(UTC).replace(tzinfo=None)
    for q in quoted:
        minutes = (q - utc).total_seconds() / 60
        if -12 * 60 - 5 <= minutes <= 14 * 60 + 5:
            off = minutes % 30
            if min(off, 30 - off) <= 4:  # time zones are whole or half hours; clocks round to the minute
                return True
    return False


# ---------------------------------------------------------------- fingerprints and matching


def normalize(text: str) -> str:
    """Letters and digits only, so line wrapping, markdown and link formatting don't matter."""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"\]\([^)]*\)", "]", t)  # markdown link targets
    t = re.sub(r"<[^<>\n]{1,300}>", " ", t)  # <mailto:..> / <https://..> added by plain-text bodies
    t = re.sub(r"(?i)\b(?:https?://|mailto:|www\.)\S+", " ", t)
    return "".join(ch for ch in t.lower() if ch.isalnum())


def name_key(name: str) -> str:
    """'MORGAN, Alex' and 'Alex MORGAN' -> 'alex morgan'."""
    toks = re.findall(r"\w+", unicodedata.normalize("NFKC", name or "").casefold())
    return " ".join(sorted(toks))


PROBE = 40


def fingerprint(own: str) -> dict:
    n = normalize(own)
    head = n[:1500]
    if len(head) < 60:
        return {"n": len(n), "short": head}
    probes = [head[:PROBE], head[len(head) // 2 - PROBE // 2 :][:PROBE], head[-PROBE:]]
    return {"n": len(n), "probes": probes}


def _text_match(fp: dict, seg_norm: str, strong: bool) -> bool:
    if "probes" in fp:
        hits = sum(p in seg_norm for p in fp["probes"])
        return hits >= (2 if strong else 3)
    short = fp.get("short", "")
    return bool(short) and strong and short in seg_norm[: len(short) + 200]


class History:
    """Fingerprints of exported messages and quoted-history matching against them."""

    def __init__(self, ws: Workspace, index=None, save: bool = True):
        self.ws = ws
        self.index = index or message_index(ws)
        state = ws.load_json(STATE, {})
        cache = state.get("messages", {}) if state.get("v") == _VERSION else {}
        self.fps: dict[str, dict] = {}
        changed = False
        for rec in self.index.all():
            if rec.get("source") == "teams" or rec.get("kind") == "chat":
                continue
            sig = f"{rec.get('path')}|{rec.get('updated_at') or rec.get('exported_at') or ''}"
            fp = cache.get(rec["id"])
            if not fp or fp.get("sig") != sig:
                try:
                    _, body = read_message(ws, rec)
                except FileNotFoundError:
                    continue
                own, _ = split(body)
                fp = {"sig": sig, **fingerprint(own)}
                changed = True
            self.fps[rec["id"]] = fp
        changed |= set(cache) != set(self.fps)
        self.by_email: dict[str, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_subject: dict[str, list[str]] = {}
        for mid in self.fps:
            rec = self.index.get(mid)
            frm = rec.get("from") or {}
            if frm.get("email"):
                self.by_email.setdefault(frm["email"].lower(), []).append(mid)
            if frm.get("name"):
                self.by_name.setdefault(name_key(frm["name"]), []).append(mid)
            self.by_subject.setdefault(normalize_subject(rec.get("subject", "")), []).append(mid)
        if changed and save:
            ws.save_json(STATE, {"v": _VERSION, "messages": self.fps})

    @classmethod
    def of(cls, ws: Workspace) -> History:
        """One History per workspace object, rebuilt when the message index file changes."""
        p = ws.path("state/index.jsonl")
        stamp = p.stat().st_mtime_ns if p.exists() else 0
        cached = getattr(ws, "_history", None)
        if not cached or cached[0] != stamp:
            cached = (stamp, cls(ws))
            ws._history = cached
        return cached[1]

    def context(self, rec: dict, body: str) -> str:
        """Own text + quoted text not exported elsewhere, without notes (rules, search)."""
        return self.render(rec, body, pointers=False)

    # -- matching

    def match(self, seg: Segment, exclude: str | None = None, scope=None) -> str | None:
        """Id of the exported message this quoted message is a copy of, if any."""
        seg_norm = normalize(seg.text)
        if not seg_norm:
            return None
        dates = seg.dates
        email, key = (seg.sender.get("email") or "").lower(), name_key(seg.sender.get("name", ""))
        by_sender = set(self.by_email.get(email, [])) | (set(self.by_name.get(key, [])) if key else set())
        others = set(self.by_subject.get(normalize_subject(seg.subject), [])) - by_sender if seg.subject else set()
        best = None
        for mid in sorted(by_sender | others, key=lambda m: self.index.get(m).get("date", ""), reverse=True):
            if mid == exclude or (scope is not None and mid not in scope):
                continue
            when = same_moment(dates, self.index.get(mid).get("date"))
            if when is False:
                continue
            strong = mid in by_sender and (when is True or "probes" in self.fps[mid])
            if _text_match(self.fps[mid], seg_norm, strong):
                if when is True:
                    return mid
                best = best or mid
        return best

    # -- rendering

    def render(
        self,
        rec: dict,
        body: str,
        scope=None,
        seen: dict | None = None,
        labels: dict | None = None,
        pointers: bool = True,
    ) -> str:
        """Own text + quoted messages not already exported (in `scope`; None = anywhere).

        `seen` (shared across the messages of one thread) prints quoted text only once.
        `pointers=False` leaves out the one-line notes (for rules and search)."""
        own, segs = split(body)
        out = [own] if own else []
        added = self.added(rec, segs)
        if added and pointers:
            out.append(f"[added to the conversation here: {fmt_people(added, 8)}]")
        for seg in segs:
            head = " | ".join(x for x in (fmt_person(seg.sender), seg.sent, seg.subject) if x)
            mid = self.match(seg, exclude=rec.get("id"), scope=scope)
            if mid:
                if pointers:
                    lab = (labels or {}).get(mid)
                    tag = f", labels: {', '.join(lab['labels'])}" if lab else ""
                    out.append(f"[quoted: {head} = message {mid}, exported separately{tag}]")
                continue
            if seen is not None:
                norm = normalize(seg.text)
                h = short_hash(norm[:1500], 12)
                if h in seen or any(len(norm) > 80 and norm[:1500] in s for s in seen.get("_texts", [])):
                    if pointers:
                        out.append(f"[quoted: {head}, same text as in message {seen.get(h, 'above')}]")
                    continue
                seen[h] = rec.get("id")
                seen.setdefault("_texts", []).append(norm[:5000])
            if seg.text:
                out.append((f"--- quoted: {head} ---\n" if pointers else "") + seg.text)
        return "\n\n".join(out).strip()

    @staticmethod
    def added(rec: dict, segs: list[Segment]) -> list[dict]:
        """People on this message who were not on the quoted message it answers or forwards."""
        if not segs or not (segs[0].to or segs[0].cc):
            return []
        prev = [segs[0].sender, *segs[0].to, *segs[0].cc]
        emails = {p["email"] for p in prev if p.get("email")}
        names = {name_key(p["name"]) for p in prev if p.get("name")}
        out = []
        for p in [rec.get("from") or {}, *(rec.get("to") or []), *(rec.get("cc") or [])]:
            if not p or (p.get("email") or "").lower() in emails or name_key(p.get("name", "")) in names:
                continue
            if p not in out:
                out.append(p)
        return out


# ---------------------------------------------------------------- reports


def explain(ws: Workspace, mid: str) -> str:
    """`kbmail history show <id>`: the quoted messages in one message and what they match."""
    h = History(ws)
    rec = h.index.get(mid)
    if not rec:
        raise KeyError(mid)
    _, body = read_message(ws, rec)
    own, segs = split(body)
    lines = [
        f"{mid}  {rec.get('date', '')[:16]}  {fmt_person(rec.get('from'))}  {rec.get('subject', '')}",
        f"own text: {len(own)} chars, quoted messages: {len(segs)}",
    ]
    added = h.added(rec, segs)
    if added:
        lines.append(f"added to the conversation: {fmt_people(added, 20)}")
    for n, seg in enumerate(segs, 1):
        m = h.match(seg, exclude=mid)
        when = ", ".join(d.strftime("%Y-%m-%d %H:%M") for d in seg.dates) or "?"
        lines += [
            "",
            f"[{n}] from {fmt_person(seg.sender) or '?'} | sent {seg.sent or '?'} ({when}) | {seg.subject}",
            f"    to: {fmt_people(seg.to, 6) or '-'}" + (f" | cc: {fmt_people(seg.cc, 6)}" if seg.cc else ""),
            f"    {len(seg.text)} chars -> "
            + (f"already exported as {m} ({h.index.get(m).get('date', '')[:16]})" if m else "NOT exported: kept"),
        ]
    return "\n".join(lines)


def status(ws: Workspace) -> dict:
    """Counts over the whole export (reads every message)."""
    h = History(ws)
    stats = {"messages": 0, "with_history": 0, "quoted": 0, "already_exported": 0, "kept": 0}
    contained: set[str] = set()
    for mid in h.fps:
        rec = h.index.get(mid)
        try:
            _, body = read_message(ws, rec)
        except FileNotFoundError:
            continue
        _, segs = split(body)
        stats["messages"] += 1
        stats["with_history"] += bool(segs)
        for seg in segs:
            stats["quoted"] += 1
            m = h.match(seg, exclude=mid)
            if m:
                stats["already_exported"] += 1
                contained.add(m)
            else:
                stats["kept"] += 1
    stats["messages_quoted_elsewhere"] = len(contained)
    return stats
