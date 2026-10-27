"""Open questions for the user (step 8): unknown terms, systems, missing links.

review/questions.yaml is a plain list the user can edit in VS Code:

- id: q0007
  question: What is "ZPLAN"? Is there a documentation page?
  context: thread t1a2b..: Capacity plan review
  step: kb-select
  status: open            # open | answered | ingested | dismissed
  answer: ""              # free text; URLs found here are fetched into raw/web/

`kbmail questions ingest` turns answers into raw/notes/answers-<date>.md and
fetches linked pages (markitdown) into raw/web/, so the wiki picks them up.
"""

from __future__ import annotations

import re
from datetime import datetime

from kbmail.convert import url_to_markdown
from kbmail.redact import redact
from kbmail.textutil import dump_frontmatter, now_iso, slugify
from kbmail.workspace import Workspace

FILE = "review/questions.yaml"
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def load(ws: Workspace) -> list[dict]:
    return ws.load_yaml(FILE, []) or []


def save(ws: Workspace, qs: list[dict]) -> None:
    ws.save_yaml(FILE, qs)


def add_question(ws: Workspace, question: str, context: str = "", step: str = "manual") -> str:
    qs = load(ws)
    norm = question.strip().lower()
    for q in qs:
        if q.get("question", "").strip().lower() == norm and q.get("status") in ("open", "answered"):
            return q["id"]
    nxt = max([int(q["id"][1:]) for q in qs if str(q.get("id", "")).startswith("q")] or [0]) + 1
    qid = f"q{nxt:04d}"
    qs.append({"id": qid, "question": question.strip(), "context": context, "step": step,
               "created": now_iso(), "status": "open", "answer": ""})
    save(ws, qs)
    return qid


def interactive_answer(ws: Workspace, input_fn=input, out=print) -> int:
    qs = load(ws)
    n = 0
    open_qs = [q for q in qs if q.get("status") == "open"]
    out(f"{len(open_qs)} open question(s). Type an answer (URLs welcome), Enter = later, 'd' = dismiss, 'q' = quit.")
    for q in open_qs:
        out("-" * 70)
        out(f"{q['id']}: {q['question']}\n  ({q.get('context', '')})")
        a = input_fn("> ").strip()
        if a == "q":
            break
        if not a:
            continue
        if a == "d":
            q["status"] = "dismissed"
        else:
            q.update(answer=a, status="answered", answered=now_iso())
            n += 1
        save(ws, qs)
    return n


def ingest(ws: Workspace, fetch: bool = True, log=print) -> dict:
    qs = load(ws)
    todo = [q for q in qs if q.get("status") in ("open", "answered") and str(q.get("answer") or "").strip()]
    if not todo:
        log("No answered questions to ingest.")
        return {"answers": 0, "pages": 0}
    day = datetime.now().strftime("%Y-%m-%d")
    pages = 0
    body = [f"# Answers from the user ({day})", "",
            "Authoritative context provided by the mailbox owner in answer to open questions.", ""]
    for q in todo:
        body += [f"## {q['question']}", "", f"- question id: {q['id']}", f"- context: {q.get('context', '')}", "",
                 str(q["answer"]).strip(), ""]
        if fetch:
            for url in URL_RE.findall(str(q["answer"])):
                text, err = url_to_markdown(url)
                if err or not text.strip():
                    log(f"  could not fetch {url}: {err or 'empty page'} - save the page (PDF/HTML) into raw/inbox/ "
                        "and run `kbmail raw import`.")
                    body.append(f"- link (not fetched, needs manual save): {url}")
                    continue
                text, _ = redact(text, ws.cfg("privacy.extra_patterns", []))
                p = ws.path("raw/web", f"{slugify(url.split('://', 1)[1], 80)}.md")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(dump_frontmatter({"source_url": url, "fetched": now_iso(), "question": q["id"]}, text),
                             encoding="utf-8")
                body.append(f"- linked page saved: [{url}]({'../web/' + p.name})")
                pages += 1
        body.append("")
        q.update(status="ingested", ingested=now_iso())
    p = ws.path("raw/notes", f"answers-{day}.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        prev = p.read_text(encoding="utf-8").rstrip() + "\n\n"
        p.write_text(prev + "\n".join(body[3:]), encoding="utf-8")
    else:
        p.write_text("\n".join(body), encoding="utf-8")
    save(ws, qs)
    ws.record_run("questions_ingest", answers=len(todo), pages=pages)
    log(f"Ingested {len(todo)} answer(s) into {ws.rel(p)}; fetched {pages} page(s) into raw/web/.")
    return {"answers": len(todo), "pages": pages}


def counts(ws: Workspace) -> dict:
    out: dict = {}
    for q in load(ws):
        out[q.get("status", "open")] = out.get(q.get("status", "open"), 0) + 1
    return out
