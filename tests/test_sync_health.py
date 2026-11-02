from datetime import datetime, timedelta

from kbmail import outlook
from kbmail.cli import main
from kbmail.status import collect
from kbmail.workspace import Workspace


def _sync(ws) -> int:
    return main(["-w", str(ws.root), "sync"])


def test_sync_without_outlook_is_not_a_failure(ws):
    # Not Windows / no pywin32: workspaces fed by `kbmail import-eml` keep working as before.
    assert _sync(ws) == 0
    assert collect(Workspace(ws.root))["sync_problems"] == []


def test_failed_export_is_reported_until_a_sync_succeeds(ws, monkeypatch, capsys):
    def broken(ws, **kw):
        raise RuntimeError("Outlook is not running")

    monkeypatch.setattr(outlook, "export_mail", broken)
    assert _sync(ws) == 1  # a scheduled task sees the failure
    assert "export FAILED" in capsys.readouterr().err
    s = collect(Workspace(ws.root))
    assert s["sync_problems"][0]["step"] == "export"
    assert s["next"][0].startswith("WARNING: the last `kbmail sync`")
    assert "Outlook is not running" in s["next"][0]

    monkeypatch.setattr(outlook, "export_mail", lambda ws, **kw: {})
    monkeypatch.setattr(outlook, "export_calendar", lambda ws, **kw: 0)
    assert _sync(ws) == 0
    assert not any(a.startswith("WARNING") for a in collect(Workspace(ws.root))["next"])


def test_status_warns_about_a_stale_export(ws, monkeypatch):
    old = (datetime.now().astimezone() - timedelta(days=5)).replace(microsecond=0).isoformat()
    ws.save_json("state/runs.json", {"export_mail": {"last_run": old, "exported": 3}})
    s = collect(Workspace(ws.root))
    assert s["next"][0].startswith("WARNING: no mail exported for 5 days")

    monkeypatch.setenv("KBMAIL_SYNC__WARN_AFTER_DAYS", "0")  # 0 turns the warning off
    assert not any(a.startswith("WARNING") for a in collect(Workspace(ws.root))["next"])
