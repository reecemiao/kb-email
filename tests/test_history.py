"""Quoted history: forwards keep what they forward, replies don't repeat exported mail."""

from datetime import UTC, datetime, timedelta

from kbmail import history, kb
from kbmail.cli import main
from kbmail.history import History, parse_loose_dt, split
from kbmail.importers import import_eml
from kbmail.labels import interactive_review, load_labels, prepare_batches, save_labels, set_label
from kbmail.records import message_index
from tests.conftest import make_eml

quiet = lambda *a, **k: None

# The original was sent 2026-07-30 15:48 at UTC+8 = 07:48 UTC; the forwarder's Outlook
# shows it in its own time zone.
ORIG = datetime(2026, 7, 30, 7, 48, 20, tzinfo=UTC)
DAN = (
    "Hi Chris,\n\nThe design notes are on the wiki here https://docs.example/intro - they also cover "
    "the capacity plan and the roadmap.\n\nIn short, the platform runs the batch jobs "
    "for billing and reporting. Every new service should be deployed there unless it needs special hardware."
)


def _fw_body(sent: str) -> str:
    return (
        "FYI.\n\n\nINTERNAL\n\n\nFrom: Alex MORGAN <alex@corp.example>\n\nSent: " + sent + "\n\n"
        "To: Chris LEE <chris@corp.example>\n\nSubject: RE: APP.PLATFORM\n\n\n\n" + DAN.replace("\n", "\n\n")
    )


def _mailbox(d, with_original: bool):
    d.mkdir(parents=True, exist_ok=True)
    if with_original:
        make_eml(
            d / "0.eml",
            subject="RE: APP.PLATFORM",
            frm="Alex MORGAN <alex@corp.example>",
            to=["chris@corp.example"],
            date=ORIG,
            body=DAN,
        )
        # Same sender, same text a week earlier: must not be taken for the quoted mail.
        make_eml(
            d / "0b.eml",
            subject="RE: APP.PLATFORM",
            frm="Alex MORGAN <alex@corp.example>",
            to=["chris@corp.example"],
            date=ORIG - timedelta(days=7),
            body=DAN,
        )
    fw_id = make_eml(
        d / "1.eml",
        subject="FW: APP.PLATFORM",
        frm="Chris LEE <chris@corp.example>",
        to=["taro@contoso.com", "sam@corp.example"],
        date=ORIG + timedelta(days=46),
        body=_fw_body("30 July 2026 15:48"),
    )
    make_eml(
        d / "2.eml",
        subject="RE: FW: APP.PLATFORM",
        frm="Sam WU <sam@corp.example>",
        to=["chris@corp.example"],
        cc=["taro@contoso.com", "new.person@corp.example"],
        date=ORIG + timedelta(days=47),
        refs=fw_id,
        body="Thanks Chris, very useful.\n\nFrom: Chris LEE <chris@corp.example>\nSent: Monday, September 14, 2026 "
        "3:48 PM\nTo: Taro <taro@contoso.com>; Sam WU <sam@corp.example>\nSubject: FW: APP.PLATFORM\n\n"
        + _fw_body("Thursday, July 30, 2026 3:48 PM"),
    )
    return d


def _by_subject(ws):
    return {r["subject"]: r for r in message_index(ws).all()}


def test_forward_keeps_what_it_forwards(ws, tmp_path):
    import_eml(ws, [str(_mailbox(tmp_path / "eml", with_original=False))], log=quiet)
    prepare_batches(ws, log=quiet)
    batch = ws.path("work/labeling/batch_001.md").read_text(encoding="utf-8")
    assert "FYI." in batch and "runs the batch jobs for billing" in batch
    assert "--- quoted: Alex MORGAN <alex@corp.example> | 30 July 2026 15:48 | RE: APP.PLATFORM ---" in batch
    # The reply quotes the forward (exported) -> pointer; Dan's mail is not exported -> kept once per message.
    msgs = _by_subject(ws)
    fw, re_ = msgs["FW: APP.PLATFORM"], msgs["RE: FW: APP.PLATFORM"]
    assert f"= message {fw['id']}, exported separately" in batch
    assert "[added to the conversation here: new.person@corp.example]" in batch

    # interactive review shows the forwarded text too
    labels = load_labels(ws)
    for mid in (fw["id"], re_["id"]):
        set_label(labels, mid, ["infra"], "proposed", "copilot", 0.5, "test")
    save_labels(ws, labels)
    shown = []
    interactive_review(ws, input_fn=lambda _: "q", out=shown.append)
    assert any("batch jobs" in s for s in shown)

    # KB selection for the thread: Dan's text appears once, not in both messages.
    assert fw["thread_id"] == re_["thread_id"]
    labels = load_labels(ws)
    for mid in (fw["id"], re_["id"]):
        set_label(labels, mid, ["infra"], "confirmed", "user", 1.0, "test")
    save_labels(ws, labels)
    kb.prepare_select(ws, log=quiet)
    sel = ws.path("work/kb_select/batch_001.md").read_text(encoding="utf-8")
    assert sel.count("batch jobs") == 1
    assert f"= message {fw['id']}, exported separately" in sel


