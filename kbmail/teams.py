"""Microsoft Teams chats -> the same message records as mail (one record per chat per day).

Routes (see docs/teams.md for which one fits your tenant):

1. Graph via PowerShell (no app registration of your own): scripts/teams-export.ps1
   signs in with Microsoft Graph PowerShell and saves chats as JSON; `kbmail teams
   import` ingests them (also any Graph JSON saved by hand, e.g. from Graph Explorer).
2. Graph via kbmail (MSAL): needs an Entra ID app registration (public client) that
   your tenant allows; set teams.client_id / teams.tenant in kbmail.yaml.
   Delta: per-chat watermark on lastModifiedDateTime.
3. Pasted text: `kbmail teams import-text` parses chats copied from the Teams window.
4. Outlook (experimental): Exchange keeps a compliance copy of Teams chats in a
   hidden mailbox folder ("TeamsMessagesData"). Some Outlook profiles expose it
   to COM, many don't. `kbmail teams probe-outlook` tells you whether yours does.

Because chat-days become ordinary records (source: teams), labeling, rules,
KB selection, people inference and the wiki all work on Teams unchanged.
"""

from __future__ import annotations

import html as _html
import json
import re
import time
from datetime import UTC, datetime, timedelta

from kbmail.records import message_id_for, message_index, write_message
from kbmail.textutil import now_iso, parse_dt, short_hash, utc_iso
from kbmail.workspace import Workspace

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Chat.Read", "User.Read"]
STATE = "state/teams_state.json"


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s or "")
    s = re.sub(r"(?is)<attachment[^>]*>.*?</attachment>", "[attachment]", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(s)).strip()


# ---------------------------------------------------------------- shared writer


def write_chat_day(
    ws: Workspace,
    index,
    chat_key: str,
    title: str,
    chat_type: str,
    members: list[dict],
    day: str,
    messages: list[dict],
    origin: str,
) -> str:
    """Merge `messages` into the stored chat-day and (re)write its record. Returns record id."""
    raw_dir = ws.path("data/messages/teams/_raw", short_hash(chat_key, 12))
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_p = raw_dir / f"{day}.json"
    stored = json.loads(raw_p.read_text(encoding="utf-8")) if raw_p.exists() else {}
    before = len(stored)
    for m in messages:
        stored[m["id"]] = m
    raw_p.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
    msgs = sorted(stored.values(), key=lambda m: m["time"])
    me = ws.me_emails
    speakers = [m["from"] for m in msgs if m["from"].get("email", "").lower() not in me]
    lead = max(speakers, key=lambda p: sum(1 for s in speakers if s == p)) if speakers else msgs[0]["from"]
    mid = message_id_for(f"teams|{chat_key}|{day}")
    body = "\n\n".join(
        f"**{parse_dt(m['time']).strftime('%H:%M')} {m['from'].get('name') or m['from'].get('email')}**: {m['text']}"
        for m in msgs
    )
    meta = {
        "id": mid,
        "source": "teams",
        "kind": "chat",
        "folder": chat_type,
        "origin": origin,
        "direction": "sent" if not speakers else "received",
        "date": parse_dt(msgs[0]["time"]).astimezone().replace(microsecond=0).isoformat(),
        "subject": title,
        "from": lead,
        "to": [p for p in members if p != lead],
        "cc": [],
        "conversation_id": chat_key,
        "conversation_topic": title,
        "messages": len(msgs),
        "attachments": [],
    }
    old = index.get(mid)
    meta["exported_at"] = (old or {}).get("exported_at") or now_iso()
    meta["updated_at"] = now_iso() if len(stored) != before or not old else (old or {}).get("updated_at", "")
    write_message(ws, meta, body)
    index.upsert(meta)
    return mid


