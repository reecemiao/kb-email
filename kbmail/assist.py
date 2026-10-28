"""Drafting support (step 10): show/search mail, triage what needs a reply, writing-style samples."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from kbmail.history import History
from kbmail.labels import load_labels
from kbmail.records import calendar_index, fmt_people, fmt_person, message_index, read_message, threads
from kbmail.textutil import BM25, compact, now_iso, parse_dt, strip_quoted
from kbmail.workspace import OTHERS, Workspace

REQUEST_RE = re.compile(
    r"\?|？|\b(please|could you|can you|would you|let me know|kindly|need your|action required"
    r"|by (mon|tue|wed|thu|fri|eod|end of))"
    r"|お願い|ください|下さい|いただけ|頂け|ご確認|ご検討|ご連絡|いかがでしょう|でしょうか|ますか",
    re.I,
)


def show(ws: Workspace, mid: str, thread: bool = False, full: bool = False) -> str:
    index = message_index(ws)
    rec = index.get(mid)
    if rec is None:
        tmap = threads(ws, index)
        if mid in tmap:
            msgs, thread = tmap[mid], True
        else:
            raise KeyError(f"unknown message/thread id {mid}")
    else:
        msgs = threads(ws, index)[rec["thread_id"]] if thread else [rec]
    labels = load_labels(ws)
    hist = History.of(ws)
    scope, seen = ({m["id"] for m in msgs}, {}) if thread else (None, None)
    out = []
    for m in msgs:
        _, body = read_message(ws, m)
        lab = labels.get(m["id"], {})
        out += [
            f"## {m['id']} ({m.get('date', '')[:16]}) thread {m.get('thread_id')}",
            f"From: {fmt_person(m.get('from'))}",
            f"To: {fmt_people(m.get('to'), 30)}",
        ]
        if m.get("cc"):
            out.append(f"Cc: {fmt_people(m.get('cc'), 30)}")
        out.append(f"Subject: {m.get('subject', '')}")
        if lab:
            out.append(f"Labels: {', '.join(lab['labels'])} ({lab['status']})")
        atts = [a["name"] for a in m.get("attachments") or [] if not a.get("inline")]
        if atts:
            out.append(f"Attachments: {', '.join(atts)}")
        out += ["", body if full else hist.render(m, body, scope=scope, seen=seen, labels=labels), ""]
    return "\n".join(out)


def mail_search(ws: Workspace, query: str, limit: int = 15, area: str | None = None) -> list[dict]:
    index = message_index(ws)
    labels = load_labels(ws)
    hist = History.of(ws)
    docs = {}
    for r in index.all():
        if area and area not in labels.get(r["id"], {}).get("labels", []):
            continue
        try:
            _, body = read_message(ws, r)
        except FileNotFoundError:
            body = ""
        docs[r["id"]] = (
            f"{r.get('subject', '')} {r.get('subject', '')} {fmt_person(r.get('from'))} {hist.context(r, body)[:4000]}"
        )
    out = []
    for mid, score in BM25(docs).search(query, limit):
        r = index.get(mid)
        out.append(
            {
                "id": mid,
                "score": round(score, 2),
                "date": r.get("date", "")[:10],
                "from": fmt_person(r.get("from")),
                "subject": r.get("subject", ""),
                "thread": r.get("thread_id"),
            }
        )
    return out


def triage(ws: Workspace, days: int | None = None, log=print) -> dict:
    """List threads that probably need a reply from me, follow-ups I am owed, and meetings to prepare."""
    days = days or ws.cfg("triage.days", 14)
    follow_days = ws.cfg("triage.follow_up_after_days", 3)
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    labels = load_labels(ws)
    me = ws.me_emails
    need_reply, follow_up = [], []
    for tid, msgs in threads(ws).items():
        msgs = sorted(msgs, key=lambda r: r.get("date", ""))
        last = msgs[-1]
        dt = parse_dt(last.get("date"))
        if not dt or dt < since:
            continue
        lab = labels.get(last["id"], {})
        if lab.get("labels") == [OTHERS] and lab.get("status") in ("confirmed", "auto"):
            continue
        try:
            _, body = read_message(ws, last)
        except FileNotFoundError:
            body = ""
        text = strip_quoted(body)
        asks = bool(REQUEST_RE.search(text))
        sent_by_me = last.get("direction") == "sent" or (last.get("from") or {}).get("email", "") in me
        to_me = any(p.get("email", "").lower() in me for p in last.get("to") or []) or not me
        area = ", ".join(lab.get("labels", [])) or "unlabeled"
        item = {
            "thread": tid,
            "id": last["id"],
            "date": last.get("date", "")[:16],
            "from": fmt_person(last.get("from")),
            "subject": last.get("subject", ""),
            "area": area,
            "excerpt": compact(text, 220),
        }
        if not sent_by_me and (asks or to_me):
            item["score"] = (2 if asks else 0) + (1 if to_me else 0) + (1 if last.get("importance") == "high" else 0)
            item["age_days"] = (now - dt).days
            need_reply.append(item)
        elif sent_by_me and asks and (now - dt).days >= follow_days:
            item["age_days"] = (now - dt).days
            follow_up.append(item)
    need_reply.sort(key=lambda d: (-d["score"], d["date"]))
    follow_up.sort(key=lambda d: -d["age_days"])
    meetings = []
    for ev in calendar_index(ws).all():
        st = parse_dt(ev.get("start"))
        if st and now <= st <= now + timedelta(days=3):
            org = (ev.get("organizer") or {}).get("email", "").lower()
            meetings.append(
                {
                    "id": ev["id"],
                    "start": ev["start"][:16],
                    "subject": ev.get("subject", ""),
                    "organizer_is_me": bool(org and org in me),
                    "attendees": len(ev.get("attendees") or []),
                }
            )
    lines = [
        f"# Triage ({now_iso()})",
        "",
        "Heuristic list generated by `kbmail triage`; refine with `/kb-suggest`.",
        "",
        f"## Probably waiting for my reply ({len(need_reply)})",
        "",
    ]
    for d in need_reply[:60]:
        lines.append(
            f"- [{d['area']}] `{d['id']}` {d['date']} **{d['subject']}** - {d['from']} "
            f"({d['age_days']}d, score {d['score']})\n  > {d['excerpt']}"
        )
    lines += ["", f"## I asked, no answer yet (>= {follow_days} days) ({len(follow_up)})", ""]
    for d in follow_up[:40]:
        lines.append(
            f"- [{d['area']}] `{d['id']}` {d['date']} **{d['subject']}** ({d['age_days']}d)\n  > {d['excerpt']}"
        )
    lines += ["", f"## Meetings in the next 3 days ({len(meetings)})", ""]
    for m in meetings:
        lines.append(
            f"- `{m['id']}` {m['start']} {m['subject']} ({m['attendees']} attendees"
            f"{', I organize' if m['organizer_is_me'] else ''})"
        )
    p = ws.path("work/triage/TRIAGE.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ws.record_run("triage", need_reply=len(need_reply), follow_up=len(follow_up), meetings=len(meetings))
    log(f"Triage: {len(need_reply)} need reply, {len(follow_up)} follow-up, {len(meetings)} meeting(s) -> {ws.rel(p)}")
    return {"need_reply": need_reply, "follow_up": follow_up, "meetings": meetings}


def style_samples(ws: Workspace, n: int = 40, log=print) -> str:
    """Collect my own recent sent texts (quotes stripped) for /kb-style."""
    me = ws.me_emails
    sent = [
        r
        for r in message_index(ws).all()
        if r.get("direction") == "sent" or (r.get("from") or {}).get("email", "") in me
    ]
    sent = sent[-n * 2 :]
    lines = [f"# Writing samples ({len(sent)} recent sent messages)", ""]
    kept = 0
    for r in reversed(sent):
        try:
            _, body = read_message(ws, r)
        except FileNotFoundError:
            continue
        text = strip_quoted(body).strip()
        if len(text) < 40:
            continue
        lines += [f"## {r['id']} | to {fmt_people(r.get('to'), 3)} | {r.get('subject', '')}", "", text[:2500], ""]
        kept += 1
        if kept >= n:
            break
    p = ws.path("work/style/SAMPLES.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote {kept} sample(s) to {ws.rel(p)}. Next: /kb-style.")
    return ws.rel(p)
