"""End-to-end pipeline test: Copilot's part is simulated by writing the result files it would write."""

import json

from kbmail import assist, images, kb, people, questions, rules, wiki
from kbmail.importers import import_eml
from kbmail.labels import import_results, interactive_review, load_labels, prepare_batches
from kbmail.records import message_index
from kbmail.status import collect
from kbmail.textutil import dump_frontmatter, parse_frontmatter

quiet = lambda *a, **k: None  # noqa: E731


def _by_subject(ws):
    return {r["subject"]: r for r in message_index(ws).all()}


def _label_everything(ws):
    prepare_batches(ws, log=quiet)
    state = ws.load_json("state/label_batches.json")
    subj = {r["id"]: r["subject"] for r in message_index(ws).all()}
    for b in state["open"].values():
        lines = []
        for mid in b["ids"]:
            s = subj[mid].lower()
            label = "payments" if "payment" in s or "請求" in s else "infra" if "server" in s else "others"
            lines.append(json.dumps({"id": mid, "labels": [label], "confidence": 0.8, "reason": "test"}))
        ws.path(b["result"]).write_text("\n".join(lines), encoding="utf-8")
    import_results(ws, log=quiet)


def test_full_pipeline(ws, mailbox):
    # 1. export (eml import stands in for Outlook) + delta de-duplication
    assert import_eml(ws, [str(mailbox)], log=quiet) == 5
    assert import_eml(ws, [str(mailbox)], log=quiet) == 0
    msgs = _by_subject(ws)
    assert msgs["RE: Payment API cut-over plan"]["thread_id"] == msgs["Payment API cut-over plan"]["thread_id"]
    assert msgs["RE: Payment API cut-over plan"]["direction"] == "sent"

    # 2. Copilot labeling round trip
    _label_everything(ws)
    labels = load_labels(ws)
    assert {v["status"] for v in labels.values()} == {"proposed"}
    assert labels[msgs["Weekly newsletter"]["id"]]["labels"] == ["others"]

    # 3. interactive review: accept, change one to infra+payments, accept rest
    answers = iter(["", "2,1", "", "", ""])
    interactive_review(ws, input_fn=lambda _: next(answers), out=quiet)
    labels = load_labels(ws)
    assert all(v["status"] == "confirmed" for v in labels.values())
    assert sum(1 for v in labels.values() if v["labels"] == ["infra", "payments"]) == 1

    # 4. rules: Copilot writes rules.yaml, Python evaluates/commits
    ws.save_yaml("config/rules.yaml", {"threshold": 2, "rules": [
        {"id": "r001", "area": "others", "weight": 2, "when": {"from": ["noreply"]}},
        {"id": "r002", "area": "payments", "weight": 2, "when": {"subject": ["re:payment|請求"]}},
        {"id": "r003", "area": "bogus", "weight": 1, "when": {"subject": ["x"]}},
    ]})
    assert rules.commit(ws, log=quiet) is False  # unknown area rejected
    data = ws.load_yaml("config/rules.yaml")
    data["rules"].pop()
    ws.save_yaml("config/rules.yaml", data)
    assert rules.commit(ws, note="first rules", log=quiet) is True
    ev = rules.evaluate(ws)
    assert ev["per_area"]["others"]["precision"] == 1.0
    ctx = ws.path(rules.prepare_context(ws, log=quiet)).read_text(encoding="utf-8")
    assert "Strong signals" in ctx and "Rules journal" in ctx
    assert list(ws.path("state/rules_history").glob("rules_*.yaml"))

    # 5. KB selection: Copilot selects the payments thread and asks a question
    kb.prepare_select(ws, log=quiet)
    st = ws.load_json(kb.SELECT_BATCHES)
    lines = []
    for b in st["open"].values():
        for tid in b["threads"]:
            dec = "select" if any(msgs[s]["thread_id"] == tid for s in ("Payment API cut-over plan",)) else "skip"
            lines.append(json.dumps({"thread": tid, "decision": dec, "title": "Payment API cut-over",
                                     "messages": "all", "attachments": "all",
                                     "questions": ["What is the rollback runbook URL?"] if dec == "select" else []}))
        ws.path(b["result"]).write_text("\n".join(lines), encoding="utf-8")
    stats = kb.import_select(ws, log=quiet)
    assert stats["select"] == 1 and stats["questions"] == 1

    # 6. conversion: html table, docx, inline png, svg, redaction, quote stripping
    cstats = kb.convert(ws, log=quiet)
    assert cstats["converted"] == 1 and cstats["redactions"] >= 1
    tid = msgs["Payment API cut-over plan"]["thread_id"]
    src = ws.path(kb.CONVERT_DIR, tid, "thread.md").read_text(encoding="utf-8")
    assert "hunter2secret" not in src
    assert "idempotency keys" in src and "timeout" in src
    assert "old quoted text" not in src
    assert "description pending" in src
    reg = images.load(ws)
    assert {v["type"] for v in reg.values()} == {"image", "svg"}
    q = kb.write_queue(ws, log=quiet)
    assert q == {"ready": 0, "blocked": 1, "formatted": 0}

    # 7. images: user describes bitmap, Copilot converts SVG to mermaid
    desc = ws.load_yaml(images.DESC)
    for e in desc:
        if e["type"] == "image":
            e["description"] = "Cut-over timeline chart"
        else:
            ws.path(images.DIR, e["id"] + ".mmd").write_text("graph LR\n  Client --> API", encoding="utf-8")
    ws.save_yaml(images.DESC, desc)
    images.import_descriptions(ws, log=quiet)
    assert kb.write_queue(ws, log=quiet)["ready"] == 1
    src = ws.path(kb.CONVERT_DIR, tid, "thread.md").read_text(encoding="utf-8")
    assert "Cut-over timeline chart" in src and "```mermaid" in src

    # 8. questions: user answers, ingest writes raw/notes
    qs = questions.load(ws)
    qs[0]["answer"] = "The runbook is in the ops wiki (no public link)."
    ws.save_yaml(questions.FILE, qs)
    questions.ingest(ws, fetch=False, log=quiet)
    assert list(ws.path("raw/notes").glob("answers-*.md"))

    # 9. /kb-format writes the raw file with source_version
    conv = ws.load_json(kb.CONVERTED)[tid]
    target = ws.path(conv["raw_target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_frontmatter({"title": "Payment API cut-over", "thread": tid,
                                        "source_version": conv["signature"]}, "# Payment API cut-over\n"),
                      encoding="utf-8")
    assert kb.write_queue(ws, log=quiet)["formatted"] == 1

    # 10. wiki delta: queue -> mark -> change -> diff in queue
    counts = wiki.write_queue(ws, log=quiet)
    assert counts["new"] == 2
    wiki.mark(ws, [], all_queued=True, log=quiet)
    assert wiki.write_queue(ws, log=quiet) == {"new": 0, "changed": 0, "deleted": 0}
    target.write_text(target.read_text(encoding="utf-8") + "\nNew fact: freeze starts 2026-11-14.\n", encoding="utf-8")
    assert wiki.write_queue(ws, log=quiet)["changed"] == 1
    assert "+New fact: freeze starts" in ws.path(wiki.QUEUE).read_text(encoding="utf-8")

    # 11. people + triage
    ppl = people.build(ws, log=quiet)
    assert ppl["sarah@contoso.com"]["from_them"] == 1 and ppl["sarah@contoso.com"]["to_them"] == 1
    assert ppl["sarah@contoso.com"]["internal"] is True
    tri = assist.triage(ws, log=quiet)
    assert any("請求" in d["subject"] for d in tri["need_reply"])
    assert not any("newsletter" in d["subject"].lower() for d in tri["need_reply"])

    # status reflects the state
    s = collect(ws)
    assert s["unlabeled"] == 0 and s["wiki"]["queue_changed"] == 1


