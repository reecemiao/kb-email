"""Messages that carry no content of their own: meeting responses without a comment.

"Accepted: Weekly sync" with an empty body (or only a classification line such as INTERNAL)
adds nothing to labeling, rules or the knowledge base. Who attends is in the calendar export
(each attendee's `response`). A response with a comment ("Declined - on leave, please send the
notes") is kept like any other mail.
"""

from __future__ import annotations

import re
import unicodedata

from kbmail.workspace import Workspace

# Outlook: IPM.Schedule.Meeting.Resp.Pos / .Neg / .Tent, and "X forwarded your meeting" notifications.
RESPONSE_CLASSES = ("ipm.schedule.meeting.resp", "ipm.schedule.meeting.notification.forward")
RESPONSE_SUBJECT_RE = re.compile(
    r"^\s*(accepted|declined|tentative|tentatively accepted|承諾|辞退|仮承諾|"
    r"已接受|已拒绝|已拒絕|暂定|暫定|接受|拒绝|拒絕)\s*[:：]",
    re.I,
)
# Text a response may carry that is not a comment: the copied invitation, join links, sign-offs.
_INVITE_RE = re.compile(
    r"^[\s>*_]*(-{2,}\s*(original appointment|元の予定|原始约会|原始約會)\s*-{2,}|_{20,})\s*$", re.I | re.M
)


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
        "ありがとうございます",
        "ありがとうございました",
        "了解しました",
        "了解です",
        "承知しました",
        "よろしくお願いします",
        "よろしくお願いいたします",
        "谢谢",
        "謝謝",
        "收到",
        "好的",
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