def ingest_graph_chat(ws: Workspace, index, chat: dict, messages: list[dict], origin: str, since=None):
    """Graph chat + chatMessage objects -> chat-day records. Returns (messages, records, newest, title)."""
    members = [
        {"name": m.get("displayName", ""), "email": (m.get("email") or "").lower(), "uid": m.get("userId", "")}
        for m in chat.get("members") or []
    ]
    by_uid = {m["uid"]: m for m in members if m["uid"]}
    title = chat.get("topic") or "Chat with " + ", ".join(
        m["name"] for m in members[:5] if m["email"] not in ws.me_emails
    )
    days_map: dict[str, list[dict]] = {}
    newest = since
    for m in messages:
        if m.get("messageType", "message") != "message" or m.get("deletedDateTime"):
            continue
        user = (m.get("from") or {}).get("user") or {}
        who = by_uid.get(user.get("id"), {"name": user.get("displayName", "?"), "email": ""})
        text = html_to_text((m.get("body") or {}).get("content", ""))
        for a in m.get("attachments") or []:
            if a.get("name"):
                text += f"\n[file: {a['name']}]({a.get('contentUrl', '')})"
        created = parse_dt(m["createdDateTime"]).astimezone()
        days_map.setdefault(created.strftime("%Y-%m-%d"), []).append(
            {
                "id": m["id"],
                "time": created.isoformat(),
                "from": {"name": who["name"], "email": who["email"]},
                "text": text,
            }
        )
        mod = parse_dt(m.get("lastModifiedDateTime") or m["createdDateTime"])
        newest = mod if newest is None else max(newest, mod)
    plain = [{"name": m["name"], "email": m["email"]} for m in members]
    for day, msgs in days_map.items():
        write_chat_day(ws, index, chat["id"], title, chat.get("chatType", "chat"), plain, day, msgs, origin)
    return sum(len(v) for v in days_map.values()), len(days_map), newest, title


# ---------------------------------------------------------------- Graph route


def _token(ws: Workspace) -> str:
    t = ws.config["teams"]
    if not t.get("client_id"):
        raise RuntimeError(
            "teams.via is 'graph' but teams.client_id is not set. Without an Entra app registration use "
            "teams.via: powershell (scripts/teams-export.ps1) - see docs/teams.md"
        )
    try:
        import msal  # type: ignore
    except ImportError as e:
        raise RuntimeError("Install the Teams extra: uv tool install -e '.[teams]'") from e
    cache = msal.SerializableTokenCache()
    cache_p = ws.path(t["token_cache"])
    if cache_p.exists():
        cache.deserialize(cache_p.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        t["client_id"], authority=f"https://login.microsoftonline.com/{t['tenant']}", token_cache=cache
    )
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(cache.serialize(), encoding="utf-8")
    if "access_token" not in result:
        raise RuntimeError(f"Sign-in failed: {result.get('error_description', result)}")
    return result["access_token"]


def _get_all(session, url: str, params: dict | None = None, max_pages: int = 200):
    pages = 0
    while url and pages < max_pages:
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        data = r.json()
        yield from data.get("value", [])
        url, params = data.get("@odata.nextLink"), None
        pages += 1


def export_graph(ws: Workspace, days: int | None = None, log=print) -> dict:
    import requests  # type: ignore

    token = _token(ws)
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    state = ws.load_json(STATE, {"chats": {}})
    now = datetime.now(UTC)
    default_since = now - timedelta(days=days or ws.cfg("teams.days", 30))
    index = message_index(ws)
    n_msgs = n_records = 0
    for chat in _get_all(s, f"{GRAPH}/me/chats", {"$expand": "members", "$top": "50"}):
        cid = chat["id"]
        last_upd = parse_dt(chat.get("lastUpdatedDateTime"))
        cstate = state["chats"].get(cid, {})
        since = default_since if days or not cstate.get("watermark") else parse_dt(cstate["watermark"])
        if last_upd and last_upd <= since:
            continue
        params = {
            "$top": "50",
            "$orderby": "lastModifiedDateTime desc",
            "$filter": f"lastModifiedDateTime gt {utc_iso(since)}",
        }
        messages = list(_get_all(s, f"{GRAPH}/chats/{cid}/messages", params))
        got, recs, newest, title = ingest_graph_chat(ws, index, chat, messages, "graph", since)
        n_msgs += got
        n_records += recs
        state["chats"][cid] = {"watermark": newest.isoformat(), "title": title, "last_run": now_iso()}
    index.save()
    ws.save_json(STATE, state)
    ws.record_run("export_teams", messages=n_msgs, records=n_records, route="graph")
    log(f"Teams (Graph): {n_msgs} new/edited message(s) -> {n_records} chat-day record(s).")
    return {"messages": n_msgs, "records": n_records}


