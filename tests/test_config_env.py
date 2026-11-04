"""Settings precedence: CLI > environment > kbmail.yaml > defaults. .env files are not read."""

import os

import pytest
import yaml

from kbmail.cli import main
from kbmail.workspace import (
    DEFAULT_CONFIG,
    Workspace,
    WorkspaceError,
    _leaf_paths,
    init_workspace,
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("KBMAIL_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)  # keep a stray .env in the repo's cwd out of the tests
    r = tmp_path / "ws"
    init_workspace(r)
    (r / "kbmail.yaml").write_text(yaml.safe_dump({"outlook": {"days": 10}, "triage": {"days": 5}}), encoding="utf-8")
    return r


def test_precedence_yaml_environment_cli(root, monkeypatch):
    ws = Workspace(root)
    assert ws.cfg("outlook.days") == 10 and ws.config_sources["outlook.days"] == "kbmail.yaml"
    assert ws.config_sources["labeling.batch_size"] == "default"
    monkeypatch.setenv("KBMAIL_OUTLOOK__DAYS", "30")
    ws = Workspace(root)
    assert ws.cfg("outlook.days") == 30 and ws.config_sources["outlook.days"] == "environment"
    assert ws.cfg("triage.days") == 5  # untouched keys keep their own source
    # an explicit CLI value beats everything
    assert ws.opt(3, "outlook.days") == 3 and ws.opt(None, "outlook.days") == 30


def test_dotenv_files_are_not_read_but_warned_about(root, monkeypatch):
    (root / ".env").write_text("# old\nKBMAIL_OUTLOOK__DAYS=20\nexport KBMAIL_TRIAGE__DAYS=7\n", encoding="utf-8")
    (root.parent / ".env").write_text("# KBMAIL_OUTLOOK__DAYS=25\n", encoding="utf-8")  # commented: silent
    ws = Workspace(root)
    assert ws.cfg("outlook.days") == 10 and ws.cfg("triage.days") == 5
    assert ws.config_sources["outlook.days"] == "kbmail.yaml"
    [w] = [w for w in ws.config_warnings if ".env" in w]
    assert "no longer read" in w and "KBMAIL_OUTLOOK__DAYS" in w and "KBMAIL_TRIAGE__DAYS" in w


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


def test_workspace_location_from_environment(root, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (elsewhere / ".env").write_text(f"KBMAIL_WORKSPACE={root}\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"KBMAIL_WORKSPACE in a \.env file is no longer read"):
        Workspace.find()
    monkeypatch.setenv("KBMAIL_WORKSPACE", str(root))
    assert Workspace.find().root == root.resolve()


def test_every_setting_has_an_env_name_in_config_show(root, capsys):
    assert main(["-w", str(root), "config", "show"]) == 0
    out = capsys.readouterr().out
    for path in _leaf_paths(DEFAULT_CONFIG):
        assert "KBMAIL_" + "__".join(p.upper() for p in path) + "]" in out, path


def test_init_writes_no_env_files_and_upgrade_removes_generated_ones(root, capsys, monkeypatch):
    assert not (root / ".env.example").exists() and not (root / ".env").exists()
    template = "# kbmail settings. Copy to .env (or set real environment variables) and uncomment what you need.\n"
    (root / ".env.example").write_text(template + "# KBMAIL_OUTLOOK__DAYS=30\n", encoding="utf-8")
    (root / ".env").write_text(template + "# KBMAIL_OUTLOOK__DAYS=30\n", encoding="utf-8")
    written = init_workspace(root, upgrade=True)
    assert "removed .env.example" in written and "removed .env" in written
    assert not (root / ".env.example").exists() and not (root / ".env").exists()
    # a .env the user edited is kept (kbmail warns about it instead)
    (root / ".env").write_text(template + "KBMAIL_OUTLOOK__DAYS=60\n", encoding="utf-8")
    init_workspace(root, upgrade=True)
    assert (root / ".env").exists()
    monkeypatch.setenv("KBMAIL_MAIL__SEARCH_LIMIT", "3")
    assert main(["-w", str(root), "config", "show", "mail"]) == 0
    captured = capsys.readouterr()
    assert "mail.search_limit = 3" in captured.out and "environment; KBMAIL_MAIL__SEARCH_LIMIT" in captured.out
    assert "no longer read" in captured.err
