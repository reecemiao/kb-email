"""Area discovery: Copilot proposes areas from a mail digest, the user picks them into kbmail.yaml."""

import json

import yaml

from kbmail import areas, labels
from kbmail.cli import main
from kbmail.importers import import_eml
from kbmail.records import message_index
from kbmail.status import collect
from kbmail.workspace import Workspace, init_workspace

quiet = lambda *a, **k: None


def _template_ws(tmp_path, mailbox=None) -> Workspace:
    """A workspace with the template's kbmail.yaml (comments, area1/area2 placeholders), only me.emails set."""
    root = tmp_path / "tpl"
    init_workspace(root)
    cfg = root / "kbmail.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("emails: []", "emails: [taro@contoso.com]", 1), encoding="utf-8"
    )
    ws = Workspace(root)
    if mailbox:
        import_eml(ws, [str(mailbox)], log=quiet)
    return ws


def _tid(ws, subject_part):
    return next(r["thread_id"] for r in message_index(ws).all() if subject_part in r["subject"])


def _write_result(ws, rows):
    ws.path(areas.RESULT).write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_placeholders_block_labeling_and_point_to_discovery(tmp_path, mailbox):
    ws = _template_ws(tmp_path, mailbox)
    assert [a["name"] for a in ws.areas] == ["area1", "area2"] and areas.configured_areas(ws) == []
    assert labels.prepare_batches(ws, log=quiet) == []  # no batches into placeholder areas
    nxt = collect(ws)["next"]
    assert any("kbmail areas discover" in a for a in nxt)
    assert not any("label prepare" in a for a in nxt)


def test_discover_import_pick_round_trip(tmp_path, mailbox):
    ws = _template_ws(tmp_path, mailbox)
    assert areas.discover(ws, log=quiet) == areas.DIGEST
    digest = ws.path(areas.DIGEST).read_text(encoding="utf-8")
    pay, srv, bill = _tid(ws, "Payment API"), _tid(ws, "Server patching"), _tid(ws, "Billing system")
    assert f"### {pay} | Payment API cut-over plan" in digest and "you sent 1" in digest
    assert areas.RESULT in digest and "Already configured" not in digest
    assert "| Sarah Tanaka, ken@vendor.example.com\n" in digest  # one entry per address, the named one
    assert any("/kb-areas" in a for a in collect(ws)["next"])

    _write_result(
        ws,
        [
            {
                "name": "Payments",
                "description": "Payment API and billing",
                "keywords": "payment, billing",
                "threads": [pay, bill, "t-not-in-digest"],
            },
            {"name": "infra", "description": "Servers and patching", "keywords": ["server"], "threads": [srv]},
            {"name": "others", "description": "x"},
            {"name": "infra", "description": "again"},
            {"name": "no-desc", "description": ""},
        ],
    )
    assert collect(ws)["areas"]["result_ready"]
    s = areas.import_proposals(ws, log=quiet)
    assert s == {"imported": 2, "errors": 3, "skipped": 0}
    props = ws.load_json(areas.STATE)["proposals"]
    assert [(p["name"], p["n_threads"], p["n_messages"]) for p in props] == [("payments", 2, 3), ("infra", 1, 1)]
    assert props[0]["keywords"] == ["payment", "billing"]
    assert not collect(ws)["areas"]["result_ready"]
    assert any("kbmail areas pick" in a for a in collect(ws)["next"])

    assert areas.pick(ws, ["2"], out=quiet) == ["infra"]  # placeholders dropped, nothing else configured
    text = ws.path("kbmail.yaml").read_text(encoding="utf-8")
    assert "# The areas you want mail sorted into." in text and "outlook:\n  days: 30" in text  # comments kept
    assert Workspace(ws.root).areas == [
        {"name": "infra", "description": "Servers and patching", "keywords": ["server"]}
    ]
    assert areas.pick(ws, ["payments", "infra"], out=quiet) == ["infra", "payments"]  # added, no duplicate
    assert areas.pick(ws, ["1"], replace=True, out=quiet) == ["payments"]
    assert labels.prepare_batches(ws, log=quiet)  # labeling works now


