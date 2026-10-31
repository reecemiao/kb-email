import pytest

from kbmail import rules
from kbmail.records import message_index
from kbmail.redact import redact
from kbmail.teams import html_to_text, write_chat_day
from kbmail.textutil import normalize_subject, strip_quoted, tokenize
from kbmail.workspace import init_workspace


def test_strip_quoted_english_and_japanese():
    en = "New text here.\n\nFrom: Bob <b@x.com>\nSent: Monday\nTo: me\n\nold text"
    assert strip_quoted(en) == "New text here."
    ja = "了解しました。\n\n差出人: 佐藤\n送信日時: 2026年10月1日\n宛先: 山田\n\n古い本文"
    assert strip_quoted(ja) == "了解しました。"
    md = "Reply.\n\n**From:** Bob\n**Sent:** Monday\n\nold"
    assert strip_quoted(md) == "Reply."
    # a "From:" line that is not a reply header is kept
    assert "From: the team" in strip_quoted("Hello\nFrom: the team, thanks for all\nbye")


def test_tokenize_cjk_bigrams():
    toks = tokenize("請求システム migration")
    assert "請求" in toks and "migration" in toks


def test_normalize_subject():
    assert normalize_subject("RE: Fw: 返信: Budget") == "budget"


def test_redact():
    text, n = redact("password: abc123\nkey AKIAABCDEFGHIJKLMNOP card 4111 1111 1111 1111 order 1234 5678 9012 3456")
    assert "abc123" not in text and "AKIA" not in text and "4111" not in text
    assert "1234 5678 9012 3456" in text  # fails Luhn -> kept
    assert n == 3


@pytest.mark.parametrize(
    "text, secret",
    [
        ("初期パスワード：Abc12345", "Abc12345"),  # no \b between 期 and パ
        ("仮パスワードは Abc12345 です", "Abc12345"),
        ("パスワードはAbc12345です", "Abc12345"),
        ("仮パスワードは「abc def」です", "abc def"),
        ("暗証番号: 4821", "4821"),
        ("Password is hunter2.", "hunter2"),
        ("PW: Abc12345", "Abc12345"),
        ("PIN: 4821", "4821"),
        ("pwd = 'my pass phrase'", "pass phrase"),
        ("Server=x;User Id=sa;Password=S3cret!;Database=y", "S3cret"),
        ("clone https://ken:S3cret@git.example.com/repo.git", "S3cret"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("DefaultEndpointsProtocol=https;AccountKey=abcdEFGH1234abcdEFGH1234abcd==;", "abcdEFGH1234"),
        ("https://x.blob.core.windows.net/c?sv=2020&sig=abcdEFGH1234%2Bxyz&se=1", "abcdEFGH1234"),
        ("Amex 3782 822463 10005", "822463"),
        ("カード番号 4111111111111111 です", "4111111111111111"),
    ],
)
def test_redact_catches(text, secret):
    out, n = redact(text)
    assert secret not in out and n == 1, out


@pytest.mark.parametrize(
    "text",
    [
        "the password is required for login",
        "パスワードが変わりました。再設定してください",
        "order 1234567812345670 shipped",  # Luhn-valid, but ungrouped and no card context
        "tracking 4111111111111111",
        "spin: 5, opinion: none",
        "basic understanding of the API",
        "call 090-1234-5678",
    ],
)
def test_redact_keeps(text):
    assert redact(text) == (text, 0)


def test_redact_keeps_spacing_around_cards():
    assert redact("card 4111 1111 1111 1111 exp 12/29")[0] == "card [REDACTED-CARD] exp 12/29"


def test_rule_matching_fields():
    rec = {
        "from": {"name": "Ken", "email": "ken@mail.vendor.com"},
        "to": [],
        "cc": [],
        "subject": "PRJ-123 status",
        "folder": "Inbox",
        "direction": "received",
    }
    data = {
        "threshold": 2,
        "rules": [
            {"id": "a", "area": "x", "weight": 1, "when": {"from_domain": ["vendor.com"]}},
            {"id": "b", "area": "x", "weight": 1, "when": {"subject": [r"re:\bPRJ-\d+"]}},
            {
                "id": "c",
                "area": "x",
                "weight": 5,
                "when": {"subject": ["status"]},
                "unless": {"from_domain": ["vendor.com"]},
            },
        ],
    }
    areas, scores, hits = rules.classify(data, rules.features(rec))
    assert areas == ["x"] and scores["x"] == 2 and hits["x"] == ["a", "b"]


def test_teams_chat_day_merge(tmp_path):
    ws_root = tmp_path / "w"
    init_workspace(ws_root)
    from kbmail.workspace import Workspace

    ws = Workspace(ws_root)
    idx = message_index(ws)
    members = [{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@x.com"}]
    m1 = {"id": "1", "time": "2026-10-20T09:00:00+09:00", "from": members[0], "text": "hello"}
    m2 = {"id": "2", "time": "2026-10-20T09:05:00+09:00", "from": members[1], "text": "hi"}
    rid = write_chat_day(ws, idx, "chat1", "Chat A/B", "oneOnOne", members, "2026-10-20", [m1], "graph")
    rid2 = write_chat_day(ws, idx, "chat1", "Chat A/B", "oneOnOne", members, "2026-10-20", [m2], "graph")
    assert rid == rid2
    body = ws.path(idx.get(rid)["path"]).read_text(encoding="utf-8")
    assert "hello" in body and "hi" in body
    assert html_to_text("<p>a&amp;b</p><br>c") == "a&b\n\nc"


def test_init_upgrade_keeps_user_files(tmp_path):
    root = tmp_path / "w"
    init_workspace(root)
    (root / "kbmail.yaml").write_text("areas: [mine]\n", encoding="utf-8")
    prompt = root / ".github/skills/kb-label/SKILL.md"
    prompt.write_text("stale", encoding="utf-8")
    init_workspace(root, upgrade=True)
    assert (root / "kbmail.yaml").read_text(encoding="utf-8") == "areas: [mine]\n"
    assert prompt.read_text(encoding="utf-8") != "stale"
    assert "{{TODAY}}" not in (root / "wiki/overview.md").read_text(encoding="utf-8")


def test_eml_meeting_reply_without_comment_is_skipped(ws, tmp_path):
    from email.message import EmailMessage

    from kbmail.importers import import_eml

    d = tmp_path / "eml"
    d.mkdir()
    for n, text in enumerate(["", "Can we move it to 3pm? The vendor joins late."]):
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = "Accepted: Design review", "Ken <ken@x.com>", "taro@contoso.com"
        m["Message-ID"] = f"<reply{n}@x>"
        m.set_content(text or "\n")
        m.add_alternative("BEGIN:VCALENDAR\nMETHOD:REPLY\nEND:VCALENDAR\n", subtype="calendar")
        (d / f"{n}.eml").write_bytes(bytes(m))
    assert import_eml(ws, [str(d)], log=lambda *a: None) == 1
    (rec,) = message_index(ws).all()
    assert rec["message_class"] == "IPM.Schedule.Meeting.Resp"
