"""Teams without an own Entra app: saved Graph JSON (PowerShell / Graph Explorer), pasted text, transcripts."""

import json
from datetime import datetime

import pytest

from kbmail import teams
from kbmail.convert import file_to_markdown, vtt_to_markdown
from kbmail.importers import import_eml
from kbmail.records import message_index, read_message

quiet = lambda *a, **k: None

CHAT = {
    "id": "19:abc@thread.v2",
    "chatType": "group",
    "topic": "Payment cut-over",
    "lastUpdatedDateTime": "2026-10-27T03:00:00Z",
    "members": [
        {"displayName": "Sarah Tanaka", "email": "sarah@contoso.com", "userId": "u1"},
        {"displayName": "Taro Yamada", "email": "taro@contoso.com", "userId": "u2"},
    ],
}


def msg(mid, when, uid, name, html, chat_id=None):
    m = {
        "id": mid,
        "messageType": "message",
        "createdDateTime": when,
        "lastModifiedDateTime": when,
        "from": {"user": {"id": uid, "displayName": name}},
        "body": {"contentType": "html", "content": html},
        "attachments": [],
    }
    if chat_id:
        m["chatId"] = chat_id
    return m


def write_ps_output(folder, stamp="20261027-090000"):
    """What scripts/teams-export.ps1 writes: raw Graph pages, untouched."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"chats-{stamp}.json").write_text(json.dumps([{"value": [CHAT]}]), encoding="utf-8")
    pages = [
        {
            "value": [
                msg("1", "2026-10-27T01:00:00Z", "u1", "Sarah Tanaka", "<p>Freeze starts <b>11/14</b></p>"),
                msg("2", "2026-10-27T01:05:00Z", "u2", "Taro Yamada", "<p>OK, rollback at 2% errors</p>"),
            ]
        },
        {
            "value": [
                {**msg("3", "2026-10-27T01:06:00Z", "u1", "Sarah Tanaka", "x"), "messageType": "systemEventMessage"}
            ]
        },
    ]
    (folder / f"messages-{stamp}-1.json").write_text(
        json.dumps({"chatId": CHAT["id"], "pages": pages}), encoding="utf-8-sig"
    )


def test_import_powershell_output_and_rerun(ws):
    folder = ws.path("data/teams-graph")
    write_ps_output(folder)
    r = teams.import_graph_json(ws, log=quiet)
    assert r == {"messages": 2, "records": 1, "chats": 1}  # system event skipped
    rec = next(x for x in message_index(ws).all() if x["source"] == "teams")
    assert rec["subject"] == "Payment cut-over" and rec["from"]["email"] in {"sarah@contoso.com", "taro@contoso.com"}
    body = read_message(ws, rec)[1]
    assert "Freeze starts 11/14" in body and "Taro Yamada" in body
    assert not list(folder.glob("*.json")) and len(list((folder / "_imported").glob("*.json"))) == 2
    # Next export overlaps by an hour: same message ids merge instead of duplicating.
    write_ps_output(folder, stamp="20261028-090000")
    teams.import_graph_json(ws, log=quiet)
    assert sum(1 for x in message_index(ws).all() if x["source"] == "teams") == 1
    assert read_message(ws, rec)[1].count("Freeze starts") == 1


def test_import_graph_explorer_response_without_chat_metadata(ws, tmp_path):
    saved = tmp_path / "graph-explorer.json"
    saved.write_text(
        json.dumps(
            {"value": [msg("9", "2026-10-26T05:00:00Z", "u9", "Ken", "<div>Vendor SLA is 99.9%</div>", "19:x")]}
        ),
        encoding="utf-8",
    )
    r = teams.import_graph_json(ws, [str(saved)], log=quiet)
    assert r["messages"] == 1 and saved.exists()  # files outside teams.import_dir are left in place


def test_parse_pasted_chat_formats():
    fb = datetime(2026, 10, 27, 12, 0).astimezone()
    bracket = (
        "[10/27/2026 9:30 AM] Sarah Tanaka: Freeze starts on 11/14\n"
        "[10/27/2026 9:32 AM] Taro Yamada\nOK.\nRollback at 2%."
    )
    m = teams.parse_pasted_chat(bracket, fb)
    assert [x["name"] for x in m] == ["Sarah Tanaka", "Taro Yamada"]
    assert m[1]["text"] == "OK.\nRollback at 2%." and m[0]["time"].startswith("2026-10-27T09:30")
    new_teams = "Sarah Tanaka\n10/27 9:30 AM\nFreeze starts on 11/14\n\nTaro Yamada\n9:32 AM\nOK"
    m = teams.parse_pasted_chat(new_teams, fb)
    assert [(x["name"], x["text"]) for x in m] == [("Sarah Tanaka", "Freeze starts on 11/14"), ("Taro Yamada", "OK")]
    ja = "[2026/10/27 9:30] 佐藤 花子: 請求システムの件、確認しました\n[2026/10/27 午後 1:05] 山田 太郎: ありがとう"
    m = teams.parse_pasted_chat(ja, fb)
    assert m[0]["name"] == "佐藤 花子" and m[1]["time"].startswith("2026-10-27T13:05")
    plain = teams.parse_pasted_chat("just some notes\nwithout headers", fb)
    assert len(plain) == 1 and plain[0]["name"] == "(unknown)"


def test_import_text_links_speakers_to_mail_contacts(ws, mailbox, tmp_path):
    import_eml(ws, [str(mailbox)], log=quiet)  # Sarah Tanaka <sarah@contoso.com> is known from mail
    f = tmp_path / "Payment cut-over.txt"
    f.write_text("[10/27/2026 9:30 AM] Sarah Tanaka: Freeze starts on 11/14\n", encoding="utf-8")
    r = teams.import_text(ws, [str(f)], log=quiet)
    assert r == {"messages": 1, "records": 1}
    rec = next(x for x in message_index(ws).all() if x["source"] == "teams")
    assert rec["subject"] == "Payment cut-over" and rec["from"]["email"] == "sarah@contoso.com"
    teams.import_text(ws, [str(f)], log=quiet)  # re-importing the same paste is a no-op
    assert read_message(ws, rec)[1].count("Freeze starts") == 1


def test_vtt_transcript_to_markdown(tmp_path):
    vtt = (
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Sarah Tanaka>Let's review the cut-over.</v>\n\n"
        "2\n00:00:04.500 --> 00:00:06.000\n<v Sarah Tanaka>Freeze is 11/14.</v>\n\n"
        "3\n00:00:06.500 --> 00:00:08.000\n<v Taro Yamada>Agreed.</v>\n"
    )
    md = vtt_to_markdown(vtt)
    assert md.count("**Sarah Tanaka**") == 1 and "**Taro Yamada** (00:00:06):" in md and "Freeze is 11/14." in md
    p = tmp_path / "meeting.vtt"
    p.write_text(vtt, encoding="utf-8")
    assert file_to_markdown(p)[0] == md


def test_graph_route_without_client_id_points_to_alternatives(ws):
    with pytest.raises(RuntimeError, match=r"teams\.via: powershell"):
        teams.export(ws, "graph")


def test_workspace_ships_the_powershell_script(ws):
    script = ws.path("scripts/teams-export.ps1").read_text(encoding="utf-8")
    assert "Connect-MgGraph" in script and "Chat.Read" in script and "-OutputType Json" in script
