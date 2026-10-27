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
