"""SVG -> Mermaid: simple drawings and draw.io convert; everything else gets a digest for Copilot."""

import base64
import gzip
import html
import urllib.parse
import zlib

from kbmail import images
from kbmail.cli import main
from kbmail.svg2mermaid import convert, parse_transform, path_points

quiet = lambda *a, **k: None

FLOW = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300">
<defs><marker id="a"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect width="600" height="300" fill="white"/>
<text x="300" y="20" text-anchor="middle">Order flow</text>
<g transform="translate(20,60)">
  <rect x="0" y="0" width="120" height="50" rx="6"/><text x="60" y="30" text-anchor="middle">Client</text>
  <rect x="220" y="0" width="120" height="50"/>
  <text x="280" y="22" text-anchor="middle"><tspan x="280">API</tspan><tspan x="280" dy="14">Gateway</tspan></text>
  <polygon points="470,0 530,25 470,50 410,25"/><text x="470" y="30" text-anchor="middle">Valid?</text>
  <ellipse cx="470" cy="170" rx="60" ry="25"/><text x="470" y="175" text-anchor="middle">注文DB</text>
  <line x1="120" y1="25" x2="220" y2="25" marker-end="url(#a)"/>
  <text x="170" y="18" text-anchor="middle">HTTPS</text>
  <line x1="340" y1="25" x2="410" y2="25" marker-end="url(#a)"/>
  <path d="M470 50 V145" fill="none" stroke-dasharray="4 2" marker-end="url(#a)"/>
</g></svg>"""

# PowerPoint-style export: boxes are closed paths, the arrowhead is a separate small triangle.
PPT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 400">
<path d="M100 100 L250 100 L250 160 L100 160 Z" fill="#ddd"/><text x="120" y="135">Collect logs</text>
<path d="M450 100 L600 100 L600 160 L450 160 Z" fill="#ddd"/><text x="470" y="135">Analyze</text>
<path d="M250 130 L440 130" fill="none" stroke="#000"/>
<path d="M440 124 L452 130 L440 136 Z" fill="#000"/>
</svg>"""

CHART = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
<line x1="40" y1="260" x2="380" y2="260"/><line x1="40" y1="20" x2="40" y2="260"/>
<rect x="60" y="100" width="40" height="160"/><rect x="140" y="60" width="40" height="200"/>
<text x="80" y="280">Q1</text><text x="160" y="280">Q2</text><text x="10" y="20">Sales (M¥)</text>
</svg>"""

ICON = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><path d="M12 2 L22 22 L2 22 Z"/></svg>"""

DRAWIO_MODEL = """<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="grp" value="AWS" style="swimlane;" vertex="1" parent="1"><mxGeometry x="300" width="300" height="200" as="geometry"/></mxCell>
<mxCell id="a" value="&lt;b&gt;User&lt;/b&gt;" style="ellipse;" vertex="1" parent="1"><mxGeometry y="50" width="80" height="40" as="geometry"/></mxCell>
<mxCell id="b" value="Load&lt;br&gt;balancer" style="rounded=1;" vertex="1" parent="grp"><mxGeometry x="20" y="50" width="80" height="40" as="geometry"/></mxCell>
<UserObject id="c" label="App server"><mxCell style="shape=process;" vertex="1" parent="grp"><mxGeometry x="180" y="50" width="80" height="40" as="geometry"/></mxCell></UserObject>
<mxCell id="d" value="DB" style="shape=cylinder3;" vertex="1" parent="1"><mxGeometry x="700" y="50" width="60" height="60" as="geometry"/></mxCell>
<mxCell id="e1" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e2" value="REST" style="dashed=1;" edge="1" parent="1" source="b" target="c"><mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e3" style="startArrow=classic;" edge="1" parent="1" source="c" target="d"><mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="l3" value="SQL" style="edgeLabel;" vertex="1" connectable="0" parent="e3"><mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel>"""


def drawio_svg(model: str = DRAWIO_MODEL, compressed: bool = True) -> str:
    if compressed:
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = base64.b64encode(co.compress(urllib.parse.quote(model).encode()) + co.flush()).decode()
        mxfile = f'<mxfile><diagram name="Arch" id="x">{packed}</diagram></mxfile>'
    else:
        mxfile = f'<mxfile><diagram name="Arch" id="x">{model}</diagram></mxfile>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" content="{html.escape(mxfile)}" width="800" height="200">'
        '<rect x="0" y="0" width="5" height="5"/></svg>'
    )


def wrapper_svg(size: int = 9000) -> str:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * size).decode()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
        f'<image xlink:href="data:image/png;base64,{png}" width="100" height="100"/></svg>'
    )


def test_simple_flowchart_with_transforms_labels_and_markers():
    r = convert(FLOW.encode())
    assert r.kind == "simple" and (r.nodes, r.edges) == (4, 3)
    m = r.mermaid
    assert 'title: "Order flow"' in m and "flowchart LR" in m
    assert 'n1("Client")' in m and 'n2["API<br/>Gateway"]' in m and 'n3{"Valid?"}' in m and 'n4(["注文DB"])' in m
    assert 'n1 -->|"HTTPS"| n2' in m and "n2 --> n3" in m and "n3 -.-> n4" in m