def test_rediscovery_skips_covered_threads_and_configured_names(tmp_path, mailbox):
    ws = _template_ws(tmp_path, mailbox)
    ws.path("kbmail.yaml").write_text(
        areas.replace_areas_block(
            ws.path("kbmail.yaml").read_text(encoding="utf-8"),
            [{"name": "payments", "description": "Payments", "keywords": ["payment"]}],
        ),
        encoding="utf-8",
    )
    ws = Workspace(ws.root)
    lab = labels.load_labels(ws)
    pay = _tid(ws, "Payment API")
    for r in message_index(ws).all():
        if r["thread_id"] == pay:
            labels.set_label(lab, r["id"], ["payments"], "confirmed", "user")
    labels.save_labels(ws, lab)
    areas.discover(ws, log=quiet)
    digest = ws.path(areas.DIGEST).read_text(encoding="utf-8")
    assert pay not in digest and "Already configured" in digest and "`payments` - Payments" in digest
    _write_result(ws, [{"name": "payments", "description": "dup"}, {"name": "ops", "description": "Ops"}])
    assert areas.import_proposals(ws, log=quiet) == {"imported": 1, "errors": 0, "skipped": 1}
    assert areas.pick(ws, all_=True, out=quiet) == ["payments", "ops"]


def test_pick_unknown_or_cancelled_changes_nothing(tmp_path, mailbox):
    ws = _template_ws(tmp_path, mailbox)
    areas.discover(ws, log=quiet)
    _write_result(ws, [{"name": "ops", "description": "Ops"}])
    areas.import_proposals(ws, log=quiet)
    before = ws.path("kbmail.yaml").read_text(encoding="utf-8")
    assert areas.pick(ws, ["7"], out=quiet) is None
    assert areas.pick(ws, [], out=quiet) is None  # no tty, no selection
    assert areas.pick(ws, [], input_fn=lambda _: "", out=quiet) is None
    assert ws.path("kbmail.yaml").read_text(encoding="utf-8") == before
    assert areas.pick(ws, [], input_fn=lambda _: "1", out=quiet) == ["ops"]


def test_replace_areas_block_keeps_the_rest():
    text = (
        "me:\n  emails: [a@b.c]\n\n# areas comment\nareas:\n- name: old\n  # inner\n- old2\n"
        "\n# outlook comment\noutlook:\n  days: 5\n"
    )
    new = areas.replace_areas_block(text, [{"name": "Café ops", "description": 'say "hi": x', "keywords": ["a:b"]}])
    assert new.startswith("me:\n  emails: [a@b.c]\n\n# areas comment\nareas:\n")
    assert new.endswith("\n# outlook comment\noutlook:\n  days: 5\n") and "old" not in new
    cfg = yaml.safe_load(new)
    assert cfg["areas"] == [{"name": "Café ops", "description": 'say "hi": x', "keywords": ["a:b"]}]
    assert cfg["outlook"] == {"days": 5}
    flow = areas.replace_areas_block("areas: []\nme: {}\n", [{"name": "x", "description": "", "keywords": []}])
    assert yaml.safe_load(flow) == {"areas": [{"name": "x", "description": "", "keywords": []}], "me": {}}
    assert yaml.safe_load(areas.replace_areas_block("me: {}\n", [{"name": "x"}]))["areas"][0]["name"] == "x"


def test_cli_pick_accepts_names_after_options(tmp_path, mailbox):
    ws = _template_ws(tmp_path, mailbox)
    assert main(["-w", str(ws.root), "areas", "discover"]) == 0
    _write_result(ws, [{"name": "ops", "description": "Ops"}, {"name": "hr", "description": "HR"}])
    assert main(["-w", str(ws.root), "areas", "import"]) == 0
    assert main(["-w", str(ws.root), "areas", "list"]) == 0
    assert main(["-w", str(ws.root), "areas", "pick", "--replace", "2"]) == 0
    assert Workspace(ws.root).area_names == ["ops"]  # sorted by threads (both 0), then name: [1] hr, [2] ops
