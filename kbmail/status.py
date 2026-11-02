"""`kbmail status`: one place that says what has been done and what is due next."""

from __future__ import annotations

from datetime import datetime

from kbmail import images, kb, questions, rules, wiki
from kbmail.labels import label_counts, load_labels
from kbmail.records import calendar_index, message_index
from kbmail.textutil import parse_dt
from kbmail.workspace import Workspace


def collect(ws: Workspace) -> dict:
    index = message_index(ws)
    labels = load_labels(ws)
    lab = label_counts(ws)
    unlabeled = sum(1 for mid in index.items if mid not in labels)
    open_label_batches = ws.load_json("state/label_batches.json", {"open": {}})["open"]
    label_results = [n for n, b in open_label_batches.items() if ws.path(b["result"]).exists()]
    sel_batches = ws.load_json("state/kb_select_batches.json", {"open": {}})["open"]
    sel_results = [n for n, b in sel_batches.items() if ws.path(b["result"]).exists()]
    rules_due, rules_why = rules.due(ws)
    img = images.counts(ws)
    img_pending = img.get("image:pending", 0)
    svg_pending = img.get("svg:pending", 0)
    q = questions.counts(ws)
    kbs = kb.status(ws)
    conv = ws.load_json(kb.CONVERTED, {})
    unformatted = sum(1 for e in conv.values() if not kb.is_formatted(ws, e))
    wq = wiki.status(ws)
    all_runs = ws.load_json("state/runs.json", {})
    runs = {k: v.get("last_run") for k, v in all_runs.items() if not k.startswith("_")}
    last_export = parse_dt(runs.get("export_mail"))
    s = {
        "messages": len(index),
        "calendar_events": len(calendar_index(ws)),
        "labels": lab,
        "unlabeled": unlabeled,
        "label_batches_open": len(open_label_batches),
        "label_results_ready": label_results,
        "rules_due": rules_due,
        "rules_why": rules_why,
        "kb": kbs,
        "kb_select_open": len(sel_batches),
        "kb_select_results_ready": sel_results,
        "unformatted_threads": unformatted,
        "images": img,
        "images_pending": img_pending,
        "svg_pending": svg_pending,
        "questions": q,
        "wiki": wq,
        "last_runs": runs,
        "sync_problems": (all_runs.get("sync") or {}).get("problems") or [],
        "export_age_days": (datetime.now().astimezone() - last_export).total_seconds() / 86400 if last_export else None,
    }
    s["next"] = next_actions(ws, s)
    return s


def warnings(ws: Workspace, s: dict) -> list[str]:
    """Problems that silently stop the knowledge base from updating: listed before anything else."""
    out = []
    when = (s["last_runs"].get("sync") or "")[:16]
    for p in s.get("sync_problems") or []:
        out.append(
            f"WARNING: the last `kbmail sync` ({when}) failed at {p.get('step')}: {p.get('error')}. "
            "Fix the cause (Outlook running? signed in? VPN?) and run `kbmail sync` again."
        )
    warn_days, age = ws.cfg("sync.warn_after_days"), s.get("export_age_days")
    if warn_days and age is not None and age > warn_days and ws.cfg("sync.export"):
        out.append(
            f"WARNING: no mail exported for {int(age)} days (last export {s['last_runs']['export_mail'][:16]}). "
            "Run `kbmail sync` or check the scheduled task and state/sync.log."
        )
    return out