# ---------------------------------------------------------------- PowerShell / saved-JSON route (no app registration)

PS_SCRIPT = "scripts/teams-export.ps1"


def _graph_objects(data, chat_id: str | None = None):
    """Yield (kind, obj, chat_id) for chats and chat messages anywhere in saved Graph JSON."""
    if isinstance(data, list):
        for item in data:
            yield from _graph_objects(item, chat_id)
    elif isinstance(data, dict):
        if "chatType" in data and "id" in data:
            yield "chat", data, data["id"]
        elif "createdDateTime" in data and ("messageType" in data or "body" in data):
            yield "message", data, data.get("chatId") or chat_id
        else:
            chat_id = data.get("chatId", chat_id)
            for key in ("value", "pages", "messages", "chats", "chat"):
                if key in data:
                    yield from _graph_objects(data[key], chat_id)


def import_graph_json(ws: Workspace, paths: list[str] | None = None, move: bool = True, log=print) -> dict:
    """Import Teams chats saved as Graph JSON (scripts/teams-export.ps1, Graph Explorer, ...)."""
    import shutil
    from pathlib import Path

    folder = ws.path(ws.cfg("teams.import_dir", "data/teams-graph"))
    files: list[Path] = []
    for p in [Path(x) for x in paths] if paths else [folder]:
        p = p if p.is_absolute() else ws.path(str(p)) if not p.exists() else p
        files += sorted(f for f in p.rglob("*.json") if "_imported" not in f.parts) if p.is_dir() else [p]
    chats: dict[str, dict] = {}
    msgs: dict[str, dict[str, dict]] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            log(f"  skipped {f.name}: {e}")
            continue
        for kind, obj, cid in _graph_objects(data):
            if kind == "chat":
                chats[cid] = {**chats.get(cid, {}), **obj}
            elif cid:
                msgs.setdefault(cid, {})[obj["id"]] = obj
    index = message_index(ws)
    n_msgs = n_records = 0
    for cid, by_id in msgs.items():
        chat = chats.get(cid) or {"id": cid, "chatType": "chat"}
        got, recs, _, _ = ingest_graph_chat(ws, index, chat, list(by_id.values()), "graph-json")
        n_msgs += got
        n_records += recs
    index.save()
    if move:
        for f in files:
            if f.parent == folder or folder in f.parents:
                done = f.parent / "_imported"
                done.mkdir(exist_ok=True)
                shutil.move(str(f), done / f.name)
    ws.record_run("export_teams", messages=n_msgs, records=n_records, route="graph-json")
    log(f"Teams (saved Graph JSON): {n_msgs} message(s) from {len(msgs)} chat(s) -> {n_records} chat-day record(s).")
    return {"messages": n_msgs, "records": n_records, "chats": len(msgs)}


def export(ws: Workspace, via: str, days: int | None = None, log=print) -> dict:
    if via == "outlook":
        return export_outlook(ws, days, log=log)
    if via == "graph":
        return export_graph(ws, days, log=log)
    return export_powershell(ws, days, log=log)


