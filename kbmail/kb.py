"""Knowledge-base candidate selection and conversion (steps 5-7).

Unit of work = thread (Outlook conversation / Teams chat). A thread is
(re)assessed when it gains messages, so later runs only look at deltas.

  kbmail kb prepare  -> work/kb_select/batch_NNN.md       (Copilot: /kb-select)
  kbmail kb import   -> state/kb_selection.json
  kbmail kb convert  -> work/convert/<thread>/thread.md   (markitdown + images + redaction)
                        work/convert/QUEUE.md             (Copilot: /kb-format -> raw/...)
"""

from __future__ import annotations

import json
import re
import shutil
from itertools import batched
from pathlib import Path

from kbmail import images as imgs
from kbmail.convert import (
    ANY_IMG_RE,
    CID_IMG_RE,
    CONVERTIBLE_EXT,
    DATA_IMG_RE,
    file_to_markdown,
    html_to_markdown,
    office_media,
)
from kbmail.labels import load_labels
from kbmail.records import fmt_people, fmt_person, message_index, read_message, threads
from kbmail.redact import redact
from kbmail.textutil import compact, now_iso, parse_frontmatter, short_hash, slugify, strip_quoted
from kbmail.workspace import OTHERS, Workspace

SELECTION = "state/kb_selection.json"
SELECT_BATCHES = "state/kb_select_batches.json"
CONVERTED = "state/kb_convert.json"
SELECT_DIR = "work/kb_select"
CONVERT_DIR = "work/convert"
IMG_MARK = "[[IMAGE:{}]]"


def _thread_areas(ws: Workspace, msgs: list[dict], labels: dict) -> list[str]:
    ok = {"confirmed", "auto"} if ws.cfg("kb.include_auto_labels", True) else {"confirmed"}
    areas: list[str] = []
    for m in msgs:
        lab = labels.get(m["id"])
        if lab and lab.get("status") in ok:
            areas += [a for a in lab["labels"] if a != OTHERS and a not in areas]
    return areas


def eligible_threads(ws: Workspace) -> dict[str, dict]:
    labels = load_labels(ws)
    out = {}
    for tid, msgs in threads(ws).items():
        areas = _thread_areas(ws, msgs, labels)
        if areas:
            out[tid] = {"messages": msgs, "areas": areas}
    return out


# ---------------------------------------------------------------- selection


def prepare_select(ws: Workspace, batch_threads: int = 8, log=print) -> list[str]:
    sel = ws.load_json(SELECTION, {})
    state = ws.load_json(SELECT_BATCHES, {"next": 1, "open": {}})
    in_open = {t for b in state["open"].values() for t in b["threads"]}
    todo = []
    for tid, t in eligible_threads(ws).items():
        ids = {m["id"] for m in t["messages"]}
        prev = sel.get(tid)
        if tid in in_open:
            continue
        if prev and ids <= set(prev.get("assessed_ids", [])):
            continue
        todo.append((tid, t, prev))
    if not todo:
        log("No threads need KB assessment.")
        return []
    chars = min(ws.cfg("kb.message_chars", 6000), 2500)
    written = []
    for chunk in batched(todo, batch_threads):
        name = f"batch_{state['next']:03d}"
        state["next"] += 1
        result = f"{SELECT_DIR}/{name}.result.jsonl"
        lines = [
            f"# KB selection {name} ({len(chunk)} threads)",
            "",
            "Decide which threads contain durable knowledge worth adding to the knowledge base",
            "(decisions, specifications, how-tos, explanations, status of projects, who owns what, numbers, dates).",
            "Skip pure logistics, thanks, scheduling ping-pong, newsletters, and duplicates.",
            "",
            f"Write **`{result}`**, one JSON object per thread:",
            "",
            '```json\n{"thread": "t0123456789a", "decision": "select", "title": "Short descriptive title", '
            '"reason": "why", "topics": ["..."], "messages": "all", "attachments": "all", '
            '"questions": ["What is system XYZ? (need a link to its docs)"]}\n```',
            "",
            "- `decision`: `select` or `skip`.",
            "- `messages`: `all` or a list of message ids to keep (drop pure 'thanks' replies).",
            "- `attachments`: `all`, `none`, or a list of attachment file names worth converting.",
            "- `questions`: things you could not understand (acronyms, systems, missing context, links the user "
            "should provide). They are copied to review/questions.yaml for the user.",
            "",
            "## Threads",
            "",
        ]
        for tid, t, prev in chunk:
            msgs = t["messages"]
            lines += [
                f"## Thread {tid}",
                f"- areas: {', '.join(t['areas'])}",
                f"- subject: {msgs[-1].get('subject', '')}",
                f"- {len(msgs)} message(s), {msgs[0].get('date', '')[:10]} .. {msgs[-1].get('date', '')[:10]}",
            ]
            if prev:
                lines.append(
                    f"- previously: **{prev['decision']}** ({prev.get('reason', '')}); "
                    f"new messages arrived since - reassess the whole thread"
                )
            lines.append("")
            for i, m in enumerate(msgs):
                try:
                    _, body = read_message(ws, m)
                except FileNotFoundError:
                    body = ""
                text = body if i == 0 else strip_quoted(body)
                atts = [a["name"] for a in m.get("attachments") or [] if not a.get("inline")]
                lines += [
                    f"### {m['id']} | {m.get('date', '')[:16]} | {fmt_person(m.get('from'))}",
                    f"to: {fmt_people(m.get('to'), 5)}",
                ]
                if atts:
                    lines.append(f"attachments: {', '.join(atts)}")
                lines += ["", redact(compact(text, chars))[0], ""]
        p = ws.path(SELECT_DIR, f"{name}.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        state["open"][name] = {
            "threads": [t[0] for t in chunk],
            "file": ws.rel(p),
            "result": result,
            "created": now_iso(),
            "ids": {tid: [m["id"] for m in t["messages"]] for tid, t, _ in chunk},
        }
        written.append(ws.rel(p))
    ws.save_json(SELECT_BATCHES, state)
    ws.record_run("kb_prepare", batches=len(written), threads=len(todo))
    log(
        f"Wrote {len(written)} KB selection batch(es) for {len(todo)} thread(s). "
        "Next: /kb-select, then `kbmail kb import`."
    )
    return written


