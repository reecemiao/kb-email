"""Outlook export via pywin32 (Windows + classic Outlook desktop only).

Design notes
------------
* Delta: per-folder watermark (newest ReceivedTime exported) in
  state/export_state.json; the next run starts at watermark - overlap_hours and
  de-duplicates against state/index.jsonl, so nothing is exported twice.
* Folder iteration sorts by ReceivedTime descending and stops at the cut-off
  instead of using Items.Restrict, which avoids locale-dependent date strings.
* pywin32 returns Outlook local times tagged as UTC; `com_dt` re-reads the
  wall-clock value as local time.
* The calendar needs Restrict (recurring series expand to unbounded lists),
  but results are still range-checked in Python.
* New Outlook (the WebView-based "new Outlook for Windows") has no COM API.
  Use classic Outlook, or the Graph route described in docs/teams.md.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from kbmail.records import calendar_index, message_id_for, message_index, message_paths, write_message
from kbmail.textutil import dump_frontmatter, now_iso, parse_dt, short_hash, slugify
from kbmail.workspace import Workspace

OL_DEFAULT_FOLDERS = {"inbox": 6, "sent": 5, "deleted": 3, "outbox": 4, "drafts": 16, "junk": 23, "calendar": 9}
OL_MAIL = 43
OL_MEETING_CLASSES = {53, 54, 55, 56, 57}
OL_ATT_OLE = 6
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
SKIP_FOLDER_NAMES = {"deleted items", "junk email", "outbox", "drafts", "sync issues", "conversation history",
                     "rss feeds", "rss subscriptions", "削除済みアイテム", "迷惑メール", "送信トレイ", "下書き",
                     "同期の失敗", "会話の履歴"}

EXPORT_STATE = "state/export_state.json"


class OutlookUnavailable(RuntimeError):
    pass


def connect():
    try:
        import win32com.client  # type: ignore
    except ImportError as e:  # pragma: no cover - Windows only
        raise OutlookUnavailable(
            "pywin32 is not installed or this is not Windows. Install with `pip install pywin32` on Windows "
            "with classic Outlook. On other machines use `kbmail import-eml`."
        ) from e
    app = win32com.client.Dispatch("Outlook.Application")
    return app.GetNamespace("MAPI")


def com_dt(value) -> datetime | None:
    """Convert a COM date to an aware local datetime (see module notes)."""
    if value is None:
        return None
    try:
        if value.year >= 4500:  # Outlook's "None" date (1/1/4501)
            return None
        naive = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
    except (AttributeError, ValueError):
        return None
    return naive.astimezone()


def _safe(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:  # COM raises pywintypes.com_error for missing properties
        return default


# ---------------------------------------------------------------- addresses

def sender_of(item) -> dict:
    name = _safe(lambda: item.SenderName, "")
    addr = _safe(lambda: item.SenderEmailAddress, "")
    if _safe(lambda: item.SenderEmailType, "") == "EX":
        smtp = _safe(lambda: item.PropertyAccessor.GetProperty(PR_SENDER_SMTP), "")
        if not smtp:
            smtp = _safe(lambda: item.Sender.GetExchangeUser().PrimarySmtpAddress, "")
        addr = smtp or addr
    return {"name": name, "email": (addr or "").lower()}


def recipient_smtp(r) -> str:
    smtp = _safe(lambda: r.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS), "")
    if not smtp:
        smtp = _safe(lambda: r.AddressEntry.GetExchangeUser().PrimarySmtpAddress, "")
    if not smtp:
        smtp = _safe(lambda: r.Address, "")
    return (smtp or "").lower() if "@" in (smtp or "") else ""


def recipients_of(item) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"to": [], "cc": [], "bcc": []}
    kinds = {1: "to", 2: "cc", 3: "bcc"}
    recips = _safe(lambda: item.Recipients)
    if recips is None:
        return out
    for i in range(1, _safe(lambda: recips.Count, 0) + 1):
        r = recips.Item(i)
        kind = kinds.get(_safe(lambda: r.Type, 1), "to")
        out[kind].append({"name": _safe(lambda: r.Name, ""), "email": recipient_smtp(r)})
    return out


# ---------------------------------------------------------------- folders

def _walk(folder, include_subfolders: bool):
    yield folder
    if include_subfolders:
        for i in range(1, _safe(lambda: folder.Folders.Count, 0) + 1):
            yield from _walk(folder.Folders.Item(i), True)


def folder_by_path(ns, path: str):
    """Resolve 'Inbox/Projects/X' (relative to the default store) or '\\\\Store Name\\Folder\\Sub'."""
    path = path.strip()
    if path.startswith("\\\\"):
        parts = [p for p in path.split("\\") if p]
        folder = ns.Folders.Item(parts[0])
        rest = parts[1:]
    else:
        parts = [p for p in re.split(r"[\\/]", path) if p]
        first = parts[0].lower()
        if first in OL_DEFAULT_FOLDERS:
            folder = ns.GetDefaultFolder(OL_DEFAULT_FOLDERS[first])
        else:
            root = ns.GetDefaultFolder(6).Parent
            folder = root.Folders.Item(parts[0])
        rest = parts[1:]
    for name in rest:
        folder = folder.Folders.Item(name)
    return folder


def resolve_folders(ns, folders: list[str], include_subfolders: bool, all_folders: bool,
                    extra: list[str]) -> list[tuple[object, bool]]:
    """Return [(folder, is_sent)] to export."""
    sent_path = _safe(lambda: ns.GetDefaultFolder(5).FolderPath, "")
    chosen = []
    if all_folders:
        root = ns.GetDefaultFolder(6).Parent
        for i in range(1, root.Folders.Count + 1):
            f = root.Folders.Item(i)
            if _safe(lambda: f.DefaultItemType, 0) != 0:  # 0 = olMailItem
                continue
            if f.Name.lower() in SKIP_FOLDER_NAMES:
                continue
            chosen.extend(_walk(f, include_subfolders))
    else:
        for name in folders:
            key = name.lower()
            f = ns.GetDefaultFolder(OL_DEFAULT_FOLDERS[key]) if key in OL_DEFAULT_FOLDERS else folder_by_path(ns, name)
            chosen.extend(_walk(f, include_subfolders))
    for p in extra or []:
        chosen.extend(_walk(folder_by_path(ns, p), include_subfolders))
    seen, out = set(), []
    for f in chosen:
        fp = f.FolderPath
        if fp in seen:
            continue
        seen.add(fp)
        out.append((f, fp == sent_path or fp.startswith(sent_path + "\\")))
    return out


# ---------------------------------------------------------------- mail export

def _save_attachments(ws: Workspace, item, files_dir: Path, html: str, max_bytes: int) -> list[dict]:
    atts = []
    coll = _safe(lambda: item.Attachments)
    for i in range(1, _safe(lambda: coll.Count, 0) + 1):
        a = coll.Item(i)
        if _safe(lambda: a.Type, 1) == OL_ATT_OLE:
            continue
        name = _safe(lambda: a.FileName, "") or _safe(lambda: a.DisplayName, f"attachment{i}")
        size = _safe(lambda: a.Size, 0)
        cid = _safe(lambda: a.PropertyAccessor.GetProperty(PR_ATTACH_CONTENT_ID), "") or ""
        inline = bool(cid) and f"cid:{cid}" in (html or "")
        rec = {"name": name, "bytes": size, "inline": inline}
        if cid:
            rec["content_id"] = cid
        if size > max_bytes:
            rec["skipped"] = f"larger than {max_bytes // 1_000_000} MB"
        else:
            files_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:150] or f"attachment{i}"
            dst = files_dir / f"{i:02d}_{safe}"
            try:
                a.SaveAsFile(str(dst))
                rec["file"] = ws.rel(dst)
            except Exception as e:  # pragma: no cover
                rec["skipped"] = f"save failed: {e}"
        atts.append(rec)
    return atts


def export_item(ws: Workspace, item, folder, is_sent: bool, index, force: bool) -> str | None:
    """Export one mail/meeting item. Returns the message id, or None when skipped."""
    cls = _safe(lambda: item.Class, 0)
    if cls != OL_MAIL and cls not in OL_MEETING_CLASSES:
        return None
    imid = _safe(lambda: item.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID), "")
    entry_id = _safe(lambda: item.EntryID, "")
    mid = message_id_for(imid or entry_id)
    if mid in index and not force:
        return None
    received = com_dt(_safe(lambda: item.ReceivedTime)) or com_dt(_safe(lambda: item.SentOn)) or datetime.now().astimezone()
    sender = sender_of(item)
    recips = recipients_of(item)
    me = ws.me_emails
    direction = "sent" if is_sent or (sender["email"] and sender["email"] in me) else "received"
    meta = {
        "id": mid,
        "source": "outlook",
        "kind": "email" if cls == OL_MAIL else "meeting_item",
        "folder": _safe(lambda: folder.Name, ""),
        "folder_path": _safe(lambda: folder.FolderPath, ""),
        "direction": direction,
        "date": received.replace(microsecond=0).isoformat(),
        "subject": _safe(lambda: item.Subject, "") or "",
        "from": sender,
        "to": recips["to"],
        "cc": recips["cc"],
        "bcc": recips["bcc"],
        "conversation_id": _safe(lambda: item.ConversationID, ""),
        "conversation_topic": _safe(lambda: item.ConversationTopic, ""),
        "internet_message_id": imid,
        "entry_id": entry_id,
        "store_id": _safe(lambda: folder.StoreID, ""),
        "importance": {0: "low", 1: "normal", 2: "high"}.get(_safe(lambda: item.Importance, 1), "normal"),
        "categories": [c.strip() for c in (_safe(lambda: item.Categories, "") or "").split(",") if c.strip()],
        "message_class": _safe(lambda: item.MessageClass, ""),
    }
    o = ws.config["outlook"]
    body = _safe(lambda: item.Body, "") or ""
    if len(body) > o["max_body_chars"]:
        body = body[: o["max_body_chars"]] + "\n\n[… truncated by kbmail]"
    _, files_dir = message_paths(ws, meta)
    html = ""
    if o["save_html"] and _safe(lambda: item.BodyFormat, 1) == 2:
        html = _safe(lambda: item.HTMLBody, "") or ""
        if html:
            files_dir.mkdir(parents=True, exist_ok=True)
            (files_dir / "body.html").write_text(html, encoding="utf-8")
            meta["html_file"] = ws.rel(files_dir / "body.html")
    meta["attachments"] = (
        _save_attachments(ws, item, files_dir, html, int(o["attachment_max_mb"] * 1_000_000))
        if o["save_attachments"] else []
    )
    write_message(ws, meta, body)
    index.upsert(meta)
    return mid


def export_mail(ws: Workspace, days: int | None = None, include_subfolders: bool | None = None,
                all_folders: bool = False, folders: list[str] | None = None, force: bool = False,
                log=print) -> dict:
    ns = connect()
    o = ws.config["outlook"]
    include_subfolders = o["include_subfolders"] if include_subfolders is None else include_subfolders
    state = ws.load_json(EXPORT_STATE, {"folders": {}})
    index = message_index(ws)
    now = datetime.now().astimezone()
    summary = {"exported": 0, "skipped_existing": 0, "folders": {}}
    for folder, is_sent in resolve_folders(ns, folders or o["folders"], include_subfolders, all_folders,
                                           o.get("extra_folders") or []):
        fpath = folder.FolderPath
        fstate = state["folders"].get(fpath, {})
        if days is not None:
            since = now - timedelta(days=days)
        elif fstate.get("last_received"):
            since = parse_dt(fstate["last_received"]) - timedelta(hours=o["overlap_hours"])
        else:
            since = now - timedelta(days=o["days"])
        items = folder.Items
        items.Sort("[ReceivedTime]", True)
        item = items.GetFirst()
        newest = parse_dt(fstate.get("last_received")) if fstate.get("last_received") else None
        n_new = n_seen = 0
        while item is not None:
            rt = com_dt(_safe(lambda: item.ReceivedTime))
            if rt is not None and rt < since:
                break
            n_seen += 1
            mid = export_item(ws, item, folder, is_sent, index, force)
            if mid:
                n_new += 1
            if rt and (newest is None or rt > newest):
                newest = rt
            if n_new and n_new % 100 == 0:
                index.save()
                log(f"  … {n_new} exported from {fpath}")
            item = items.GetNext()
        state["folders"][fpath] = {
            "last_received": newest.isoformat() if newest else fstate.get("last_received"),
            "last_run": now_iso(),
            "since_used": since.isoformat(),
        }
        summary["folders"][fpath] = {"new": n_new, "seen": n_seen, "since": since.isoformat()}
        summary["exported"] += n_new
        summary["skipped_existing"] += n_seen - n_new
        log(f"{fpath}: {n_new} new / {n_seen} in window (since {since:%Y-%m-%d %H:%M})")
    index.save()
    ws.save_json(EXPORT_STATE, state)
    ws.record_run("export_mail", exported=summary["exported"])
    return summary


# ---------------------------------------------------------------- calendar

def _calendar_window(items, start: datetime, end: datetime):
    """Restrict the (sorted, recurrence-expanded) calendar to [start, end].

    Jet date strings are parsed with the Windows locale, so the restriction is
    sanity-checked; if it fails or looks wrong, fall back to walking the sorted
    items (the caller skips < start and stops at > end, so this stays correct).
    """
    fmt = "%m/%d/%Y %I:%M %p"
    try:
        r = items.Restrict(f"[Start] >= '{start.strftime(fmt)}' AND [Start] <= '{end.strftime(fmt)}'")
        first = r.GetFirst()
        st = com_dt(_safe(lambda: first.Start)) if first is not None else None
        if st is not None and start - timedelta(days=2) <= st <= end:
            return r, first
    except Exception:
        pass
    return items, items.GetFirst()


def _attendees(item) -> list[dict]:
    out = []
    recips = _safe(lambda: item.Recipients)
    kinds = {1: "required", 2: "optional", 3: "resource"}
    for i in range(1, _safe(lambda: recips.Count, 0) + 1):
        r = recips.Item(i)
        out.append({"name": _safe(lambda: r.Name, ""), "email": recipient_smtp(r),
                    "type": kinds.get(_safe(lambda: r.Type, 1), "required")})
    return out


_TEAMS_BOILERPLATE = re.compile(r"_{20,}.*?(Microsoft Teams|Teams 会議|Join the meeting).*?_{20,}", re.S | re.I)


def export_calendar(ws: Workspace, days: int | None = None, future_days: int | None = None, log=print) -> dict:
    ns = connect()
    o = ws.config["outlook"]
    future_days = o["calendar_future_days"] if future_days is None else future_days
    now = datetime.now().astimezone()
    last = ws.last_run("export_calendar")
    if days is None and last:
        # Delta: re-scan from the previous run (events get edited); future events are always re-scanned.
        start = max(parse_dt(last["last_run"]) - timedelta(days=1), now - timedelta(days=o["days"]))
    else:
        start = now - timedelta(days=o["days"] if days is None else days)
    end = now + timedelta(days=future_days)
    items = ns.GetDefaultFolder(9).Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True
    restricted, item = _calendar_window(items, start, end)
    index = calendar_index(ws)
    n = 0
    guard = 0
    while item is not None and guard < 500_000:
        guard += 1
        st = com_dt(_safe(lambda: item.Start))
        if st is None or st < start:
            item = restricted.GetNext()
            continue
        if st > end:
            break
        gid = _safe(lambda: item.GlobalAppointmentID, "") or _safe(lambda: item.EntryID, "")
        eid = "c" + short_hash(f"{gid}|{st.isoformat()}", 11)
        body = _safe(lambda: item.Body, "") or ""
        location = _safe(lambda: item.Location, "") or ""
        online = bool(re.search(r"teams\.microsoft\.com|zoom\.us|webex", body + location, re.I))
        body = _TEAMS_BOILERPLATE.sub("[online meeting join info removed]", body)[:8000]
        org = _safe(lambda: item.GetOrganizer(), None)
        meta = {
            "id": eid,
            "source": "outlook-calendar",
            "subject": _safe(lambda: item.Subject, "") or "",
            "start": st.isoformat(),
            "end": (com_dt(_safe(lambda: item.End)) or st).isoformat(),
            "location": location,
            "organizer": {"name": _safe(lambda: org.Name, "") if org else _safe(lambda: item.Organizer, ""),
                          "email": (_safe(lambda: org.GetExchangeUser().PrimarySmtpAddress, "") if org else "").lower()},
            "attendees": _attendees(item),
            "is_recurring": bool(_safe(lambda: item.IsRecurring, False)),
            "is_online": online,
            "response_status": _safe(lambda: item.ResponseStatus, 0),
            "categories": [c.strip() for c in (_safe(lambda: item.Categories, "") or "").split(",") if c.strip()],
            "global_id": gid,
        }
        d = ws.path("data", "calendar", st.strftime("%Y-%m"))
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{st.strftime('%Y%m%d-%H%M')}_{eid}_{slugify(meta['subject'], 40)}.md"
        meta["path"] = ws.rel(p)
        meta["date"] = meta["start"]
        p.write_text(dump_frontmatter(meta, body), encoding="utf-8")
        index.upsert(meta)
        n += 1
        item = restricted.GetNext()
    index.save()
    ws.record_run("export_calendar", events=n, window_start=start.isoformat(), window_end=end.isoformat())
    log(f"Calendar: {n} events between {start:%Y-%m-%d} and {end:%Y-%m-%d}")
    return {"events": n}


# ---------------------------------------------------------------- drafts (never sends)

def md_to_html(md: str) -> str:
    """Tiny Markdown -> HTML for email drafts (paragraphs, bullets, bold)."""
    import html as _html
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        lines = block.splitlines()
        if all(re.match(r"\s*[-*]\s+", ln) for ln in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(_html.escape(re.sub(r'^\s*[-*]\s+', '', ln)))}</li>"
                                         for ln in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(_html.escape(ln)) for ln in lines) + "</p>")
    return "<div style=\"font-family:Calibri,Meiryo,sans-serif;font-size:11pt\">" + "".join(out) + "</div>"


def _inline(s: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def create_outlook_draft(ws: Workspace, draft_path: str, log=print) -> None:
    from kbmail.textutil import parse_frontmatter
    meta, body = parse_frontmatter(ws.path(draft_path).read_text(encoding="utf-8"))
    ns = connect()
    app = ns.Application
    reply_to = meta.get("reply_to")
    if reply_to:
        rec = message_index(ws).get(reply_to)
        if not rec or not rec.get("entry_id"):
            raise ValueError(f"reply_to {reply_to} not found in index (or has no entry_id)")
        orig = (ns.GetItemFromID(rec["entry_id"], rec["store_id"]) if rec.get("store_id")
                else ns.GetItemFromID(rec["entry_id"]))
        mail = orig.ReplyAll() if meta.get("reply_all", True) else orig.Reply()
        mail.HTMLBody = md_to_html(body) + mail.HTMLBody
    else:
        mail = app.CreateItem(0)
        mail.HTMLBody = md_to_html(body)
    if meta.get("subject"):
        mail.Subject = meta["subject"]
    for field, attr in (("to", "To"), ("cc", "CC")):
        if meta.get(field):
            val = meta[field]
            setattr(mail, attr, "; ".join(val) if isinstance(val, list) else str(val))
    mail.Save()  # stays in Drafts; kbmail never sends mail
    log(f"Draft saved to Outlook Drafts: {mail.Subject}")
