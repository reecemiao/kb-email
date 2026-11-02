"""SVG -> Mermaid for simple diagrams, and a readable text digest for everything else.

Three paths, tried in order:

1. **draw.io / diagrams.net exports** carry the original diagram in the root
   ``content`` attribute (often base64 + raw-deflate + URL-encoded). Unpacking
   it gives exact vertices, edges, labels and containers -> exact Mermaid.
2. **Simple box-and-arrow drawings**: shapes (rect, circle, ellipse, polygon,
   closed path) with text inside, joined by connectors (line, polyline, open
   path) whose ends touch shapes. Geometry is resolved with transforms, arrow
   direction comes from markers or small arrowhead shapes at line ends.
3. **Anything else** (charts, tables, drawings, maps, SVGs wrapping raster
   images): classified as complex. The digest lists canvas, texts in reading
   order, shapes, connectors and what they touch, embedded images, and a
   heuristic draft, so Copilot can finish the job *without* vision and without
   reading a huge raw SVG.

``.svgz`` (gzip) input is decompressed transparently. Embedded raster images
(``<image href="data:...">``) are returned so the caller can register them for
description.
"""

from __future__ import annotations

import base64
import gzip
import html
import json
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

SKIP_TAGS = {
    "defs",
    "marker",
    "symbol",
    "clipPath",
    "mask",
    "pattern",
    "style",
    "metadata",
    "title",
    "desc",
    "script",
    "linearGradient",
    "radialGradient",
    "filter",
}
MAX_SHAPES = 120


# ---------------------------------------------------------------- result


@dataclass
class SvgResult:
    kind: str  # drawio | simple | complex | icon | raster-wrapper | invalid
    mermaid: str = ""  # final Mermaid (drawio/simple) or a heuristic draft (complex)
    digest: str = ""  # Markdown digest for Copilot / humans
    reasons: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    nodes: int = 0
    edges: int = 0
    rasters: list[tuple[str, bytes]] = field(default_factory=list)  # (extension, bytes) of embedded images

    @property
    def is_simple(self) -> bool:
        return self.kind in ("drawio", "simple")

    @property
    def summary(self) -> str:
        if self.is_simple:
            src = "draw.io model" if self.kind == "drawio" else "SVG geometry"
            return f"Auto-converted from {src}: {self.nodes} node(s), {self.edges} connection(s)."
        return f"{self.kind}: " + "; ".join(self.reasons)


# ---------------------------------------------------------------- small helpers

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _num(v, default: float = 0.0) -> float:
    m = _NUM.search(str(v)) if v is not None else None
    return float(m.group(0)) if m else default


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _style(el) -> dict[str, str]:
    out = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _attr(el, name: str, st: dict | None = None) -> str | None:
    st = _style(el) if st is None else st
    return st.get(name) or el.get(name)


