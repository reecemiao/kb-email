"""Label store, Copilot labeling batches, and the interactive / CSV review.

state/labels.json:
  { "<message id>": {"labels": ["area1"], "status": "proposed|auto|confirmed",
                      "source": "copilot|rule|user", "confidence": 0.8, "reason": "...",
                      "updated": "...", "history": [...] } }

status: proposed = Copilot suggestion awaiting review; auto = assigned by rules;
confirmed = reviewed by the user (the ground truth for rule refreshes).
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from itertools import batched

from kbmail.records import fmt_people, fmt_person, message_index, read_message
from kbmail.redact import redact
from kbmail.textutil import compact, now_iso, strip_quoted
from kbmail.workspace import OTHERS, Workspace

LABELS = "state/labels.json"
BATCHES = "state/label_batches.json"
WORKDIR = "work/labeling"


def load_labels(ws: Workspace) -> dict:
    return ws.load_json(LABELS, {})


def save_labels(ws: Workspace, labels: dict) -> None:
    ws.save_json(LABELS, labels)


def set_label(
    labels: dict, mid: str, areas: list[str], status: str, source: str, confidence=None, reason: str = ""
) -> None:
    prev = labels.get(mid)
    hist = (prev or {}).get("history", [])
    if prev:
        hist = [*hist, {k: prev.get(k) for k in ("labels", "status", "source", "updated")}][-10:]
    labels[mid] = {
        "labels": areas,
        "status": status,
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "updated": now_iso(),
        "history": hist,
    }


# ---------------------------------------------------------------- batches for Copilot


def _area_block(ws: Workspace) -> list[str]:
    lines = []
    for i, a in enumerate(ws.areas, 1):
        kw = f" Keywords: {', '.join(a['keywords'])}." if a["keywords"] else ""
        lines.append(f"{i}. `{a['name']}` - {a['description']}{kw}")
    lines.append(f"{len(ws.areas) + 1}. `{OTHERS}` - not related to any area above")
    return lines


def prepare_batches(ws: Workspace, batch_size: int | None = None, limit: int | None = None, log=print) -> list[str]:
    from kbmail.rules import classify, features, load_rules

    batch_size = batch_size or ws.cfg("labeling.batch_size", 40)
    preview = ws.cfg("labeling.preview_chars", 700)
    labels = load_labels(ws)
    state = ws.load_json(BATCHES, {"next": 1, "open": {}})
    in_open = {mid for b in state["open"].values() for mid in b["ids"]}
    pending = [r for r in message_index(ws).all() if r["id"] not in labels and r["id"] not in in_open]
    if limit:
        pending = pending[:limit]
    if not pending:
        log("Nothing to label: every message is labeled or already in an open batch.")
        return []
    rules = load_rules(ws)
    written = []
    for chunk in batched(pending, batch_size):
        name = f"batch_{state['next']:03d}"
        state["next"] += 1
        result = f"{WORKDIR}/{name}.result.jsonl"
        lines = [
            f"# Label batch {name} ({len(chunk)} messages)",
            "",
            "Assign each message to one or more areas, or `others`.",
            "",
            "## Areas",
            "",
            *_area_block(ws),
            "",
            "## Output",
            "",
            f"Write **`{result}`** with exactly one JSON object per line, one line per message below:",
            "",
            '```json\n{"id": "m0123456789a", "labels": ["area1"], "confidence": 0.85, "reason": "short why"}\n```',
            "",
            "confidence: 0.0-1.0, how sure you are. "
            "Use several labels only when the message is truly about several areas.",
            "",
            "## Messages",
            "",
        ]
        for rec in chunk:
            try:
                _, body = read_message(ws, rec)
            except FileNotFoundError:
                body = ""
            hint = ""
            if rules["rules"]:
                _, scores, _ = classify(rules, features(rec, body))
                if scores:
                    hint = "rule scores (below threshold): " + ", ".join(f"{a}={s:g}" for a, s in scores.items())
            atts = ", ".join(a["name"] for a in rec.get("attachments") or [] if not a.get("inline"))
            lines += [
                f"### {rec['id']}",
                f"- {rec.get('date', '')[:16]} | {rec.get('source')} {rec.get('kind', '')} | "
                f"{rec.get('folder', '')} | {rec.get('direction', '')}",
                f"- from: {fmt_person(rec.get('from'))}",
                f"- to: {fmt_people(rec.get('to'), 5)}"
                + (f" | cc: {fmt_people(rec.get('cc'), 4)}" if rec.get("cc") else ""),
                f"- subject: {rec.get('subject', '')}",
            ]
            if atts:
                lines.append(f"- attachments: {atts}")
            if hint:
                lines.append(f"- {hint}")
            text, _ = redact(compact(strip_quoted(body), preview))
            lines += ["", "> " + text.replace("\n", "\n> "), ""]
        p = ws.path(WORKDIR, f"{name}.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        state["open"][name] = {
            "ids": [r["id"] for r in chunk],
            "created": now_iso(),
            "file": ws.rel(p),
            "result": result,
        }
        written.append(ws.rel(p))
    ws.save_json(BATCHES, state)
    ws.record_run("label_prepare", batches=len(written), messages=len(pending))
    log(
        f"Wrote {len(written)} batch file(s) for {len(pending)} message(s) in {WORKDIR}/. "
        "Next: run /kb-label in Copilot Chat, then `kbmail label import`."
    )
    return written


def import_results(ws: Workspace, log=print) -> dict:
    labels = load_labels(ws)
    state = ws.load_json(BATCHES, {"next": 1, "open": {}})
    done_dir = ws.path(WORKDIR, "done")
    summary = {"imported": 0, "missing": 0, "errors": 0, "batches": 0}
    for name, b in list(state["open"].items()):
        rp = ws.path(b["result"])
        if not rp.exists():
            continue
        wanted = set(b["ids"])
        got = set()
        for n, line in enumerate(rp.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip().rstrip(",")
            if not line or line.startswith(("```", "//", "#")):
                continue
            try:
                obj = json.loads(line)
                mid = obj["id"]
                raw = obj.get("labels") or obj.get("label")
                raw = raw if isinstance(raw, list) else [raw]
                areas = [ws.normalize_label(x) for x in raw]
                if mid not in wanted or None in areas or not areas:
                    raise ValueError(f"unknown id or label: {mid} {raw}")
            except (ValueError, KeyError, TypeError) as e:
                summary["errors"] += 1
                log(f"  {name} line {n}: {e}")
                continue
            if OTHERS in areas and len(areas) > 1:
                areas = [a for a in areas if a != OTHERS]
            cur = labels.get(mid)
            if cur and cur.get("status") == "confirmed":
                continue  # never overwrite the user's decision
            set_label(labels, mid, areas, "proposed", "copilot", obj.get("confidence"), obj.get("reason", ""))
            got.add(mid)
            summary["imported"] += 1
        missing = wanted - got - {m for m in wanted if labels.get(m, {}).get("status") == "confirmed"}
        summary["missing"] += len(missing)
        if missing:
            log(f"  {name}: {len(missing)} message(s) had no valid result; they return to the queue.")
        done_dir.mkdir(parents=True, exist_ok=True)
        for f in (ws.path(b["file"]), rp):
            if f.exists():
                shutil.move(str(f), done_dir / f.name)
        del state["open"][name]
        summary["batches"] += 1
    save_labels(ws, labels)
    ws.save_json(BATCHES, state)
    ws.record_run("label_import", **summary)
    log(
        f"Imported {summary['imported']} label(s) from {summary['batches']} batch(es); "
        f"{summary['errors']} bad line(s). Next: `kbmail review`."
    )
    return summary


# ---------------------------------------------------------------- interactive review


def _open_file(path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as e:
        print(f"(could not open {path}: {e})")


def review_queue(
    ws: Workspace, source: str = "proposed", area: str | None = None, order: str = "confidence"
) -> list[str]:
    labels = load_labels(ws)
    statuses = {"proposed": {"proposed"}, "auto": {"auto"}, "all": {"proposed", "auto"}}[source]
    ids = [m for m, v in labels.items() if v.get("status") in statuses and (not area or area in v["labels"])]
    if order == "confidence":
        ids.sort(key=lambda m: (labels[m].get("confidence") is None, labels[m].get("confidence") or 0))
    return ids


def interactive_review(
    ws: Workspace,
    source: str = "proposed",
    area: str | None = None,
    order: str = "confidence",
    accept_above: float | None = None,
    input_fn=input,
    out=print,
) -> dict:
    labels = load_labels(ws)
    index = message_index(ws)
    ids = [m for m in review_queue(ws, source, area, order) if m in index]
    stats = {"confirmed": 0, "changed": 0, "skipped": 0}
    if accept_above is not None:
        for mid in list(ids):
            c = labels[mid].get("confidence")
            if c is not None and c >= accept_above:
                lab = labels[mid]
                set_label(
                    labels, mid, lab["labels"], "confirmed", "user", c, lab.get("reason", "") + " (bulk-accepted)"
                )
                stats["confirmed"] += 1
                ids.remove(mid)
        save_labels(ws, labels)
        out(f"Bulk-accepted {stats['confirmed']} label(s) with confidence >= {accept_above}.")
    choices = ws.labels_allowed
    menu = "  ".join(f"[{i}] {a}" for i, a in enumerate(choices, 1))
    out(
        f"\n{len(ids)} label(s) to review. Keys: Enter=accept  1-{len(choices)} (e.g. 1,3)=set label(s)  "
        "v=view full  o=open .md file  s=skip  b=back  q=save & quit\n"
    )
    i = 0
    while 0 <= i < len(ids):
        mid = ids[i]
        rec, lab = index.get(mid), labels[mid]
        try:
            _, body = read_message(ws, rec)
        except FileNotFoundError:
            body = ""
        conf = lab.get("confidence")
        out("-" * 78)
        out(
            f"[{i + 1}/{len(ids)}] {mid}  {rec.get('date', '')[:16]}  "
            f"{rec.get('folder', '')}/{rec.get('direction', '')}"
        )
        out(f"From: {fmt_person(rec.get('from'))}")
        out(f"To:   {fmt_people(rec.get('to'), 4)}")
        out(f"Subj: {rec.get('subject', '')}")
        out(compact(strip_quoted(body), 900))
        out(f"\n=> proposed: {', '.join(lab['labels'])}  (conf {conf}, {lab.get('source')}): {lab.get('reason', '')}")
        out(menu)
        ans = input_fn("> ").strip().lower()
        if ans == "q":
            break
        if ans == "b":
            i = max(0, i - 1)
            continue
        if ans == "s":
            stats["skipped"] += 1
            i += 1
            continue
        if ans == "v":
            out(body)
            input_fn("(Enter to continue) ")
            continue
        if ans == "o":
            _open_file(ws.path(rec["path"]))
            continue
        if ans == "":
            set_label(labels, mid, lab["labels"], "confirmed", "user", conf, lab.get("reason", ""))
            stats["confirmed"] += 1
        else:
            try:
                picked = [choices[int(x) - 1] for x in ans.replace(" ", "").split(",") if x]
            except (ValueError, IndexError):
                out("?? not understood")
                continue
            if OTHERS in picked and len(picked) > 1:
                picked = [p for p in picked if p != OTHERS]
            set_label(labels, mid, picked, "confirmed", "user", 1.0, f"changed by user from {lab['labels']}")
            stats["changed"] += 1
        save_labels(ws, labels)
        i += 1
    save_labels(ws, labels)
    ws.record_run("review", **stats)
    out(f"\nReview saved: {stats}")
    return stats


# ---------------------------------------------------------------- CSV round trip (Excel review)

CSV_FILE = "review/labels_review.csv"


def export_csv(ws: Workspace, source: str = "proposed", log=print) -> str:
    labels = load_labels(ws)
    index = message_index(ws)
    p = ws.path(CSV_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig so Excel shows Japanese correctly
        w = csv.writer(f)
        w.writerow(["id", "date", "from", "subject", "proposed", "confidence", "reason", "decision"])
        for mid in review_queue(ws, source):
            rec = index.get(mid)
            if not rec:
                continue
            lab = labels[mid]
            w.writerow(
                [
                    mid,
                    rec.get("date", "")[:16],
                    (rec.get("from") or {}).get("email", ""),
                    rec.get("subject", ""),
                    ";".join(lab["labels"]),
                    lab.get("confidence"),
                    lab.get("reason", ""),
                    "",
                ]
            )
    log(
        f"Wrote {ws.rel(p)}. Fill 'decision' with: ok | area names separated by ';' | blank = undecided. "
        "Then `kbmail review import-csv`."
    )
    return ws.rel(p)


def import_csv(ws: Workspace, log=print) -> dict:
    labels = load_labels(ws)
    stats = {"confirmed": 0, "changed": 0, "invalid": 0}
    with ws.path(CSV_FILE).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mid, dec = row["id"], (row.get("decision") or "").strip()
            if not dec or mid not in labels:
                continue
            if dec.lower() in ("ok", "y", "yes", "✓"):
                lab = labels[mid]
                set_label(labels, mid, lab["labels"], "confirmed", "user", lab.get("confidence"), lab.get("reason", ""))
                stats["confirmed"] += 1
                continue
            areas = [ws.normalize_label(x) for x in dec.replace(",", ";").split(";") if x.strip()]
            if not areas or None in areas:
                stats["invalid"] += 1
                log(f"  {mid}: invalid decision {dec!r}")
                continue
            set_label(labels, mid, areas, "confirmed", "user", 1.0, "set in CSV review")
            stats["changed"] += 1
    save_labels(ws, labels)
    ws.record_run("review", **stats)
    log(f"CSV review imported: {stats}")
    return stats


def label_counts(ws: Workspace) -> dict:
    out: dict = {}
    for v in load_labels(ws).values():
        out.setdefault(v["status"], 0)
        out[v["status"]] += 1
    return out
