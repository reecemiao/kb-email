"""LLM Wiki v2 support: delta ingest queue, lint (with confidence decay), hybrid search, graph.

The wiki itself (wiki/**.md) is written by Copilot following wiki/SCHEMA.md.
Python does the bookkeeping that LLMs are bad at:

* state/wiki_manifest.json  - sha256 of every raw/ file at the time it was ingested
* state/wiki_snapshots/     - copy of each ingested raw file, used to send Copilot
                              only a diff when a raw file changes
* work/wiki/QUEUE.md        - new / changed (with diff) / deleted raw files
* work/wiki/LINT.md         - structural problems + stale (decayed) pages
* state/wiki_graph.json     - typed graph built from front-matter relations + wikilinks
"""

from __future__ import annotations

import difflib
import json
import math
import re
from datetime import datetime
from pathlib import Path

from kbmail.convert import file_to_markdown
from kbmail.redact import redact
from kbmail.textutil import BM25, dump_frontmatter, now_iso, parse_dt, parse_frontmatter, sha256_bytes, short_hash, slugify
from kbmail.workspace import Workspace

MANIFEST = "state/wiki_manifest.json"
SNAPDIR = "state/wiki_snapshots"
QUEUE = "work/wiki/QUEUE.md"
LINT = "work/wiki/LINT.md"
GRAPH = "state/wiki_graph.json"
AUDIT = "state/wiki_audit.jsonl"
RAW_TEXT = {".md", ".txt"}
SPECIAL_PAGES = {"index.md", "log.md", "SCHEMA.md"}
REQUIRED = ("title", "type", "status", "confidence", "sources", "updated")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


# ---------------------------------------------------------------- raw drop-zone import

def raw_import(ws: Workspace, log=print) -> int:
    """Convert anything dropped in raw/inbox/ (pdf, docx, pptx, xlsx, html, ...) into raw/docs/*.md."""
    inbox = ws.path("raw/inbox")
    n = 0
    for f in sorted(inbox.rglob("*")) if inbox.exists() else []:
        if not f.is_file() or f.name.startswith("."):
            continue
        out = ws.path("raw/docs", slugify(f.stem, 80) + ".md")
        if f.suffix.lower() in RAW_TEXT:
            text, err = f.read_text(encoding="utf-8", errors="replace"), None
        else:
            text, err = file_to_markdown(f)
        if err or not text.strip():
            log(f"  {f.name}: conversion failed ({err or 'empty'}) - left in raw/inbox")
            continue
        if ws.cfg("privacy.redact", True):
            text, _ = redact(text, ws.cfg("privacy.extra_patterns", []))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump_frontmatter({"source_file": f.name, "imported": now_iso()}, text), encoding="utf-8")
        done = ws.path("raw/inbox/_imported")
        done.mkdir(exist_ok=True)
        f.replace(done / f.name)
        n += 1
        log(f"  {f.name} -> {ws.rel(out)}")
    log(f"Imported {n} file(s) from raw/inbox.")
    return n


# ---------------------------------------------------------------- delta queue

def _raw_files(ws: Workspace) -> dict[str, Path]:
    out = {}
    raw = ws.path("raw")
    for f in sorted(raw.rglob("*")) if raw.exists() else []:
        rel = ws.rel(f)
        if f.is_file() and f.suffix.lower() in RAW_TEXT and not rel.startswith("raw/inbox/"):
            out[rel] = f
    return out


def _snap(ws: Workspace, rel: str) -> Path:
    return ws.path(SNAPDIR, short_hash(rel, 16) + ".md")


def compute_queue(ws: Workspace) -> dict[str, list[str]]:
    man = ws.load_json(MANIFEST, {})
    files = _raw_files(ws)
    new, changed = [], []
    for rel, f in files.items():
        h = sha256_bytes(f.read_bytes())
        if rel not in man:
            new.append(rel)
        elif man[rel]["sha256"] != h:
            changed.append(rel)
    deleted = [rel for rel in man if rel not in files]
    return {"new": new, "changed": changed, "deleted": deleted}


def _citing_pages(ws: Workspace, rel: str) -> list[str]:
    out = []
    for p in ws.path("wiki").rglob("*.md"):
        if rel in p.read_text(encoding="utf-8", errors="replace"):
            out.append(ws.rel(p))
    return out


