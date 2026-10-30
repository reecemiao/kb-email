"""Settings precedence: CLI > environment > .env (cwd) > .env (workspace) > kbmail.yaml > defaults."""

import os

import pytest
import yaml

from kbmail.cli import main
from kbmail.workspace import (
    DEFAULT_CONFIG,
    Workspace,
    WorkspaceError,
    _leaf_paths,
    env_example,
    env_overrides,
    init_workspace,
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("KBMAIL_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)  # keep the repo's own cwd .env (if any) out of the tests
    r = tmp_path / "ws"
    init_workspace(r)
    (r / "kbmail.yaml").write_text(yaml.safe_dump({"outlook": {"days": 10}, "triage": {"days": 5}}), encoding="utf-8")
    return r


def test_precedence_yaml_dotenv_environment(root, monkeypatch):
    assert Workspace(root).cfg("outlook.days") == 10
    (root / ".env").write_text("KBMAIL_OUTLOOK__DAYS=20\nKBMAIL_TRIAGE__DAYS=7\n", encoding="utf-8")
    ws = Workspace(root)
    assert ws.cfg("outlook.days") == 20 and ws.cfg("triage.days") == 7
    assert ws.config_sources["outlook.days"].endswith(".env")
    assert ws.config_sources["labeling.batch_size"] == "default"
    # a .env in the current directory beats the workspace one
    (root.parent / ".env").write_text("KBMAIL_OUTLOOK__DAYS=25\n", encoding="utf-8")
    assert Workspace(root).cfg("outlook.days") == 25
    # real environment beats both
    monkeypatch.setenv("KBMAIL_OUTLOOK__DAYS", "30")
    ws = Workspace(root)
    assert ws.cfg("outlook.days") == 30 and ws.config_sources["outlook.days"] == "environment"
    assert ws.cfg("triage.days") == 7  # untouched keys keep their own source
    # and an explicit CLI value beats everything
    assert ws.opt(3, "outlook.days") == 3 and ws.opt(None, "outlook.days") == 30


def test_types_are_taken_from_defaults(root, monkeypatch):
    env = {
        "KBMAIL_OUTLOOK__INCLUDE_SUBFOLDERS": "yes",
        "KBMAIL_OUTLOOK__FOLDERS": "inbox, sent, Inbox/Projects",
        "KBMAIL_ME__EMAILS": "[a@x.com, b@x.com]",
        "KBMAIL_REVIEW__ACCEPT_ABOVE": "0.9",
        "KBMAIL_WIKI__STALE_BELOW": "0.5",
        "KBMAIL_WIKI__HALF_LIFE_DAYS__FAST": "10",
        "KBMAIL_AREAS": "payments,infra",
        "KBMAIL_TEAMS__CLIENT_ID": "0000-1111",
        "KBMAIL_LABELING__LIMIT": "",  # empty = not set
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ws = Workspace(root)
    assert ws.cfg("outlook.include_subfolders") is True
    assert ws.cfg("outlook.folders") == ["inbox", "sent", "Inbox/Projects"]
    assert ws.cfg("me.emails") == ["a@x.com", "b@x.com"] and ws.me_emails == {"a@x.com", "b@x.com"}
    assert ws.cfg("review.accept_above") == 0.9 and ws.cfg("wiki.stale_below") == 0.5
    assert ws.cfg("wiki.half_life_days") == {"fast": 10, "normal": 180, "slow": 730}
    assert ws.area_names == ["payments", "infra"]
    assert ws.cfg("teams.client_id") == "0000-1111"
    assert ws.cfg("labeling.limit") is None


def test_unknown_and_invalid_values(root, monkeypatch):
    monkeypatch.setenv("KBMAIL_OUTLOOK__DAYZ", "3")
    ws = Workspace(root)
    ws.config  # noqa: B018
    assert any("KBMAIL_OUTLOOK__DAYZ" in w for w in ws.config_warnings)
    monkeypatch.setenv("KBMAIL_COPILOT__VISION", "maybe")
    with pytest.raises(WorkspaceError, match="KBMAIL_COPILOT__VISION"):
        Workspace(root).config  # noqa: B018


def test_workspace_location_from_dotenv(root, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text(f"KBMAIL_WORKSPACE={root}\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert Workspace.find().root == root.resolve()


def test_env_example_lists_every_setting_with_its_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = env_example()
    for path in _leaf_paths(DEFAULT_CONFIG):
        assert "KBMAIL_" + "__".join(p.upper() for p in path) + "=" in text
    # uncommenting the whole template must reproduce the defaults exactly
    lines = [ln[2:] for ln in text.splitlines() if ln.startswith("# KBMAIL_") and "WORKSPACE" not in ln]
    (tmp_path / ".env").write_text("\n".join(lines), encoding="utf-8")
    overrides, _, warnings = env_overrides()
    assert not warnings
    for path in _leaf_paths(overrides):
        got, want = overrides, DEFAULT_CONFIG
        for p in path:
            got, want = got[p], want[p]
        assert got == want, path


def test_init_writes_env_files_and_config_cli(root, capsys, monkeypatch):
    assert (root / ".env.example").exists() and (root / ".env").exists()
    (root / ".env").write_text("KBMAIL_MAIL__SEARCH_LIMIT=3\n", encoding="utf-8")
    assert main(["-w", str(root), "config", "show", "mail"]) == 0
    out = capsys.readouterr().out
    assert "mail.search_limit = 3" in out and ".env" in out and "KBMAIL_MAIL__SEARCH_LIMIT" in out
    # --upgrade refreshes .env.example but never touches the user's .env
    init_workspace(root, upgrade=True)
    assert (root / ".env").read_text(encoding="utf-8") == "KBMAIL_MAIL__SEARCH_LIMIT=3\n"