def import_select(ws: Workspace, log=print) -> dict:
    from kbmail.questions import add_question

    sel = ws.load_json(SELECTION, {})
    state = ws.load_json(SELECT_BATCHES, {"next": 1, "open": {}})
    stats = {"select": 0, "skip": 0, "errors": 0, "questions": 0}
    for name, b in list(state["open"].items()):
        rp = ws.path(b["result"])
        if not rp.exists():
            continue
        for n, line in enumerate(rp.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip().rstrip(",")
            if not line or line.startswith(("```", "#", "//")):
                continue
            try:
                obj = json.loads(line)
                tid = obj["thread"]
                if tid not in b["ids"] or obj.get("decision") not in ("select", "skip"):
                    raise ValueError(f"bad thread/decision: {tid} {obj.get('decision')}")
            except (ValueError, KeyError, TypeError) as e:
                stats["errors"] += 1
                log(f"  {name} line {n}: {e}")
                continue
            sel[tid] = {
                "decision": obj["decision"],
                "title": obj.get("title", ""),
                "reason": obj.get("reason", ""),
                "topics": obj.get("topics", []),
                "messages": obj.get("messages", "all"),
                "attachments": obj.get("attachments", "all"),
                "assessed_ids": b["ids"][tid],
                "assessed_at": now_iso(),
            }
            stats[obj["decision"]] += 1
            for q in obj.get("questions") or []:
                add_question(ws, q, context=f"thread {tid}: {obj.get('title', '')}", step="kb-select")
                stats["questions"] += 1
        done = ws.path(SELECT_DIR, "done")
        done.mkdir(parents=True, exist_ok=True)
        for f in (ws.path(b["file"]), rp):
            if f.exists():
                shutil.move(str(f), done / f.name)
        del state["open"][name]
    ws.save_json(SELECTION, sel)
    ws.save_json(SELECT_BATCHES, state)
    ws.record_run("kb_import", **stats)
    log(f"KB selection imported: {stats}. Next: `kbmail kb convert`.")
    return stats


# ---------------------------------------------------------------- conversion


def _signature(msgs: list[dict]) -> str:
    return short_hash("|".join(f"{m['id']}:{m.get('updated_at', '')}" for m in msgs), 10)


def _raw_target(ws: Workspace, tid: str, msgs: list[dict], areas: list[str], title: str) -> str:
    src = msgs[0].get("source", "outlook")
    base = "raw/teams" if src == "teams" else "raw/email"
    area = slugify(areas[0] if areas else "general", 30)
    date = msgs[0].get("date", "")[:10]
    return f"{base}/{area}/{date}_{slugify(title or msgs[-1].get('subject', ''), 50)}_{tid}.md"


def _convert_message(ws: Workspace, reg: dict, tid: str, m: dict, first: bool, att_choice) -> list[str]:
    meta, body = read_message(ws, m)
    ctx = f"{m.get('date', '')[:10]} '{m.get('subject', '')}' from {fmt_person(m.get('from'))}"
    cid_files = {
        a["content_id"]: ws.path(a["file"])
        for a in meta.get("attachments") or []
        if a.get("content_id") and a.get("file") and ws.path(a["file"]).exists()
    }
    if meta.get("html_file") and ws.path(meta["html_file"]).exists():
        text = html_to_markdown(ws.path(meta["html_file"]))
        text = CID_IMG_RE.sub(lambda mm: f"[[CID:{mm.group(1)}]]" if mm.group(1) in cid_files else "", text)
        text = DATA_IMG_RE.sub("", text)
        text = ANY_IMG_RE.sub("", text)  # remote images (tracking pixels, logos)
    else:
        text = body
    if not first:
        text = strip_quoted(text)

    def _inline(mm):  # register only inline images that survive quote stripping
        f = cid_files[mm.group(1)]
        iid = imgs.register(
            ws, reg, f.read_bytes(), f.suffix, {"thread": tid, "message": m["id"], "origin": "inline", "context": ctx}
        )
        return IMG_MARK.format(iid) if iid else ""

    text = re.sub(r"\[\[CID:([^\]]+)\]\]", _inline, text)
    out = [
        f"## Message {m['id']}",
        "",
        f"- date: {m.get('date', '')}",
        f"- from: {fmt_person(m.get('from'))}",
        f"- to: {fmt_people(m.get('to'), 20)}",
    ]
    if m.get("cc"):
        out.append(f"- cc: {fmt_people(m.get('cc'), 20)}")
    out += [f"- subject: {m.get('subject', '')}", "", text.strip(), ""]
    for a in meta.get("attachments") or []:
        if a.get("inline") and a.get("content_id") in cid_files and meta.get("html_file"):
            continue  # inline image, already placed in the body
        if att_choice != "all" and (att_choice == "none" or a["name"] not in (att_choice or [])):
            continue
        if not a.get("file") or not ws.path(a["file"]).exists():
            out += [f"### Attachment: {a['name']}", "", f"(not exported: {a.get('skipped', 'missing')})", ""]
            continue
        f = ws.path(a["file"])
        ext = f.suffix.lower()
        src = {"thread": tid, "message": m["id"], "origin": a["name"], "context": f"{ctx}; attachment {a['name']}"}
        out += [f"### Attachment: {a['name']}", ""]
        if ext in imgs.IMAGE_EXT | imgs.SVG_EXT:
            iid = imgs.register(ws, reg, f.read_bytes(), ext, src)
            out += [IMG_MARK.format(iid) if iid else "(small image ignored)", ""]
            continue
        if ext in CONVERTIBLE_EXT or ext in {".txt", ".md", ".csv"}:
            md, err = file_to_markdown(f)
            md = ANY_IMG_RE.sub("", md)
            out += [md.strip() if not err else f"(conversion failed: {err})", ""]
            media = [
                imgs.register(ws, reg, data, Path(n).suffix, {**src, "origin": f"{a['name']}:{n}"})
                for n, data in office_media(f)
            ]
            media = [x for x in media if x]
            if media:
                out += [f"Images inside {a['name']}:", "", *[IMG_MARK.format(x) for x in media], ""]
        else:
            out += [f"(binary attachment {ext} not converted)", ""]
    return out


def convert(ws: Workspace, force: bool = False, log=print) -> dict:
    sel = ws.load_json(SELECTION, {})
    conv = ws.load_json(CONVERTED, {})
    reg = imgs.load(ws)
    all_threads = eligible_threads(ws)
    index = message_index(ws)
    stats = {"converted": 0, "unchanged": 0, "redactions": 0}
    for tid, s in sel.items():
        if s["decision"] != "select":
            continue
        t = all_threads.get(tid)
        msgs = t["messages"] if t else [index.get(i) for i in s["assessed_ids"] if index.get(i)]
        keep = s.get("messages", "all")
        if keep != "all":
            msgs = [m for m in msgs if m["id"] in keep or m["id"] not in s["assessed_ids"]]
        if not msgs:
            continue
        sig = _signature(msgs)
        prev = conv.get(tid)
        if prev and prev.get("signature") == sig and not force:
            stats["unchanged"] += 1
            continue
        areas = (t or {}).get("areas") or (prev or {}).get("areas") or []
        target = (prev or {}).get("raw_target") or _raw_target(ws, tid, msgs, areas, s.get("title", ""))
        parts = [
            f"# {s.get('title') or msgs[-1].get('subject', '')}",
            "",
            f"- thread: {tid}",
            f"- areas: {', '.join(areas)}",
            f"- topics: {', '.join(s.get('topics', []))}",
            f"- messages: {len(msgs)} ({msgs[0].get('date', '')[:10]} .. {msgs[-1].get('date', '')[:10]})",
            "",
        ]
        for i, m in enumerate(msgs):
            parts += _convert_message(ws, reg, tid, m, i == 0, s.get("attachments", "all"))
        text = "\n".join(parts)
        if ws.cfg("privacy.redact", True):
            text, n = redact(text, ws.cfg("privacy.extra_patterns", []))
            stats["redactions"] += n
        d = ws.path(CONVERT_DIR, tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "thread.src.md").write_text(text, encoding="utf-8")
        conv[tid] = {
            "signature": sig,
            "converted_at": now_iso(),
            "raw_target": target,
            "areas": areas,
            "title": s.get("title", ""),
            "message_ids": [m["id"] for m in msgs],
            "images": sorted(set(_markers(text))),
            "formatted": False,
        }
        stats["converted"] += 1
    imgs.save(ws, reg)
    imgs.write_review_files(ws, reg)
    ws.save_json(CONVERTED, conv)
    ws.record_run("kb_convert", **stats)
    log(
        f"Converted {stats['converted']} thread(s) "
        f"({stats['unchanged']} unchanged, {stats['redactions']} redaction(s))."
    )
    write_queue(ws, log=log)
    return stats


def _markers(text: str) -> list[str]:
    return re.findall(r"\[\[IMAGE:(img_[0-9a-f]+)\]\]", text)


def is_formatted(ws: Workspace, entry: dict) -> bool:
    p = ws.path(entry["raw_target"])
    if not p.exists():
        return False
    meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
    return str(meta.get("source_version", "")) == entry["signature"]


def write_queue(ws: Workspace, log=print) -> dict:
    """Render thread.md for unformatted threads and list the ones ready for /kb-format."""
    conv = ws.load_json(CONVERTED, {})
    sel = ws.load_json(SELECTION, {})
    reg = imgs.load(ws)
    ready, blocked, done = [], [], 0
    for tid, e in conv.items():
        if sel.get(tid, {}).get("decision") != "select":
            continue  # re-assessed as skip after conversion
        if is_formatted(ws, e):
            e["formatted"] = True
            done += 1
            continue
        e["formatted"] = False
        src = ws.path(CONVERT_DIR, tid, "thread.src.md")
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        pending = [i for i in e.get("images", []) if not imgs.is_resolved(reg, i)]
        for iid in e.get("images", []):
            text = text.replace(IMG_MARK.format(iid), imgs.render(ws, reg, iid))
        ws.path(CONVERT_DIR, tid, "thread.md").write_text(text, encoding="utf-8")
        (blocked if pending else ready).append((tid, e, pending))
    ws.save_json(CONVERTED, conv)
    lines = [
        "# Format queue",
        "",
        f"Generated {now_iso()} by `kbmail kb convert` / `kbmail kb queue`.",
        "Run `/kb-format` in Copilot Chat. For each READY thread: read `source`, write `target` "
        "(see the prompt for the format) and copy `source_version` into the target's front matter.",
        "",
        f"## Ready ({len(ready)})",
        "",
    ]
    for tid, e, _ in ready:
        exists = ws.path(e["raw_target"]).exists()
        lines += [
            f"- thread `{tid}`: {e.get('title', '')}",
            f"  - source: `{CONVERT_DIR}/{tid}/thread.md`",
            f"  - target: `{e['raw_target']}`" + (" (exists: UPDATE it, keep earlier content)" if exists else ""),
            f"  - source_version: `{e['signature']}`",
            f"  - areas: {', '.join(e.get('areas', []))}",
        ]
    lines += ["", f"## Blocked on image descriptions ({len(blocked)})", ""]
    for tid, e, pending in blocked:
        lines.append(f"- `{tid}` {e.get('title', '')}: waiting for {', '.join(pending)} (see review/images/REVIEW.md)")
    lines += ["", f"Already formatted: {done}"]
    p = ws.path(CONVERT_DIR, "QUEUE.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Format queue: {len(ready)} ready, {len(blocked)} blocked on images, {done} done -> {ws.rel(p)}")
    return {"ready": len(ready), "blocked": len(blocked), "formatted": done}


def status(ws: Workspace) -> dict:
    sel = ws.load_json(SELECTION, {})
    conv = ws.load_json(CONVERTED, {})
    open_b = ws.load_json(SELECT_BATCHES, {"open": {}})["open"]
    return {
        "selected": sum(1 for v in sel.values() if v["decision"] == "select"),
        "skipped": sum(1 for v in sel.values() if v["decision"] == "skip"),
        "open_batches": len(open_b),
        "converted": len(conv),
        "formatted": sum(1 for e in conv.values() if is_formatted(ws, e)),
    }