def export_powershell(ws: Workspace, days: int | None = None, log=print) -> dict:
    """Run scripts/teams-export.ps1 (Microsoft Graph PowerShell sign-in), then import its output."""
    import shutil
    import subprocess

    script = ws.path(PS_SCRIPT)
    if not script.exists():
        raise RuntimeError(f"{PS_SCRIPT} not found; run `kbmail init . --upgrade`")
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise RuntimeError("PowerShell not found (this route is for Windows)")
    cmd = [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Out",
        str(ws.path(ws.cfg("teams.import_dir", "data/teams-graph"))),
    ]
    if days:
        cmd += ["-Days", str(days)]
    res = subprocess.run(cmd, cwd=ws.root)
    if res.returncode != 0:
        why = {
            3: "Microsoft Graph PowerShell is not installed",
            4: "sign-in or consent was refused for Chat.Read",
            5: "PowerShell runs in Constrained Language Mode (application control) on this computer",
        }.get(res.returncode, "see its output above")
        raise RuntimeError(
            f"{PS_SCRIPT} exited with {res.returncode}: {why}. Try `kbmail teams probe-outlook` "
            f"(teams.via: outlook), or: {NO_API_ROUTES}"
        )
    return import_graph_json(ws, log=log)


# ---------------------------------------------------------------- pasted chat text (no API at all)

_BRACKET_HEADER = re.compile(r"^\[(?P<ts>[^\]]{3,40})\]\s*(?P<name>[^:\n\]]{1,80}?)\s*(?::\s*(?P<text>.*))?$")
_TS_LINE = re.compile(
    r"^\s*(?:(?:\d{4}[/.-]|\d{4}年)?\d{1,2}[/.月-]\d{1,4}日?(?:[/.-]\d{2,4})?,?\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\s*$"
)
_TS_FORMATS = [
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M %p",
    "%m/%d/%y %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y年%m月%d日 %H:%M",
    "%d.%m.%Y %H:%M",
    "%m/%d %I:%M %p",
    "%m/%d %H:%M",
    "%I:%M %p",
    "%H:%M",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %I:%M %p",
    "%Y-%m-%d %I:%M %p",
    "%Y年%m月%d日 %I:%M %p",
]


def _norm_ts(text: str) -> str:
    """'午後 1:05' -> '1:05 PM' (Japanese puts AM/PM before the time)."""
    t = re.sub(
        r"(午前|午後)\s*(\d{1,2}:\d{2}(?::\d{2})?)",
        lambda m: m.group(2) + (" AM" if m.group(1) == "午前" else " PM"),
        text,
    )
    return re.sub(r"\s+", " ", t).strip()


def _parse_ts(text: str, fallback: datetime) -> datetime:
    t = _norm_ts(text)
    t = re.sub(r"(?i)\b([ap])\.?m\.?$", lambda m: m.group(1).upper() + "M", t)
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:
            dt = dt.replace(year=fallback.year)
        if "%d" not in fmt:
            dt = dt.replace(year=fallback.year, month=fallback.month, day=fallback.day)
        return dt.astimezone()
    return fallback


def parse_pasted_chat(text: str, fallback: datetime) -> list[dict]:
    """Split text copied from Teams into messages. Handles '[time] Name: text' and 'Name / time / text'."""
    lines = text.replace("\r\n", "\n").split("\n")
    msgs: list[dict] = []
    cur: dict | None = None

    def start(name, ts, first=""):
        nonlocal cur
        cur = {"name": name.strip(), "time": _parse_ts(ts, fallback), "lines": [first] if first else []}
        msgs.append(cur)

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        m = _BRACKET_HEADER.match(line.strip())
        if m and _TS_LINE.match(_norm_ts(m.group("ts"))):
            start(m.group("name"), m.group("ts"), m.group("text") or "")
        elif (
            line.strip()
            and len(line.strip()) <= 60
            and i + 1 < len(lines)
            and _TS_LINE.match(_norm_ts(lines[i + 1]))
            and not _TS_LINE.match(line)
        ):
            start(line, lines[i + 1].strip())
            i += 1
        elif cur is not None:
            cur["lines"].append(line)
        elif line.strip():
            start("(unknown)", "", line)
            cur["time"] = fallback
        i += 1
    out = []
    for n, m in enumerate(msgs):
        body = "\n".join(m["lines"]).strip()
        if not body:
            continue
        key = f"{m['name']}|{m['time'].isoformat()}|{body[:200]}|{n if m['name'] == '(unknown)' else ''}"
        out.append({"id": "p" + short_hash(key, 12), "time": m["time"].isoformat(), "name": m["name"], "text": body})
    return out


