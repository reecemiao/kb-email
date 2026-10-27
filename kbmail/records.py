"""Normalized message records (Outlook mail, meeting items, Teams chat-days, imported .eml).

Each message is one Markdown file with YAML front matter, so both people and
Copilot can read it directly:

    data/messages/<source>/<YYYY-MM>/<YYYYMMDD-HHMM>_<id>.md
    data/messages/<source>/<YYYY-MM>/<id>_files/   (attachments, body.html)

`state/index.jsonl` holds one metadata line per message (no body). It is the
ledger used for de-duplication and delta processing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from kbmail.textutil import dump_frontmatter, normalize_subject, now_iso, parse_dt, parse_frontmatter, short_hash
from kbmail.workspace import Workspace

INDEX = "state/index.jsonl"
CAL_INDEX = "state/calendar_index.jsonl"

# Metadata keys copied into the index (everything except bulky lists stays in the .md).
_INDEX_KEYS = (
    "id", "source", "kind", "folder", "direction", "date", "subject", "from", "to", "cc",
    "conversation_id", "conversation_topic", "thread_id", "attachments", "path", "exported_at",
    "updated_at", "entry_id", "store_id", "importance", "categories",
)


def message_id_for(key: str) -> str:
    return "m" + short_hash(key, 11)


def thread_id_for(meta: dict) -> str:
    """Group messages into threads by Outlook ConversationID, else by normalized subject."""
    conv = meta.get("conversation_id")
    if conv:
        return "t" + short_hash(f"{meta.get('source', '')}|{conv}", 11)
    return "t" + short_hash("subj|" + normalize_subject(meta.get("subject", "")), 11)


def message_dir(ws: Workspace, meta: dict) -> Path:
    dt = parse_dt(meta["date"])
    return ws.path("data", "messages", meta.get("source", "outlook"), dt.strftime("%Y-%m"))


def message_paths(ws: Workspace, meta: dict) -> tuple[Path, Path]:
    dt = parse_dt(meta["date"])
    d = message_dir(ws, meta)
    md = d / f"{dt.strftime('%Y%m%d-%H%M')}_{meta['id']}.md"
    files = d / f"{meta['id']}_files"
    return md, files


def write_message(ws: Workspace, meta: dict, body: str) -> Path:
    """Write the .md for a message whose attachments (if any) are already saved."""
    meta.setdefault("thread_id", thread_id_for(meta))
    meta.setdefault("exported_at", now_iso())
    md, _ = message_paths(ws, meta)
    md.parent.mkdir(parents=True, exist_ok=True)
    meta["path"] = ws.rel(md)
    md.write_text(dump_frontmatter(meta, body), encoding="utf-8")
    return md


def read_message(ws: Workspace, meta_or_path) -> tuple[dict, str]:
    path = meta_or_path["path"] if isinstance(meta_or_path, dict) else meta_or_path
    p = ws.path(path) if not Path(path).is_absolute() else Path(path)
    return parse_frontmatter(p.read_text(encoding="utf-8"))


class JsonlIndex:
    def __init__(self, ws: Workspace, rel: str = INDEX):
        self.ws = ws
        self.rel = rel
        self.items: dict[str, dict] = {}
        p = ws.path(rel)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.items[rec["id"]] = rec

    def __contains__(self, mid: str) -> bool:
        return mid in self.items

    def __len__(self) -> int:
        return len(self.items)

    def get(self, mid: str) -> dict | None:
        return self.items.get(mid)

    def upsert(self, meta: dict) -> None:
        self.items[meta["id"]] = {k: meta[k] for k in _INDEX_KEYS if k in meta} | {
            k: meta[k] for k in ("start", "end", "location", "organizer", "attendees") if k in meta
        }

    def all(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda r: r.get("date") or r.get("start") or "")

    def save(self) -> None:
        p = self.ws.path(self.rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in self.all():
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, p)


def message_index(ws: Workspace) -> JsonlIndex:
    return JsonlIndex(ws, INDEX)


def calendar_index(ws: Workspace) -> JsonlIndex:
    return JsonlIndex(ws, CAL_INDEX)


def participants(meta: dict) -> list[str]:
    out = []
    for p in [meta.get("from") or {}, *(meta.get("to") or []), *(meta.get("cc") or [])]:
        e = (p.get("email") or "").lower()
        if e and e not in out:
            out.append(e)
    return out


def fmt_person(p: dict | None) -> str:
    if not p:
        return ""
    name, email = p.get("name") or "", p.get("email") or ""
    if name and email and name.lower() != email.lower():
        return f"{name} <{email}>"
    return name or email


def fmt_people(people: list[dict] | None, limit: int = 8) -> str:
    people = people or []
    s = "; ".join(fmt_person(p) for p in people[:limit])
    if len(people) > limit:
        s += f"; +{len(people) - limit} more"
    return s


def threads(ws: Workspace, index: JsonlIndex | None = None) -> dict[str, list[dict]]:
    index = index or message_index(ws)
    out: dict[str, list[dict]] = {}
    for rec in index.all():
        out.setdefault(rec.get("thread_id") or thread_id_for(rec), []).append(rec)
    return out