def test_new_mail_after_rules_is_labeled_by_rules(ws, mailbox, tmp_path):
    import_eml(ws, [str(mailbox)], log=quiet)
    _label_everything(ws)
    ws.save_yaml("config/rules.yaml", {"threshold": 2, "rules": [
        {"id": "r001", "area": "others", "weight": 2, "when": {"from_domain": ["news.example.org"]}}]})
    assert rules.commit(ws, log=quiet)
    from tests.conftest import make_eml
    d = tmp_path / "eml2"
    d.mkdir()
    make_eml(d / "n.eml", subject="Another newsletter", frm="News <noreply@mail.news.example.org>",
             to=["taro@contoso.com"], body="...")
    import_eml(ws, [str(d)], log=quiet)
    rules.apply_rules(ws, log=quiet)
    lab = load_labels(ws)[_by_subject(ws)["Another newsletter"]["id"]]
    assert lab["status"] == "auto" and lab["labels"] == ["others"]
    assert prepare_batches(ws, log=quiet) == []  # nothing left for Copilot


def test_wiki_lint_and_search(ws):
    page = dump_frontmatter({"title": "Payment API", "type": "system", "status": "active", "confidence": 0.9,
                             "decay": "fast", "last_confirmed": "2020-01-01", "updated": "2020-01-01",
                             "sources": ["raw/email/missing.md"],
                             "relations": [{"type": "owned_by", "target": "people/sarah-tanaka"}]},
                            "# Payment API\nIdempotency keys are required. See [[people/sarah-tanaka]] and [[nowhere]].")
    person = dump_frontmatter({"title": "Sarah Tanaka", "type": "person", "status": "active", "confidence": 0.8,
                               "updated": "2026-10-01", "sources": []}, "# Sarah\nOwns payments.")
    ws.path("wiki/systems").mkdir(parents=True)
    ws.path("wiki/people").mkdir(parents=True)
    ws.path("wiki/systems/payment-api.md").write_text(page, encoding="utf-8")
    ws.path("wiki/people/sarah-tanaka.md").write_text(person, encoding="utf-8")
    c = wiki.lint(ws, log=quiet)
    assert c["broken_links"] == 1 and c["missing_sources"] == 1 and c["stale"] == 1
    assert c["orphans"] >= 1  # payment-api has no inbound links and is not in the index
    res = wiki.search(ws, "idempotency")
    assert res[0]["path"] == "wiki/systems/payment-api.md"
    assert any(r["path"] == "wiki/people/sarah-tanaka.md" for r in res)  # via graph expansion
    g = wiki.build_graph(ws)
    assert "owned_by" in wiki.graph_mermaid(g, "systems/payment-api")


def test_frontmatter_roundtrip_japanese():
    meta, body = parse_frontmatter(dump_frontmatter({"subject": "請求システム", "to": [{"name": "佐藤"}]}, "本文"))
    assert meta["subject"] == "請求システム" and body.strip() == "本文"