def test_separate_arrowhead_shapes_give_direction():
    r = convert(PPT.encode())
    assert r.kind == "simple" and 'n1["Collect logs"]' in r.mermaid and "n1 --> n2" in r.mermaid


def test_drawio_model_is_converted_exactly_also_from_svgz():
    for svg in (drawio_svg(compressed=True), drawio_svg(compressed=False)):
        r = convert(gzip.compress(svg.encode()), "arch.svgz")
        assert r.kind == "drawio" and (r.nodes, r.edges) == (4, 3), r.reasons
        m = r.mermaid
        assert 'subgraph n1["AWS"]' in m and 'n3("Load<br/>balancer")' in m and 'n4[["App server"]]' in m
        assert 'n2(["User"])' in m and 'n5[("DB")]' in m
        assert "n2 --> n3" in m and 'n3 -.->|"REST"| n4' in m and 'n4 <-->|"SQL"| n5' in m


def test_drawio_with_dangling_edge_goes_to_copilot():
    model = DRAWIO_MODEL.replace('source="a" target="b"', 'source="a"')
    r = convert(drawio_svg(model).encode())
    assert r.kind == "complex" and "not attached" in r.reasons[0]


def test_chart_is_complex_and_digest_has_the_geometry():
    r = convert(CHART.encode(), "chart.svg")
    assert r.kind == "complex" and any("no connectors" in x for x in r.reasons)
    d = r.digest
    assert "- Sales (M¥)" in d and "rect (140,60)-(180,260)" in d and "unattached line (40,260) -> (380,260)" in d


def test_icon_wrapper_invalid_and_entities():
    assert convert(ICON.encode()).kind == "icon"
    w = convert(wrapper_svg().encode())
    assert w.kind == "raster-wrapper" and w.rasters[0][0] == ".png"
    assert convert(b"<svg><broken").kind == "invalid"
    bomb = b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg/>'
    assert "entity" in convert(bomb).reasons[0]


def test_geometry_helpers():
    assert parse_transform("translate(10,20) scale(2)") == (2, 0, 0, 2, 10, 20)
    pts, closed = path_points("M10 10 h20 v20 l-20 0 z")
    assert pts == [(10, 10), (30, 10), (30, 30), (10, 30)] and closed


def test_registry_processing(ws):
    reg = {}
    src = {"thread": "t1", "message": "m1", "origin": "x", "context": "ctx"}
    ids = {
        name: images.register(ws, reg, data.encode(), ".svg", {**src, "origin": name})
        for name, data in (("flow", FLOW), ("chart", CHART), ("icon", ICON), ("wrap", wrapper_svg()))
    }
    stats = images.process_svgs(ws, reg, log=quiet)
    assert stats == {"mermaid": 1, "skipped": 1, "wrapper": 1, "copilot": 1}
    flow, chart, icon, wrap = (reg[ids[k]] for k in ("flow", "chart", "icon", "wrap"))
    assert flow["status"] == "mermaid" and flow["auto"] and ws.path(images.DIR, ids["flow"] + ".mmd").exists()
    assert icon["status"] == "skipped"
    assert chart["status"] == "pending" and ws.path(chart["digest"]).exists()
    # The wrapper is resolved only once its embedded image is described.
    (inner,) = wrap["embedded"]
    assert reg[inner]["type"] == "image" and not images.is_resolved(reg, ids["wrap"])
    reg[inner].update(status="described", description="Screenshot of the order screen")
    assert images.is_resolved(reg, ids["wrap"])
    assert "order screen" in images.render(ws, reg, ids["wrap"])
    assert "```mermaid" in images.render(ws, reg, ids["flow"])
    # Review files point Copilot at the digest, not the raw SVG.
    images.write_review_files(ws, reg)
    entry = next(e for e in ws.load_yaml(images.DESC) if e["id"] == ids["chart"])
    assert entry["digest"] == ids["chart"] + ".svg.md" and "no connectors" in entry["why_not_automatic"]
    # A second run doesn't redo work.
    assert images.process_svgs(ws, reg, log=quiet) == {"mermaid": 0, "skipped": 0, "wrapper": 0, "copilot": 0}


def test_cli_exit_codes(tmp_path, capsys):
    (tmp_path / "flow.svg").write_text(FLOW, encoding="utf-8")
    (tmp_path / "chart.svg").write_text(CHART, encoding="utf-8")
    assert main(["svg2mermaid", str(tmp_path / "flow.svg")]) == 0
    assert capsys.readouterr().out.startswith("---\ntitle:")
    assert main(["svg2mermaid", str(tmp_path / "chart.svg"), "-o", str(tmp_path / "chart.md")]) == 3
    assert (tmp_path / "chart.md").read_text(encoding="utf-8").startswith("# SVG digest")