def write_queue(ws: Workspace, max_diff_lines: int = 250, log=print) -> dict:
    q = compute_queue(ws)
    lines = [f"# Wiki ingest queue ({now_iso()})", "",
             "Process with `/kb-wiki-ingest`. After each file is fully integrated, run "
             "`kbmail wiki mark <path>` (or `kbmail wiki mark --all` at the end).", "",
             f"## New raw files ({len(q['new'])})", ""]
    for rel in q["new"]:
        size = ws.path(rel).stat().st_size
        lines.append(f"- `{rel}` ({size // 1024 + 1} KB)")
    lines += ["", f"## Changed raw files ({len(q['changed'])}) - integrate only the diff", ""]
    for rel in q["changed"]:
        snap = _snap(ws, rel)
        old = snap.read_text(encoding="utf-8").splitlines() if snap.exists() else []
        new = ws.path(rel).read_text(encoding="utf-8").splitlines()
        diff = list(difflib.unified_diff(old, new, "ingested", "current", lineterm="", n=1))
        lines += [f"### `{rel}`", f"Pages citing it: {', '.join(_citing_pages(ws, rel)) or 'none'}", "", "```diff",
                  *diff[:max_diff_lines], *(["... (diff truncated; read the file)"] if len(diff) > max_diff_lines else []),
                  "```", ""]
    lines += ["", f"## Deleted raw files ({len(q['deleted'])}) - mark dependent claims stale or remove the source", ""]
    for rel in q["deleted"]:
        lines.append(f"- `{rel}` cited by: {', '.join(_citing_pages(ws, rel)) or 'none'}")
    p = ws.path(QUEUE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    counts = {k: len(v) for k, v in q.items()}
    log(f"Wiki queue: {counts} -> {QUEUE}")
    return counts


def mark(ws: Workspace, paths: list[str], all_queued: bool = False, log=print) -> int:
    man = ws.load_json(MANIFEST, {})
    q = compute_queue(ws)
    if all_queued:
        paths = q["new"] + q["changed"] + q["deleted"]
    n = 0
    audit = ws.path(AUDIT)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as log_f:
        for rel in paths:
            rel = rel.replace("\\", "/").lstrip("./")
            f = ws.path(rel)
            if not f.exists():
                if rel in man:
                    del man[rel]
                    _snap(ws, rel).unlink(missing_ok=True)
                    log_f.write(json.dumps({"at": now_iso(), "op": "forget", "raw": rel}) + "\n")
                    n += 1
                continue
            data = f.read_bytes()
            op = "update" if rel in man else "ingest"
            man[rel] = {"sha256": sha256_bytes(data), "ingested_at": now_iso()}
            snap = _snap(ws, rel)
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_bytes(data)
            log_f.write(json.dumps({"at": now_iso(), "op": op, "raw": rel}) + "\n")
            n += 1
    ws.save_json(MANIFEST, man)
    ws.record_run("wiki_mark", files=n)
    log(f"Marked {n} raw file(s) as ingested.")
    return n


# ---------------------------------------------------------------- pages / graph

def load_pages(ws: Workspace) -> dict[str, dict]:
    """{'people/sarah-tanaka': {'meta':..., 'body':..., 'path':...}} keyed by path under wiki/ without .md."""
    out = {}
    root = ws.path("wiki")
    for p in sorted(root.rglob("*.md")) if root.exists() else []:
        key = p.relative_to(root).with_suffix("").as_posix()
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        out[key] = {"meta": meta, "body": body, "path": ws.rel(p)}
    return out


def _resolver(pages: dict[str, dict]):
    by_key = {k.lower(): k for k in pages}
    by_stem: dict[str, str] = {}
    for k, pg in pages.items():
        by_stem.setdefault(k.split("/")[-1].lower(), k)
        for alias in [pg["meta"].get("title"), *(pg["meta"].get("aliases") or [])]:
            if alias:
                by_stem.setdefault(str(alias).lower(), k)

    def resolve(target: str) -> str | None:
        t = target.strip().removesuffix(".md").lstrip("/").removeprefix("wiki/").lower()
        return by_key.get(t) or by_stem.get(t) or by_stem.get(t.split("/")[-1])
    return resolve


def build_graph(ws: Workspace, pages: dict | None = None) -> dict:
    pages = pages or load_pages(ws)
    resolve = _resolver(pages)
    edges = []
    for k, pg in pages.items():
        for target in LINK_RE.findall(pg["body"]):
            t = resolve(target)
            if t and t != k:
                edges.append({"from": k, "to": t, "type": "links_to"})
        for rel in pg["meta"].get("relations") or []:
            if not isinstance(rel, dict) or not rel.get("target"):
                continue
            t = resolve(str(rel["target"]).strip("[]"))
            edges.append({"from": k, "to": t or str(rel["target"]), "type": rel.get("type", "related_to"),
                          "confidence": rel.get("confidence"), "resolved": bool(t)})
    nodes = {k: {"title": pg["meta"].get("title", k), "type": pg["meta"].get("type", "")} for k, pg in pages.items()}
    graph = {"built": now_iso(), "nodes": nodes, "edges": edges}
    ws.save_json(GRAPH, graph)
    return graph


def neighbors(graph: dict, node: str, depth: int = 1) -> dict[str, int]:
    adj: dict[str, set] = {}
    for e in graph["edges"]:
        adj.setdefault(e["from"], set()).add(e["to"])
        adj.setdefault(e["to"], set()).add(e["from"])
    seen = {node: 0}
    frontier = [node]
    for d in range(1, depth + 1):
        nxt = []
        for n in frontier:
            for m in adj.get(n, ()):
                if m not in seen:
                    seen[m] = d
                    nxt.append(m)
        frontier = nxt
    return seen


def graph_mermaid(graph: dict, center: str, depth: int = 1) -> str:
    keep = neighbors(graph, center, depth)
    ids = {k: f"n{i}" for i, k in enumerate(keep)}
    lines = ["graph LR"]
    for k, nid in ids.items():
        title = graph["nodes"].get(k, {}).get("title", k).replace('"', "'")
        lines.append(f'  {nid}["{title}"]')
    for e in graph["edges"]:
        if e["from"] in ids and e["to"] in ids and e["type"] != "links_to":
            lines.append(f"  {ids[e['from']]} -- {e['type']} --> {ids[e['to']]}")
    for e in graph["edges"]:
        if e["from"] in ids and e["to"] in ids and e["type"] == "links_to":
            lines.append(f"  {ids[e['from']]} -.-> {ids[e['to']]}")
    return "\n".join(dict.fromkeys(lines))


# ---------------------------------------------------------------- lifecycle

def effective_confidence(ws: Workspace, meta: dict, today: datetime | None = None) -> float | None:
    try:
        conf = float(meta.get("confidence"))
    except (TypeError, ValueError):
        return None
    when = parse_dt(str(meta.get("last_confirmed") or meta.get("updated") or ""))
    if not when:
        return conf
    today = today or datetime.now().astimezone()
    half = ws.cfg("wiki.half_life_days", {}).get(str(meta.get("decay", "normal")), 180)
    age = max((today - when).days, 0)
    return round(conf * math.pow(0.5, age / half), 3)


def lint(ws: Workspace, log=print) -> dict:
    pages = load_pages(ws)
    resolve = _resolver(pages)
    graph = build_graph(ws, pages)
    inbound: dict[str, int] = {k: 0 for k in pages}
    for e in graph["edges"]:
        if e["to"] in inbound and e["from"] != e["to"]:
            inbound[e["to"]] += 1
    index_text = ws.path("wiki/index.md").read_text(encoding="utf-8") if ws.path("wiki/index.md").exists() else ""
    stale_below = ws.cfg("wiki.stale_below", 0.35)
    issues: dict[str, list[str]] = {k: [] for k in ("frontmatter", "broken_links", "missing_sources", "orphans",
                                                    "not_in_index", "stale", "supersession")}
    for k, pg in pages.items():
        name = k.split("/")[-1] + ".md"
        if name in SPECIAL_PAGES or k.startswith("_"):
            continue
        meta = pg["meta"]
        missing = [f for f in REQUIRED if f not in meta]
        if missing:
            issues["frontmatter"].append(f"{pg['path']}: missing {', '.join(missing)}")
        for target in LINK_RE.findall(pg["body"]):
            if not resolve(target):
                issues["broken_links"].append(f"{pg['path']}: [[{target}]]")
        for e in graph["edges"]:
            if e["from"] == k and e["type"] != "links_to" and not e.get("resolved", True):
                issues["broken_links"].append(f"{pg['path']}: relation {e['type']} -> {e['to']}")
        for src in meta.get("sources") or []:
            s = str(src).split("#")[0].strip()
            if s.startswith("raw/") and not ws.path(s).exists():
                issues["missing_sources"].append(f"{pg['path']}: {s}")
        if inbound.get(k, 0) == 0 and k not in index_text:
            issues["orphans"].append(pg["path"])
        elif k not in index_text:
            issues["not_in_index"].append(pg["path"])
        eff = effective_confidence(ws, meta)
        if meta.get("status", "active") == "active" and eff is not None and eff < stale_below:
            issues["stale"].append(f"{pg['path']}: effective confidence {eff} (stored {meta.get('confidence')}, "
                                   f"last_confirmed {meta.get('last_confirmed') or meta.get('updated')})")
        if meta.get("status") == "superseded" and not meta.get("superseded_by"):
            issues["supersession"].append(f"{pg['path']}: superseded but no superseded_by")
    lines = [f"# Wiki lint ({now_iso()})", "", f"{len(pages)} page(s). Fix with `/kb-wiki-lint`.", ""]
    for kind, items in issues.items():
        lines += [f"## {kind} ({len(items)})", "", *[f"- {i}" for i in items[:300]], ""]
    p = ws.path(LINT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    counts = {k: len(v) for k, v in issues.items()}
    ws.record_run("wiki_lint", **counts)
    log(f"Lint: {counts} -> {LINT}")
    return counts


# ---------------------------------------------------------------- search

def search(ws: Workspace, query: str, limit: int = 10, include_raw: bool = False, expand: bool = True) -> list[dict]:
    """Hybrid search: BM25 over pages + graph expansion, fused with reciprocal rank fusion."""
    pages = load_pages(ws)
    docs = {}
    for k, pg in pages.items():
        if k.split("/")[-1] + ".md" in SPECIAL_PAGES:
            continue
        m = pg["meta"]
        title = " ".join([str(m.get("title", "")), *map(str, m.get("aliases") or [])])
        docs[k] = f"{title} {title} {title} {pg['body']}"
    if include_raw:
        for rel, f in _raw_files(ws).items():
            docs["@" + rel] = f.read_text(encoding="utf-8", errors="replace")
    bm = BM25(docs).search(query, limit=50)
    rankings = [[k for k, _ in bm]]
    if expand and bm:
        graph = build_graph(ws, pages)
        gscore: dict[str, float] = {}
        for k, s in bm[:5]:
            for n, d in neighbors(graph, k, 1).items():
                if d > 0:
                    gscore[n] = gscore.get(n, 0) + s / (d + 1)
        rankings.append([k for k, _ in sorted(gscore.items(), key=lambda kv: -kv[1])])
    fused: dict[str, float] = {}
    for weight, r in zip((1.0, 0.5), rankings):  # graph neighbours count half as much as direct hits
        for rank, k in enumerate(r):
            fused[k] = fused.get(k, 0) + weight / (60 + rank)
    results = []
    for k, s in fused.items():
        if k.startswith("@"):
            results.append({"key": k, "path": k[1:], "title": k[1:], "score": s, "kind": "raw"})
            continue
        pg = pages.get(k)
        if not pg:
            continue
        eff = effective_confidence(ws, pg["meta"])
        adj = s * (0.7 + 0.3 * eff) if eff is not None else s
        if pg["meta"].get("status") == "superseded":
            adj *= 0.5
        results.append({"key": k, "path": pg["path"], "title": pg["meta"].get("title", k), "score": adj,
                        "kind": pg["meta"].get("type", ""), "confidence": eff})
    results.sort(key=lambda r: -r["score"])
    return results[:limit]


def status(ws: Workspace) -> dict:
    q = compute_queue(ws)
    return {"pages": len(load_pages(ws)), "queue_new": len(q["new"]), "queue_changed": len(q["changed"]),
            "queue_deleted": len(q["deleted"]), "raw_ingested": len(ws.load_json(MANIFEST, {}))}
