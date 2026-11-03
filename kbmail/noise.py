"""Text that carries no content: meeting responses without a comment, and boilerplate in messages.

"Accepted: Weekly sync" with an empty body (or only a classification line such as INTERNAL)
adds nothing to labeling, rules or the knowledge base. Who attends is in the calendar export
(each attendee's `response`). A response with a comment ("Declined - on leave, please send the
notes") is kept like any other mail.

`strip_boilerplate()` removes what repeats in every mail without saying anything: signature blocks
after a sign-off, legal disclaimers and "external sender" banners, Teams/Zoom/Webex join
instructions, and classification lines. It runs before text goes to Copilot (labeling and
selection batches) and into `raw/`, so neither Copilot's context nor the knowledge base pays for it.
It is deliberately conservative: when in doubt, text stays.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from kbmail.workspace import Workspace

# Outlook: IPM.Schedule.Meeting.Resp.Pos / .Neg / .Tent, and "X forwarded your meeting" notifications.
RESPONSE_CLASSES = ("ipm.schedule.meeting.resp", "ipm.schedule.meeting.notification.forward")
RESPONSE_SUBJECT_RE = re.compile(
    r"^\s*(accepted|declined|tentative|tentatively accepted|已接受|已拒绝|暂定|暂时接受)\s*[:\uff1a]",
    re.I,
)
# Text a response may carry that is not a comment: the copied invitation, join links, sign-offs.
_INVITE_RE = re.compile(r"^[\s>*_]*(-{2,}\s*(original appointment|原始约会)\s*-{2,}|_{20,})\s*$", re.I | re.M)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\W_]+", " ", t).strip()


_COURTESY = {
    _norm(c)
    for c in (
        "thanks",
        "thank you",
        "thx",
        "ok",
        "okay",
        "noted",
        "sure",
        "will attend",
        "see you",
        "see you there",
    )
}


def is_meeting_response(meta: dict) -> bool:
    cls = (meta.get("message_class") or "").lower()
    if cls:
        return cls.startswith(RESPONSE_CLASSES)
    return bool(RESPONSE_SUBJECT_RE.match(meta.get("subject") or ""))


def response_comment(ws: Workspace, body: str) -> str:
    """What the person wrote in a meeting response, without the copied invite and boilerplate lines."""
    from kbmail.history import split

    m = _INVITE_RE.search(body or "")
    own, _ = split(body[: m.start()] if m else body or "")
    patterns = [re.compile(p, re.I) for p in ws.cfg("outlook.boilerplate_lines", []) or []]
    lines = [ln for ln in own.splitlines() if not any(p.fullmatch(ln.strip()) for p in patterns)]
    return "\n".join(lines).strip()


def is_empty_response(ws: Workspace, meta: dict, body: str) -> bool:
    """A meeting response (accept/decline/tentative) with nothing written in it."""
    if not is_meeting_response(meta):
        return False
    note = _norm(response_comment(ws, body))
    return not note or note in _COURTESY


# ---------------------------------------------------------------- boilerplate inside messages

# Lines History.render() writes between a message's own text and its quoted messages.
_MARKER_RE = re.compile(r"^(--- quoted: .* ---|\[quoted: .*\]|\[added to the conversation here: .*\])$")
_SIGNOFF_WORDS = (
    r"(best|kind|warm|warmest|many thanks and|thanks and|thanks &|with best|with kind)?\s*regards"
    r"|best wishes|all the best|thanks|thank you|many thanks|thanks again|cheers|sincerely|yours sincerely"
    r"|yours truly|rgds|brgds|谢谢|此致|此致敬礼|祝好"
)
_AMBIGUOUS = r"best|yours|br|kr"  # also ordinary words ("Best option"): a name only after a comma
_PUNCT = r"[,.!\uff0c\u3002]"  # also the full-width comma and full stop
_NAME = r"(\s*[-\u2013\u2014]?\s*[A-Z\u4e00-\u9fff][^\s,.!]{0,29}){1,3}"
# "Best regards," / "Thanks, Sarah" / "Cheers - Ken" / "Best, Bob": a sign-off, optionally with a short name.
_SIGNOFF_RE = re.compile(
    rf"^(({_SIGNOFF_WORDS})\s*{_PUNCT}?({_NAME})?|({_AMBIGUOUS})\s*([,\uff0c]{_NAME}|{_PUNCT}?))$", re.I
)
_SIGNOFF_ONLY_RE = re.compile(rf"^({_SIGNOFF_WORDS}|{_AMBIGUOUS})\s*{_PUNCT}?$", re.I)
_SIG_DELIM_RE = re.compile(r"^--\s*$")  # RFC 3676 "-- "
_PS_RE = re.compile(r"^(p\.?\s?s\.?|n\.?b\.?)\b", re.I)
_DISCLAIMER_RES = [
    re.compile(p, re.I | re.S)
    for p in (
        r"\b(this|the information (contained )?in this|the contents of this)\s+(e-?mail|message|communication|"
        r"transmission)\b.{0,300}\b(is |are |may be |may contain |contains? )?(strictly )?(confidential|privileged|"
        r"intended (solely |only )?for)",
        r"\bif you (are not the intended recipient|have received this (e-?mail|message|communication)?\s*"
        r"(in error|by mistake))",
        r"\bplease consider the environment before printing",
        r"\b(originated|came|comes) from outside (of )?(the |your |our )?(organi[sz]ation|company)\b",
        r"\bdo not click (on )?links or open attachments unless\b",
        r"\bthis (e-?mail|message) has been (scanned|checked) (for viruses|by)\b",
    )
]
_JOIN_START_RE = re.compile(
    r"^(microsoft teams (meeting|need help\?)|join (the |this |zoom |webex |microsoft teams )?meeting( now)?"
    r"|join on your computer|click here to join the meeting|join with google meet|join zoom meeting)\b",
    re.I,
)
_JOINISH_RE = re.compile(
    r"meeting id|passcode|password|dial|tap mobile|join|teams|zoom|webex|meet\.google|learn more|meeting options"
    r"|help|conference id|local number|reset pin|organi[sz]er|download|privacy|phone|audio|video conferencing"
    r"|room|sip|h\.323|https?://|^\+?[\d\s()\-,#*.]{7,}|^or call in|^find a local",
    re.I,
)
_RULE_RE = re.compile(r"^[\s\\*]*(\\?_){10,}[\s\\*]*$")  # a line of underscores (Outlook/Teams invite frame)
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def _plain(line: str) -> str:
    """A Markdown/text line without emphasis, escapes, quote marks and link syntax, for matching."""
    t = _LINK_RE.sub(r"\1 \2", line)
    t = re.sub(r"[*\\`>#]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _drop_join_blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _JOIN_START_RE.match(_plain(lines[i]).strip("_ ")):
            out.append(lines[i])
            i += 1
            continue
        if out and _RULE_RE.match(out[-1]):
            out.pop()  # the frame line above the invite
        j, n = i + 1, 0
        while j < len(lines) and n < 40:
            t = _plain(lines[j])
            if _RULE_RE.match(lines[j]):
                j += 1  # closing frame line
                break
            if t and not _JOINISH_RE.search(t):
                break
            j, n = j + 1, n + 1
        i = j
    return out


def _paragraphs(lines: list[str]) -> list[list[str]]:
    paras, cur = [], []
    for ln in lines:
        if ln.strip():
            cur.append(ln)
        else:
            if cur:
                paras.append(cur)
            paras.append([ln])
            cur = []
    if cur:
        paras.append(cur)
    return paras


def _drop_disclaimers(lines: list[str], extra: tuple[re.Pattern, ...]) -> list[str]:
    out: list[str] = []
    for para in _paragraphs(lines):
        text = " ".join(_plain(ln) for ln in para)
        if text and len(text) <= 2000 and any(p.search(text) for p in (*_DISCLAIMER_RES, *extra)):
            continue
        out += para
    return out


def _drop_signature(lines: list[str]) -> list[str]:
    """Cut the block after the last sign-off when everything after it looks like a signature."""
    content = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(content) < 2:
        return lines
    for k in range(len(content) - 1, 0, -1):  # never the first line: "Thanks!" can open a message
        i = content[k]
        t = _plain(lines[i]).strip("_ ")
        delim = bool(_SIG_DELIM_RE.match(lines[i].strip()))
        if not (delim or _SIGNOFF_RE.match(t)):
            continue
        rest = [_plain(lines[j]) for j in content[k + 1 :]]
        keep = i + 1
        if not delim and _SIGNOFF_ONLY_RE.match(t) and rest and len(rest[0]) <= 40:
            keep = content[k + 1] + 1  # the name under "Best regards,"
            rest = rest[1:]
        if not rest:
            return lines
        if len(rest) > 10 or sum(map(len, rest)) > 800 or any(len(r) > 100 or _PS_RE.match(r) for r in rest):
            return lines  # too long or too wordy for a signature: content
        return lines[: i if delim else keep]
    return lines


@lru_cache(maxsize=32)
def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.I) for p in patterns)


def strip_boilerplate(text: str, extra_patterns=(), boilerplate_lines=()) -> str:
    """Text without signatures, disclaimers, meeting-join blocks and classification lines.

    Works on the output of History.render(): each part between the quoted-message marker lines is
    cleaned on its own, so a quoted message loses its signature too and markers are never touched.
    `extra_patterns`: regexes; a paragraph matching one is dropped. `boilerplate_lines`: regexes for
    whole lines to drop (`outlook.boilerplate_lines`)."""
    if not text:
        return text
    extra = _compile(tuple(extra_patterns or ()))
    whole = _compile(tuple(boilerplate_lines or ()))
    parts: list[list[str]] = [[]]
    for ln in text.split("\n"):
        if _MARKER_RE.match(ln.strip()):
            parts += [[ln], []]
        else:
            parts[-1].append(ln)
    out: list[str] = []
    for part in parts:
        if len(part) == 1 and _MARKER_RE.match(part[0].strip()):
            out += part
            continue
        part = [ln for ln in part if not any(p.fullmatch(_plain(ln)) for p in whole)]
        part = _drop_signature(_drop_disclaimers(_drop_join_blocks(part), extra))
        out += [*part, ""]  # keep the blank line before the next marker
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def strip_join_blocks(text: str) -> str:
    """Only the meeting-join blocks. Safe on a whole body before History.split(): their underscore
    frame lines would otherwise be read as the start of quoted mail."""
    return "\n".join(_drop_join_blocks(text.split("\n"))) if text else text


def clean(ws: Workspace, text: str) -> str:
    """strip_boilerplate() with the workspace settings (`cleanup.*`, `outlook.boilerplate_lines`)."""
    if not ws.cfg("cleanup.boilerplate", True):
        return text
    return strip_boilerplate(
        text, ws.cfg("cleanup.extra_patterns", []) or [], ws.cfg("outlook.boilerplate_lines", []) or []
    )


def render_clean(ws: Workspace, hist, rec: dict, body: str, **kw) -> str:
    """History.render() of a message without boilerplate: what Copilot and raw/ get to see."""
    if not ws.cfg("cleanup.boilerplate", True):
        return hist.render(rec, body, **kw)
    return clean(ws, hist.render(rec, strip_join_blocks(body), **kw))
