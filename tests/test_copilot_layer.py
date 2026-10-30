"""The VS Code / Copilot customization files shipped in the workspace template are well-formed."""

import json
import re
from pathlib import Path

import pytest

from kbmail.cli import build_parser, main
from kbmail.textutil import parse_frontmatter
from kbmail.workspace import init_workspace


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "ws"
    init_workspace(root)
    return root


def _skills(root: Path) -> dict[str, tuple[dict, str, Path]]:
    out = {}
    for p in sorted((root / ".github/skills").glob("*/SKILL.md")):
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        out[p.parent.name] = (meta, body, p)
    return out


def test_skills_follow_the_agent_skills_spec(ws_root):
    skills = _skills(ws_root)
    assert {"kb-sync", "kb-label", "kb-draft", "kbmail-cli"} <= set(skills)
    assert not (ws_root / ".github/prompts").exists()
    for folder, (meta, body, path) in skills.items():
        assert meta["name"] == folder, path
        assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"]), path
        assert 20 < len(meta["description"]) <= 1024, path
        assert "use " in meta["description"].lower(), f"{path}: description should say when to use it"
        assert "${input" not in body, f"{path}: ${{input:}} is a prompt-file feature"
        for link in re.findall(r"\]\((\.{1,2}/[^)]+)\)", body):
            assert (path.parent / link).resolve().exists(), f"{path}: broken link {link}"
    assert skills["kbmail-cli"][0]["user-invocable"] is False
    assert skills["kb-start"][0]["disable-model-invocation"] is True


def test_cli_skill_documents_every_command(ws_root):
    body = _skills(ws_root)["kbmail-cli"][1]
    commands = build_parser()._subparsers._group_actions[0].choices
    for name in commands:
        assert f"`kbmail {name}" in body, f"kbmail {name} missing from the kbmail-cli skill"


def test_agent_hook_and_instructions(ws_root):
    meta, _ = parse_frontmatter((ws_root / ".github/agents/kb-librarian.agent.md").read_text(encoding="utf-8"))
    assert meta["name"] == "KB Librarian"
    assert "execute" in meta["tools"] and "web" not in meta["tools"]
    assert all(h["agent"] == "KB Librarian" and h["prompt"].startswith("/kb-") for h in meta["handoffs"])
    assert (ws_root / "AGENTS.md").exists() and not (ws_root / ".github/copilot-instructions.md").exists()
    hook = json.loads((ws_root / ".github/hooks/kbmail.json").read_text(encoding="utf-8"))
    assert hook["version"] == 1 and hook["hooks"]["sessionStart"][0]["command"] == "kbmail hook session-start"
    for p in (ws_root / ".github/instructions").glob("*.instructions.md"):
        assert parse_frontmatter(p.read_text(encoding="utf-8"))[0]["applyTo"]


def test_session_start_hook_outputs_context(ws_root, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(ws_root), "source": "startup"})))
    assert main(["hook", "session-start"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "Next actions" in out["additionalContext"] and "kbmail export" in out["additionalContext"]
    # outside a workspace it still returns valid JSON instead of failing the session
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(ws_root.parent)})))
    assert main(["hook", "session-start"]) == 0
    assert "status unavailable" in json.loads(capsys.readouterr().out)["additionalContext"]


def test_upgrade_removes_old_prompt_files_but_keeps_user_ones(ws_root):
    prompts = ws_root / ".github/prompts"
    prompts.mkdir(parents=True)
    (prompts / "kb-label.prompt.md").write_text("---\nagent: agent\n---\nold", encoding="utf-8")
    (prompts / "my-own.prompt.md").write_text("---\n---\nmine", encoding="utf-8")
    (ws_root / ".github/copilot-instructions.md").write_text(
        "# Copilot instructions for this kbmail workspace\nold", encoding="utf-8"
    )
    init_workspace(ws_root, upgrade=True)
    assert not (prompts / "kb-label.prompt.md").exists() and (prompts / "my-own.prompt.md").exists()
    assert not (ws_root / ".github/copilot-instructions.md").exists()