def _clean_label(text: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</div>|</p>", "\n", text or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _mm_label(text: str) -> str:
    """Quote a label for Mermaid ("..." with <br/> for line breaks)."""
    text = text.replace('"', "#quot;").replace("\n", "<br/>")
    return f'"{text}"'


# ---------------------------------------------------------------- affine transforms

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def _mul(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(s: str | None) -> Matrix:
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in _NUM.findall(args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            t = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m = _mul(m, t)
    return m


# ---------------------------------------------------------------- path parsing (vertices only)


def path_points(d: str) -> tuple[list[tuple[float, float]], bool]:
    """Vertices (segment end points) of an SVG path and whether it closes."""
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM.pattern, d or "")
    pts: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    cmd = ""
    closed = False
    i = 0
    nargs = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                closed = True
                x, y = sx, sy
                continue
        if not cmd:
            break
        n = nargs[cmd.upper()]
        args = toks[i : i + n]
        if len(args) < n or any(re.fullmatch(r"[A-Za-z]", a) for a in args):
            break
        v = [float(a) for a in args]
        i += n
        rel = cmd.islower()
        up = cmd.upper()
        if up == "H":
            x = x + v[0] if rel else v[0]
        elif up == "V":
            y = y + v[0] if rel else v[0]
        else:
            nx, ny = v[-2], v[-1]
            x, y = (x + nx, y + ny) if rel else (nx, ny)
        if up == "M":
            sx, sy = x, y
            cmd = "l" if rel else "L"  # subsequent pairs are implicit linetos
        pts.append((x, y))
    return pts, closed


# ---------------------------------------------------------------- geometry model


@dataclass
class Shape:
    sid: str
    kind: str
    x0: float
    y0: float
    x1: float
    y1: float
    dashed: bool = False
    texts: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def area(self) -> float:
        return max(self.x1 - self.x0, 0) * max(self.y1 - self.y0, 0)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2

    def contains(self, x: float, y: float, margin: float = 0) -> bool:
        return self.x0 - margin <= x <= self.x1 + margin and self.y0 - margin <= y <= self.y1 + margin

    def encloses(self, o: Shape) -> bool:
        return self is not o and self.x0 <= o.x0 and self.y0 <= o.y0 and self.x1 >= o.x1 and self.y1 >= o.y1

    def dist(self, x: float, y: float) -> float:
        dx = max(self.x0 - x, 0, x - self.x1)
        dy = max(self.y0 - y, 0, y - self.y1)
        return math.hypot(dx, dy)

    @property
    def label(self) -> str:
        return "\n".join(t for _, _, t in sorted(self.texts, key=lambda t: (round(t[1]), t[0])))


@dataclass
class Conn:
    cid: str
    pts: list[tuple[float, float]]
    m_start: bool = False
    m_end: bool = False
    dashed: bool = False


@dataclass
class Geometry:
    shapes: list[Shape] = field(default_factory=list)
    texts: list[tuple[float, float, str]] = field(default_factory=list)
    conns: list[Conn] = field(default_factory=list)
    rasters: list[tuple[str, bytes]] = field(default_factory=list)
    raster_refs: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    width: float = 0
    height: float = 0


def _bbox(m: Matrix, pts) -> tuple[float, float, float, float]:
    tp = [_apply(m, x, y) for x, y in pts]
    xs, ys = [p[0] for p in tp], [p[1] for p in tp]
    return min(xs), min(ys), max(xs), max(ys)


def _font_size(el, inherited: float) -> float:
    fs = _attr(el, "font-size")
    return _num(fs, inherited) if fs else inherited


def _data_uri(href: str) -> tuple[str, bytes] | None:
    m = re.match(r"data:image/([\w.+-]+)(;base64)?,(.*)", href or "", re.S)
    if not m:
        return None
    ext = {"jpeg": ".jpg", "svg+xml": ".svg"}.get(m.group(1).lower(), "." + m.group(1).lower())
    try:
        data = base64.b64decode(m.group(3)) if m.group(2) else urllib.parse.unquote_to_bytes(m.group(3))
    except ValueError:
        return None
    return ext, data


def extract_geometry(root) -> Geometry:
    g = Geometry()
    vb = [_num(v) for v in (root.get("viewBox") or "").replace(",", " ").split()]
    g.width = _num(root.get("width"), vb[2] if len(vb) == 4 else 0)
    g.height = _num(root.get("height"), vb[3] if len(vb) == 4 else 0)
    counter = {"s": 0, "c": 0}

    def add_shape(kind, box, el, st):
        counter["s"] += 1
        dashed = bool(_attr(el, "stroke-dasharray", st)) and _attr(el, "stroke-dasharray", st) != "none"
        g.shapes.append(Shape(f"S{counter['s']}", kind, *box, dashed=dashed))

    def add_conn(pts, el, st):
        counter["c"] += 1
        ms = (_attr(el, "marker-start", st) or "none") != "none"
        me = (_attr(el, "marker-end", st) or "none") != "none"
        dashed = bool(_attr(el, "stroke-dasharray", st)) and _attr(el, "stroke-dasharray", st) != "none"
        g.conns.append(Conn(f"C{counter['c']}", pts, ms, me, dashed))

    def text_of(el) -> str:
        spans = [t for t in el if _tag(t) == "tspan"]
        if spans:  # one tspan per line in most exporters
            parts = [el.text or ""] + ["".join(t.itertext()) for t in spans]
            return _clean_label("\n".join(x for x in parts if x.strip()))
        return _clean_label("".join(el.itertext()))

    def walk(el, m: Matrix, fs: float):
        tag = _tag(el)
        if tag in SKIP_TAGS:
            return
        st = _style(el)
        if (_attr(el, "display", st) == "none") or (_attr(el, "visibility", st) == "hidden"):
            return
        m = _mul(m, parse_transform(el.get("transform")))
        fs = _font_size(el, fs)
        g.counts[tag] = g.counts.get(tag, 0) + 1
        if tag == "switch":  # draw.io: <foreignObject> HTML label + <text> fallback; prefer the text
            kids = [k for k in el if _tag(k) == "text"] or list(el)[:1]
            for k in kids:
                walk(k, m, fs)
            return
        if tag == "rect":
            x, y, w, h = (_num(el.get(a)) for a in ("x", "y", "width", "height"))
            if w > 0 and h > 0:
                add_shape(
                    "rect-rounded" if _num(el.get("rx")) > 0 else "rect",
                    _bbox(m, [(x, y), (x + w, y + h), (x + w, y), (x, y + h)]),
                    el,
                    st,
                )
        elif tag in ("circle", "ellipse"):
            cx, cy = _num(el.get("cx")), _num(el.get("cy"))
            rx = _num(el.get("r")) if tag == "circle" else _num(el.get("rx"))
            ry = _num(el.get("r")) if tag == "circle" else _num(el.get("ry"))
            if rx > 0 and ry > 0:
                add_shape(
                    "ellipse",
                    _bbox(m, [(cx - rx, cy - ry), (cx + rx, cy + ry), (cx + rx, cy - ry), (cx - rx, cy + ry)]),
                    el,
                    st,
                )
        elif tag == "polygon":
            v = [float(n) for n in _NUM.findall(el.get("points") or "")]
            pts = list(zip(v[::2], v[1::2], strict=False))
            if len(pts) >= 3:
                add_shape("diamond" if _is_diamond(pts) else "polygon", _bbox(m, pts), el, st)
        elif tag in ("line", "polyline"):
            if tag == "line":
                pts = [(_num(el.get("x1")), _num(el.get("y1"))), (_num(el.get("x2")), _num(el.get("y2")))]
            else:
                v = [float(n) for n in _NUM.findall(el.get("points") or "")]
                pts = list(zip(v[::2], v[1::2], strict=False))
            if len(pts) >= 2:
                add_conn([_apply(m, x, y) for x, y in pts], el, st)
        elif tag == "path":
            pts, closed = path_points(el.get("d") or "")
            if len(pts) >= 2:
                tp = [_apply(m, x, y) for x, y in pts]
                fill = _attr(el, "fill", st)
                has_marker = any((_attr(el, k, st) or "none") != "none" for k in ("marker-start", "marker-end"))
                if (closed or (fill and fill != "none" and len(pts) >= 3)) and not has_marker:
                    xs, ys = [p[0] for p in tp], [p[1] for p in tp]
                    add_shape("path", (min(xs), min(ys), max(xs), max(ys)), el, st)
                else:
                    add_conn(tp, el, st)
        elif tag == "text":
            s = text_of(el)
            if s:
                first = next((t for t in el.iter() if _tag(t) == "tspan" and t.get("x")), el)
                x, y = _num(first.get("x"), _num(el.get("x"))), _num(first.get("y"), _num(el.get("y")))
                anchor = _attr(el, "text-anchor", st) or "start"
                width = sum(fs if ord(ch) > 0x2E80 else fs * 0.55 for ch in s.split("\n")[0])
                cx = x if anchor == "middle" else (x - width / 2 if anchor == "end" else x + width / 2)
                tx, ty = _apply(m, cx, y - fs * 0.35)
                g.texts.append((tx, ty, s))
            return
        elif tag == "foreignObject":
            s = text_of(el)
            if s:
                x, y, w, h = (_num(el.get(a)) for a in ("x", "y", "width", "height"))
                tx, ty = _apply(m, x + w / 2, y + h / 2)
                g.texts.append((tx, ty, s))
            return
        elif tag == "image":
            g.raster_refs += 1
            href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            got = _data_uri(href)
            if got:
                g.rasters.append(got)
            return
        for child in el:
            walk(child, m, fs)

    walk(root, IDENTITY, 12.0)
    return g


def _is_diamond(pts) -> bool:
    if len(pts) != 4:
        return False
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    tol = 0.15 * max(max(xs) - min(xs), max(ys) - min(ys), 1)
    on_axis = sum(1 for x, y in pts if abs(x - cx) < tol or abs(y - cy) < tol)
    return on_axis == 4


# ---------------------------------------------------------------- generic SVG -> Mermaid


def _seg_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _node_shape(kind: str, label: str) -> str:
    q = _mm_label(label or " ")
    return {"ellipse": f"([{q}])", "diamond": f"{{{q}}}", "rect-rounded": f"({q})"}.get(kind, f"[{q}]")


def analyze(g: Geometry) -> tuple[SvgResult, dict]:
    """Heuristic interpretation of extracted geometry. Returns (result, details for the digest)."""
    all_pts = [(s.x0, s.y0) for s in g.shapes] + [(s.x1, s.y1) for s in g.shapes]
    all_pts += [p for c in g.conns for p in c.pts] + [(x, y) for x, y, _ in g.texts]
    if all_pts:
        span = math.hypot(
            max(p[0] for p in all_pts) - min(p[0] for p in all_pts),
            max(p[1] for p in all_pts) - min(p[1] for p in all_pts),
        )
    else:
        span = math.hypot(g.width, g.height)
    tol = max(6.0, min(0.02 * span, 20.0))
    reasons: list[str] = []

    # Full-canvas background rectangles are not nodes.
    big = [s for s in g.shapes if g.width and g.height and s.area >= 0.8 * g.width * g.height]
    shapes = [s for s in g.shapes if s not in big]
    small = [s for s in shapes if max(s.x1 - s.x0, s.y1 - s.y0) <= 2.5 * tol]

    # Texts -> smallest containing shape.
    free_texts = []
    for t in g.texts:
        holders = [s for s in shapes if s.contains(t[0], t[1], 2)]
        if holders:
            min(holders, key=lambda s: s.area).texts.append(t)
        else:
            free_texts.append(t)
    small = [s for s in small if not s.texts]
    candidates = [s for s in shapes if s not in small]
    containers = {s.sid for s in candidates if any(s.encloses(o) for o in candidates)}

    def nearest(p, exclude=None):
        best, bd = None, tol
        for s in candidates:
            if s is exclude:
                continue
            d = s.dist(*p)
            if s.sid in containers and d == 0:
                d = tol * 0.9  # prefer a leaf node over the container that also contains the point
            if d <= bd:
                best, bd = s, d
        return best

    def arrow_at(p) -> bool:
        return any(math.hypot(s.center[0] - p[0], s.center[1] - p[1]) <= 1.5 * tol for s in small)

    edges, unresolved = [], []
    used_texts = set()
    for c in g.conns:
        a, b = c.pts[0], c.pts[-1]
        sa = nearest(a)
        sb = nearest(b, exclude=sa)
        if not sa or not sb or sa is sb:
            unresolved.append(c)
            continue
        fwd, back = c.m_end or arrow_at(b), c.m_start or arrow_at(a)
        label_parts = []
        for t in free_texts:
            if id(t) in used_texts:
                continue
            if min(_seg_dist((t[0], t[1]), p, q) for p, q in zip(c.pts, c.pts[1:], strict=False)) <= 1.5 * tol:
                label_parts.append(t)
                used_texts.add(id(t))
        edges.append((sa, sb, fwd, back, c.dashed, " ".join(t[2] for t in label_parts), c))
    orphans = [t for t in free_texts if id(t) not in used_texts]
    connected = {s.sid for e in edges for s in e[:2]}
    nodes = [s for s in candidates if (s.label or s.sid in connected) and s.sid not in containers]
    node_ids = {s.sid for s in nodes}
    groups = [s for s in candidates if s.sid in containers]
    unlabeled = [s for s in nodes if not s.label]

    # Diagram title: one free text above everything else.
    title = ""
    if orphans and nodes:
        top = min(orphans, key=lambda t: t[1])
        if top[1] < min(s.y0 for s in nodes):
            title = top[2]
            orphans = [t for t in orphans if t is not top]

    # Classification.
    if not g.texts and not g.conns and not g.rasters and len(g.shapes) <= 40:
        kind = "icon"
        reasons.append("no text or connectors (icon/logo/decoration)")
    elif g.raster_refs and len(g.texts) <= 1 and len(candidates) <= 2:
        kind = "raster-wrapper"
        reasons.append(f"wraps {g.raster_refs} raster image(s)")
    else:
        if g.raster_refs:
            reasons.append(f"{g.raster_refs} embedded raster image(s)")
        if len([s for s in nodes if s.label]) < 2:
            reasons.append("fewer than 2 labeled boxes")
        if not edges:
            reasons.append("no connectors between boxes (chart, table, drawing or text layout?)")
        if unresolved:
            reasons.append(f"{len(unresolved)} connector(s) not attached to two boxes")
        if len(orphans) > max(1, len(g.texts) // 10):
            reasons.append(f"{len(orphans)} text(s) outside any box or connector")
        if len(unlabeled) > 1:
            reasons.append(f"{len(unlabeled)} connected boxes without text")
        if len(g.shapes) > MAX_SHAPES:
            reasons.append(f"{len(g.shapes)} shapes (too many for a reliable conversion)")
        kind = "complex" if reasons else "simple"

    # Mermaid (final for simple, draft for complex).
    xs = [s.center[0] for s in nodes] or [0]
    ys = [s.center[1] for s in nodes] or [0]
    direction = "LR" if max(xs) - min(xs) > max(ys) - min(ys) else "TD"
    ids = {s.sid: f"n{i}" for i, s in enumerate(sorted(nodes, key=lambda s: (s.y0, s.x0)), 1)}
    lines = []
    if title:
        lines += ["---", "title: " + json.dumps(title.replace("\n", " "), ensure_ascii=False), "---"]
    lines.append(f"flowchart {direction}")
    grouped = set()
    for gi, grp in enumerate(sorted(groups, key=lambda s: -s.area), 1):
        members = [
            s
            for s in nodes
            if grp.encloses(s)
            and s.sid not in grouped
            and not any(o is not grp and o.encloses(s) and grp.encloses(o) for o in groups)
        ]
        if not members:
            continue
        lines.append(f"  subgraph g{gi}[{_mm_label(grp.label or ' ')}]")
        for s in members:
            lines.append(f"    {ids[s.sid]}{_node_shape(s.kind, s.label)}")
            grouped.add(s.sid)
        lines.append("  end")
    for s in sorted(nodes, key=lambda s: (s.y0, s.x0)):
        if s.sid not in grouped:
            lines.append(f"  {ids[s.sid]}{_node_shape(s.kind, s.label)}")
    for sa, sb, fwd, back, dashed, label, _ in edges:
        if sa.sid not in node_ids or sb.sid not in node_ids:
            continue
        if fwd and back:
            arrow = "<-.->" if dashed else "<-->"
        elif fwd or back:
            arrow = "-.->" if dashed else "-->"
        else:
            arrow = "-.-" if dashed else "---"
        a, b = (sb, sa) if back and not fwd else (sa, sb)
        lab = f"|{_mm_label(label)}|" if label else ""
        lines.append(f"  {ids[a.sid]} {arrow}{lab} {ids[b.sid]}")
    mermaid = "\n".join(lines)
    res = SvgResult(
        kind=kind,
        mermaid=mermaid if nodes else "",
        reasons=reasons,
        texts=[t[2] for t in sorted(g.texts, key=lambda t: (round(t[1] / tol), t[0]))],
        nodes=len(nodes),
        edges=len(edges),
        rasters=g.rasters,
    )
    details = {
        "tol": tol,
        "nodes": nodes,
        "shapes": candidates,
        "groups": groups,
        "edges": edges,
        "unresolved": unresolved,
        "orphans": orphans,
        "small": small,
        "title": title,
    }
    return res, details


# ---------------------------------------------------------------- draw.io


def _drawio_models(root) -> list[tuple[str, ET.Element]]:
    content = root.get("content") or ""
    if "mxGraphModel" not in content and "mxfile" not in content:
        return []
    try:
        top = _safe_fromstring(content)
    except ET.ParseError:
        return []
    if _tag(top) == "mxGraphModel":
        return [("page 1", top)]
    out = []
    for i, d in enumerate([e for e in top if _tag(e) == "diagram"], 1):
        name = d.get("name") or f"page {i}"
        model = next((e for e in d if _tag(e) == "mxGraphModel"), None)
        if model is None and (d.text or "").strip():
            try:
                raw = zlib.decompress(base64.b64decode(d.text.strip()), -15).decode("utf-8")
                model = _safe_fromstring(urllib.parse.unquote(raw))
            except (ValueError, zlib.error, ET.ParseError):
                model = None
        if model is not None:
            out.append((name, model))
    return out


_DRAWIO_SHAPES = [
    ("rhombus", "{%s}"),
    ("ellipse", "([%s])"),
    ("cylinder", "[(%s)]"),
    ("datastore", "[(%s)]"),
    ("hexagon", "{{%s}}"),
    ("parallelogram", "[/%s/]"),
    ("process", "[[%s]]"),
    ("rounded=1", "(%s)"),
]


def drawio_to_mermaid(models: list[tuple[str, ET.Element]]) -> SvgResult:
    name, model = models[0]
    cells: dict[str, dict] = {}
    for el in model.iter():
        tag = _tag(el)
        if tag == "mxCell" and el.get("id"):
            cells.setdefault(el.get("id"), {}).update(el.attrib)
        elif tag in ("UserObject", "object") and el.get("id"):
            inner = next((c for c in el if _tag(c) == "mxCell"), None)
            attrs = dict(inner.attrib) if inner is not None else {}
            attrs.update(id=el.get("id"), value=el.get("label", ""))
            cells[el.get("id")] = attrs
    vertices = {k: v for k, v in cells.items() if v.get("vertex") == "1"}
    edges = {k: v for k, v in cells.items() if v.get("edge") == "1"}
    # Edge labels are child vertices of the edge.
    edge_labels: dict[str, list[str]] = {}
    for k, v in list(vertices.items()):
        if v.get("parent") in edges:
            edge_labels.setdefault(v["parent"], []).append(_clean_label(v.get("value", "")))
            del vertices[k]
    containers = {v.get("parent") for v in vertices.values()} & set(vertices)
    containers |= {k for k, v in vertices.items() if re.search(r"swimlane|container=1|group", v.get("style", ""))}
    ids = {k: f"n{i}" for i, k in enumerate(vertices, 1)}
    reasons: list[str] = []  # blocking problems -> leave to Copilot
    notes: list[str] = []  # informational
    direction = "TD"

    def shape(v) -> str:
        style = v.get("style", "")
        label = _mm_label(_clean_label(v.get("value", "")) or " ")
        for key, fmt in _DRAWIO_SHAPES:
            if key in style:
                return fmt % label
        return f"[{label}]"

    def emit(parent: str, indent: str) -> list[str]:
        out = []
        for k, v in vertices.items():
            if v.get("parent", "1") != parent and not (parent == "" and v.get("parent") not in vertices):
                continue
            if k in containers:
                out.append(f"{indent}subgraph {ids[k]}[{_mm_label(_clean_label(v.get('value', '')) or ' ')}]")
                out += emit(k, indent + "  ")
                out.append(f"{indent}end")
            else:
                out.append(f"{indent}{ids[k]}{shape(v)}")
        return out

    lines = [f"flowchart {direction}", *emit("", "  ")]
    n_edges = 0
    for k, e in edges.items():
        s, t = e.get("source"), e.get("target")
        if s not in ids or t not in ids:
            reasons.append(f"edge {k} is not attached at both ends")
            continue
        style = e.get("style", "")
        dashed = "dashed=1" in style
        start = "startArrow=" in style and "startArrow=none" not in style
        end = "endArrow=none" not in style
        arrow = (
            ("<-.->" if dashed else "<-->")
            if start and end
            else (("-.->" if dashed else "-->") if end or start else ("-.-" if dashed else "---"))
        )
        if start and not end:
            s, t = t, s
        label = " ".join(x for x in [_clean_label(e.get("value", "")), *edge_labels.get(k, [])] if x)
        lab = f"|{_mm_label(label)}|" if label else ""
        lines.append(f"  {ids[s]} {arrow}{lab} {ids[t]}")
        n_edges += 1
    # Pick LR when the diagram is wider than tall.
    geo = {}
    for el in model.iter():
        if _tag(el) == "mxCell" and el.get("id") in vertices:
            g = next((c for c in el if _tag(c) == "mxGeometry"), None)
            if g is not None:
                geo[el.get("id")] = (_num(g.get("x")), _num(g.get("y")))
    pts = list(geo.values())
    if pts and max(p[0] for p in pts) - min(p[0] for p in pts) > max(p[1] for p in pts) - min(p[1] for p in pts):
        lines[0] = "flowchart LR"
    if not vertices:
        reasons.append("draw.io model has no shapes")
    if len(models) > 1:
        notes.append(f"{len(models)} pages; only '{name}' converted")
    texts = [_clean_label(v.get("value", "")) for v in vertices.values() if v.get("value")]
    return SvgResult(
        kind="complex" if reasons else "drawio",
        mermaid="\n".join(lines),
        reasons=reasons + notes,
        texts=texts,
        nodes=len(vertices) - len(containers),
        edges=n_edges,
    )


# ---------------------------------------------------------------- entry points


def _safe_fromstring(text: str | bytes):
    head = text[:4096] if isinstance(text, str) else text[:4096].decode("utf-8", "replace")
    if "<!ENTITY" in head:
        raise ET.ParseError("SVG with entity declarations is not supported")
    return ET.fromstring(text)


def load_svg(data: bytes):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return _safe_fromstring(data)


def convert(data: bytes, name: str = "image.svg") -> SvgResult:
    try:
        root = load_svg(data)
    except (ET.ParseError, OSError, EOFError) as e:
        return SvgResult(
            kind="invalid",
            reasons=[f"cannot parse SVG: {e}"],
            digest=f"# SVG digest: {name}\n\nCould not parse the file: {e}\n",
        )
    models = _drawio_models(root)
    g = extract_geometry(root)
    if models:
        res = drawio_to_mermaid(models)
        res.rasters = g.rasters
        details = None
    else:
        res, details = analyze(g)
    res.digest = digest(name, len(data), g, res, details, models)
    return res


def digest(name: str, size: int, g: Geometry, res: SvgResult, details: dict | None, models) -> str:
    def p(x, y):
        return f"({x:.0f},{y:.0f})"

    lines = [
        f"# SVG digest: {name}",
        "",
        f"- size: {size // 1024 + 1} KB; canvas: {g.width:.0f} x {g.height:.0f}",
        "- elements: " + ", ".join(f"{k} {v}" for k, v in sorted(g.counts.items(), key=lambda kv: -kv[1])[:15]),
        f"- classification: **{res.kind}**" + (f" ({'; '.join(res.reasons)})" if res.reasons else ""),
    ]
    if models:
        lines.append(f"- draw.io model found ({len(models)} page(s)); converted from the model, not from pixels")
    if g.raster_refs:
        lines.append(f"- embedded raster images: {g.raster_refs} ({len(g.rasters)} extracted for description)")
    lines += ["", "## Texts (reading order: top to bottom, left to right)", ""]
    lines += [f"- {t.replace(chr(10), ' / ')}" for t in res.texts[:400]] or ["- (none)"]
    if details:
        lines += ["", "## Shapes (x0,y0)-(x1,y1), with the text inside", ""]
        groups = {s.sid for s in details["groups"]}
        for s in sorted(details["shapes"], key=lambda s: (s.y0, s.x0))[:300]:
            role = "group" if s.sid in groups else s.kind
            text = s.label.replace(chr(10), " / ") or "(no text)"
            lines.append(f"- {s.sid} {role} {p(s.x0, s.y0)}-{p(s.x1, s.y1)}: {text}")
        if not details["shapes"]:
            lines.append("- (none)")
        if details["small"]:
            lines.append(f"- plus {len(details['small'])} small mark(s) (arrowheads, bullets, icons)")
        lines += ["", "## Connectors", ""]
        for sa, sb, fwd, back, dashed, label, c in details["edges"][:200]:
            arrow = "<->" if fwd and back else ("->" if fwd else ("<-" if back else "-"))
            lines.append(
                f"- {c.cid}: {sa.sid} {arrow} {sb.sid}"
                + (f" label '{label}'" if label else "")
                + (" dashed" if dashed else "")
            )
        for c in details["unresolved"][:100]:
            lines.append(
                f"- {c.cid}: unattached line {p(*c.pts[0])} -> {p(*c.pts[-1])}"
                + (" arrow at end" if c.m_end else "")
                + (" arrow at start" if c.m_start else "")
            )
        if not details["edges"] and not details["unresolved"]:
            lines.append("- (none)")
        if details["orphans"]:
            lines += ["", "## Free-standing texts (not in a box, not on a connector)", ""]
            lines += [
                f"- {p(x, y)} {t.replace(chr(10), ' / ')}"
                for x, y, t in sorted(details["orphans"], key=lambda t: (t[1], t[0]))[:200]
            ]
    if res.mermaid:
        heading = "## Mermaid" if res.is_simple else "## Draft Mermaid (heuristic; verify against the lists above)"
        lines += ["", heading, "", "```mermaid", res.mermaid, "```"]
    return "\n".join(lines) + "\n"
