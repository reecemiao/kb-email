import threading

import pytest

from kbmail import lock
from kbmail.cli import main
from kbmail.importers import import_eml
from kbmail.labels import interactive_review, load_labels, save_labels, set_label
from kbmail.lock import LockBusy, workspace_lock
from kbmail.records import message_index
from tests.conftest import make_eml

quiet = lambda *a, **k: None


def _held_elsewhere(ws, command="sync"):
    """Hold the workspace lock in another thread until the returned event is set."""
    got, release = threading.Event(), threading.Event()

    def run():
        with workspace_lock(ws, command, log=quiet):
            got.set()
            release.wait(10)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert got.wait(5)
    return release, t


def test_lock_is_exclusive_and_released(ws):
    release, t = _held_elsewhere(ws)
    with pytest.raises(LockBusy, match="kbmail sync"), workspace_lock(ws, "label import", wait=0, log=quiet):
        pass  # the error says who holds the lock
    release.set()
    t.join(5)
    with workspace_lock(ws, "label import", wait=0, log=quiet):
        pass
    assert not ws.path(lock.HOLDER).exists()


def test_lock_waits_then_proceeds(ws):
    release, t = _held_elsewhere(ws)
    msgs = []
    threading.Timer(0.3, release.set).start()
    with workspace_lock(ws, "label import", wait=5, poll=0.05, log=msgs.append):
        pass
    t.join(5)
    assert msgs and "kbmail sync" in msgs[0]


def test_lock_is_reentrant_in_one_thread(ws):
    with workspace_lock(ws, "sync", wait=0, log=quiet), workspace_lock(ws, "inner", wait=0, log=quiet):
        pass


def test_cli_reports_busy_workspace(ws, monkeypatch, capsys):
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0)
    release, t = _held_elsewhere(ws)
    try:
        assert main(["-w", str(ws.root), "label", "prepare"]) == 3
        assert "kbmail sync" in capsys.readouterr().err
        assert main(["-w", str(ws.root), "status"]) == 0  # read-only commands don't wait
    finally:
        release.set()
        t.join(5)
    assert main(["-w", str(ws.root), "label", "prepare"]) == 0


def test_review_keeps_what_other_commands_write_meanwhile(ws, tmp_path):
    src = tmp_path / "eml"
    src.mkdir()
    for i in range(3):
        make_eml(
            src / f"{i}.eml",
            subject=f"Server patching {i}",
            frm="ken@vendor.com",
            to=["taro@contoso.com"],
            body=f"Patch window {i} for the network servers.",
        )
    import_eml(ws, [src], log=quiet)
    a, b, c = sorted(message_index(ws).items)
    labels = {}
    for mid in (a, b):
        set_label(labels, mid, ["infra"], "proposed", "copilot", 0.5, "test")
    save_labels(ws, labels)

    calls = []

    def answer(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            # While the user looks at the first message, the scheduled sync labels a new one by rule.
            current = load_labels(ws)
            set_label(current, c, ["infra"], "auto", "rule", None, "rule r1")
            save_labels(ws, current)
            return ""  # accept the first proposal
        return "2"  # second message: set to area 2

    stats = interactive_review(ws, order="date", input_fn=answer, out=quiet)
    assert stats["confirmed"] == 1 and stats["changed"] == 1
    final = load_labels(ws)
    assert final.get(c, {}).get("status") == "auto"  # the sync's label survived the review's saves
    assert {final[a]["status"], final[b]["status"]} == {"confirmed"}  # and so did the user's answers