def import_text(ws: Workspace, paths: list[str], title: str | None = None, log=print) -> dict:
    """Import chats copied from the Teams window (one file per chat) as chat-day records."""
    from pathlib import Path

    from kbmail.records import message_index as _mi

    # Map display names to e-mail addresses seen in mail, so people inference links them up.
    known: dict[str, str] = {}
    for rec in _mi(ws).all():
        for p in [rec.get("from") or {}, *(rec.get("to") or []), *(rec.get("cc") or [])]:
            if p.get("name") and p.get("email"):
                known.setdefault(p["name"].strip().lower(), p["email"].lower())
    index = message_index(ws)
    n_msgs = n_records = 0
    for f in [Path(x) for x in paths]:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        fallback = datetime.fromtimestamp(f.stat().st_mtime).astimezone()
        parsed = parse_pasted_chat(text, fallback)
        chat_title = title or f.stem.replace("_", " ")
        people = {}
        days_map: dict[str, list[dict]] = {}
        for m in parsed:
            person = {"name": m["name"], "email": known.get(m["name"].lower(), "")}
            people[m["name"]] = person
            day = parse_dt(m["time"]).strftime("%Y-%m-%d")
            days_map.setdefault(day, []).append({"id": m["id"], "time": m["time"], "from": person, "text": m["text"]})
        for day, msgs in days_map.items():
            write_chat_day(
                ws,
                index,
                "paste:" + chat_title.lower(),
                chat_title,
                "pasted",
                list(people.values()),
                day,
                msgs,
                "paste",
            )
            n_records += 1
        n_msgs += len(parsed)
        log(f"  {f.name}: {len(parsed)} message(s), {len(people)} speaker(s), {len(days_map)} day(s)")
    index.save()
    ws.record_run("export_teams", messages=n_msgs, records=n_records, route="paste")
    log(f"Teams (pasted text): {n_msgs} message(s) -> {n_records} chat-day record(s).")
    return {"messages": n_msgs, "records": n_records}


# ---------------------------------------------------------------- Outlook route (experimental)

TEAMS_FOLDER_NAMES = {"teamsmessagesdata", "team chat", "teamchat"}
TEAMS_FOLDER_LOOKUP = ("TeamsMessagesData", "Team Chat", "TeamChat")
NO_API_ROUTES = (
    "Routes that need no API: Share to Outlook (the threads arrive as mail), "
    "`kbmail teams import-text <file>` for chats copied from the Teams window, and meeting transcripts "
    "(.vtt/.docx) dropped into raw/inbox/ -> `kbmail raw import`. See docs/teams.md."
)


def _find_teams_folder(ns, log=print):
    from kbmail.outlook import _safe

    root = ns.GetDefaultFolder(6).Parent
    roots = [root, *[_safe(lambda s=s: s.GetRootFolder()) for s in _stores(ns)]]
    for r in [x for x in roots if x is not None]:
        # Hidden folders can be missing from enumeration but still open by name.
        for name in TEAMS_FOLDER_LOOKUP:
            f = _safe(lambda r=r, name=name: r.Folders.Item(name))
            if f is not None:
                return f
    stack = [(root, 0)]
    while stack:
        f, depth = stack.pop()
        name = (_safe(lambda: f.Name, "") or "").lower().replace(" ", "")
        if name in TEAMS_FOLDER_NAMES:
            return f
        if depth < 3:
            for i in range(1, _safe(lambda: f.Folders.Count, 0) + 1):
                stack.append((f.Folders.Item(i), depth + 1))
    return None