def test_reply_points_to_exported_original(ws, tmp_path):
    import_eml(ws, [str(_mailbox(tmp_path / "eml", with_original=True))], log=quiet)
    msgs = _by_subject(ws)
    fw = msgs["FW: APP.PLATFORM"]
    originals = sorted(
        (r for r in message_index(ws).all() if r["subject"] == "RE: APP.PLATFORM"), key=lambda r: r["date"]
    )
    week_before, orig = originals
    h = History(ws)
    _, segs = split(ws.path(fw["path"]).read_text(encoding="utf-8"))
    assert h.match(segs[0], exclude=fw["id"]) == orig["id"]  # 15:48 local = 07:48 UTC; the older copy is ruled out
    text = h.render(fw, ws.path(fw["path"]).read_text(encoding="utf-8").split("---", 2)[2])
    assert "batch jobs" not in text and f"= message {orig['id']}" in text
    assert week_before["id"] not in text
    assert ws.path(history.STATE).exists()

    st = history.status(ws)
    assert st["already_exported"] == 3 and st["kept"] == 0 and st["messages_quoted_elsewhere"] == 2
    assert main(["-w", str(ws.root), "history", "show", fw["id"]]) == 0


def test_split_formats_and_dates():
    own, segs = split(
        "Reply.\n\n**From:** Bob <b@x.com> **Sent:** Monday, July 27, 2026 1:00 PM **To:** me **Subject:** hi\n\nold"
    )
    assert own == "Reply." and segs[0].sender["email"] == "b@x.com" and segs[0].subject == "hi"
    own, segs = split("ok\n\nOn Thu, Jul 30, 2026 at 3:48 PM Dan <dan@x.com> wrote:\n> line1\n>> deeper\n")
    assert own == "ok" and segs[0].text == "line1\ndeeper" and segs[0].dates == [datetime(2026, 7, 30, 15, 48)]
    own, segs = split(
        "Got it.\n\nFrom: Sato <sato@x.example>\nSent: Thursday, October 1, 2026 1:05 PM\nTo: Taro\n"
        "Subject: plan\n\nbody"
    )
    assert own == "Got it." and segs[0].dates == [datetime(2026, 10, 1, 13, 5)] and segs[0].text == "body"
    own, segs = split("Hello\nFrom: the team, thanks for all\nbye")
    assert segs == [] and "From: the team" in own
    assert parse_loose_dt("07/08/2026 10:00") == [datetime(2026, 8, 7, 10, 0), datetime(2026, 7, 8, 10, 0)]
    assert parse_loose_dt("30 July 2026 15:48") == [datetime(2026, 7, 30, 15, 48)]
    assert parse_loose_dt("Monday") == []


# ---------------------------------------------------------------- Simplified Chinese clients


def _fw_body_zh(colon: str) -> str:
    """A forward from Simplified Chinese Outlook: 发件人 / 发送时间 / 收件人 / 主题 headers."""
    return (
        "FYI.\n\n"
        f"发件人{colon} Alex MORGAN <alex@corp.example>\n"
        f"发送时间{colon} 2026年7月30日 星期四 下午 3:48\n"
        f"收件人{colon} Chris LEE <chris@corp.example>\n"
        f"主题{colon} RE: APP.PLATFORM\n\n" + DAN
    )


def test_chinese_outlook_forward_points_to_exported_original(ws, tmp_path):
    d = _mailbox(tmp_path / "eml", with_original=True)
    make_eml(
        d / "3.eml",
        subject="转发: APP.PLATFORM",
        frm="Chris LEE <chris@corp.example>",
        to=["taro@contoso.com"],
        date=ORIG + timedelta(days=50),
        body=_fw_body_zh("\uff1a"),  # full-width colon, as Chinese Outlook writes it
    )
    import_eml(ws, [str(d)], log=quiet)
    msgs = _by_subject(ws)
    zh = msgs["转发: APP.PLATFORM"]
    orig = max((r for r in message_index(ws).all() if r["subject"] == "RE: APP.PLATFORM"), key=lambda r: r["date"])
    own, segs = split(ws.path(zh["path"]).read_text(encoding="utf-8").split("---", 2)[2])
    assert own.strip() == "FYI." and segs[0].sender["email"] == "alex@corp.example"
    assert segs[0].dates == [datetime(2026, 7, 30, 15, 48)]
    assert History(ws).match(segs[0], exclude=zh["id"]) == orig["id"]  # 15:48 at UTC+8 = the exported 07:48 UTC


def test_split_simplified_chinese_formats():
    own, segs = split(_fw_body_zh(":"))
    assert own == "FYI." and segs[0].subject == "RE: APP.PLATFORM" and segs[0].text.startswith("Hi Chris")
    own, segs = split("Thanks\n\n2026年7月30日周四 15:48\uff0cDan <dan@x.com> 写道\uff1a\n> line1\n")
    assert own == "Thanks" and segs[0].sender["email"] == "dan@x.com" and segs[0].text == "line1"
    assert segs[0].dates == [datetime(2026, 7, 30, 15, 48)]
    own, segs = split("See below.\n\n-----原始邮件-----\n发件人: Bob <b@x.com>\n发送时间: 2026年7月27日 13:00\n\nold")
    assert own == "See below." and segs[0].sender["email"] == "b@x.com"
    assert parse_loose_dt("2026年7月30日 星期四 下午3:48") == [datetime(2026, 7, 30, 15, 48)]
    assert parse_loose_dt("2026年7月30日(周四) 上午9:05") == [datetime(2026, 7, 30, 9, 5)]
    assert parse_loose_dt("2026年7月30日 15:48") == [datetime(2026, 7, 30, 15, 48)]