def next_actions(ws: Workspace, s: dict) -> list[str]:
    out = warnings(ws, s)
    if not ws.areas:
        out.append("Edit kbmail.yaml: set `me.emails` and the list of `areas`.")
    if s["messages"] == 0:
        out.append("Export mail: `kbmail export --days 30` (Windows/Outlook) or `kbmail import-eml <folder>`.")
        return out
    if s["label_results_ready"]:
        out.append(
            f"Import Copilot labels: `kbmail label import` ({len(s['label_results_ready'])} result file(s) ready)."
        )
    elif s["label_batches_open"]:
        out.append(f"Copilot: run `/kb-label` ({s['label_batches_open']} batch(es) in work/labeling/).")
    elif s["unlabeled"]:
        out.append(f"{s['unlabeled']} unlabeled message(s): `kbmail label apply-rules` then `kbmail label prepare`.")
    if s["labels"].get("proposed"):
        out.append(
            f"Review {s['labels']['proposed']} proposed label(s): `kbmail review` (or `kbmail review export-csv`)."
        )
    if s["rules_due"]:
        out.append(f"Refresh rules ({s['rules_why']}): `kbmail rules prepare`, then Copilot `/kb-rules`.")
    if s["kb_select_results_ready"]:
        out.append("Import KB selection: `kbmail kb import`.")
    elif s["kb_select_open"]:
        out.append(f"Copilot: run `/kb-select` ({s['kb_select_open']} batch(es) in work/kb_select/).")
    if s["svg_pending"]:
        out.append(f"Copilot: run `/kb-svg` ({s['svg_pending']} SVG(s) too complex for automatic Mermaid conversion).")
    if s["images_pending"]:
        who = (
            "Copilot `/kb-images`"
            if ws.cfg("copilot.vision")
            else "you: review/images/REVIEW.md + `kbmail images import`"
        )
        out.append(f"{s['images_pending']} image(s) need descriptions ({who}).")
    if s["questions"].get("open"):
        out.append(
            f"{s['questions']['open']} open question(s) in review/questions.yaml: "
            "answer, then `kbmail questions ingest`."
        )
    if s["questions"].get("answered"):
        out.append("Ingest answered questions: `kbmail questions ingest`.")
    if s["unformatted_threads"]:
        out.append(
            f"Copilot: run `/kb-format` ({s['unformatted_threads']} converted thread(s); see work/convert/QUEUE.md)."
        )
    w = s["wiki"]
    if w["queue_new"] or w["queue_changed"] or w["queue_deleted"]:
        out.append(
            f"Copilot: run `/kb-wiki-ingest` ({w['queue_new']} new, {w['queue_changed']} changed, "
            f"{w['queue_deleted']} deleted raw file(s))."
        )
    if not out:
        out.append("All caught up. Try `/kb-suggest` for emails to send, or `/kb-draft <topic or message id>`.")
    return out


def hook_context(ws: Workspace) -> str:
    """Compact status for the agent session-start hook (`kbmail hook session-start`)."""
    s = collect(ws)
    lines = [
        f"kbmail workspace {ws.root} - state at session start:",
        f"- messages {s['messages']}, calendar events {s['calendar_events']}, unlabeled {s['unlabeled']}, "
        f"labels {s['labels'] or '-'}",
        f"- rules: {'refresh DUE, ' if s['rules_due'] else ''}{s['rules_why']}",
        f"- KB: {s['kb']}; unformatted threads {s['unformatted_threads']}; images pending {s['images_pending']}, "
        f"SVGs for /kb-svg {s['svg_pending']}; questions {s['questions'] or '-'}",
        f"- wiki: {s['wiki']}",
        f"- copilot.vision: {str(bool(ws.cfg('copilot.vision'))).lower()}",
        "Next actions (in order):",
        *[f"{i}. {a}" for i, a in enumerate(s["next"], 1)],
        "Use the matching /kb-* skill for each action and follow AGENTS.md.",
    ]
    return "\n".join(lines)


def print_status(ws: Workspace, log=print) -> dict:
    s = collect(ws)
    log(f"Workspace: {ws.root}")
    log(f"Messages: {s['messages']}  calendar events: {s['calendar_events']}  unlabeled: {s['unlabeled']}")
    log(f"Labels: {s['labels'] or '-'}")
    log(f"Rules: {'DUE - ' if s['rules_due'] else ''}{s['rules_why']}")
    log(f"KB: {s['kb']}  unformatted: {s['unformatted_threads']}")
    log(f"Images: {s['images'] or '-'}  questions: {s['questions'] or '-'}")
    log(f"Wiki: {s['wiki']}")
    log("\nNext:")
    for i, a in enumerate(s["next"], 1):
        log(f"  {i}. {a}")
    return s
