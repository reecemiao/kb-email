"""Area discovery: Copilot proposes the areas (topics) from a digest of the mailbox, the user picks.

The other mode is the user writing `areas` in kbmail.yaml by hand; both end in that list.

work/areas/DISCOVER.md             digest of the busiest threads + the result format (`kbmail areas discover`)
work/areas/proposals.result.jsonl  Copilot's proposals, one JSON object per line (`/kb-areas`)
state/area_discovery.json          the digest's threads and the imported, numbered proposals
kbmail.yaml `areas:`               the picked areas (`kbmail areas pick`); only that block is rewritten
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

import yaml

from kbmail.history import History
from kbmail.labels import NOISE, load_labels
from kbmail.lock import workspace_lock
from kbmail.noise import render_clean
from kbmail.records import message_index, read_message, threads
from kbmail.redact import redact
from kbmail.textutil import compact, now_iso
from kbmail.workspace import CONFIG_NAME, OTHERS, Workspace

DIGEST = "work/areas/DISCOVER.md"
RESULT = "work/areas/proposals.result.jsonl"
STATE = "state/area_discovery.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_PLACEHOLDER_RE = re.compile(r"^area\d+$")
_TOP_KEY_RE = re.compile(r"^[^\s#-][^:]*:")


def configured_areas(ws: Workspace) -> list[dict]:
    """The areas the user really has: the template's placeholders (area1, area2 without keywords) don't count."""
    return [a for a in ws.areas if not (_PLACEHOLDER_RE.match(a["name"]) and not a["keywords"])]


# ---------------------------------------------------------------- digest for Copilot


def _person(p: dict | None) -> str:
    p = p or {}
    return p.get("name") or p.get("email") or ""


def discover(ws: Workspace, max_threads: int | None = None, preview: int | None = None, log=print) -> str | None:
    """Write the digest Copilot proposes areas from: the threads the user took part in first, then the biggest.

    Threads already labeled into a configured area are left out, so a later run looks for what the
    current areas miss. Mail labeled as noise (meeting responses without a comment) is left out too.
    """
    max_threads = max_threads or ws.cfg("labeling.discover_threads", 150)
    preview = preview or ws.cfg("labeling.discover_preview_chars", 200)
    index = message_index(ws)
    if not len(index):
        log("No mail exported yet: run `kbmail export` (or `kbmail import-eml`) first.")
        return None
    labels = load_labels(ws)
    current = configured_areas(ws)
    candidates = []
    for tid, msgs in threads(ws, index).items():
        labs = [labels.get(m["id"]) for m in msgs]
        if all(lab and OTHERS not in lab["labels"] for lab in labs):
            continue  # covered by the configured areas
        if all(lab and lab.get("source") == NOISE for lab in labs):
            continue
        msgs = sorted(msgs, key=lambda m: m.get("date", ""))
        sent = sum(1 for m in msgs if m.get("direction") == "sent")
        candidates.append((sent > 0, len(msgs), msgs[-1].get("date", ""), tid, msgs, sent))
    candidates.sort(key=lambda c: c[:3], reverse=True)
    picked = candidates[:max_threads]
    if not picked:
        log("Nothing to discover: every thread is already labeled into a configured area.")
        return None
    hist = History.of(ws)
    me = ws.me_emails
    lines = [
        f"# Discover areas ({len(picked)} of {len(candidates)} threads)",
        "",
        "Propose the areas this mailbox should be sorted into. An area is a recurring subject of the user's",
        "work (a project, product, customer, system or duty): broad enough for many threads, narrow enough",
        "that the label means something. Aim for 4-12 areas. Mail that fits none (newsletters, notifications,",
        f"HR/admin, social) becomes `{OTHERS}`: don't propose an area for it.",
        "",
    ]
    if current:
        lines += ["## Already configured (don't propose these again; the threads below are what they miss)", ""]
        lines += [f"- `{a['name']}` - {a['description']}" for a in current]
        lines.append("")
    lines += [
        "## Output",
        "",
        f"Write **`{RESULT}`** with exactly one JSON object per line, one line per area:",
        "",
        '```json\n{"name": "payments", "description": "Payment API, billing and invoices with vendor X", '
        '"keywords": ["payment", "invoice", "PAYMIG"], "threads": ["t0123456789a", "t0123456789b"]}\n```',
        "",
        f"- name: short lowercase slug (a-z, 0-9, `-`), not `{OTHERS}`",
        "- description: one line saying what belongs in the area (labeling uses it later)",
        "- keywords: 3-8 words typical for the area (product, project, customer names, acronyms)",
        "- threads: the ids of *every* thread below that belongs to the area; a thread may be in two areas",
        "",
        "## Threads",
        "",
    ]
    digest = {}
    for _, n, _, tid, msgs, sent in picked:
        people, names = Counter(), {}
        for m in msgs:
            for p in [m.get("from"), *(m.get("to") or []), *(m.get("cc") or [])]:
                key = ((p or {}).get("email") or _person(p)).lower()
                if key and key not in me:
                    people[key] += 1
                    if (p.get("name") or "") or key not in names:  # a display name beats a bare address
                        names[key] = _person(p)
        who = ", ".join(names[k] for k, _ in people.most_common(3))
        more = f" +{len(people) - 3}" if len(people) > 3 else ""
        first, last = msgs[0].get("date", "")[:10], msgs[-1].get("date", "")[:10]
        when = first if first == last else f"{first}..{last}"
        you = f", you sent {sent}" if sent else ""
        lines.append(f"### {tid} | {msgs[0].get('subject') or '(no subject)'}")
        lines.append(f"- {n} msg(s), {when}{you} | {who}{more}")
        text = ""
        for m in msgs:  # the first message with text of its own says what the thread is about
            try:
                _, body = read_message(ws, m)
            except FileNotFoundError:
                continue
            text = compact(render_clean(ws, hist, m, body, labels=labels), preview)
            if text.strip():
                break
        if text.strip():
            lines.append("> " + redact(text)[0].replace("\n", " "))
        lines.append("")
        digest[tid] = n
    p = ws.path(DIGEST)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    ws.save_json(STATE, {"created": now_iso(), "threads": digest, "proposals": []})
    ws.record_run("areas_discover", threads=len(picked), candidates=len(candidates))
    log(
        f"Wrote {DIGEST} ({len(picked)} of {len(candidates)} thread(s)). "
        "Next: run /kb-areas in Copilot Chat, then `kbmail areas import`."
    )
    return ws.rel(p)


# ---------------------------------------------------------------- Copilot's proposals


def import_proposals(ws: Workspace, log=print) -> dict:
    """Validate Copilot's proposals and number them for `kbmail areas pick`. Safe to run again."""
    state = ws.load_json(STATE, {})
    digest = state.get("threads") or {}
    rp = ws.path(RESULT)
    summary = {"imported": 0, "errors": 0, "skipped": 0}
    if not digest or not rp.exists():
        log(f"Nothing to import: run `kbmail areas discover`, then /kb-areas to write {RESULT}.")
        return summary
    current = {a["name"].lower() for a in configured_areas(ws)}
    proposals, seen = [], set()
    for n, line in enumerate(rp.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip().rstrip(",")
        if not line or line.startswith(("```", "//", "#")):
            continue
        try:
            obj = json.loads(line)
            name = re.sub(r"[\s_]+", "-", str(obj["name"]).strip().lower())
            desc = str(obj.get("description") or "").strip()
            kws = obj.get("keywords") or []
            kws = [k.strip() for k in kws.split(",")] if isinstance(kws, str) else [str(k).strip() for k in kws]
            if not NAME_RE.match(name) or name == OTHERS:
                raise ValueError(f"bad area name {obj['name']!r} (lowercase a-z, 0-9, -; not {OTHERS})")
            if not desc:
                raise ValueError(f"{name}: description is empty")
            if name in seen:
                raise ValueError(f"{name}: proposed twice")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            summary["errors"] += 1
            log(f"  line {n}: {e}")
            continue
        seen.add(name)
        if name in current:
            summary["skipped"] += 1
            log(f"  line {n}: `{name}` is already configured; skipped.")
            continue
        tids = [t for t in dict.fromkeys(str(t) for t in obj.get("threads") or []) if t in digest]
        unknown = len(obj.get("threads") or []) - len(tids)
        if unknown > 0:
            log(f"  line {n}: {name}: ignored {unknown} thread id(s) not in the digest.")
        proposals.append(
            {
                "name": name,
                "description": desc,
                "keywords": [k for k in dict.fromkeys(kws) if k],
                "threads": tids,
                "n_threads": len(tids),
                "n_messages": sum(digest[t] for t in tids),
            }
        )
    proposals.sort(key=lambda p: (-p["n_threads"], p["name"]))
    covered = {t for p in proposals for t in p["threads"]}
    state.update(
        proposals=proposals,
        imported=now_iso(),
        result_mtime=rp.stat().st_mtime,
        unassigned=len(set(digest) - covered),
    )
    ws.save_json(STATE, state)
    summary["imported"] = len(proposals)
    ws.record_run("areas_import", **summary)
    for line in format_proposals(ws):
        log(line)
    log(
        f"Imported {len(proposals)} proposal(s); {summary['errors']} bad line(s). "
        "Next: pick the ones to keep with `kbmail areas pick <numbers or names>`."
    )
    return summary


def format_proposals(ws: Workspace) -> list[str]:
    state = ws.load_json(STATE, {})
    props = state.get("proposals") or []
    current = configured_areas(ws)
    lines = []
    if current:
        lines.append("Configured areas: " + ", ".join(a["name"] for a in current))
    if not props:
        lines.append("No proposals: run `kbmail areas discover`, then /kb-areas, then `kbmail areas import`.")
        return lines
    for i, p in enumerate(props, 1):
        lines.append(f"[{i}] {p['name']}  ({p['n_threads']} thread(s), {p['n_messages']} msg(s)): {p['description']}")
        if p["keywords"]:
            lines.append(f"     keywords: {', '.join(p['keywords'])}")
    total = len(state.get("threads") or {})
    lines.append(f"{state.get('unassigned', 0)} of {total} digest thread(s) fit no proposal (they become `{OTHERS}`).")
    return lines


# ---------------------------------------------------------------- the user's pick -> kbmail.yaml


def _yaml_str(s: str) -> str:
    return s if NAME_RE.match(s) else json.dumps(s, ensure_ascii=False)  # a JSON string is a valid YAML scalar


def areas_block(areas: list[dict]) -> str:
    out = ["areas:"]
    for a in areas:
        kws = ", ".join(json.dumps(str(k), ensure_ascii=False) for k in a.get("keywords") or [])
        out += [
            f"  - name: {_yaml_str(a['name'])}",
            f"    description: {json.dumps(a.get('description') or '', ensure_ascii=False)}",
            f"    keywords: [{kws}]",
        ]
    return "\n".join(out) + "\n"


def replace_areas_block(text: str, areas: list[dict]) -> str:
    """kbmail.yaml with only its top-level `areas:` block replaced: comments and other settings stay as they are."""
    block = areas_block(areas)
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^areas\s*:", line)), None)
    if start is None:
        new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    else:
        last = start
        for i in range(start + 1, len(lines)):
            if _TOP_KEY_RE.match(lines[i]):
                break
            if lines[i].strip() and not lines[i].startswith("#"):
                last = i  # comments and blank lines after the list belong to the next section
        new = "".join(lines[:start]) + block + "".join(lines[last + 1 :])
    got = (yaml.safe_load(new) or {}).get("areas")
    if [a["name"] for a in got or []] != [a["name"] for a in areas]:
        raise ValueError("could not rewrite the areas block of kbmail.yaml; edit it by hand")
    return new


def pick(
    ws: Workspace,
    selections: list[str] | None = None,
    all_: bool = False,
    replace: bool = False,
    input_fn=None,
    out=print,
) -> list[str] | None:
    """Write the chosen proposals into kbmail.yaml `areas`. Returns the new area names, or None if nothing changed.

    By default they are added to the areas already configured (the template placeholders are dropped);
    `replace` keeps only the chosen ones.
    """
    props = ws.load_json(STATE, {}).get("proposals") or []
    if not props:
        out("No proposals: run `kbmail areas discover`, then /kb-areas, then `kbmail areas import`.")
        return None
    if all_:
        chosen = list(props)
    else:
        if not selections:
            if input_fn is None:
                out(
                    "Say which to keep: `kbmail areas pick 1 3 5` (numbers or names from `kbmail areas list`) "
                    "or `--all`."
                )
                return None
            for line in format_proposals(ws):
                out(line)
            ans = input_fn("Keep which? (e.g. 1,3,5  a = all  Enter = cancel) > ").strip()
            if not ans:
                out("Nothing changed.")
                return None
            selections = ["all"] if ans.lower() in ("a", "all") else [ans]
        if [s.lower() for s in selections] == ["all"]:
            chosen = list(props)
        else:
            by_name = {p["name"]: p for p in props}
            chosen = []
            for tok in (t for s in selections for t in re.split(r"[,\s]+", s.strip()) if t):
                if tok.isdigit() and 1 <= int(tok) <= len(props):
                    p = props[int(tok) - 1]
                elif tok.lower() in by_name:
                    p = by_name[tok.lower()]
                else:
                    out(
                        f"Unknown proposal {tok!r}: use the numbers or names from `kbmail areas list`. Nothing changed."
                    )
                    return None
                if p not in chosen:
                    chosen.append(p)
    kept = [] if replace else configured_areas(ws)
    names = {a["name"].lower() for a in kept}
    new = [*kept, *({k: p[k] for k in ("name", "description", "keywords")} for p in chosen if p["name"] not in names)]
    with workspace_lock(ws, "areas pick"):
        p = ws.path(CONFIG_NAME)
        text = replace_areas_block(p.read_text(encoding="utf-8") if p.exists() else "", new)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
        ws._config = None
        ws.record_run("areas_pick", picked=len(chosen), areas=len(new))
    out(f"kbmail.yaml areas: {', '.join(a['name'] for a in new)}.")
    used = {a for v in load_labels(ws).values() for a in v.get("labels") or []}
    if dropped := used - {a["name"] for a in new} - {OTHERS}:
        out(
            f"Labels still use {', '.join(sorted(dropped))}, which is no longer an area: relabel that mail "
            "(`kbmail review --source all`) and check config/rules.yaml."
        )
    out("Next: `kbmail label prepare`, then /kb-label in Copilot Chat.")
    return [a["name"] for a in new]


def status(ws: Workspace) -> dict:
    state = ws.load_json(STATE, {})
    rp = ws.path(RESULT)
    has_digest = bool(state.get("threads"))
    return {
        "configured": len(configured_areas(ws)),
        "digest": has_digest and ws.path(DIGEST).exists(),
        "result_ready": has_digest and rp.exists() and rp.stat().st_mtime != state.get("result_mtime"),
        "proposals": len(state.get("proposals") or []),
    }
