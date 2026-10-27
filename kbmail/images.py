"""Image registry: images found in selected mail/attachments that need a text description.

Copilot can't always see images (vision may be disabled by policy), so every
image is copied to review/images/<id>.<ext> and described either by
  * the user  (copilot.vision: false)  -> edit review/images/descriptions.yaml
                                          or run `kbmail images describe`
  * Copilot   (copilot.vision: true)   -> /kb-images prompt fills the same YAML
SVGs go through kbmail.svg2mermaid first (`process_svgs`):
  * draw.io exports and simple box-and-arrow diagrams -> review/images/<id>.mmd
    automatically (status "mermaid", auto: true)
  * icons/logos (no text, no connectors) -> skipped
  * SVGs wrapping raster images -> the embedded images are extracted and
    described like any other image
  * everything else stays pending for Copilot (/kb-svg), which reads the text
    digest review/images/<id>.svg.md instead of the raw SVG, so it works with
    vision disabled.

Images are de-duplicated by content hash, and tiny ones (logos, signature
icons) are ignored by size.
"""

from __future__ import annotations

from kbmail import svg2mermaid
from kbmail.labels import _open_file
from kbmail.textutil import now_iso, sha256_bytes
from kbmail.workspace import Workspace

REGISTRY = "state/images.json"
DIR = "review/images"
DESC = "review/images/descriptions.yaml"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf"}
SVG_EXT = {".svg", ".svgz"}
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
    img = reg.get(iid, {})
    return img.get("status") in RESOLVED and all(is_resolved(reg, e) for e in img.get("embedded", []))


def digest_path(ws: Workspace, iid: str):
    return ws.path(DIR, iid + ".svg.md")


def process_svgs(ws: Workspace, reg: dict, force: bool = False, log=print) -> dict:
    """Auto-handle pending SVGs; with force, also redo earlier automatic results."""
    stats = {"mermaid": 0, "skipped": 0, "wrapper": 0, "copilot": 0}
    for iid, img in list(reg.items()):
        if img["type"] != "svg" or not (img["status"] == "pending" or (force and img.get("auto"))):
            continue
        if img.get("svg_kind") and not force:
            continue  # already analysed and left for Copilot
        res = svg2mermaid.convert(ws.path(img["file"]).read_bytes(), img["file"].split("/")[-1])
        digest_path(ws, iid).write_text(res.digest, encoding="utf-8")
        embedded = []
        for n, (ext, data) in enumerate(res.rasters, 1):
            for src in img["sources"]:
                eid = register(ws, reg, data, ext, {**src, "origin": f"embedded image {n} in {iid}"})
                if eid and eid not in embedded and eid != iid:
                    embedded.append(eid)
        img.update(svg_kind=res.kind, svg_reasons=res.reasons, embedded=embedded, digest=ws.rel(digest_path(ws, iid)))
        if res.is_simple:
            ws.path(DIR, iid + ".mmd").write_text(res.mermaid + "\n", encoding="utf-8")
            img.update(status="mermaid", description=res.summary, auto=True, resolved=now_iso())
            stats["mermaid"] += 1
        elif res.kind == "icon" or (res.kind == "raster-wrapper" and not embedded):
            img.update(status="skipped", description=res.summary, auto=True, resolved=now_iso())
            stats["skipped"] += 1
        elif res.kind == "raster-wrapper":
            img.update(status="described", description="", auto=True, resolved=now_iso())
            stats["wrapper"] += 1
        else:
            img["status"] = "pending"
            stats["copilot"] += 1
    if any(stats.values()):
        log(
            f"SVGs: {stats['mermaid']} converted to Mermaid, {stats['skipped']} icon(s) skipped, "
            f"{stats['wrapper']} raster wrapper(s) unpacked, {stats['copilot']} left for Copilot (/kb-svg)."
        )
    return stats


def render(ws: Workspace, reg: dict, iid: str) -> str:
    """Text that replaces an image inside converted Markdown."""
    img = reg.get(iid)
    if not img:
        return ""
    st = img["status"]
    if st == "skipped":
        return ""
    if img.get("embedded"):  # SVG wrapping raster images: show what was drawn plus the images
        inner = "".join(render(ws, reg, e) for e in img["embedded"])
        if st == "described" and not img.get("description"):
            return inner
        img = {**img, "embedded": []}
        return render(ws, {**reg, iid: img}, iid) + inner
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
        entry = {
            "id": iid,
            "type": img["type"],
            "file": img["file"].split("/")[-1],
            "context": ctx,
            "description": prev.get("description", ""),
            "skip": bool(prev.get("skip", False)),
        }
        if img.get("digest"):
            entry["digest"] = img["digest"].split("/")[-1]
            entry["why_not_automatic"] = "; ".join(img.get("svg_reasons", []))
        entries.append(entry)
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
        lines += [
            f"{len(svgs)} SVG(s) were too complex for automatic conversion. Copilot handles them without vision "
            "from their text digests (`<id>.svg.md`): run `/kb-svg`.",
            "",
        ]
    for e in entries:
        lines += [f"## {e['id']} ({e['type']})", "", f"Context: {e['context']}", ""]
        if e.get("digest"):
            lines += [f"Digest: [{e['digest']}]({e['digest']}) ({e['why_not_automatic']})", ""]
        lines += [f"![{e['id']}]({e['file']})", ""]
    auto = sorted((k, v) for k, v in reg.items() if v.get("auto") and v["status"] == "mermaid")
    if auto:
        lines += ["## Converted automatically (spot-check if you like)", ""]
        lines += [
            f"- [{k}]({k}.mmd) from [{v['file'].split('/')[-1]}]({v['file'].split('/')[-1]}): "
            f"{v.get('description', '')}"
            for k, v in auto
        ]
        lines += [
            "",
            "If one is wrong, fix its .mmd before the thread is formatted (/kb-format); it is used as-is.",
            "",
        ]
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
