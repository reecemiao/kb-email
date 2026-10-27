"""Microsoft Teams chats -> the same message records as mail (one record per chat per day).

Two routes (see docs/teams.md for the feasibility discussion):

1. Microsoft Graph (recommended): delegated `Chat.Read` with device-code sign-in
   via MSAL. Needs an Entra ID app registration (public client) that your tenant
   allows; set teams.client_id / teams.tenant in kbmail.yaml.
   Delta: per-chat watermark on lastModifiedDateTime.
2. Outlook (experimental): Exchange keeps a compliance copy of Teams chats in a
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
from datetime import datetime, timedelta, timezone

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

def write_chat_day(ws: Workspace, index, chat_key: str, title: str, chat_type: str, members: list[dict],
                   day: str, messages: list[dict], origin: str) -> str:
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
    body = "\n\n".join(f"**{parse_dt(m['time']).strftime('%H:%M')} {m['from'].get('name') or m['from'].get('email')}**: {m['text']}"
                       for m in msgs)
    meta = {
        "id": mid, "source": "teams", "kind": "chat", "folder": chat_type, "origin": origin,
        "direction": "sent" if not speakers else "received",
        "date": parse_dt(msgs[0]["time"]).astimezone().replace(microsecond=0).isoformat(),
        "subject": title, "from": lead,
        "to": [p for p in members if p != lead], "cc": [],
        "conversation_id": chat_key, "conversation_topic": title,
        "messages": len(msgs), "attachments": [],
    }
    old = index.get(mid)
    meta["exported_at"] = (old or {}).get("exported_at") or now_iso()
    meta["updated_at"] = now_iso() if len(stored) != before or not old else (old or {}).get("updated_at", "")
    write_message(ws, meta, body)
    index.upsert(meta)
    return mid


# ---------------------------------------------------------------- Graph route

def _token(ws: Workspace) -> str:
    try:
        import msal  # type: ignore
    except ImportError as e:
        raise RuntimeError("Install the Teams extra: pip install 'kbmail[teams]'") from e
    t = ws.config["teams"]
    if not t.get("client_id"):
        raise RuntimeError("Set teams.client_id (and teams.tenant) in kbmail.yaml - see docs/teams.md")
    cache = msal.SerializableTokenCache()
    cache_p = ws.path(t["token_cache"])
    if cache_p.exists():
        cache.deserialize(cache_p.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(t["client_id"], authority=f"https://login.microsoftonline.com/{t['tenant']}",
                                       token_cache=cache)
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
    now = datetime.now(timezone.utc)
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
        members = [{"name": m.get("displayName", ""), "email": (m.get("email") or "").lower(), "uid": m.get("userId", "")}
                   for m in chat.get("members") or []]
        by_uid = {m["uid"]: m for m in members if m["uid"]}
        title = chat.get("topic") or "Chat with " + ", ".join(m["name"] for m in members[:5] if m["email"] not in ws.me_emails)
        days_map: dict[str, list[dict]] = {}
        newest = since
        params = {"$top": "50", "$orderby": "lastModifiedDateTime desc", "$filter": f"lastModifiedDateTime gt {utc_iso(since)}"}
        for m in _get_all(s, f"{GRAPH}/chats/{cid}/messages", params):
            if m.get("messageType") != "message" or m.get("deletedDateTime"):
                continue
            user = ((m.get("from") or {}).get("user") or {})
            who = by_uid.get(user.get("id"), {"name": user.get("displayName", "?"), "email": ""})
            text = html_to_text((m.get("body") or {}).get("content", ""))
            for a in m.get("attachments") or []:
                if a.get("name"):
                    text += f"\n[file: {a['name']}]({a.get('contentUrl', '')})"
            created = parse_dt(m["createdDateTime"]).astimezone()
            days_map.setdefault(created.strftime("%Y-%m-%d"), []).append(
                {"id": m["id"], "time": created.isoformat(), "from": {"name": who["name"], "email": who["email"]}, "text": text})
            mod = parse_dt(m.get("lastModifiedDateTime") or m["createdDateTime"])
            newest = max(newest, mod)
            n_msgs += 1
        for day, msgs in days_map.items():
            write_chat_day(ws, index, cid, title, chat.get("chatType", "chat"),
                           [{"name": m["name"], "email": m["email"]} for m in members], day, msgs, "graph")
            n_records += 1
        state["chats"][cid] = {"watermark": newest.isoformat(), "title": title, "last_run": now_iso()}
    index.save()
    ws.save_json(STATE, state)
    ws.record_run("export_teams", messages=n_msgs, records=n_records, route="graph")
    log(f"Teams (Graph): {n_msgs} new/edited message(s) -> {n_records} chat-day record(s).")
    return {"messages": n_msgs, "records": n_records}


# ---------------------------------------------------------------- Outlook route (experimental)

TEAMS_FOLDER_NAMES = {"teamsmessagesdata", "team chat", "teamchat"}


def _find_teams_folder(ns, log=print):
    from kbmail.outlook import _safe
    root = ns.GetDefaultFolder(6).Parent
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


def probe_outlook(ws: Workspace, log=print) -> bool:
    from kbmail.outlook import _safe, connect
    ns = connect()
    f = _find_teams_folder(ns)
    if f is None:
        log("No Teams chat folder is visible to Outlook COM in this profile. Use the Graph route (docs/teams.md).")
        return False
    n = _safe(lambda: f.Items.Count, 0)
    log(f"Found {f.FolderPath} with {n} item(s). Try: kbmail teams export --via outlook --days 7")
    return True


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
        g = groups.setdefault((conv, rt.strftime("%Y-%m-%d") if rt else "unknown"),
                              {"title": topic, "members": [sender, *rcp["to"]], "msgs": []})
        g["msgs"].append({"id": _safe(lambda: it.EntryID, ""), "time": (rt or datetime.now().astimezone()).isoformat(),
                          "from": sender, "text": html_to_text(_safe(lambda: it.HTMLBody, "") or _safe(lambda: it.Body, ""))})
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