def _stores(ns) -> list:
    from kbmail.outlook import _safe

    stores = _safe(lambda: ns.Stores)
    return [stores.Item(i) for i in range(1, _safe(lambda: stores.Count, 0) + 1)] if stores is not None else []


def probe_outlook(ws: Workspace, log=print) -> bool:
    from kbmail.outlook import _safe, connect

    ns = connect()
    for st in _stores(ns):
        cached = _safe(lambda st=st: st.IsCachedExchange)
        mode = {True: "cached Exchange mode", False: "online mode"}.get(cached, "unknown mode")
        log(f"Mailbox store: {_safe(lambda st=st: st.DisplayName, '?')} ({mode})")
    f = _find_teams_folder(ns)
    if f is None:
        log(
            "No Teams chat folder is visible to Outlook COM in this profile. In cached Exchange mode Outlook often "
            "does not sync it. " + NO_API_ROUTES
        )
        return False
    n = _safe(lambda: f.Items.Count, 0)
    log(f"Found {f.FolderPath} with {n} item(s). Try: kbmail teams export --via outlook --days 7")
    if n == 0:
        log("The folder is empty in this profile (not synced). " + NO_API_ROUTES)
    return n > 0


def export_outlook(ws: Workspace, days: int | None = None, log=print) -> dict:
    from kbmail.outlook import _safe, com_dt, connect, recipients_of, sender_of

    ns = connect()
    f = _find_teams_folder(ns)
    if f is None:
        raise RuntimeError("Teams chat folder not visible in Outlook; use --via graph")
    state = ws.load_json(STATE, {"chats": {}, "outlook": {}})
    since = datetime.now().astimezone() - timedelta(days=days or ws.cfg("teams.days", 30))
    if not days and state.get("outlook", {}).get("watermark"):
        since = parse_dt(state["outlook"]["watermark"]) - timedelta(hours=1)
    items = f.Items
    items.Sort("[ReceivedTime]", True)
    index = message_index(ws)
    groups: dict[tuple[str, str], dict] = {}
    newest = since
    it = items.GetFirst()
    while it is not None:
        rt = com_dt(_safe(lambda: it.ReceivedTime))
        if rt and rt < since:
            break
        conv = _safe(lambda: it.ConversationID, "") or _safe(lambda: it.ConversationTopic, "") or "teams"
        topic = _safe(lambda: it.ConversationTopic, "") or _safe(lambda: it.Subject, "") or "Teams chat"
        rcp = recipients_of(it)
        sender = sender_of(it)
        g = groups.setdefault(
            (conv, rt.strftime("%Y-%m-%d") if rt else "unknown"),
            {"title": topic, "members": [sender, *rcp["to"]], "msgs": []},
        )
        g["msgs"].append(
            {
                "id": _safe(lambda: it.EntryID, ""),
                "time": (rt or datetime.now().astimezone()).isoformat(),
                "from": sender,
                "text": html_to_text(_safe(lambda: it.HTMLBody, "") or _safe(lambda: it.Body, "")),
            }
        )
        if rt and rt > newest:
            newest = rt
        it = items.GetNext()
    for (conv, day), g in groups.items():
        write_chat_day(ws, index, "outlook:" + conv, g["title"], "chat", g["members"], day, g["msgs"], "outlook")
    index.save()
    state.setdefault("outlook", {})["watermark"] = newest.isoformat()
    ws.save_json(STATE, state)
    ws.record_run("export_teams", records=len(groups), route="outlook")
    log(f"Teams (Outlook folder): {len(groups)} chat-day record(s).")
    return {"records": len(groups)}
