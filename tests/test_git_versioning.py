"""Versioning a workspace with git: what is tracked, what is refused, and what survives a clone."""

import json
import shutil
import subprocess

import pytest

from kbmail import gitops, images, kb, wiki
from kbmail.importers import import_eml
from kbmail.labels import load_labels, save_labels
from kbmail.workspace import init_workspace, merge_managed_block
from tests.test_pipeline import _label_everything

quiet = lambda *a, **k: None

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(ws, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(ws.root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _select_all(ws):
    kb.prepare_select(ws, log=quiet)
    for b in ws.load_json(kb.SELECT_BATCHES)["open"].values():
        lines = [json.dumps({"thread": t, "decision": "select", "title": "T", "messages": "all"}) for t in b["threads"]]
        ws.path(b["result"]).write_text("\n".join(lines), encoding="utf-8")
    kb.import_select(ws, log=quiet)


@pytest.fixture
def populated(ws, mailbox, monkeypatch):
    monkeypatch.chdir(ws.root)
    import_eml(ws, [str(mailbox)], log=quiet)
    _label_everything(ws)
    labels = load_labels(ws)
    for lab in labels.values():
        lab["status"] = "confirmed"  # what `kbmail review` would do
    save_labels(ws, labels)
    _select_all(ws)
    kb.convert(ws, log=quiet)
    ws.path("raw/email/payments/x.md").parent.mkdir(parents=True, exist_ok=True)
    ws.path("raw/email/payments/x.md").write_text("# Cut-over\nPlanned for 2026-11-15.\n", encoding="utf-8")
    ws.path("raw/inbox/spec.pdf").write_bytes(b"%PDF-1.4")
    ws.path("review/questions.yaml").write_text("- id: q0001\n  answer: ''\n", encoding="utf-8")
    ws.path("state/msal_cache.bin").write_bytes(b"token")
    ws.path("drafts/2026-10-28_reply.md").write_text("draft", encoding="utf-8")
    wiki.mark(ws, [], all_queued=True, log=quiet)
    gitops.setup(ws, log=quiet)
    return ws


def test_gitignore_tracks_knowledge_and_keeps_the_mailbox_out(populated):
    ws = populated
    git(ws, "add", "-A")
    staged = set(git(ws, "diff", "--cached", "--name-only").split())
    for must in (
        "kbmail.yaml",
        ".env.example",
        ".gitignore",
        ".gitattributes",
        ".githooks/pre-commit",
        "AGENTS.md",
        "README.md",
        "wiki/SCHEMA.md",
        "raw/email/payments/x.md",
        "config/rules.yaml",
        "review/questions.yaml",
        "review/images/descriptions.yaml",
        "state/labels.json",
        "state/kb_selection.json",
        "state/kb_convert.json",
        "state/images.json",
        "state/wiki_manifest.json",
        "state/runs.json",
        ".github/skills/kb-sync/SKILL.md",
    ):
        assert must in staged, must
    assert any(p.startswith("state/wiki_snapshots/") for p in staged)
    assert any(p.startswith("review/images/") and p.endswith(".mmd") for p in staged)  # auto-converted SVG
    for path in staged:
        assert not path.startswith(("data/", "work/", "raw/inbox/", "drafts/")), path
        assert path not in (".env", "state/msal_cache.bin", "state/index.jsonl", "state/label_batches.json"), path
        assert not path.endswith((".png", ".svg")), path
    assert gitops.check(ws, staged=True, log=quiet) == 0
    assert git(ws, "config", "core.hooksPath").strip() == ".githooks"


def test_check_refuses_forced_exports_and_secrets(populated, capsys):
    ws = populated
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", "base")
    exported = next(ws.path("data").rglob("*.md"))
    git(ws, "add", "-f", str(exported.relative_to(ws.root)))
    ws.path("wiki/systems").mkdir(parents=True, exist_ok=True)
    ws.path("wiki/systems/db.md").write_text("# DB\nConnect with password: hunter2secret\n", encoding="utf-8")
    git(ws, "add", "wiki/systems/db.md")
    assert gitops.check(ws, staged=True) == 1
    out = capsys.readouterr().out
    assert "raw mail" in out and "wiki/systems/db.md: looks like a password" in out and "line(s) 2" in out
    # The hook blocks the commit itself.
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e.x", "-C", str(ws.root), "commit", "-m", "leak"],
        capture_output=True,
        text=True,
    )
    if shutil.which("kbmail"):
        assert res.returncode != 0


def test_upgrade_refreshes_managed_block_and_keeps_user_rules(tmp_path):
    root = tmp_path / "w"
    init_workspace(root)
    gi = root / ".gitignore"
    gi.write_text(gi.read_text(encoding="utf-8").replace("drafts/", "drafts/old-rule") + "\n# mine\nscratch/\n")
    init_workspace(root, upgrade=True)
    text = gi.read_text(encoding="utf-8")
    assert "scratch/" in text and "# mine" in text and "drafts/old-rule" not in text and "\ndrafts/\n" in text
    # A hand-made .gitignore without the block keeps its lines after the managed block.
    merged = merge_managed_block("node_modules/\n", "# >>> kbmail managed x >>>\ndata/\n# <<< kbmail managed <<<\n")
    assert merged.index("data/") < merged.index("node_modules/")


def test_line_endings_do_not_requeue_raw_files(ws, monkeypatch):
    monkeypatch.chdir(ws.root)
    p = ws.path("raw/notes/a.md")
    p.write_bytes(b"line 1\nline 2\n")
    wiki.mark(ws, ["raw/notes/a.md"], log=quiet)
    p.write_bytes(b"line 1\r\nline 2\r\n")  # what a Windows checkout of the same commit looks like
    assert wiki.compute_queue(ws)["changed"] == []
    p.write_bytes(b"line 1\r\nline 3\r\n")
    assert wiki.compute_queue(ws)["changed"] == ["raw/notes/a.md"]


def test_fresh_clone_rebuilds_work_files_and_images(populated):
    ws = populated
    reg = images.load(ws)
    assert reg
    shutil.rmtree(ws.path("work"))
    for img in reg.values():
        ws.path(img["file"]).unlink(missing_ok=True)
    stats = kb.convert(ws, log=quiet)
    assert stats["converted"] >= 1
    assert any(ws.path("work/convert").rglob("thread.src.md"))
    assert all(ws.path(img["file"]).exists() for img in images.load(ws).values())
