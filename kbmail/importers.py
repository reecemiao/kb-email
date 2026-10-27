"""Import saved .eml files (any OS). Useful for testing, archives, or mail saved by hand."""

from __future__ import annotations

import email
import email.policy
import re
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from kbmail.records import message_id_for, message_index, message_paths, write_message
from kbmail.workspace import Workspace


def _people(values) -> list[dict]:
    return [{"name": n, "email": a.lower()} for n, a in getaddresses(values or []) if a]


def import_eml(ws: Workspace, paths: list[str], force: bool = False, log=print) -> int:
    index = message_index(ws)
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        files.extend(sorted(p.rglob("*.eml")) if p.is_dir() else [p])
    n = 0
    for f in files:
        msg = email.message_from_bytes(f.read_bytes(), policy=email.policy.default)
        key = (msg.get("Message-ID") or "").strip() or f"file:{f.resolve()}"
        mid = message_id_for(key)
        if mid in index and not force:
            continue
        try:
            dt = parsedate_to_datetime(msg.get("Date")).astimezone()
        except Exception:
            from datetime import datetime
            dt = datetime.fromtimestamp(f.stat().st_mtime).astimezone()
        sender = (_people([msg.get("From", "")]) or [{"name": "", "email": ""}])[0]
        direction = "sent" if sender["email"] in ws.me_emails else "received"
        refs = (msg.get("References") or "").split()
        # Thread root = first References entry (or the message itself), like Outlook's ConversationID.
        conv = refs[0] if refs else (msg.get("In-Reply-To") or "").strip() or (msg.get("Message-ID") or "").strip()
        meta = {
            "id": mid,
            "source": "outlook",
            "kind": "email",
            "folder": "Sent Items" if direction == "sent" else "Inbox",
            "direction": direction,
            "date": dt.replace(microsecond=0).isoformat(),
            "subject": str(msg.get("Subject", "")),
            "from": sender,
            "to": _people(msg.get_all("To")),
            "cc": _people(msg.get_all("Cc")),
            "conversation_id": conv or "",
            "internet_message_id": key,
            "imported_from": str(f),
        }
        _, files_dir = message_paths(ws, meta)
        body_part = msg.get_body(preferencelist=("plain", "html"))
        body = body_part.get_content() if body_part else ""
        html_part = msg.get_body(preferencelist=("html",))
        html_text = html_part.get_content() if html_part is not None else ""
        if html_part is not None:
            files_dir.mkdir(parents=True, exist_ok=True)
            (files_dir / "body.html").write_text(html_part.get_content(), encoding="utf-8")
            meta["html_file"] = ws.rel(files_dir / "body.html")
            if body_part is html_part:
                body = re.sub(r"<[^>]+>", " ", body)
        atts = []
        # walk() rather than iter_attachments(): inline images live inside multipart/related.
        parts = [pt for pt in msg.walk() if not pt.is_multipart() and pt is not body_part and pt is not html_part
                 and (pt.get_filename() or pt.get("Content-ID"))]
        for i, part in enumerate(parts, 1):
            name = part.get_filename() or f"inline{i}.{part.get_content_subtype()}"
            data = part.get_payload(decode=True) or b""
            files_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\-]+", "_", name)
            dst = files_dir / f"{i:02d}_{safe}"
            dst.write_bytes(data)
            cid = (part.get("Content-ID") or "").strip("<> ")
            rec = {"name": name, "bytes": len(data), "file": ws.rel(dst), "inline": bool(cid) and f"cid:{cid}" in html_text}
            if cid:
                rec["content_id"] = cid
            atts.append(rec)
        meta["attachments"] = atts
        write_message(ws, meta, body)
        index.upsert(meta)
        n += 1
    index.save()
    ws.record_run("import_eml", imported=n)
    log(f"Imported {n} message(s).")
    return n
