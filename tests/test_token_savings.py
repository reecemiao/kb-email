"""What keeps Copilot's context small: boilerplate cleanup, thread labels, and raw files assembled by kbmail."""

import json
from datetime import datetime, timedelta

from kbmail import kb
from kbmail.importers import import_eml
from kbmail.labels import THREAD, load_labels, prepare_batches, save_labels, set_label
from kbmail.noise import strip_boilerplate
from kbmail.records import message_index
from kbmail.textutil import dump_frontmatter, parse_frontmatter
from tests.conftest import make_eml

quiet = lambda *a, **k: None
CLASSIFICATION = ["(?i)(internal|public|confidential|restricted|highly restricted)"]


# ---------------------------------------------------------------- cleanup


def test_signature_after_sign_off_is_dropped_but_name_kept():
    text = (
        "Hi Taro,\nThe cut-over is on 11-15.\n\nBest regards,\nHanako Sato\nSenior Engineer | Payments\n"
        "Contoso Ltd.\nTel: +81 3 1234 5678\nhttps://contoso.com"
    )
    assert strip_boilerplate(text) == "Hi Taro,\nThe cut-over is on 11-15.\n\nBest regards,\nHanako Sato"
    assert strip_boilerplate("Thanks,\nBob\n--\nBob Smith\nACME Corp") == "Thanks,\nBob"


def test_content_that_only_looks_like_a_sign_off_stays():
    for text in (
        "Thanks!\n\nRollback if error > 2%\nOwner: Ken",  # opening line, not a sign-off
        "Done.\nThanks,\nBob\n\nP.S. the meeting moved to 3pm",
        "Intro\nBest Practices\n- cache keys\n- retries",
        "Plan:\n- step 1\nBest,\nBob\n" + "a long sentence that is clearly content and not a signature " * 3,
    ):
        assert strip_boilerplate(text) == text.strip()


def test_disclaimers_banners_join_blocks_and_classification_lines():
    text = (
        "CAUTION: This email originated from outside of the organization. Do not click links or open "
        "attachments unless you recognize the sender.\n\n"
        "Agenda below.\nInternal\n\n"
        + "_"
        * 80
        + "\nMicrosoft Teams meeting\nJoin on your computer, mobile app or room device\n"
        "[Click here to join the meeting](https://teams.microsoft.com/l/meetup-join/x)\n"
        "Meeting ID: 123 456 789\nPasscode: abc\nDownload Teams | Join on the web\n"
        "Learn More | Meeting options\n" + "_" * 80 + "\n\nAfter the call we decide.\n\n"
        "This email and any attachments are confidential and intended solely for the addressee. If you "
        "have received this email in error please notify the sender."
    )
    assert strip_boilerplate(text, boilerplate_lines=CLASSIFICATION) == "Agenda below.\n\nAfter the call we decide."
    assert strip_boilerplate("Plan A.\n\nSent from my iPhone", ["(?i)^sent from my"]) == "Plan A."


def test_quoted_messages_are_cleaned_on_their_own_and_markers_kept():
    text = (
        "See below.\nThanks, Sarah\n\n[added to the conversation here: Ken <ken@x.com>]\n\n"
        "--- quoted: Ken | Mon | Plan ---\nThe plan is X.\n\nKind regards\nKen\nVendor Inc\n+1 555 1234\n\n"
        "[quoted: Bob | Sun | Plan = message m1, exported separately]"
    )
    assert strip_boilerplate(text) == (
        "See below.\nThanks, Sarah\n\n[added to the conversation here: Ken <ken@x.com>]\n\n"
        "--- quoted: Ken | Mon | Plan ---\nThe plan is X.\n\nKind regards\nKen\n\n"
        "[quoted: Bob | Sun | Plan = message m1, exported separately]"
    )


def _signed_mail(tmp_path):
    d = tmp_path / "eml"
    d.mkdir()
    make_eml(
        d / "a.eml",
        subject="Billing export format",
        frm="Hanako Sato <hanako@contoso.com>",
        to=["taro@contoso.com"],
        body="Hi Taro,\nThe billing export moves to CSV in December.\n\nBest regards,\nHanako\n"
        "Billing Team Lead\nContoso Ltd.\nMobile: +81 90 1234 5678\n\n"
        "This message is confidential and intended only for the named recipient.",
    )
    return d


def test_label_batches_carry_no_boilerplate(ws, tmp_path):
    import_eml(ws, [str(_signed_mail(tmp_path))], log=quiet)
    batch = ws.path(prepare_batches(ws, log=quiet)[0]).read_text(encoding="utf-8")
    assert "moves to CSV in December" in batch and "Hanako" in batch
    assert "Mobile:" not in batch and "confidential" not in batch


