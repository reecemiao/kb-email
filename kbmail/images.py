"""Image registry: images found in selected mail/attachments that need a text description.

Copilot can't always see images (vision may be disabled by policy), so every
image is copied to review/images/<id>.<ext> and described either by
  * the user  (copilot.vision: false)  -> edit review/images/descriptions.yaml
                                          or run `kbmail images describe`
  * Copilot   (copilot.vision: true)   -> /kb-images prompt fills the same YAML
SVGs are text, so Copilot can always read them: /kb-svg converts diagrams to
Mermaid (review/images/<id>.mmd) or writes a description.

Images are de-duplicated by content hash, and tiny ones (logos, signature
icons) are ignored by size.
"""

from __future__ import annotations

from kbmail.labels import _open_file
from kbmail.textutil import now_iso, sha256_bytes
from kbmail.workspace import Workspace

REGISTRY = "state/images.json"
DIR = "review/images"
DESC = "review/images/descriptions.yaml"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf"}
SVG_EXT = {".svg"}
RESOLVED = {"described", "skipped", "mermaid"}


def load(ws: Workspace) -> dict:
    return ws.load_json(REGISTRY, {})


def save(ws: Workspace, reg: dict) -> None:
    ws.save_json(REGISTRY, reg)


def register(ws: Workspace, reg: dict, data: bytes, ext: str, source: dict) -> str | None:
    """Add an image (bytes) to the registry. Returns its id, or None when ignored."""
    ext = ext.lower()
    if ext not in IMAGE_EXT | SVG_EXT:
        return None
    if ext not in SVG_EXT and len(data) < ws.cfg("images.min_bytes", 6000):
        return None
    iid = "img_" + sha256_bytes(data)[:10]
    if iid not in reg:
        p = ws.path(DIR, iid + ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        reg[iid] = {
            "file": ws.rel(p),
            "type": "svg" if ext in SVG_EXT else "image",
            "bytes": len(data),
            "status": "pending",
            "description": "",
            "sources": [],
            "created": now_iso(),
        }
    srcs = reg[iid]["sources"]
    if source not in srcs:
        srcs.append(source)
    return iid


def is_resolved(reg: dict, iid: str) -> bool:
    return reg.get(iid, {}).get("status") in RESOLVED


def render(ws: Workspace, reg: dict, iid: str) -> str:
    """Text that replaces an image inside converted Markdown."""
    img = reg.get(iid)
    if not img:
        return ""
    st = img["status"]
    if st == "skipped":
        return ""
    if st == "mermaid":
        mmd = ws.path(DIR, iid + ".mmd")
        code = mmd.read_text(encoding="utf-8").strip() if mmd.exists() else ""
        desc = f"\n{img['description']}" if img.get("description") else ""
        return f"\n> Diagram ({iid}, converted from SVG):{desc}\n\n```mermaid\n{code}\n```\n"
    if st == "described":
        desc = img["description"].strip().replace("\n", "\n> ")
        return f"\n> **[Image {iid}]** {desc}\n"
    return f"\n> **[Image {iid} - description pending]**\n"


def write_review_files(ws: Workspace, reg: dict, vision: bool | None = None) -> dict:
    """Write descriptions.yaml (pending items only) and REVIEW.md (visual preview)."""
    vision = ws.cfg("copilot.vision", False) if vision is None else vision
    pending = {k: v for k, v in reg.items() if v["status"] == "pending"}
    existing = {e["id"]: e for e in (ws.load_yaml(DESC, []) or []) if isinstance(e, dict) and "id" in e}
    entries = []
    for iid, img in sorted(pending.items()):
        prev = existing.get(iid, {})
        ctx = "; ".join(s.get("context", "") for s in img["sources"][:3])
        entries.append(
            {
                "id": iid,
                "type": img["type"],
                "file": img["file"].split("/")[-1],
                "context": ctx,
                "description": prev.get("description", ""),
                "skip": bool(prev.get("skip", False)),
            }
        )
    ws.save_yaml(DESC, entries)
    who = "Copilot (/kb-images)" if vision else "you"
    lines = [
        "# Images waiting for a description",
        "",
        f"Described by: **{who}**. Fill `description` in `descriptions.yaml` (or set `skip: true`), "
        "then run `kbmail images import`. Interactive alternative: `kbmail images describe`.",
        "",
        "Good descriptions say what the image *means*: chart axes and trend, the architecture's boxes and arrows, "
        "the key numbers in a screenshot, and which system/screen it is.",
        "",
    ]
    svgs = [e for e in entries if e["type"] == "svg"]
    if svgs:
        lines += [f"{len(svgs)} SVG file(s) can be handled by Copilot even without vision: run `/kb-svg`.", ""]
    for e in entries:
        lines += [f"## {e['id']} ({e['type']})", "", f"Context: {e['context']}", "", f"![{e['id']}]({e['file']})", ""]
    ws.path(DIR, "REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    return {"pending": len(entries), "svg": len(svgs)}


def import_descriptions(ws: Workspace, log=print) -> dict:
    reg = load(ws)
    stats = {"described": 0, "skipped": 0, "mermaid": 0}
    for e in ws.load_yaml(DESC, []) or []:
        iid = e.get("id")
        if iid not in reg or reg[iid]["status"] in RESOLVED:
            continue
        mmd = ws.path(DIR, iid + ".mmd")
        if e.get("skip"):
            reg[iid].update(status="skipped", resolved=now_iso())
            stats["skipped"] += 1
        elif mmd.exists():
            reg[iid].update(status="mermaid", description=(e.get("description") or "").strip(), resolved=now_iso())
            stats["mermaid"] += 1
        elif (e.get("description") or "").strip():
            reg[iid].update(status="described", description=e["description"].strip(), resolved=now_iso())
            stats["described"] += 1
    # Mermaid files written without touching the YAML also count.
    for iid, img in reg.items():
        if img["status"] == "pending" and ws.path(DIR, iid + ".mmd").exists():
            img.update(status="mermaid", resolved=now_iso())
            stats["mermaid"] += 1
    save(ws, reg)
    write_review_files(ws, reg)
    ws.record_run("images_import", **stats)
    log(f"Images resolved: {stats}. Pending: {sum(1 for v in reg.values() if v['status'] == 'pending')}.")
    return stats


def interactive_describe(ws: Workspace, input_fn=input, out=print) -> dict:
    reg = load(ws)
    pending = [k for k, v in sorted(reg.items()) if v["status"] == "pending" and v["type"] == "image"]
    out(
        f"{len(pending)} image(s) pending. For each: type a description (end with an empty line), "
        "'s' = skip (irrelevant), 'n' = next/later, 'q' = quit.\n"
    )
    n = 0
    for iid in pending:
        img = reg[iid]
        out("-" * 70)
        out(f"{iid}  {img['file']}  ({img['bytes'] // 1024} KB)")
        out("Context: " + "; ".join(s.get("context", "") for s in img["sources"][:3]))
        _open_file(ws.path(img["file"]))
        first = input_fn("> ").strip()
        if first.lower() == "q":
            break
        if first.lower() == "n":
            continue
        if first.lower() == "s":
            img.update(status="skipped", resolved=now_iso())
        else:
            lines = [first]
            while True:
                more = input_fn("  ")
                if not more.strip():
                    break
                lines.append(more)
            img.update(status="described", description="\n".join(lines).strip(), resolved=now_iso())
            n += 1
        save(ws, reg)
    write_review_files(ws, reg)
    out(f"Described {n} image(s).")
    return {"described": n}


def counts(ws: Workspace) -> dict:
    out: dict = {}
    for v in load(ws).values():
        key = f"{v['type']}:{v['status']}"
        out[key] = out.get(key, 0) + 1
    return out
