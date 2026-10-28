"""Relationship signals from mail + calendar metadata (step 11).

Python computes the hard numbers (who writes to whom, who is CC'd, who meets
with whom, in which areas, how quickly you reply). Copilot (/kb-people) turns
them into wiki/people/*.md pages with an inferred relationship (manager, peer,
customer, vendor, ...) that the user can correct in config/people.yaml.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import pairwise

from kbmail.labels import load_labels
from kbmail.records import calendar_index, message_index
from kbmail.textutil import email_domain, now_iso, parse_dt
from kbmail.workspace import Workspace

STATE = "state/people.json"
DIGEST = "work/people/PEOPLE_DIGEST.md"
OVERRIDES = "config/people.yaml"


def _overrides(ws: Workspace) -> dict:
    data = ws.load_yaml(OVERRIDES, {}) or {}
    alias = {}
    for canon, info in (data.get("people") or {}).items():
        for a in (info or {}).get("aliases") or []:
            alias[a.lower()] = canon.lower()
    return {
        "alias": alias,
        "ignore": {e.lower() for e in data.get("ignore") or []},
        "people": {k.lower(): v or {} for k, v in (data.get("people") or {}).items()},
    }


def _detect_me(ws: Workspace, recs: list[dict]) -> set[str]:
    me = set(ws.me_emails)
    if not me:
        c = Counter((r.get("from") or {}).get("email", "") for r in recs if r.get("direction") == "sent")
        if c:
            me.add(c.most_common(1)[0][0])
    return me


def build(ws: Workspace, log=print) -> dict:
    ov = _overrides(ws)
    recs = message_index(ws).all()
    me = _detect_me(ws, recs)
    labels = load_labels(ws)
    canon = lambda e: ov["alias"].get((e or "").lower(), (e or "").lower())  # noqa: E731
    P: dict[str, dict] = defaultdict(
        lambda: {
            "names": Counter(),
            "from_them": 0,
            "to_them": 0,
            "cc_them": 0,
            "they_cc_me": 0,
            "meetings": 0,
            "areas": Counter(),
            "co": Counter(),
            "first": None,
            "last": None,
            "reply_hours": [],
        }
    )
    by_thread: dict[str, list[dict]] = defaultdict(list)

    def touch(e, dt):
        if dt:
            p = P[e]
            p["first"] = min(p["first"] or dt, dt)
            p["last"] = max(p["last"] or dt, dt)

    for r in recs:
        dt = r.get("date", "")
        frm = canon((r.get("from") or {}).get("email"))
        to = [canon(p.get("email")) for p in r.get("to") or [] if p.get("email")]
        cc = [canon(p.get("email")) for p in r.get("cc") or [] if p.get("email")]
        names = {
            canon(p.get("email")): p.get("name")
            for p in [r.get("from") or {}, *(r.get("to") or []), *(r.get("cc") or [])]
        }
        lab = labels.get(r["id"], {})
        areas = lab.get("labels", []) if lab.get("status") in ("confirmed", "auto") else []
        everyone = [e for e in dict.fromkeys([frm, *to, *cc]) if e and e not in me and e not in ov["ignore"]]
        for e in everyone:
            if names.get(e):
                P[e]["names"][names[e]] += 1
            touch(e, dt)
            for a in areas:
                P[e]["areas"][a] += 1
            for o in everyone:
                if o != e:
                    P[e]["co"][o] += 1
        if frm in me:
            for e in to:
                if e in P:
                    P[e]["to_them"] += 1
            for e in cc:
                if e in P:
                    P[e]["cc_them"] += 1
        elif frm in P:
            P[frm]["from_them"] += 1
            if any(m in cc for m in me):
                P[frm]["they_cc_me"] += 1
        by_thread[r.get("thread_id", "")].append(r)

    # How fast do I reply to each person (signal of priority / seniority)?
    for msgs in by_thread.values():
        msgs.sort(key=lambda r: r.get("date", ""))
        for a, b in pairwise(msgs):
            fa = canon((a.get("from") or {}).get("email"))
            if fa in P and canon((b.get("from") or {}).get("email")) in me:
                da, db = parse_dt(a.get("date")), parse_dt(b.get("date"))
                if da and db and db > da:
                    P[fa]["reply_hours"].append((db - da).total_seconds() / 3600)

    for ev in calendar_index(ws).all():
        att = [canon(a.get("email")) for a in ev.get("attendees") or [] if a.get("email")]
        org = canon((ev.get("organizer") or {}).get("email"))
        everyone = [e for e in dict.fromkeys([org, *att]) if e and e not in me and e not in ov["ignore"]]
        if len(everyone) > 25:
            continue  # all-hands meetings say little about relationships
        for e in everyone:
            P[e]["meetings"] += 1
            touch(e, ev.get("start", ""))
            for o in everyone:
                if o != e:
                    P[e]["co"][o] += 0.5

    out = {}
    for e, p in P.items():
        total = p["from_them"] + p["to_them"] + p["cc_them"] + p["meetings"]
        if total == 0:
            continue
        rh = sorted(p["reply_hours"])
        out[e] = {
            "email": e,
            "name": p["names"].most_common(1)[0][0] if p["names"] else e,
            "domain": email_domain(e),
            "internal": bool(me) and email_domain(e) in {email_domain(m) for m in me},
            "from_them": p["from_them"],
            "to_them": p["to_them"],
            "cc_them": p["cc_them"],
            "they_cc_me": p["they_cc_me"],
            "meetings": p["meetings"],
            "score": total,
            "areas": dict(p["areas"].most_common(5)),
            "top_co": [c for c, _ in p["co"].most_common(6)],
            "median_reply_hours": round(rh[len(rh) // 2], 1) if rh else None,
            "first": p["first"],
            "last": p["last"],
            "override": ov["people"].get(e, {}),
        }
    _add_directory(ws, out, me)
    prev = ws.load_json(STATE, {}).get("people", {})
    for e, d in out.items():
        old = prev.get(e)
        d["changed"] = old is None or old.get("score") != d["score"]
    ws.save_json(STATE, {"built": now_iso(), "me": sorted(me), "people": out})
    _write_digest(ws, out, me)
    ws.record_run("people_build", people=len(out), changed=sum(d["changed"] for d in out.values()))
    log(f"People: {len(out)} contact(s), {sum(d['changed'] for d in out.values())} new/changed -> {DIGEST}")
    return out


def _add_directory(ws: Workspace, out: dict, me: set[str]) -> None:
    """Attach directory facts (kbmail directory sync) and add key org contacts without recent mail."""
    from kbmail import directory

    state = directory.load(ws)
    if not state.get("people"):
        return
    key_people = set(state.get("chain") or [])
    for email in state["people"]:
        if directory.org_relation(email, state, me) in ("direct-report", "manager"):
            key_people.add(email)
    for email in key_people - set(out) - me:
        rec = state["people"].get(email) or {}
        if rec.get("found"):
            out[email] = {
                "email": email,
                "name": rec["fields"].get("name", email),
                "domain": email_domain(email),
                "internal": True,
                "from_them": 0,
                "to_them": 0,
                "cc_them": 0,
                "they_cc_me": 0,
                "meetings": 0,
                "score": 0,
                "areas": {},
                "top_co": [],
                "median_reply_hours": None,
                "first": None,
                "last": None,
                "override": {},
                "no_recent_mail": True,
            }
    for email, d in out.items():
        info = directory.summary(ws, email, state, me)
        if info:
            d["directory"] = info


def _org_cell(d: dict) -> str:
    info = d.get("directory") or {}
    if not info:
        return ""
    parts = [info.get("org_relation", "")]
    parts += [info[k] for k in ("title", "department") if info.get(k)]
    mgr = (info.get("relations") or {}).get("manager")
    if mgr:
        parts.append(f"reports to {mgr}")
    return "; ".join(p for p in parts if p).replace("|", "/")


def _write_digest(ws: Workspace, people: dict, me: set[str]) -> None:
    ranked = sorted(people.values(), key=lambda d: -d["score"])
    top = ranked[: ws.cfg("people.top_n", 80)]
    top += [d for d in ranked if d.get("no_recent_mail") and d not in top]
    lines = [
        f"# People digest ({now_iso()})",
        "",
        f"Mailbox owner: {', '.join(sorted(me)) or 'unknown (set me.emails)'}",
        "Use with `/kb-people`. `changed` = new or more activity since the last digest.",
        "",
        "Columns: from_them = mails they sent you; to_them / cc_them = mails you sent them as To / CC; "
        "they_cc_me = they wrote someone else and CC'd you; meetings = shared calendar events; "
        "reply_h = your median reply time to them (hours). org = from the corporate directory "
        "(`kbmail directory sync`): relation to you, title, department, manager; authoritative for "
        "reporting lines.",
        "",
        "| person | domain | int | from_them | to_them | cc_them | they_cc_me | meetings | reply_h | areas "
        "| often with | org | changed | override |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in top:
        ov = ", ".join(f"{k}={v}" for k, v in d["override"].items() if k != "aliases")
        lines.append(
            f"| {d['name']} <{d['email']}> | {d['domain']} | {'Y' if d['internal'] else ''} | {d['from_them']} | "
            f"{d['to_them']} | {d['cc_them']} | {d['they_cc_me']} | {d['meetings']} | "
            f"{d['median_reply_hours'] or ''} | "
            f"{', '.join(f'{a}:{n}' for a, n in d['areas'].items())} | {', '.join(d['top_co'][:3])} | "
            f"{_org_cell(d)} | {'*' if d['changed'] else ''} | {ov} |"
        )
    p = ws.path(DIGEST)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