def test_cleanup_can_be_turned_off(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("KBMAIL_CLEANUP__BOILERPLATE", "false")
    import_eml(ws, [str(_signed_mail(tmp_path))], log=quiet)
    batch = ws.path(prepare_batches(ws, log=quiet)[0]).read_text(encoding="utf-8")
    assert "Mobile:" in batch


# ---------------------------------------------------------------- thread labels


def _by_subject(ws):
    return {r["subject"]: r for r in message_index(ws).all()}


def _confirm(ws, mid, areas):
    labels = load_labels(ws)
    set_label(labels, mid, areas, "confirmed", "user", 1.0, "test")
    save_labels(ws, labels)


def test_replies_take_the_confirmed_labels_of_their_thread(ws, mailbox):
    import_eml(ws, [str(mailbox)], log=quiet)
    msgs = _by_subject(ws)
    root, reply = msgs["Payment API cut-over plan"]["id"], msgs["RE: Payment API cut-over plan"]["id"]
    _confirm(ws, root, ["payments"])
    prepare_batches(ws, log=quiet)
    lab = load_labels(ws)[reply]
    assert lab["labels"] == ["payments"] and lab["status"] == "auto" and lab["source"] == THREAD
    batched = {i for b in ws.load_json("state/label_batches.json")["open"].values() for i in b["ids"]}
    assert reply not in batched and len(batched) == 3  # only the other threads go to Copilot

    _confirm(ws, root, ["payments", "infra"])  # the user corrects the thread: the reply follows
    prepare_batches(ws, log=quiet)
    assert load_labels(ws)[reply]["labels"] == ["payments", "infra"]


def test_disagreeing_thread_or_setting_off_sends_replies_back_to_copilot(ws, tmp_path):
    d = tmp_path / "eml"
    d.mkdir()
    now = datetime.now().astimezone()
    root = make_eml(
        d / "1.eml",
        subject="Plan",
        frm="A <a@contoso.com>",
        to=["taro@contoso.com"],
        body="x",
        date=now - timedelta(days=3),
    )
    for n in (2, 3):
        make_eml(
            d / f"{n}.eml",
            subject="RE: Plan",
            frm="A <a@contoso.com>",
            to=["taro@contoso.com"],
            body=f"reply {n}",
            refs=root,
            date=now - timedelta(days=3 - n + 1),
        )
    import_eml(ws, [str(d)], log=quiet)
    ids = [r["id"] for r in sorted(message_index(ws).all(), key=lambda r: r["date"])]
    _confirm(ws, ids[0], ["payments"])
    prepare_batches(ws, log=quiet)
    assert load_labels(ws)[ids[2]]["source"] == THREAD
    _confirm(ws, ids[1], ["infra"])  # the thread no longer agrees
    prepare_batches(ws, log=quiet)
    assert ids[2] not in load_labels(ws)  # back in the Copilot queue

    _confirm(ws, ids[1], ["payments"])
    prepare_batches(ws, log=quiet)
    assert load_labels(ws)[ids[2]]["source"] == THREAD
    ws.path("kbmail.yaml").write_text(
        ws.path("kbmail.yaml").read_text(encoding="utf-8") + "\nlabeling:\n  inherit_thread: false\n", "utf-8"
    )
    from kbmail.workspace import Workspace

    ws2 = Workspace(ws.root)
    prepare_batches(ws2, log=quiet)
    assert ids[2] not in load_labels(ws2)


def test_rule_labels_teams_and_subject_only_threads_are_left_alone(ws, mailbox):
    import_eml(ws, [str(mailbox)], log=quiet)
    msgs = _by_subject(ws)
    root, reply = msgs["Payment API cut-over plan"], msgs["RE: Payment API cut-over plan"]
    labels = load_labels(ws)
    set_label(labels, reply["id"], ["others"], "auto", "rule", None, "rules: r1")
    save_labels(ws, labels)
    _confirm(ws, root["id"], ["payments"])
    # A Teams chat-day and a mail without a conversation id in the same thread are never inherited.
    idx = message_index(ws)
    for rec, extra in ((msgs["Weekly newsletter"], {"source": "teams"}), (msgs["Billing system migration"], {})):
        idx.upsert({**rec, **extra, "thread_id": root["thread_id"], "conversation_id": "" if not extra else "c"})
    idx.save()
    prepare_batches(ws, log=quiet)
    labels = load_labels(ws)
    assert labels[reply["id"]]["source"] == "rule"
    assert msgs["Weekly newsletter"]["id"] not in labels and msgs["Billing system migration"]["id"] not in labels


# ---------------------------------------------------------------- raw files assembled by kbmail


def _formatted_thread(ws, tmp_path):
    """A selected, converted and assembled two-message thread (no images); returns (eml dir, root id, tid)."""
    d = tmp_path / "eml"
    d.mkdir()
    now = datetime.now().astimezone()
    root = make_eml(
        d / "1.eml",
        subject="Payment retries",
        frm="Sarah <sarah@contoso.com>",
        to=["taro@contoso.com"],
        body="Retries back off 2s, 4s, 8s.",
        date=now - timedelta(days=3),
    )
    make_eml(
        d / "2.eml",
        subject="RE: Payment retries",
        frm="Taro <taro@contoso.com>",
        to=["sarah@contoso.com"],
        body="Agreed, max 3 retries.",
        refs=root,
        date=now - timedelta(days=2),
    )
    import_eml(ws, [str(d)], log=quiet)
    for r in message_index(ws).all():
        _confirm(ws, r["id"], ["payments"])
    kb.prepare_select(ws, log=quiet)
    b = next(iter(ws.load_json(kb.SELECT_BATCHES)["open"].values()))
    tid = b["threads"][0]
    ws.path(b["result"]).write_text(
        json.dumps({"thread": tid, "decision": "select", "title": "Payment retry policy", "topics": ["retries"]}),
        "utf-8",
    )
    kb.import_select(ws, log=quiet)
    kb.convert(ws, log=quiet)
    sp = ws.path(kb.CONVERT_DIR, tid, kb.SUMMARY)
    sp.write_text(
        dump_frontmatter(
            {"topics": ["retries", "backoff"]},
            "## Summary\nExponential backoff.\n\n## Key points\n- 2s, 4s, 8s\n- max 3 retries\n",
        ),
        "utf-8",
    )
    assert kb.assemble(ws, log=quiet)["assembled"] == 1
    return d, root, tid


def test_summary_is_validated(ws, tmp_path):
    _, _, tid = _formatted_thread(ws, tmp_path)
    e = ws.load_json(kb.CONVERTED)[tid]
    raw = ws.path(e["raw_target"])
    meta, body = parse_frontmatter(raw.read_text(encoding="utf-8"))
    assert meta["topics"] == ["retries", "backoff"] and meta["title"] == "Payment retry policy"
    assert "Agreed, max 3 retries." in body

    raw.unlink()  # make the thread unformatted again and try bad summaries
    sp = ws.path(kb.CONVERT_DIR, tid, kb.SUMMARY)
    for bad in ("## Key points\n- x\n", "## Summary\nok\n\n## Key points\n- x\n\n## Conversation\n### copy\n"):
        sp.write_text(bad, "utf-8")
        assert kb.assemble(ws, log=quiet) == {"assembled": 0, "errors": 1, "waiting": 0}
        assert not raw.exists()


def test_new_messages_update_the_summary_kbmail_copied(ws, tmp_path):
    d, root, tid = _formatted_thread(ws, tmp_path)
    make_eml(
        d / "3.eml",
        subject="RE: Payment retries",
        frm="Sarah <sarah@contoso.com>",
        to=["taro@contoso.com"],
        body="Update: max 5 retries for card payments.",
        refs=root,
    )
    before = {r["id"] for r in message_index(ws).all()}
    import_eml(ws, [str(d)], log=quiet)
    (new_id,) = {r["id"] for r in message_index(ws).all()} - before
    _confirm(ws, new_id, ["payments"])
    kb.prepare_select(ws, log=quiet)
    b = next(iter(ws.load_json(kb.SELECT_BATCHES)["open"].values()))
    ws.path(b["result"]).write_text(json.dumps({"thread": tid, "decision": "select", "title": "x"}), "utf-8")
    kb.import_select(ws, log=quiet)
    kb.convert(ws, log=quiet)

    queue = ws.path(kb.CONVERT_DIR, "QUEUE.md").read_text(encoding="utf-8")
    assert f"only the new messages: {new_id}" in queue and "update: `" in queue
    sp = ws.path(kb.CONVERT_DIR, tid, kb.SUMMARY)
    meta, body = parse_frontmatter(sp.read_text(encoding="utf-8"))
    assert meta["title"] == "Payment retry policy" and body.strip().startswith("## Summary\nExponential backoff.")
    assert kb.assemble(ws, log=quiet)["errors"] == 1  # not updated yet

    sp.write_text(sp.read_text(encoding="utf-8").replace("- max 3 retries", "- max 5 retries for cards"), "utf-8")
    assert kb.assemble(ws, log=quiet)["assembled"] == 1
    _, raw = parse_frontmatter(ws.path(ws.load_json(kb.CONVERTED)[tid]["raw_target"]).read_text(encoding="utf-8"))
    assert "- max 5 retries for cards" in raw and "Update: max 5 retries for card payments." in raw
    assert raw.count("## Summary") == 1 and raw.count(kb.GENERATED_MARK) == 1


def test_head_of_a_raw_file_copilot_wrote_whole():
    old = (
        "# T\n\n## Summary\nS\n\n## Key points\n- k\n\n## Conversation\n### 2026-10-01 A\ntext\n\n"
        "## Attachments\n### a.docx\nx\n\n## Open questions\n- q\n"
    )
    assert kb._head_of(old) == "## Summary\nS\n\n## Key points\n- k\n\n## Open questions\n- q"


def test_quote_points_to_the_quoted_mail_not_to_a_later_one_with_the_same_signature(ws, tmp_path):
    """Fingerprints probe content: two short mails from one sender share mostly signature and disclaimer."""
    d = tmp_path / "eml"
    d.mkdir()
    t0 = datetime(2026, 10, 16, 23, 40, tzinfo=datetime.now().astimezone().tzinfo)
    sig = (
        "\n\nBest regards,\nSarah Tanaka\nSenior Engineer, Payments\nContoso Ltd. | 1-2-3 Marunouchi, Tokyo\n"
        "Tel: +81 3 1234 5678 | Mobile: +81 90 1234 5678\n\nThis email and any attachments are confidential "
        "and intended solely for the use of the addressee. If you have received this email in error, please "
        "notify the sender immediately and delete it."
    )
    first = "Hi,\nWe go ahead on 2026-11-22 with the cut-over." + sig
    root = make_eml(
        d / "1.eml",
        subject="Cut-over",
        frm="Sarah Tanaka <sarah@contoso.com>",
        to=["taro@contoso.com"],
        body=first,
        date=t0,
    )
    make_eml(
        d / "2.eml",
        subject="RE: Cut-over",
        frm="Taro Yamada <taro@contoso.com>",
        to=["sarah@contoso.com"],
        refs=root,
        date=t0 + timedelta(hours=3),
        body="Agreed.\n\nFrom: Sarah Tanaka <sarah@contoso.com>\n"
        f"Sent: {t0.strftime('%A, %B %d, %Y %I:%M %p')}\nTo: Taro Yamada\nSubject: Cut-over\n\n" + first,
    )
    # Later mail by the same sender, exactly six hours after the first (a plausible time-zone shift).
    make_eml(
        d / "3.eml",
        subject="RE: Cut-over",
        frm="Sarah Tanaka <sarah@contoso.com>",
        to=["taro@contoso.com"],
        refs=root,
        date=t0 + timedelta(hours=6),
        body="Checklist is in the ticket." + sig,
    )
    import_eml(ws, [str(d)], log=quiet)
    from kbmail.history import History
    from kbmail.records import read_message

    by = {r["subject"] + r["from"]["email"]: r for r in message_index(ws).all()}
    reply = by["RE: Cut-overtaro@contoso.com"]
    _, body = read_message(ws, reply)
    assert f"= message {by['Cut-oversarah@contoso.com']['id']}, exported separately" in History(ws).render(reply, body)


def test_empty_meeting_response_in_a_labeled_thread_stays_noise(ws, tmp_path, monkeypatch):
    from kbmail.workspace import Workspace

    d = tmp_path / "eml"
    d.mkdir()
    root = make_eml(d / "1.eml", subject="Plan review", frm="A <a@contoso.com>", to=["taro@contoso.com"], body="x")
    make_eml(
        d / "2.eml",
        subject="Accepted: Plan review",
        frm="B <b@contoso.com>",
        to=["a@contoso.com"],
        body="",
        refs=root,
    )
    monkeypatch.setenv("KBMAIL_OUTLOOK__SKIP_MEETING_RESPONSES", "false")  # exported by an earlier version
    import_eml(Workspace(ws.root), [str(d)], log=quiet)
    monkeypatch.delenv("KBMAIL_OUTLOOK__SKIP_MEETING_RESPONSES")
    ws = Workspace(ws.root)
    by = _by_subject(ws)
    _confirm(ws, by["Plan review"]["id"], ["payments"])
    prepare_batches(ws, log=quiet)
    lab = load_labels(ws)[by["Accepted: Plan review"]["id"]]
    assert lab["labels"] == ["others"] and lab["source"] == "noise"
